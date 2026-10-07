"""Mode engine AI-верстака: исполнение графа режима (Ф3.5b-2).

Спека: plans/_provenance/arch-2026-10-05-ai-workspace/…-mode-engine-spec.md §3, §5.

Инварианты плана (REV.12): **I4** — эффект имеет ``compute_effect_id(job,node,effect)``
без attempt, повтор шага отдаёт кэш (ledger), а не повторяет эффект; **I9** — секции
пишет только engine через ``BoardStore.write_sections`` CAS, версия доски живёт в
``job.board_versions["board"]``; **I3** — статус меняет только ``JobStore``
(продвижение курсора — ``JobStore.patch`` без смены статуса); **human-gate** —
``running→waiting_human`` + single-use ``resume_token``, ответ ``approve|edit|reject``,
``edit`` пишется секцией через CAS.

Зависимости инъектируются (jobs/boards/llm/mcp/ledger/registry) → граф исполним на
фейках в юнит-тестах и на ws-redis + LiteLLM + MCP в проде.

Квот-контур (P1-5 ревизии Ф4, wiring): порт ``QuotaPort`` опционален (``None`` →
контур выключен — юнит-тесты на фейках/локальные прогоны). При подключении движок
продлевает conc-lease на каждом шаге и LLM-вызове (``heartbeat``), re-admit'ит
резерв на старте исполнения и на resume из ``waiting_human`` (пауза резерв
ОСВОБОЖДАЕТ — см. докстроку QuotaPort), списывает фактический usage и возвращает
резерв на КАЖДОМ терминале (done/failed/cancel/gate-timeout). Постановка
(admission ДО создания job) — ``scheduler/wiring.py`` (вне движка: job ещё нет).

Per-node наблюдение (Ф6-a 6a.1, инструмент-минимум): колбэк ``on_node_usage``
+ персистентный агрегат ``usage:{node_id}`` в ledger (вызовы/cache-hit/токены/
символы промпта-выхода/wall-time). Аддитивно и best-effort — семантику
исполнения не меняет. Событие несёт ``trace_id = job:epoch`` (Ф6 TODO 2/К1):
прод-проводка — ``wiring.make_on_node_usage`` → ``ws:quota:events``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from ai_workspace.orchestrator.graph import (
    EngineError,
    ModeGraph,
    Node,
    UnknownNode,
    UnsupportedNode,
    load_mode,
)
from ai_workspace.orchestrator.job import (
    JobRecord,
    JobState,
    JobStoreError,
    compute_effect_id,
)
from ai_workspace.orchestrator.ledger import Ledger, MemoryLedger, RedisLedger

__all__ = [
    "SHAPING_DEFAULT_BUDGET",
    "SHAPING_FLOOR_CHARS",
    "SHAPING_FULL",
    "SHAPING_HEAD_SHARE",
    "EgressBlocked",
    "EngineError",
    "EngineResult",
    "LLMClient",
    "LLMResult",
    "MemoryLedger",
    "ModeEngine",
    "ModeGraph",
    "NodeFailure",
    "QuotaLeaseLost",
    "QuotaPort",
    "RedisLedger",
    "TokenInvalid",
    "ToolClient",
    "UnknownNode",
    "UnsupportedNode",
    "load_mode",
    "shape_section",
]

MAX_STEPS = 64
"""Предохранитель от бесконечного графа (не заменяет max_iterations критика)."""

_VERDICT_PREFIX_RE = re.compile(
    r"^\W*(?:VERDICT|ВЕРДИКТ)\W*[:\-—]?\s*(\w+)", re.IGNORECASE,
)
"""Вердиктная строка-«VERDICT: <токен>» (шаг 2 лестницы ``_parse_verdict``).

Маркер VERDICT/ВЕРДИКТ в любом регистре, markdown-обёртка, разделитель
``:``/``-``/``—``; токен-кандидат проверяется на вхождение в ``verdicts``
узла (синонимы не изобретаются). Якорен к началу строки: упоминание
«VERDICT: …» в середине прозы рубрики — не вердикт.
"""

logger = logging.getLogger(__name__)
"""Лог движка: best-effort предупреждения ETA-хука терминала (Ф4.4a)."""


class NodeFailure(EngineError):
    """Узел исчерпал retry и уронил шаг (job -> failed)."""


class TokenInvalid(EngineError):
    """resume_token не найден или уже использован (single-use)."""


class EgressBlocked(EngineError):
    """Зонный гейт сработал на границе вызова LLM: private не уходит в ext.

    Hardening паттерна Local-First (Ф3.10, P1 критика): зонный предикат живёт
    в ДВИЖКЕ, а не только в тестовом примитиве — иначе реальный маршрут
    zone→egress не покрыт.
    """


class QuotaLeaseLost(EngineError):
    """conc-lease резерва квоты утерян: истёк и снят свипером (P1-3/P1-5).

    ``conc_heartbeat → False``: воркер ОБЯЗАН остановить работу — место в
    conc уже отдано другому job'у пользователя, продолжение списывало бы
    чужой резерв. Job → failed (fail-loud); повторный запуск — через
    FAILED→queued: эффекты идемпотентны (I4), повторный прогон переиспользует
    кэш ledger и докручивает только незакэшированные шаги.
    """


def _chars4_usage(prompt: str, output: str) -> int:
    """Оценка токенов по измеримому факту вызова: ~4 символа/токен.

    FALLBACK (Ф6 TODO 1/К2): применяется только когда шлюз не отдал usage
    (``LLMResult.usage is None``) — списание идёт по факту объёма текста
    вызова (prompt+output), а не по выдуманным числам; инъекция ``usage_of``
    в ModeEngine заменяет оценку без правки движка. Основной путь — реальные
    токены из ``usage`` ответа шлюза (``_tokens_from_usage``).
    """
    return (len(prompt) + len(output) + 3) // 4


def _tokens_from_usage(usage: Mapping[str, Any] | None) -> int | None:
    """Реальные токены из usage-объекта ответа шлюза (Ф6 TODO 1/К2).

    ``total_tokens``, при отсутствии — сумма ``prompt_tokens +
    completion_tokens`` (все три поля подтверждены живой пробой шлюза).
    Недоступен/битый (``None``, пустой, нечисловой) → ``None``: вызывающий
    честно падает обратно на оценку (``usage_of``), не выдумывая чисел.
    """
    if not isinstance(usage, Mapping):
        return None
    try:
        total = int(usage.get("total_tokens"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total = None
    if total is not None:
        return total if total >= 0 else None
    try:
        parts = int(usage.get("prompt_tokens")) + int(usage.get("completion_tokens"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parts if parts >= 0 else None


# ── шейпинг-гигиена контекста (Ф6-a 6a.3) ─────────────────────────────────
#
# `shaping` из registry/model_classes.yaml оживлён в `_prompt`: сжатие
# ТОЛЬКО данных-секций (`## <inputs>`), контракт роли и хедер `# РОЛЬ/# УЗЕЛ`
# не сжимаются НИКОГДА (стабильный cache-friendly префикс, parity L11).
# Константы ниже — «пол» и форма среза; сам бюджет живёт в реестре
# (`max_chars_per_section`, SSOT) с дефолтом SHAPING_DEFAULT_BUDGET.

SHAPING_FLOOR_CHARS = 200
"""Пол шейпинга: бюджет ≤ пола не сжимает ничего (маркер съел бы больше,
чем осталось смысла; защита от бессмысленных конфигураций)."""

SHAPING_HEAD_SHARE = 0.8
"""Доля головы при срезе: 80% бюджета на голову / 20% на хвост. Начало
документа несёт структуру и тему, конец — выводы/вердикты; середина
наиболее избыточна для слабой модели."""

SHAPING_DEFAULT_BUDGET = 4000
"""Дефолт бюджета секции (симв.) при `shaping: compressed` без явного
`max_chars_per_section` в реестре. PLACEHOLDER — калибровка по живому
пилоту (plans/_provenance/…/Ф6a-3-shaping-check.md)."""

SHAPING_FULL = "full-context"
"""Каноническое имя режима «без сжатия» (совпадает со значением реестра)."""

_MARKER_TEMPLATE = "[…срезано {percent}% …]"


def shape_section(
    text: str,
    *,
    budget: int,
    floor: int = SHAPING_FLOOR_CHARS,
    head_share: float = SHAPING_HEAD_SHARE,
) -> str:
    """Сжать секцию-ДАННЫЕ до бюджета: голова + маркер среза + хвост (6a.3).

    Правила:

    - **no-op ниже бюджета**: ``len(text) <= budget`` → текст байт-в-байт,
      БЕЗ маркера — нулевое изменение поведения на коротких документах
      (parity-тесты и старые прогоны не зависят от шейпинга);
    - **пол**: ``budget <= floor`` → no-op (сжатие теряет смысл);
    - срез: ``head = budget*head_share`` символов головы + маркер
      ``[…срезано N% …]`` + ``tail = budget-head`` символов хвоста
      (маркер — сверх бюджета, его объём пренебрежим и не скрывает факт среза);
    - ``N%`` — доля срезанного от исходной секции, округление до целого.
    """
    if budget <= floor or len(text) <= budget:
        return text
    percent = round((len(text) - budget) * 100 / len(text))
    head = max(1, int(budget * head_share))
    tail = max(1, budget - head)
    return f"{text[:head]}\n\n{_MARKER_TEMPLATE.format(percent=percent)}\n\n{text[-tail:]}"


# ── порты (инъектируемые зависимости) ────────────────────────────────────


@dataclass(frozen=True)
class LLMResult:
    """Результат LLM-вызова (Ф6 TODO 1/К2): выход + наблюдаемость шлюза.

    ``request_id`` — из заголовка ответа шлюза ``x-litellm-call-id``;
    ``usage`` — из тела ответа (``{prompt_tokens, completion_tokens,
    total_tokens}``); контракт подтверждён живой пробой шлюза. Оба
    ``None`` — шлюз не отдал факт (например, прямой ollama без id):
    потребители обязаны fallback'ить (движок — на оценку ``usage_of``).
    """

    output: str
    request_id: str | None = None
    usage: dict[str, Any] | None = None


class LLMClient(Protocol):
    """Шлюз LiteLLM (минимальный контракт движка).

    ``job_id`` (опционально) клиент кладёт в metadata запроса LiteLLM —
    склейка вызова с job'ом на стороне шлюза (reconcile P0-1).
    """

    def complete(
        self,
        *,
        role: str,
        model_class: str,
        prompt: str,
        inputs: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        job_id: str | None = None,
    ) -> LLMResult: ...


class ToolClient(Protocol):
    """MCP-клиент (mcp-knowledge или другой сервер)."""

    def call(self, *, tool: str, args: Mapping[str, Any]) -> Any: ...


class QuotaPort(Protocol):
    """Порт квот-контура движка (P1-5: wiring admission ↔ engine, Ф4.2).

    Реализация над живым ws-redis — ``scheduler.wiring.RedisQuotaPort``;
    ``None`` в конструкторе движка выключает контур. Все операции — над
    per-job владением (``conc_release`` по маркеру, P1-2), не агрегатом.

    ПОЛИТИКА ПАУЗ ``waiting_human`` (выбрана и зафиксирована, P1-5):
    резерв ОСВОБОЖДАЕТСЯ на входе в паузу, resume берёт заново
    (``readmit``). Обоснование: (а) время ответа человека не ограничено
    (gate-timeout — отдельная политика), а продление lease требует живого
    воркера — «держать» значило бы держать личный слот сутками (гость с
    conc=1 блокировал бы сам себя и все свои job'ы на время раздумий);
    (б) exempt от свипера реинкарнировал бы утечку P1-3 (брошенный
    waiting_human держал бы слот вечно); (в) симметрия с parked (Ф4.3):
    обе паузы освобождают, оба resume делают re-admit — одно правило без
    особых случаев. Диспропорция критика («waiting_human держит, parked —
    нет», E11) закрыта в сторону «не держит никто».
    """

    def readmit(self, user: str, role: str, job_id: str) -> str:
        """Взять conc-резерв job'а заново (после паузы/утраты lease).

        Возврат: ``allow`` (резерв взят admit'ом — второй раз НЕ берётся)
        | ``deny`` | ``park``; при не-allow резерв не берётся — решение о
        судьбе job за вызывающим (движок: paused, состояние не меняется).
        """
        ...

    def heartbeat(self, user: str, job_id: str) -> bool:
        """Продлить conc-lease (воркер жив; P1-3). ``False`` — резерва нет."""
        ...

    def charge(self, user: str, tokens: int) -> None:
        """Списать фактический расход токенов дня (терминал job; D3/D7)."""
        ...

    def release(self, user: str, job_id: str) -> None:
        """Освободить резерв job'а по владению (идемпотентно, P1-2)."""
        ...


@dataclass(frozen=True)
class EngineResult:
    """Итог прогона движка до терминала, паузы или предохранителя."""

    status: Literal["done", "paused", "failed", "stopped"]
    node: str
    board_version: int = 0
    resume_token: str | None = None
    verdict: str | None = None
    artifact_id: str | None = None
    detail: str = ""


class _Pause(Exception):
    """Внутренний сигнал human-gate: прогон останавливается, job -> waiting_human."""

    def __init__(self, node_id: str, token: str, prompt: str) -> None:
        super().__init__(node_id)
        self.node_id = node_id
        self.token = token
        self.prompt = prompt


class ModeEngine:
    """Исполняет граф режима над job-store и board-store с идемпотентными эффектами."""

    def __init__(
        self,
        *,
        jobs: Any,
        boards: Any,
        graph: ModeGraph,
        llm: LLMClient,
        mcp: ToolClient,
        ledger: Ledger,
        registry: Any | None = None,
        artifacts: Any | None = None,
        decoding: Any | None = None,
        seed_loader: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
        quota: QuotaPort | None = None,
        usage_of: Callable[[str, str], int] | None = None,
        on_node_usage: Callable[[dict], None] | None = None,
        on_job_terminal: Callable[[str, float], None] | None = None,
    ) -> None:
        self.jobs = jobs
        self.boards = boards
        self.graph = graph
        self.llm = llm
        self.mcp = mcp
        self.ledger = ledger
        self.registry = registry
        self.artifacts = artifacts
        self.decoding = decoding
        self.seed_loader = seed_loader
        self.clock = clock
        self.quota = quota
        self.usage_of = usage_of if usage_of is not None else _chars4_usage
        self.on_node_usage = on_node_usage
        self.on_job_terminal = on_job_terminal

    # ── публичный API ────────────────────────────────────────────────────

    def run(self, job_id: str, *, epoch: int, max_steps: int = MAX_STEPS) -> EngineResult:
        """Прогнать job до терминала/паузы/предохранителя."""
        rec = self.jobs.get(job_id)
        if rec.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
            return EngineResult(status=self._terminal_status(rec.state), node=rec.cursor,
                                board_version=self._board_version(rec))
        if rec.state is JobState.WAITING_HUMAN:
            return EngineResult(status="paused", node=rec.cursor,
                                board_version=self._board_version(rec),
                                detail="job ожидает ответа человека")
        if rec.state is JobState.PARKED:
            # Ф4.3 (I10/D5): бюджетный hard-stop — граф НЕ исполняется (иначе
            # parked auto-перешёл бы в running и потратил бюджет); ждёт resume.
            return EngineResult(status="paused", node=rec.cursor,
                                board_version=self._board_version(rec),
                                detail="job в парке (бюджет D5 / команда) — требуется resume (Ф4.3)")

        try:
            return self._run_admitted(job_id, rec, epoch, max_steps)
        except JobStoreError:
            # Гонка с cancel()/gate_timeout() (P2-5 iter2): наш write в сторе
            # проиграл CAS — терминал зафиксирован параллельным вызовом, квоты
            # финализированы ТАМ. Прокидывать VersionConflict/IllegalTransition
            # в воркер-цикл нельзя — возвращаем фактический терминальный
            # статус (эффекты идемпотентны, I4). Нетерминальная ошибка стора
            # (напр. StaleEpoch) — наружу как есть (fail-loud).
            cur = self.jobs.get(job_id)
            if cur.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
                return EngineResult(
                    status=self._terminal_status(cur.state), node=cur.cursor,
                    board_version=self._board_version(cur),
                    detail="терминал зафиксирован параллельным вызовом (cancel/gate-timeout): CAS проигран, повторной финализации квот нет",
                )
            raise

    def _run_admitted(
        self, job_id: str, rec: JobRecord, epoch: int, max_steps: int
    ) -> EngineResult:
        """Тело run() после нетерминальных early-return'ов: стартовый
        re-admit/переход в running + цикл шагов (вызывается из run(), где
        живёт JobStoreError-обработка гонок, P2-5)."""
        if rec.state in (JobState.QUEUED, JobState.SLEEPING, JobState.PREEMPTED):
            if self.quota is not None and not self.quota.heartbeat(rec.user, job_id):
                # Lease постановки истёк (долгая очередь) либо резерва нет:
                # re-admit ДО старта. ADMIT идемпотентен по (user, job)
                # (SISMEMBER-гвард, P1-B iter2): в окне «lease истёк, свип не
                # прошёл» маркер владения ещё жив → повторного INCR нет
                # (только refresh lease); снятый свипером резерв берётся
                # заново честно (через проверки).
                action = self.quota.readmit(rec.user, rec.account_level, job_id)
                if action != "allow":
                    return EngineResult(status="paused", node=rec.cursor,
                                        board_version=self._board_version(rec),
                                        detail=f"исполнение отложено: admission={action} — квота/бюджет, job остаётся в очереди")
            rec = self.jobs.transition(job_id, JobState.RUNNING, expect_version=rec.version, epoch=epoch)

        for _ in range(max_steps):
            node = self.graph.node(rec.cursor or self.graph.start())
            try:
                self._beat(rec)
                rec, verdict, next_id = self._run_node(job_id, rec, node, epoch)
            except _Pause as pause:
                cur = self.jobs.get(job_id)  # версия могла сдвинуться внутри узла
                rec = self.jobs.transition(
                    job_id, JobState.WAITING_HUMAN, expect_version=cur.version, epoch=epoch,
                    patch={"cursor": pause.node_id},
                )
                # Политика паузы (P1-5, см. QuotaPort): слот не держим —
                # ожидание человека не занимает личный параллелизм (D6).
                self._quota_release(rec)
                return EngineResult(status="paused", node=pause.node_id,
                                    board_version=self._board_version(rec),
                                    resume_token=pause.token, detail=pause.prompt)
            except EngineError as exc:
                cur = self.jobs.get(job_id)  # секция/версия могли обновиться до провала
                rec = self.jobs.transition(job_id, JobState.FAILED, expect_version=cur.version,
                                           epoch=epoch, patch={"cursor": node.id})
                self._quota_finalize(rec)
                return EngineResult(status="failed", node=node.id,
                                    board_version=self._board_version(rec), detail=str(exc))

            if next_id is None:
                rec = self.jobs.transition(job_id, JobState.DONE, expect_version=rec.version, epoch=epoch)
                self._quota_finalize(rec)
                return EngineResult(status="done", node=node.id,
                                    board_version=self._board_version(rec), verdict=verdict,
                                    artifact_id=self._persist_artifact(job_id, rec))
            rec = self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch,
                                  patch={"cursor": next_id})
        return EngineResult(status="stopped", node=rec.cursor,
                            board_version=self._board_version(rec),
                            detail=f"предохранитель max_steps={max_steps}")

    def seed(self, job_id: str, sections: Mapping[str, str], *, epoch: int) -> JobRecord:
        """Записать входные секции job'а (``brief`` и пр.) до первого шага."""
        rec = self.jobs.get(job_id)
        versions = dict(rec.board_versions or {})
        new_v = self.boards.write_sections(
            dict(sections),
            expect_version=int(versions.get("board", 0)),
            writer_node="input",
            single_writer=True,
        )
        versions["board"] = int(new_v)
        return self.jobs.patch(
            job_id, expect_version=rec.version, epoch=epoch, patch={"board_versions": versions}
        )

    def resume(
        self,
        job_id: str,
        *,
        epoch: int,
        token: str,
        decision: str = "approve",
        edit: str | None = None,
    ) -> EngineResult:
        """Ответ человека на human-gate: ``approve`` | ``edit`` | ``reject``.

        ``edit`` пишется секцией gate-узла через board CAS (single-writer).
        Токен single-use: повторное предъявление → ``TokenInvalid``.

        Квоты (P1-5): пауза резерв не держит (см. QuotaPort) → перед
        исполнением делается ``readmit`` (резерв заново). ``deny``/``park`` →
        paused: состояние не меняется, предъявленный токен отработан
        (single-use честно расходуется), а гейт ПРОДОЛЖАЕТ ЖИТЬ — выдаётся
        свежий resume-токен того же узла (человек повторит позже им).
        Порядок: токен (исходная семантика сохранена — повторное
        предъявление на терминальном job даёт TokenInvalid) → состояние →
        readmit.
        """
        consumed = self.ledger.consume_token(job_id, token)
        if consumed is None:
            raise TokenInvalid(f"resume-токен для job {job_id!r} недействителен или использован")
        node_id = consumed
        rec = self.jobs.get(job_id)
        if rec.state is not JobState.WAITING_HUMAN:
            raise EngineError(f"job {job_id!r} не в waiting_human (state={rec.state.value})")
        if self.quota is not None:
            action = self.quota.readmit(rec.user, rec.account_level, job_id)
            if action != "allow":
                fresh = self.ledger.issue_token(job_id, node_id)
                return EngineResult(status="paused", node=node_id,
                                    board_version=self._board_version(rec),
                                    resume_token=fresh,
                                    detail=f"resume отложен: admission={action} — квота/бюджет; выдан свежий токен")
        # Durable-ответ гейта: approve делает его pass-through при повторном входе
        # (REVISE-петля), edit — показать снова после переработки.
        self.ledger.put(job_id, f"gate:{node_id}", {"decision": decision})

        if decision == "reject":
            rec = self.jobs.transition(job_id, JobState.FAILED, expect_version=rec.version,
                                       epoch=epoch, patch={"cursor": node_id})
            self._quota_finalize(rec)
            return EngineResult(status="failed", node=node_id,
                                board_version=self._board_version(rec), detail="человек отклонил")

        if decision == "edit":
            if edit is None:
                raise EngineError("decision='edit' требует непустой edit-текст")
            rec = self._write_section(job_id, rec, node_id, edit, epoch)

        rec = self.jobs.transition(job_id, JobState.RUNNING, expect_version=rec.version,
                                   epoch=epoch, patch={"cursor": node_id})
        next_id = self._gate_target(self.graph.node(node_id), decision)
        if next_id is None:
            rec = self.jobs.transition(job_id, JobState.DONE, expect_version=rec.version, epoch=epoch)
            self._quota_finalize(rec)
            return EngineResult(status="done", node=node_id, board_version=self._board_version(rec),
                                artifact_id=self._persist_artifact(job_id, rec))
        rec = self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch, patch={"cursor": next_id})
        return self.run(job_id, epoch=epoch)

    def cancel(self, job_id: str, *, epoch: int, reason: str = "command") -> EngineResult:
        """Отмена job — терминал ``cancelled`` с финализацией квот (P1-5).

        Фактический usage списывается (charge), резерв возвращается
        (release): отмена не дарит списанные токены и не держит слот.
        Статус ответа ``failed`` — терминальное отображение CANCELLED
        (как ``_terminal_status``; отдельного literal в EngineResult нет).
        Нелегальный переход (уже терминал) — ``IllegalTransition`` наружу.
        """
        rec = self.jobs.get(job_id)
        rec = self.jobs.transition(job_id, JobState.CANCELLED,
                                   expect_version=rec.version, epoch=epoch)
        self._quota_finalize(rec)
        return EngineResult(status="failed", node=rec.cursor,
                            board_version=self._board_version(rec),
                            detail=f"cancel: {reason}")

    def gate_timeout(self, job_id: str, *, epoch: int) -> EngineResult:
        """Таймаут ожидания человека: ``waiting_human → failed`` (P1-5).

        Отдельный вход (не cancel): политика таймаута гейта из таблицы
        переходов (job.py: WAITING_HUMAN → FAILED). Квоты финализируются
        как на любом терминале — usage по факту + возврат резерва.
        """
        rec = self.jobs.get(job_id)
        rec = self.jobs.transition(job_id, JobState.FAILED,
                                   expect_version=rec.version, epoch=epoch)
        self._quota_finalize(rec)
        return EngineResult(status="failed", node=rec.cursor,
                            board_version=self._board_version(rec),
                            detail="gate-timeout: человек не ответил за отведённое время")

    # ── шаг узла ─────────────────────────────────────────────────────────

    def _run_node(
        self, job_id: str, rec: JobRecord, node: Node, epoch: int
    ) -> tuple[JobRecord, str | None, str | None]:
        kind = node.kind
        if kind == "llm-step":
            return self._llm_step(job_id, rec, node, epoch)
        if kind == "tool-step":
            return self._tool_step(job_id, rec, node, epoch)
        if kind == "critic-gate":
            return self._critic_gate(job_id, rec, node, epoch)
        if kind == "human-gate":
            answered = self.ledger.get(job_id, f"gate:{node.id}")
            if answered is not None and answered.get("decision") == "approve":
                # Гейт уже утверждён (напр. повторный вход через REVISE-петлю) —
                # не спрашиваем человека снова, идём по approve-ветке.
                return rec, None, self._gate_target(node, "approve")
            token = self.ledger.issue_token(job_id, node.id)
            raise _Pause(node.id, token, str(node.get("prompt", "Требуется подтверждение")))
        raise UnsupportedNode(f"kind {kind!r} (узел {node.id!r}) — Ф3.8+ (fork/join)")

    def _llm_step(self, job_id, rec, node, epoch):
        t0 = self.clock()
        inputs = self._inputs(rec, node)
        prompt = self._prompt(node, inputs)
        eff = compute_effect_id(job_id, node.id, "llm:" + self._digest({"prompt": prompt, "inputs": inputs}))

        hit = self.ledger.get(job_id, f"fx:{eff}")
        result: LLMResult | None = None
        if hit is not None:
            output = str(hit["output"])
        else:
            result = self._call_llm_with_retry(rec, node, prompt, inputs)
            output = result.output
            self._bump_usage(job_id, prompt, output, usage=result.usage)
            self.ledger.put(job_id, f"fx:{eff}", {"output": output})

        section = self._out_section(node)
        rec = self._write_section(job_id, rec, section, output, epoch)
        self._observe_node_usage(
            job_id, node, "llm-step", prompt=prompt, output=output,
            cached=hit is not None, t0=t0, epoch=epoch,
            usage=result.usage if result is not None else None,
            request_id=result.request_id if result is not None else None,
        )
        return rec, None, self.graph.next_for(node.id)

    def _tool_step(self, job_id, rec, node, epoch):
        t0 = self.clock()
        tool = node.get("tool")
        inputs = self._inputs(rec, node)
        args = {"inputs": inputs, "policy": node.get("policy")}
        eff = compute_effect_id(job_id, node.id, "tool:" + self._digest({"tool": tool, "args": args}))
        request = json.dumps(args, ensure_ascii=False, sort_keys=True)

        hit = self.ledger.get(job_id, f"fx:{eff}")
        if hit is not None:
            payload = hit["result"]
        else:
            try:
                payload = self.mcp.call(tool=tool, args=args)
            except Exception as exc:
                raise NodeFailure(f"tool-step {node.id!r}: {tool} упал: {exc}") from exc
            self.ledger.put(job_id, f"fx:{eff}", {"result": payload})

        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        rec = self._write_section(job_id, rec, self._out_section(node), text, epoch)
        self._observe_node_usage(job_id, node, "tool-step", prompt=request, output=text,
                                 cached=hit is not None, t0=t0, epoch=epoch)
        return rec, None, self.graph.next_for(node.id)

    def _critic_gate(self, job_id, rec, node, epoch):
        t0 = self.clock()
        inputs = self._inputs(rec, node)
        prompt = self._prompt(node, inputs)
        eff = compute_effect_id(job_id, node.id, "critic:" + self._digest({"prompt": prompt}))
        hit = self.ledger.get(job_id, f"fx:{eff}")
        result: LLMResult | None = None
        if hit is not None:
            output = str(hit["output"])
        else:
            result = self._call_llm_with_retry(rec, node, prompt, inputs)
            output = result.output
            self._bump_usage(job_id, prompt, output, usage=result.usage)
            self.ledger.put(job_id, f"fx:{eff}", {"output": output})

        verdict = self._parse_verdict(node, output)
        rec = self._write_section(job_id, rec, self._out_section(node), output, epoch)
        self._observe_node_usage(
            job_id, node, "critic-gate", prompt=prompt, output=output,
            cached=hit is not None, t0=t0, epoch=epoch,
            usage=result.usage if result is not None else None,
            request_id=result.request_id if result is not None else None,
        )

        if verdict != "REVISE":
            return rec, verdict, self.graph.next_for(node.id, verdict)

        # REVISE: цикл на on_revise с ограничением max_iterations (§4б п.8).
        it_key = f"iter:{node.id}"
        counted = self.ledger.get(job_id, it_key) or {"n": 0}
        n = int(counted.get("n", 0)) + 1
        max_iter = int(node.get("max_iterations", 1))
        if n >= max_iter:
            raise NodeFailure(
                f"critic-gate {node.id!r}: достигнут max_iterations={max_iter} "
                f"на verdict=REVISE (on_max_iter: fail)"
            )
        self.ledger.put(job_id, it_key, {"n": n})
        return rec, verdict, node.get("on_revise", self.graph.start())

    # ── квот-контур (P1-5) ────────────────────────────────────────────────

    def _beat(self, rec: JobRecord) -> None:
        """Продлить conc-lease перед шагом/вызовом; утрата → fail-loud.

        ``QuotaLeaseLost`` — EngineError: ловится общим обработчиком run()
        → job failed + finalize (usage по факту; release — no-op, резерв
        уже снят свипером). Мид-ран утерю lease НЕ компенсируем re-admit'ом:
        место мог занять другой job пользователя, а масштаб взятия уже
        неконтролируем — честный терминал дешевле тихого двойного списания.
        """
        if self.quota is not None and not self.quota.heartbeat(rec.user, rec.id):
            raise QuotaLeaseLost(
                f"conc-lease job {rec.id!r} (user {rec.user!r}) истёк/утерян — "
                "резерв снят свипером; исполнение остановлено (чужой резерв не списываем)"
            )

    def _quota_release(self, rec: JobRecord) -> None:
        """Вернуть резерв job'а (пауза waiting_human; идемпотентно, P1-2)."""
        if self.quota is not None:
            self.quota.release(rec.user, rec.id)

    def _quota_finalize(self, rec: JobRecord) -> None:
        """Терминал job: списать фактический usage + вернуть резерв (P1-5).

        Crash/repeat-safe (P1-C1 iter2):

        - ``release`` — в ``finally``: резерв возвращается ВСЕГДА, даже
          когда charge отказал (отказ redis на терминале не должен течь
          слотом); отказ charge — fail-loud наружу (терминал в сторе уже
          зафиксирован).
        - ровно один заряд на терминал: ledger-маркер ``usage.charged`` —
          сколько УЖЕ списано за этот job. Повторный терминал списывает
          только дельту ``tokens − charged`` (обычно 0: FAILED→QUEUED→
          terminal — легальный путь, эффекты из кэша I4, новых токенов
          нет); прирост usage после requeue досписывается честно.
          Маркер пишется ПОСЛЕ успешного charge (подтверждение, не
          намерение): краш между charge и маркером недосписывает
          (fail-open по деньгам) — остаток c2 (pending-flag + retry на
          reconcile-tick) — Ф6.

        Usage — durable-счётчик ledger (ключ ``usage``), пополняется на
        каждом реальном LLM-вызове (кэш-попадания бесплатны, I4).

        Ф4.4a: ДО квот-ветки — ``_observe_duration`` (ETA-панель не зависит
        от включённости квот-контура; терминал — единственная точка, где
        wall-длительность job уже известна и записи уже позади).
        """
        self._observe_duration(rec)
        if self.quota is None:
            return
        used = self.ledger.get(rec.id, "usage") or {}
        tokens = int(used.get("tokens", 0))
        charged = int(used.get("charged", 0))
        try:
            delta = tokens - charged
            if delta > 0:
                self.quota.charge(rec.user, delta)
                used["charged"] = tokens
                self.ledger.put(rec.id, "usage", used)
        finally:
            self.quota.release(rec.user, rec.id)

    def _observe_duration(self, rec: JobRecord) -> None:
        """Терминал job -> wall-длительность в хук ``on_job_terminal(job_id,
        seconds)`` (Ф4.4a; прод-проводка — ``wiring.make_on_job_terminal`` ->
        ``ETAStore.observe``).

        Длительность = ``rec.updated − rec.created`` (оба ISO-UTC из
        job-store: ``created`` — постановка, ``updated`` — штамп
        терминального transition). Включает ожидание в очереди, исполнение
        и паузы waiting_human -> оценка сверху-смещённая; R5-диапазон
        (ema/p95) это покрывает честно. Пустые/битые штампы и отрицательная
        дельта (часовой сдвиг) -> ПРОПУСК без выдумывания. Сбой хука —
        warning, терминал не ломается (display-only).
        """
        if self.on_job_terminal is None:
            return
        try:
            if not rec.created or not rec.updated:
                return
            started = datetime.fromisoformat(rec.created)
            ended = datetime.fromisoformat(rec.updated)
            seconds = (ended - started).total_seconds()
            if seconds < 0:
                return
            self.on_job_terminal(rec.id, seconds)
        except Exception:  # display-only: панель не валит терминал
            logger.warning(
                "on_job_terminal(%s) упал (best-effort, игнор)",
                rec.id, exc_info=True,
            )

    def _bump_usage(
        self, job_id: str, prompt: str, output: str, *,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Учесть фактический расход LLM-вызова (durable, ключ ``usage``).

        Вызывается только при реальном вызове модели (мимо кэша эффектов).
        Токены — реальные из ``usage`` ответа шлюза (Ф6 TODO 1/К2);
        ``usage is None`` → fallback-оценка ``usage_of`` (не падаем).
        Контур выключен (``quota is None``) — счётчик не ведётся: старые
        прогоны не платят накладные расходы и не меняют ledger-контракт.
        """
        if self.quota is None:
            return
        real = _tokens_from_usage(usage)
        tokens = real if real is not None else self.usage_of(prompt, output)
        used = self.ledger.get(job_id, "usage") or {"tokens": 0}
        used["tokens"] = int(used.get("tokens", 0)) + tokens
        self.ledger.put(job_id, "usage", used)

    def _observe_node_usage(
        self,
        job_id: str,
        node: Node,
        kind: str,
        *,
        prompt: str,
        output: str,
        cached: bool,
        t0: float,
        epoch: int = 0,
        usage: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        """Per-node наблюдение (Ф6-a 6a.1): событие ``on_node_usage`` + агрегат в ledger.

        Вызывается ПОСЛЕ обработки узла (llm-step/tool-step/critic-gate;
        human-gate не измеряется). Аддитивно — семантику исполнения не меняет:

        - ``tokens``: 0 при cache-hit (LLM не вызывался — не тратим) и для
          tool-step (MCP-вызов не тратит токены); иначе — реальные токены из
          ``usage`` ответа шлюза (Ф6 TODO 1/К2), а при ``usage is None`` — та
          же fallback-оценка, что списывает ``_bump_usage`` (``usage_of``);
        - ``prompt_chars``/``output_chars`` считаются и на кэше (длина текста);
        - ``wall_s`` — по ``self.clock`` от входа в узел до записи секции;
        - персистентный агрегат ``usage:{node_id}`` (read-modify-write сумм)
          пишется ВСЕГДА, независимо от колбэка; ключи ``fx:*`` и job-level
          ``usage`` не затрагиваются;
        - колбэк — best-effort: его исключение НЕ валит узел (warning, дальше);
        - ``trace_id = f"{job_id}:{epoch}"`` (Ф6 TODO 2/К1): сквозной трейс
          шага в каждом событии; прод-проводка — ``wiring.make_on_node_usage``
          → ``ws:quota:events`` (``prio.emit_event``, второй стрим не вводится).
        """
        if kind == "tool-step":
            role, model_class, shelf = None, None, "local"
        else:
            role = str(node.get("role", node.id))
            model_class = str(node.get("model_class", "fast"))
            shelf = self.shelf_for(model_class)
        real = _tokens_from_usage(usage)
        if cached or kind == "tool-step":
            tokens = 0
        elif real is not None:
            tokens = real
        else:
            tokens = self.usage_of(prompt, output)
        wall_s = self.clock() - t0

        agg = self.ledger.get(job_id, f"usage:{node.id}") or {}
        agg["calls"] = int(agg.get("calls", 0)) + 1
        agg["cached_calls"] = int(agg.get("cached_calls", 0)) + (1 if cached else 0)
        agg["tokens"] = int(agg.get("tokens", 0)) + tokens
        agg["prompt_chars"] = int(agg.get("prompt_chars", 0)) + len(prompt)
        agg["output_chars"] = int(agg.get("output_chars", 0)) + len(output)
        agg["wall_s_last"] = wall_s
        if request_id is not None:  # id шлюза последнего реального вызова
            agg["request_id_last"] = str(request_id)
        agg["role"] = role
        agg["model_class"] = model_class
        agg["shelf"] = shelf
        self.ledger.put(job_id, f"usage:{node.id}", agg)

        if self.on_node_usage is not None:
            try:
                self.on_node_usage({
                    "job": job_id,
                    "trace_id": f"{job_id}:{epoch}",
                    "node": node.id,
                    "kind": kind,
                    "role": role,
                    "model_class": model_class,
                    "shelf": shelf,
                    "cached": cached,
                    "prompt_chars": len(prompt),
                    "output_chars": len(output),
                    "tokens": tokens,
                    "wall_s": wall_s,
                })
            except Exception:  # best-effort: наблюдение не валит узел
                logger.warning(
                    "on_node_usage(%s/%s) упал (best-effort, игнор)",
                    job_id, node.id, exc_info=True,
                )

    # ── вспомогательное ──────────────────────────────────────────────────

    def shelf_for(self, model_class: str) -> str:
        """Полка по классу модели: реестр ``model_classes`` (heavy→ext, fast→local)."""
        if self.registry is None:
            return "local"
        try:
            classes = self.registry.get("model_classes") or {}
        except Exception:  # noqa: BLE001 — реестр необязателен для исполнения
            return "local"
        spec = classes.get(model_class) or {}
        shelf = spec.get("shelf")
        if shelf:
            return str(shelf)
        return "local"  # local-only / неизвестный класс — безопасный дефолт

    def shaping_for(self, model_class: str) -> tuple[str, int]:
        """Режим шейпинга и бюджет секции по классу модели (Ф6-a 6a.3).

        Возврат ``(режим, бюджет)``; для ``full-context`` бюджет не
        используется (0). Реестр не задан / класс неизвестен / поля нет →
        ``full-context`` — поведение до 6a.3 (нулевое изменение для
        старых прогонов и тестов без реестра).
        """
        if self.registry is None:
            return (SHAPING_FULL, 0)
        try:
            classes = self.registry.get("model_classes") or {}
        except Exception:  # noqa: BLE001 — реестр необязателен для исполнения
            return (SHAPING_FULL, 0)
        spec = classes.get(model_class) or {}
        if str(spec.get("shaping") or "") != "compressed":
            return (SHAPING_FULL, 0)
        try:
            budget = int(spec.get("max_chars_per_section", SHAPING_DEFAULT_BUDGET))
        except (TypeError, ValueError):
            budget = SHAPING_DEFAULT_BUDGET
        return ("compressed", max(0, budget))

    def _guard_zone(self, rec: JobRecord, node: Node, shelf: str) -> None:
        """Зонный гейт ДО вызова модели: ``zone=private`` не уходит на внешнюю полку."""
        if rec.zone == "private" and shelf != "local":
            raise EgressBlocked(
                f"zone=private недопустим вне local: узел {node.id!r} резолвится в полку {shelf!r} "
                f"(model_class={node.get('model_class')!r})"
            )

    def _call_llm_with_retry(
        self, rec: JobRecord, node: Node, prompt: str, inputs: Mapping[str, str],
    ) -> LLMResult:
        model_class = str(node.get("model_class", "fast"))
        self._guard_zone(rec, node, self.shelf_for(model_class))
        params = self.decoding.as_params() if self.decoding is not None else {}
        attempts = int(node.get("retry", 0))
        last: Exception | None = None
        for _ in range(attempts + 1):
            self._beat(rec)  # lease жив перед КАЖДОЙ попыткой (P1-3/P1-5)
            try:
                result = self.llm.complete(
                    role=str(node.get("role", node.id)),
                    model_class=model_class,
                    prompt=prompt,
                    inputs=dict(inputs),
                    params=params,
                    job_id=rec.id,  # → metadata запроса шлюза (Ф6 TODO 1)
                )
                # легаси-клиент ещё возвращает str (переходный период) —
                # заворачиваем; isinstance, не getattr (rule 10)
                return result if isinstance(result, LLMResult) else LLMResult(output=str(result))
            except Exception as exc:  # noqa: BLE001 — ошибка шлюза LLM = повод для retry
                last = exc
        raise NodeFailure(f"llm-step {node.id!r}: исчерпан retry={attempts}: {last}")

    def _inputs(self, rec: JobRecord, node: Node) -> dict[str, str]:
        """Входы узла = секции доски по ``inputs`` (+ критика из ревизионной петли).

        Секция-критика (``on_revise: <этот узел>``) добавляется, если уже есть на
        доске: иначе повторный прогон дал бы тот же ``effect_id`` (кэш) и петля
        REVISE не двигалась бы. Отсутствующая обязательная секция → fail-closed.
        """
        _, sections = self.boards.read()
        wanted = list(node.get("inputs") or [])
        out = {name: sections[name] for name in wanted if name in sections}
        for feedback in self._feedback_sections(node.id):
            if feedback in sections:
                out[feedback] = sections[feedback]
        if not wanted:
            out = {**sections, **out} if out else dict(sections)
        missing = [name for name in wanted if name not in sections]
        if missing:
            raise NodeFailure(
                f"узел {node.id!r}: нет входных секций {missing} на доске "
                f"(есть: {sorted(sections)})"
            )
        return out

    def _persist_artifact(self, job_id: str, rec: JobRecord) -> str | None:
        """Сохранить готовый документ в artifact-store (Ф3.7, I13).

        Источник — ``output.section``/``output.sections`` режима (композиция частей:
        документ + блок цитат citer'а).
        Нет store/секции/содержимого → ``None`` (KB не засоряется молча).
        """
        if self.artifacts is None:
            return None
        output = self.graph.doc.get("output") or {}
        wanted: list[str] = []
        if output.get("section"):
            wanted.append(str(output["section"]))
        wanted.extend(str(x) for x in (output.get("sections") or []))
        if not wanted:
            return None
        _, board = self.boards.read()
        parts = [board[name] for name in wanted if board.get(name)]
        if not parts:
            return None
        # Документ + приложения (напр. документ и блок цитат citer'а): части
        # идут отдельными секциями доски, артефакт — их композиция.
        content = "\n\n".join(parts)
        record = self.artifacts.save(
            content,
            user=rec.user,
            job_id=job_id,
            type=str(output.get("type", "document")),
            zone=rec.zone,
            mode=rec.mode,
            title=f"{rec.mode}:{job_id}",
        )
        return str(record.id)

    def _gate_target(self, node: Node, decision: str) -> str | None:
        """Куда идти после ответа человека: ``on_approve``/``on_edit`` узла, иначе edge.

        ``on_approve: null`` (или несуществующий узел) → обычное ребро графа
        (для финального gate это ``None`` → job done).
        """
        target = node.get(f"on_{decision}")
        if isinstance(target, str) and target in self.graph.nodes:
            return target
        return self.graph.next_for(node.id)

    def _feedback_sections(self, node_id: str) -> list[str]:
        """Секции-замечания, адресованные ``node_id``: критика (on_revise) и правки людей.

        Без этого повторный вход в узел дал бы тот же ``effect_id`` (кэш) и не
        потребил бы ни вердикт критика, ни правку оператора (gate on_edit/on_approve).
        """
        result: list[str] = []
        for other in self.graph.nodes.values():
            critic_loop = other.kind == "critic-gate" and other.get("on_revise") == node_id
            human_edit = other.kind == "human-gate" and node_id in (
                other.get("on_edit"),
                other.get("on_approve"),
            )
            if critic_loop or human_edit:
                result.append(self._out_section(other))
        return result

    def _prompt(self, node: Node, inputs: Mapping[str, str]) -> str:
        """Промпт узла: контракт роли → ``# РОЛЬ/# УЗЕЛ`` → секции-``inputs``.

        Контракт роли (``roles.yaml → roles.<role>.contract``, Ф6-a 6a.2a) идёт
        ПЕРВОЙ частью — стабильный префикс промпта (cache-friendly), единый для
        обеих полок (parity L11). ``seed_skill`` через ``seed_loader`` — fallback
        только при отсутствии контракта (и если loader задан); loader нигде вне
        тестов не подключается. Роль без контракта → промпт без префикса
        (поведение до 6a.2a), отсутствие контракта — не ошибка.
        """
        role = str(node.get("role", node.id))
        parts: list[str] = []
        contract = None
        seed_skill = None
        if self.registry is not None:
            try:
                meta = (self.registry.get("roles") or {}).get(role) or {}
                contract = meta.get("contract")
                seed_skill = meta.get("seed_skill")
            except Exception:  # noqa: BLE001 — реестр необязателен для исполнения
                contract = None
                seed_skill = None
        if contract:
            parts.append(str(contract).strip())
        elif seed_skill and self.seed_loader is not None:
            parts.append(self.seed_loader(str(seed_skill)))
        parts.append(f"# РОЛЬ: {role}\n# УЗЕЛ: {node.id} ({node.kind})")
        # Шейпинг-гигиена (Ф6-a 6a.3): сжимаются ТОЛЬКО данные-секции; всё
        # выше (контракт + хедер) неприкосновенно — стабильный префикс.
        # full-context и секции в пределах бюджета → байт-в-байт (no-op).
        shaping, budget = self.shaping_for(str(node.get("model_class", "fast")))
        for name, value in inputs.items():
            if shaping == "compressed":
                value = shape_section(str(value), budget=budget)
            parts.append(f"## {name}\n{value}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_verdict(node: Node, output: str) -> str:
        """Лестница распознавания вердикта критика (Ф6-a 6a.2c; fail-closed).

        Живой пилот 6a.2b: слабая модель выдаёт near-miss ``**VERDICT: REVISE**``
        вместо литерального токена первой строкой → отказ парсера → секция
        verdict не пишется → документ не достигается (done=0). Лестница
        (строки вывода по порядку, выигрывает первый совпавший; токены —
        ТОЛЬКО из ``node.verdicts``, без синонимов):

        1. первый токен строки после markdown-обёртки ``*_# `` и двоеточия,
           либо начало строки — вердикт (поведение до 6a.2c, без изменений);
        2. строка ``VERDICT/ВЕРДИКТ: <токен>`` — толерантность к near-miss
           (регистр/обёртка/разделитель любые; возвращается канонический
           токен из ``verdicts``);
        3. фолбэк по всему выводу: ровно один отдельно стоящий токен-вердикт
           (строка = токен + не-словесная обёртка, напр. ``- PASS``);
           несколько разных — неоднозначность → отказ.

        Проза рубрики не матчится шагами 2–3: шаг 2 якорен к началу строки,
        шаг 3 требует строку-токен целиком. Иначе — ``NodeFailure`` (как до
        6a.2c).
        """
        verdicts = [str(v).upper() for v in (node.get("verdicts") or ["PASS", "REVISE"])]
        bare_res = [re.compile(rf"\W*{re.escape(v)}\W*", re.IGNORECASE) for v in verdicts]
        standalone: set[str] = set()
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # шаг 1 — как до 6a.2c: первый токен после обёртки/двоеточия или начало строки
            token = stripped.strip("*_# ").split(":")[0].strip().upper()
            if token in verdicts:
                return token
            upper = stripped.upper()
            for v in verdicts:
                if upper.startswith(v):
                    return v
            # шаг 2 — «VERDICT: <токен>» (near-miss живого пилота 6a.2b)
            m = _VERDICT_PREFIX_RE.match(stripped)
            if m and m.group(1).upper() in verdicts:
                return m.group(1).upper()
            # шаг 3 — накопление отдельно стоящих токенов (фолбэк после шагов 1–2)
            for v, bare_re in zip(verdicts, bare_res):
                if bare_re.fullmatch(stripped):
                    standalone.add(v)
                    break
        if len(standalone) == 1:
            return next(iter(standalone))
        raise NodeFailure(f"critic-gate {node.id!r}: вердикт не распознан в выводе (ожидались {verdicts})")

    def _out_section(self, node: Node) -> str:
        """Секция-выход узла: ``outputs[0]``, иначе ``verdict`` (critic-gate), иначе id.

        Согласовано с ``mode_lint._effective_outputs`` (critic-gate → ``verdict``);
        токены ``outputs`` берутся как есть (``document+refs`` — одна секция), чтобы
        не нарушать single-writer по секциям.
        """
        outputs = list(node.get("outputs") or [])
        if outputs:
            return str(outputs[0])
        if node.kind == "critic-gate":
            return "verdict"
        return node.id

    def _write_section(self, job_id: str, rec: JobRecord, section: str, value: str, epoch: int) -> JobRecord:
        """Записать секцию через board CAS и зафиксировать версию в job (I9)."""
        expect = self._board_version(rec)
        try:
            new_v = self.boards.write_sections(
                {section: value}, expect_version=expect, writer_node=section, single_writer=True
            )
        except Exception as exc:
            if type(exc).__name__ != "StaleBoard":
                raise
            cur, _ = self.boards.read()
            new_v = self.boards.write_sections(
                {section: value}, expect_version=cur, writer_node=section, single_writer=True
            )
        versions = dict(rec.board_versions or {})
        versions["board"] = int(new_v)
        return self.jobs.patch(job_id, expect_version=rec.version, epoch=epoch,
                               patch={"board_versions": versions})

    @staticmethod
    def _board_version(rec: JobRecord) -> int:
        return int((rec.board_versions or {}).get("board", 0))

    @staticmethod
    def _terminal_status(state: JobState) -> Literal["done", "failed"]:
        return "done" if state is JobState.DONE else "failed"

    @staticmethod
    def _digest(payload: Mapping[str, Any]) -> str:
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]
