#!/usr/bin/env python3
"""Пилот «дельта vs 7B»: qwen2.5:7b как гейт качества на REVISE-итерации.

Вопрос эксперимента: может ли малая модель (qwen2.5:7b) работать критиком,
если вместо полного документа v2 ей давать только дельту v1->v2
(изменённые/добавленные секции) плюс предыдущую критику.

Варианты промпта:
    full      — полный документ v2;
    delta     — только изменённые/добавленные секции + критика;
    delta-map — дельта + список всех заголовков v2 без тел.

Наборы пар (--pairs easy|hard|all, по умолчанию all):
    easy — fixed -> PASS, not-fixed -> REVISE: дельта достаточна по
           построению (delta_sufficient=True);
    hard — hd1/hd2 -> REVISE, delta_sufficient=False: дефект живёт вне
           дельты (неизменённая секция / межсекционное противоречие).

Токен ESCALATE разрешён только в delta/delta-map: если дельты
недостаточно для вердикта, критик отвечает ESCALATE, пилот делает
fallback-повтор с full-промптом (fallback_verdict) и метрики
escalation_rate / accuracy_raw / accuracy_after_fallback. В full токен
ESCALATE запрещён (контрактом и парсером).

Клиент инжектируется (Protocol с chat(prompt) -> str); реальная
реализация переиспользуется из ai_workspace/tools/vp_ab_pilot.py.

Exit codes: 0 — ок; 1 — ошибка конфигурации/сети; 2 — по design,
гипотеза не подтвердилась (hypothesis_gate); 3 — full-критик неточен на
hard-парах: accuracy(full) < 0.8 (hard_pair_gate). В --dry-run гейты
отключены (всегда 0).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_BASE_URL = "http://127.0.0.1:11435/v1"
VARIANTS = ("full", "delta", "delta-map")
ESCALATE_VARIANTS = frozenset({"delta", "delta-map"})
PAIR_KINDS = ("easy", "hard")
HARD_FULL_ACCURACY_MIN = 0.8

ROLE_CONTRACT = (
    "Ты — критик технической документации. КОНТРАКТ ОТВЕТА: первая строка "
    "твоего ответа — ровно одно слово PASS или REVISE заглавными буквами, "
    "без markdown-обёртки, кавычек и любых других символов. Обоснование "
    "(если требуется) — со второй строки."
)

ESCALATE_INSTRUCTION = (
    "Если дельты недостаточно для вердикта — ответь ровно ESCALATE: "
    "первая строка только это слово заглавными буквами, без markdown и "
    "кавычек; обоснование эскалации — со второй строки."
)

FULL_NO_ESCALATE = (
    "Ответ ESCALATE в этом режиме запрещён: тебе доступен полный "
    "документ v2, нехватки информации нет — отвечай PASS или REVISE."
)

VERDICTS = frozenset({"PASS", "REVISE"})
ESCALATE = "ESCALATE"

Doc = dict[str, str]  # заголовок секции -> тело секции

if TYPE_CHECKING:  # только для типов; runtime-импорт — в make_client
    from ai_workspace.tools.vp_ab_pilot import OllamaClient


class ChatClient(Protocol):
    """Минимальный контракт LLM-клиента (инжектируется в run_variant)."""

    def chat(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Pair:
    """Ground-truth пара: v1 + предыдущая критика + v2 + ожидаемый вердикт.

    kind: easy — дефект/фикс виден в дельте; hard — дефект вне дельты.
    delta_sufficient: достаточно ли ТОЛЬКО дельты для корректного
    вердикта (ground truth; для hard-пар False — ожидается ESCALATE).
    """

    name: str
    expected: str
    doc_v1: Doc
    doc_v2: Doc
    critique: str
    kind: str = "easy"
    delta_sufficient: bool = True


@dataclass(frozen=True, slots=True)
class CallRecord:
    """Метрики одного вызова критика.

    escalated: ответ по дельте был ESCALATE (только delta/delta-map);
    fallback_verdict: вердикт fallback-повтора с full-промптом (он же
    итоговый verdict записи); delta_sufficient: ground-truth флаг пары.
    """

    variant: str
    pair: str
    expected: str
    verdict: str | None
    correct: bool
    prompt_chars: int
    wall_s: float
    kind: str = "easy"
    escalated: bool = False
    fallback_verdict: str | None = None
    delta_sufficient: bool = True


_PAIR_FIXED = Pair(
    name="fixed",
    expected="PASS",
    doc_v1={
        "Обзор": (
            "База знаний mcp-knowledge хранит записи в Markdown (SSOT), "
            "поисковый индекс Qdrant строится из чанков этих файлов. Запись "
            "создаётся через write_knowledge, обновляется с optimistic "
            "locking по полю version: конфликт версий отказывает записи, "
            "а не перезатирает её."
        ),
        "Переиндексация": (
            "Команда reindex перестраивает индекс напрямую в активных "
            "коллекциях knowledge_private и knowledge_public: старые точки "
            "удаляются, новые грузятся на их место. На время перестроения "
            "поиск недоступен и запросы возвращают пустой результат — это "
            "штатное поведение, отдельная подготовка не предусмотрена."
        ),
        "Зоны доступа": (
            "Каждая запись относится к зоне public или private. Зона "
            "задаётся при создании записи и может меняться курированием "
            "(set_zone). Поиск по умолчанию ограничен зоной токена "
            "подписчика."
        ),
    },
    doc_v2={
        "Обзор": (
            "База знаний mcp-knowledge хранит записи в Markdown (SSOT), "
            "поисковый индекс Qdrant строится из чанков этих файлов. Запись "
            "создаётся через write_knowledge, обновляется с optimistic "
            "locking по полю version: конфликт версий отказывает записи, "
            "а не перезатирает её."
        ),
        "Переиндексация": (
            "Команда reindex перестраивает индекс по blue-green схеме: "
            "новые точки пишутся в коллекцию-кандидат *_v1, параллельно "
            "обслуживается текущая коллекция через алиас. После завершения "
            "загрузки выполняется атомарный своп алиаса "
            "(knowledge_private -> knowledge_private_v1, аналогично public), "
            "затем старая коллекция удаляется. Поиск остаётся доступным на "
            "всём протяжении перестроения."
        ),
        "Зоны доступа": (
            "Каждая запись относится к зоне public или private. Зона "
            "задаётся при создании записи и может меняться курированием "
            "(set_zone). Поиск по умолчанию ограничен зоной токена "
            "подписчика."
        ),
    },
    critique=(
        "REVISE. Секция «Переиндексация» фактически неверна: перестроение "
        "идёт по blue-green схеме — данные пишутся в новую коллекцию *_v1, "
        "затем выполняется атомарный свап алиаса (knowledge_private -> "
        "knowledge_private_v1, аналогично public), поиск остаётся доступным "
        "во время перестроения. Требуется описать blue-green схему и "
        "атомарный свап алиаса вместо «прямой перезаписи с недоступностью "
        "поиска»."
    ),
)

_PAIR_NOT_FIXED = Pair(
    name="not-fixed",
    expected="REVISE",
    doc_v1={
        "Архитектура": (
            "Оркестратор координирует роли через доску задачи .board.md и "
            "делегирует работу субагентам. Перед доменным навыком "
            "загружается orchestrator-core; уровень задачи "
            "(simple/complex/strategic) определяет форму User Gate."
        ),
        "Токены доступа": (
            "Для доступа к внешним сервисам агент-модель сама читает файл "
            ".env из корня проекта, извлекает нужный ключ и подставляет "
            "его в HTTP-запросы. Ключи хранятся в открытом виде и доступны "
            "любой запущенной роли."
        ),
        "Оркестрация режимов": (
            "Режимы (orchestrator, analyst, critic, code-implementer) "
            "образуют конвейер: анализ -> критика -> реализация. Возврат "
            "результата идёт через attempt_completion, метаданные доски — "
            "SSOT состояния задачи."
        ),
        "Хранение артефактов": (
            "Одноразовые скрипты создаются сразу в .trash/, постоянные — "
            "в scripts/ или app/. Заменяемые файлы перемещаются в .trash/ "
            "вместо удаления."
        ),
    },
    doc_v2={
        "Архитектура": (
            "Оркестратор координирует роли через доску задачи .board.md и "
            "делегирует работу субагентам. Перед доменным навыком "
            "загружается orchestrator-core; уровень задачи "
            "(simple/complex/strategic) определяет форму User Gate."
        ),
        "Токены доступа": (
            "Для доступа к внешним сервисам агент самостоятельно открывает "
            ".env, достаёт оттуда ключ и вкладывает его в заголовки "
            "исходящих запросов. Значения ключей лежат в проекте в "
            "открытом виде, любая роль может их прочитать при "
            "необходимости."
        ),
        "Оркестрация режимов": (
            "Режимы (orchestrator, analyst, critic, code-implementer) "
            "образуют конвейер: анализ -> критика -> реализация. Возврат "
            "результата идёт через attempt_completion, метаданные доски — "
            "SSOT состояния задачи."
        ),
        "Мониторинг": (
            "Ошибки стека агрегируются в sink наблюдаемости, недельный "
            "отчёт формируется по приоритетам P0-P3. Перед закрытием "
            "сигнатура должна исчезнуть из окна наблюдения."
        ),
    },
    critique=(
        "REVISE. Секция «Токены доступа» нарушает инвариант безопасности: "
        "модель НИКОГДА не читает .env и не видит значения ключа — токен "
        "извлекает скрипт-обёртка, которая отдаёт секрет только по "
        "явному флагу --allow-secret и точечно. Требуется переписать "
        "секцию: описать скрипт-обёртку, запрет доступа модели к .env и "
        "политику --allow-secret."
    ),
)

# --- hard-пары: дельта недостаточна по построению (delta_sufficient=False) ---

_PAIR_HD1 = Pair(
    name="hd1-unchanged-defect",
    expected="REVISE",
    doc_v1={
        "Конвейер режимов": (
            "Оркестратор ведёт задачу через доску .board.md: анализ, "
            "критика, реализация. Метаданные доски — SSOT состояния, "
            "приоритет полей implementation_status > analysis_status > "
            "next_role."
        ),
        "User Gate": (
            "После аналитики оркестратор сам решает, запускать ли "
            "реализацию: уровень задачи (simple/complex/strategic) не "
            "меняет порядок шагов, выбор implement/critic/supplement "
            "агент делает без участия оператора."
        ),
        "Артефакты": (
            "Промежуточные выводы фиксируются на доске, финальные "
            "результаты — файлами. Одноразовые скрипты создаются сразу "
            "в .trash/."
        ),
    },
    doc_v2={
        "Конвейер режимов": (
            "Оркестратор ведёт задачу через доску .board.md: анализ, "
            "критика, реализация. Метаданные доски — SSOT состояния, "
            "приоритет полей implementation_status > analysis_status > "
            "next_role."
        ),
        "User Gate": (
            "После аналитики оркестратор сам решает, запускать ли "
            "реализацию: уровень задачи (simple/complex/strategic) не "
            "меняет порядок шагов, выбор implement/critic/supplement "
            "агент делает без участия оператора."
        ),
        "Артефакты": (
            "Промежуточные выводы фиксируются на доске, финальные "
            "результаты — файлами. Одноразовые скрипты создаются сразу "
            "в .trash/, черновики и ревизии планов — в .tmp/, постоянные "
            "скрипты — в scripts/ или app/."
        ),
    },
    critique=(
        "REVISE. Секция «User Gate» нарушает инвариант: реализацию нельзя "
        "запускать без явного выбора оператора (user_choice: implement) "
        "через ask_followup_question; уровень задачи определяет форму "
        "гейта (вопрос / brainstorming / Operator Gate), но право выбора "
        "всегда у оператора, а не у агента. Требуется переписать секцию: "
        "обязательный вопрос оператору перед запуском реализации."
    ),
    kind="hard",
    delta_sufficient=False,
)

_PAIR_HD2 = Pair(
    name="hd2-cross-section",
    expected="REVISE",
    doc_v1={
        "Единый писатель доски": (
            ".board.md правит только оркестратор: субагенты возвращают "
            "результат делегированием и сами файлы доски не трогают. "
            "Инвариант single-writer исключает гонки параллельных правок."
        ),
        "Heartbeat ролей": (
            "Каждая роль при старте и после значимых изменений сама "
            "дописывает в .board.md свой heartbeat: критик — вердикт в §6, "
            "реализатор — ошибки в §5. Прямая правка доски ролями "
            "ускоряет цикл."
        ),
        "Сжатие контекста": (
            "При росте доски запускается сжатие: устаревшие чекпойнты "
            "обрезаются по дате, снимаются устаревшие DIGEST."
        ),
    },
    doc_v2={
        "Единый писатель доски": (
            ".board.md правит только оркестратор: субагенты возвращают "
            "результат делегированием и сами файлы доски не трогают. "
            "Инвариант single-writer исключает гонки параллельных правок."
        ),
        "Heartbeat ролей": (
            "Каждая роль при старте и после значимых изменений сама "
            "дописывает в .board.md свой heartbeat: критик — вердикт в §6, "
            "реализатор — ошибки в §5. Прямая правка доски ролями "
            "ускоряет цикл."
        ),
        "Сжатие контекста": (
            "При росте доски запускается сжатие: устаревшие чекпойнты "
            "обрезаются по дате, решения и CSIL перед очисткой доски "
            "переносятся в durable-лог board-decisions.jsonl."
        ),
    },
    critique=(
        "REVISE. Секции «Единый писатель доски» и «Heartbeat ролей» "
        "противоречат друг другу: первая требует, чтобы .board.md правил "
        "только оркестратор, вторая — чтобы роли сами дописывали heartbeat "
        "и вердикты. Требуется устранить противоречие: либо роли передают "
        "записи через оркестратора, либо описать протокол блокировок."
    ),
    kind="hard",
    delta_sufficient=False,
)

PAIRS: tuple[Pair, ...] = (
    _PAIR_FIXED,
    _PAIR_NOT_FIXED,
    _PAIR_HD1,
    _PAIR_HD2,
)


def select_pairs(which: str = "all") -> tuple[Pair, ...]:
    """Выборка ground-truth пар по набору: easy | hard | all."""
    if which == "all":
        return PAIRS
    if which not in PAIR_KINDS:
        raise ValueError(f"неизвестный набор пар: {which}")
    return tuple(p for p in PAIRS if p.kind == which)


def diff_sections(v1: Doc, v2: Doc) -> dict[str, object]:
    """Секционная дельта v1->v2: added/changed (заголовок -> тело v2), removed."""
    added = {h: b for h, b in v2.items() if h not in v1}
    changed = {h: b for h, b in v2.items() if h in v1 and v1[h] != b}
    removed = sorted(set(v1) - set(v2))
    return {"added": added, "changed": changed, "removed": removed}


def _render_doc(doc: Doc) -> str:
    return "\n\n".join(f"### {head}\n{body}" for head, body in doc.items())


def _render_delta(delta: dict[str, object]) -> str:
    """Человекочитаемая дельта: изменённые, добавленные, удалённые секции."""
    changed = delta.get("changed")
    added = delta.get("added")
    removed = delta.get("removed")
    parts: list[str] = []
    if isinstance(changed, dict) and changed:
        parts.append("Изменённые секции (тело — версия v2):")
        parts.extend(f"### {head}\n{body}" for head, body in changed.items())
    if isinstance(added, dict) and added:
        parts.append("Добавленные секции:")
        parts.extend(f"### {head}\n{body}" for head, body in added.items())
    if isinstance(removed, list) and removed:
        parts.append("Удалённые секции: " + ", ".join(map(str, removed)))
    if not parts:
        parts.append("(изменений нет)")
    return "\n\n".join(parts)


def build_critic_prompt(
    variant: str,
    doc_v2: Doc,
    delta: dict[str, object],
    critique: str,
    headings: list[str],
) -> str:
    """Промпт критика для выбранной вариант-стратегии (full/delta/delta-map).

    В delta/delta-map добавляется инструкция про токен ESCALATE; в full —
    явный запрет ESCALATE (полный документ доступен целиком).
    """
    if variant not in VARIANTS:
        raise ValueError(f"неизвестный вариант: {variant}")
    parts = [
        ROLE_CONTRACT,
        "",
        (
            "Задача: вторая итерация ревью. Документ правили по предыдущей "
            "критике; проверь, устранены ли замечания."
        ),
        "",
        "## Предыдущая критика",
        critique.strip(),
    ]
    if variant == "full":
        parts += ["", "## Документ v2 (полностью)", _render_doc(doc_v2)]
    elif variant == "delta":
        parts += [
            "",
            "## Дельта v1->v2 (только изменённые и добавленные секции)",
            _render_delta(delta),
            "",
            "Секции, не вошедшие в дельту, не менялись.",
        ]
    else:  # delta-map
        parts += [
            "",
            "## Дельта v1->v2 (только изменённые и добавленные секции)",
            _render_delta(delta),
            "",
            "## Карта документа v2 (все заголовки, без тел)",
            "\n".join(f"- {head}" for head in headings),
        ]
    parts += [
        "",
        (
            "Критерий: если все замечания критики учтены — PASS, иначе REVISE "
            "с кратким указанием, что осталось неисправленным."
        ),
    ]
    if variant in ESCALATE_VARIANTS:
        parts += ["", ESCALATE_INSTRUCTION]
    else:
        parts += ["", FULL_NO_ESCALATE]
    return "\n".join(parts)


def _first_token(text: str) -> str | None:
    """Первый значимый токен первой строки ответа (верхний регистр)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        cleaned = "\n".join(lines).strip()
    if not cleaned:
        return None
    first = cleaned.splitlines()[0].lstrip("`#>*- \t—").strip()
    if not first:
        return None
    return first.split()[0].strip("`*_#>:,.!?—-").upper()


def parse_verdict(text: str) -> str | None:
    """PASS/REVISE из первой строки ответа (после снятия markdown-обёртки).

    Регистронезависимо; None — если контракт первой строки нарушен.
    ESCALATE вердиктом не является — для него None (см. parse_answer).
    """
    token = _first_token(text)
    return token if token in VERDICTS else None


def parse_answer(text: str, *, allow_escalate: bool = False) -> str | None:
    """PASS/REVISE/ESCALATE из первой строки ответа критика.

    Токен ESCALATE принимается только при allow_escalate=True
    (варианты delta/delta-map); в full он запрещён контрактом — None.
    """
    token = _first_token(text)
    if token == ESCALATE:
        return ESCALATE if allow_escalate else None
    return token if token in VERDICTS else None


def pair_prompt(variant: str, pair: Pair) -> str:
    """Готовый промпт для пары (используется run_variant и dry-run печатью)."""
    delta = diff_sections(pair.doc_v1, pair.doc_v2)
    return build_critic_prompt(
        variant, pair.doc_v2, delta, pair.critique, list(pair.doc_v2)
    )


def run_variant(
    variant: str,
    pair: Pair,
    client: ChatClient | None,
    n: int = 1,
) -> list[CallRecord]:
    """n вызовов критика на (variant x pair); client=None -> dry-run (1 прогон).

    В delta/delta-map разрешён ESCALATE: при нём выполняется fallback-повтор
    с full-промптом; вердикт fallback'а становится итоговым verdict записи
    и сохраняется отдельно в fallback_verdict.
    """
    allow_escalate = variant in ESCALATE_VARIANTS
    prompt = pair_prompt(variant, pair)
    full_prompt = pair_prompt("full", pair) if allow_escalate else prompt
    records: list[CallRecord] = []
    for _ in range(1 if client is None else max(1, n)):
        verdict: str | None = None
        fallback_verdict: str | None = None
        escalated = False
        wall = 0.0
        if client is not None:
            started = time.monotonic()
            answer = parse_answer(client.chat(prompt), allow_escalate=allow_escalate)
            if answer == ESCALATE:
                escalated = True
                fallback_verdict = parse_verdict(client.chat(full_prompt))
                verdict = fallback_verdict
            else:
                verdict = answer
            wall = time.monotonic() - started
        records.append(
            CallRecord(
                variant=variant,
                pair=pair.name,
                expected=pair.expected,
                verdict=verdict,
                correct=verdict == pair.expected,
                prompt_chars=len(prompt),
                wall_s=round(wall, 3),
                kind=pair.kind,
                escalated=escalated,
                fallback_verdict=fallback_verdict,
                delta_sufficient=pair.delta_sufficient,
            )
        )
    return records


def aggregate(records: list[CallRecord]) -> dict[str, object]:
    """Агрегаты по вариантам.

    Для delta/delta-map дополнительно: escalation_rate, accuracy_raw
    (до fallback; ESCALATE = неверный вердикт) и accuracy_after_fallback.
    delta_ratio = prompt_chars(delta)/prompt_chars(full) считается ТОЛЬКО
    по easy-парам (нет easy -> None).
    """
    by_variant: dict[str, list[CallRecord]] = {}
    for rec in records:
        by_variant.setdefault(rec.variant, []).append(rec)
    out: dict[str, object] = {}
    for name, recs in sorted(by_variant.items()):
        stats: dict[str, object] = {
            "verdict_compliance": sum(r.verdict is not None for r in recs) / len(recs),
            "accuracy": sum(r.correct for r in recs) / len(recs),
            "mean_prompt_chars": statistics.mean(r.prompt_chars for r in recs),
        }
        if name in ESCALATE_VARIANTS:
            escalated_n = sum(r.escalated for r in recs)
            stats["escalation_rate"] = escalated_n / len(recs)
            stats["accuracy_raw"] = (
                sum(r.correct and not r.escalated for r in recs) / len(recs)
            )
            stats["accuracy_after_fallback"] = stats["accuracy"]
        out[name] = stats
    if "delta" in out and "full" in out:
        easy_delta = [
            r.prompt_chars for r in records if r.kind == "easy" and r.variant == "delta"
        ]
        easy_full = [
            r.prompt_chars for r in records if r.kind == "easy" and r.variant == "full"
        ]
        if easy_delta and easy_full:
            full_chars = statistics.mean(easy_full) or 1.0
            out["delta_ratio"] = statistics.mean(easy_delta) / full_chars
        else:
            out["delta_ratio"] = None
    return out


def hypothesis_gate(agg: dict[str, object]) -> int | None:
    """2 — гипотеза не подтвердилась: delta хуже full на >0.2 accuracy
    или delta нарушает контракт вердикта чаще 20% (compliance < 0.8)."""
    delta = agg.get("delta")
    if not isinstance(delta, dict):
        return None
    full = agg.get("full")
    accuracy_full = full["accuracy"] if isinstance(full, dict) else 1.0
    if delta["accuracy"] < accuracy_full - 0.2:
        return 2
    if delta["verdict_compliance"] < 0.8:
        return 2
    return None


def hard_pair_gate(records: list[CallRecord]) -> int | None:
    """3 — full-критик обязан давать accuracy >= 0.8 на hard-парах:
    неточный full-оракул обесценивает сравнение дельты с ним."""
    full_hard = [r for r in records if r.variant == "full" and r.kind == "hard"]
    if not full_hard:
        return None
    accuracy = sum(r.correct for r in full_hard) / len(full_hard)
    return 3 if accuracy < HARD_FULL_ACCURACY_MIN else None


class _OllamaPilotClient:
    """Адаптер vp_ab_pilot.OllamaClient под контракт chat(prompt) -> str.

    Переиспользует сетевой слой пилота V-P (retry x2, usage-журнал),
    а не копирует его; role/model_class фиксируются для метки полки.
    """

    def __init__(self, inner: OllamaClient) -> None:
        self._inner = inner

    def chat(self, prompt: str) -> str:
        return str(
            self._inner.complete(
                role="critic",
                model_class="vs_delta_pilot",
                prompt=prompt,
                inputs={},
            )
        )


def make_client(model: str, base_url: str) -> ChatClient:
    """Реальный клиент — OllamaClient из vp_ab_pilot (import, не копия).

    base_url здесь — префикс OpenAI-API (по умолчанию
    http://127.0.0.1:11435/v1), как в ТЗ; OllamaClient ждёт полный
    эндпоинт, поэтому достраиваем /chat/completions.
    """
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from ai_workspace.tools.vp_ab_pilot import OllamaClient

    endpoint = base_url.rstrip("/") + "/chat/completions"
    return _OllamaPilotClient(OllamaClient(url=endpoint, model=model))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Пилот «дельта vs 7B»: критик qwen2.5:7b по дельте документа",
    )
    parser.add_argument(
        "--variant",
        choices=["all", *VARIANTS],
        default="all",
        help="какую prompt-стратегию гонять (all — все три)",
    )
    parser.add_argument(
        "--pairs",
        choices=["all", *PAIR_KINDS],
        default="all",
        help="какой набор ground-truth пар гонять (all — easy + hard)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=2,
        help="повторов на (вариант x пару)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=None, help="JSON-отчёт")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="без сети: печатает промпты и метрики (verdict=NONE), гейты отключены",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    pairs = select_pairs(args.pairs)

    client: ChatClient | None = None
    if not args.dry_run:
        try:
            client = make_client(args.model, args.base_url)
        except Exception as exc:  # noqa: BLE001
            print(f"[config] не удалось собрать клиент: {exc}", file=sys.stderr)
            return 1

    records: list[CallRecord] = []
    for variant in variants:
        for pair in pairs:
            if args.dry_run:
                print("=" * 72)
                print(
                    f"[PROMPT] variant={variant} pair={pair.name} "
                    f"kind={pair.kind} expected={pair.expected}"
                )
                print(pair_prompt(variant, pair))
            try:
                records.extend(run_variant(variant, pair, client, args.n))
            except Exception as exc:  # noqa: BLE001
                print(f"[network] {variant}/{pair.name}: {exc}", file=sys.stderr)
                return 1

    agg = aggregate(records)
    print("\n== МЕТРИКИ ==")
    for rec in records:
        verdict = rec.verdict if rec.verdict is not None else "NONE"
        fallback = (
            rec.fallback_verdict if rec.fallback_verdict is not None else "NONE"
        )
        print(
            f"{rec.variant:<9} {rec.pair:<10} expected={rec.expected:<6} "
            f"verdict={verdict:<6} correct={rec.correct!s:<5} "
            f"prompt_chars={rec.prompt_chars} wall_s={rec.wall_s} "
            f"kind={rec.kind} escalated={rec.escalated!s} fallback={fallback}"
        )
    for name, value in agg.items():
        print(f"aggregate {name}: {value}")

    if args.out is not None:
        payload = {
            "model": args.model,
            "base_url": args.base_url,
            "dry_run": args.dry_run,
            "n": args.n,
            "pairs": args.pairs,
            "records": [asdict(rec) for rec in records],
            "aggregate": agg,
        }
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"отчёт: {args.out}")

    if args.dry_run:
        return 0
    gate = hard_pair_gate(records)
    if gate == 3:
        print(
            "[gate] full-критик неточен на hard-парах: accuracy(full) < 0.8",
            file=sys.stderr,
        )
        return gate
    gate = hypothesis_gate(agg)
    if gate is not None:
        print(
            "[gate] гипотеза не подтвердилась: см. aggregate delta "
            "(accuracy/verdict_compliance)",
            file=sys.stderr,
        )
    return gate if gate is not None else 0


if __name__ == "__main__":
    sys.exit(main())
