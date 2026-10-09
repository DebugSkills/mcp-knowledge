"""Живой A/B-пилот V-P (Ф6-a 6a.2b): какой промпт роли лучше на слабой модели.

trace_id: arch-2026-10-05-ai-workspace, план REV.15.

Три варианта промпта роли в ``engine._prompt`` (переключаются реестром-подделкой,
``registry/roles.yaml`` НЕ правится):

- ``empty`` — без контракта (поведение до 6a.2a);
- ``full`` — вместо контракта полный текст seed-скилла роли (зеркало
  ``.kilo/skills/…/SKILL.md``, при недоступности — ``.knowledge/skills/…``);
- ``contract`` — контракт из ``roles.yaml`` (как сейчас, 6a.2a).

Прогон живой: ollama напрямую ``http://127.0.0.1:11435`` (OpenAI-совместимый
``/v1/chat/completions``), модель ``qwen2.5:7b``. LiteLLM-шлюз с хоста не
опубликован (internal-only) → пилот А/Б промпта, НЕ тест шлюза; ext/DeepSeek
не задействуется. Все model_classes печатаются на local-полку — метка полки
в ``on_node_usage`` обязана говорить правду о маршруте.

Метрики — из порта ``on_node_usage`` (Ф6-a 6a.1): суммарные
``prompt_chars``/``tokens``/``wall_s`` по LLM-узлам, + детерминированный
СТРУКТУРНЫЙ скор 0..1 (семантику не оцениваем — честный предел пилота):

- ``verdict`` (0.2): вердикт критика распарсился ``engine._parse_verdict``
  (секция ``verdict`` пишется только после успешного парсинга);
- ``sections`` (0.4): секции ``output.sections`` режима непусты на доске;
- ``citation`` (0.2): маркер блока цитат ``src-`` (citer-стаб) в составе;
- ``length`` (0.2): длина документа в коридоре [400, 12000] симв — низ отсекает
  пустышку/отказ, верх — зацикленную генерацию 7B (при max_output_tokens=2048).

Запуск: ``python -m ai_workspace.tools.vp_ab_pilot --mode …/statya.yaml
--tasks 2 --runs 1`` (по умолчанию мало задач, чтобы уложиться по времени).
Offline-тесты: ``WS_REDIS_URL= python -m pytest ai_workspace/tests/test_vp_ab_pilot.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ai_workspace import conformance as cf
from ai_workspace.artifacts import ArtifactStore, MemoryBackend
from ai_workspace.orchestrator.engine import (
    LLMClient,
    LLMResult,
    MemoryLedger,
    ModeEngine,
    load_mode,
)
from ai_workspace.orchestrator.graph import Node
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import FakeBoards, FakeJobs
from ai_workspace.tools.golden_run import StubMCP

__all__ = [
    "BASE_VARIANT",
    "VARIANTS",
    "OllamaClient",
    "RunOutcome",
    "build_variant_registry",
    "document_checks",
    "load_seed_text",
    "main",
    "run_one",
    "score_run",
]

AI_WORKSPACE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AI_WORKSPACE_DIR.parent
DEFAULT_GOLDEN = AI_WORKSPACE_DIR / "tests" / "golden" / "golden-set.yaml"
DEFAULT_MODE = AI_WORKSPACE_DIR / "modes" / "statya.yaml"
DEFAULT_REPORT = (
    REPO_ROOT / "plans" / "_provenance" / "arch-2026-10-05-ai-workspace"
    / "Ф6a-2b-vp-pilot.md"
)

OLLAMA_URL = "http://127.0.0.1:11435/v1/chat/completions"
OLLAMA_MODELS_URL = "http://127.0.0.1:11435/v1/models"
OLLAMA_MODEL = "qwen2.5:7b"

VARIANTS: tuple[str, ...] = ("empty", "full", "contract")
"""Порядок вариантов в отчёте; contract — текущее поведение (6a.2a)."""

BASE_VARIANT = "base"
"""Не-A/B вариант: роли реестра как есть, без подделки (probe Ф7, fix §10
LIVE-PROBE-1 — «base»-прогон калибровки). В ``VARIANTS`` НЕ входит: A/B-пилот
«base» отдельным вариантом не гоняет."""

MIN_DOC_CHARS, MAX_DOC_CHARS = 400, 12000
CITATION_MARKER = "src-"
FRAGMENT_CHARS = 400
W_VERDICT, W_SECTIONS, W_CITATION, W_LENGTH = 0.2, 0.4, 0.2, 0.2


# ── клиент локальной полки (ollama напрямую, stdlib only) ─────────────────


class OllamaClient:
    """LLM-клиент local-полки: ollama OpenAI-совместимый эндпоинт, без зависимостей.

    ``params`` — decoding-pin движка (``temperature``/``seed``/
    ``max_output_tokens``); ``thinking`` в payload не идёт (эндпоинт ollama
    его не принимает). Таймаут ~120 с, одна повторная попытка. Журнал
    ``calls`` хранит живые факты (wall_s, usage из ответа, фрагмент) —
    доказательство не-стаба для отчёта.
    """

    def __init__(
        self, url: str = OLLAMA_URL, model: str = OLLAMA_MODEL,
        timeout_s: float = 120.0,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout_s = timeout_s
        self.calls: list[dict[str, Any]] = []

    def ping(self) -> list[str]:
        """Список id моделей (``GET /v1/models``) — проверка живости полки."""
        req = urllib.request.Request(OLLAMA_MODELS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(m.get("id")) for m in data.get("data", [])]

    def complete(
        self, *, role: str, model_class: str, prompt: str,
        inputs: Mapping[str, str], params: Mapping[str, Any] | None = None,
        job_id: str | None = None,
    ) -> LLMResult:
        p = dict(params or {})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(p.get("temperature", 0.0)),
            "max_tokens": int(p.get("max_output_tokens", 2048)),
        }
        if p.get("seed") is not None:
            payload["seed"] = int(p["seed"])
        if job_id is not None:
            # metadata запроса LiteLLM (Ф6 TODO 1): склейка вызова с job'ом
            payload["metadata"] = {"job_id": job_id}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for _attempt in (1, 2):  # одна повторная попытка
            req = urllib.request.Request(
                self.url, data=body, headers={"Content-Type": "application/json"},
                method="POST",
            )
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    # request_id из заголовка шлюза (Ф6 TODO 1/К2:
                    # x-litellm-call-id; прямой ollama его не шлёт → None)
                    request_id = resp.headers.get("x-litellm-call-id")
                content = str(data["choices"][0]["message"]["content"])
                usage = dict(data.get("usage") or {})
                self.calls.append({
                    "role": role, "wall_s": round(time.monotonic() - t0, 3),
                    "prompt_chars": len(prompt), "output_chars": len(content),
                    "request_id": request_id,
                    "usage": usage, "fragment": content[:200],
                })
                return LLMResult(
                    output=content,
                    request_id=str(request_id) if request_id else None,
                    usage=usage or None,
                )
            except Exception as exc:  # noqa: BLE001 — транспортная ошибка = ретрай
                last = exc
        raise RuntimeError(f"ollama: две попытки не прошли ({self.url}): {last}")

    def journal_summary(self) -> dict[str, Any]:
        """Сводка живых вызовов для отчёта (токены — из ответов ollama)."""
        if not self.calls:
            return {"calls": 0}
        pt = sum(int(c["usage"].get("prompt_tokens", 0)) for c in self.calls)
        ct = sum(int(c["usage"].get("completion_tokens", 0)) for c in self.calls)
        slowest = max(self.calls, key=lambda c: c["wall_s"])
        return {
            "calls": len(self.calls), "prompt_tokens": pt, "completion_tokens": ct,
            "max_wall_s": slowest["wall_s"],
            "sample": {k: slowest[k] for k in ("role", "wall_s", "fragment")},
        }


# ── варианты промпта: seed-тексты и реестр-подделка ───────────────────────


def _seed_candidates(seed_skill: str) -> list[Path]:
    """Пути seed-скилла: зеркало ``.kilo`` (приоритет), затем исходный путь."""
    raw = Path(seed_skill)
    out: list[Path] = []
    if raw.is_absolute():
        out.append(raw)
    else:
        if raw.parts and raw.parts[0] == ".knowledge":
            out.append(REPO_ROOT / ".kilo" / Path(*raw.parts[1:]))
        out.append(REPO_ROOT / raw)
    return out


def load_seed_text(seed_skill: str) -> str:
    """Полный текст seed-скилла как есть (включая frontmatter)."""
    for path in _seed_candidates(seed_skill):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    tried = ", ".join(str(p) for p in _seed_candidates(seed_skill))
    raise FileNotFoundError(f"seed-скилл не найден ({seed_skill}): пробовал {tried}")


class VariantRegistry:
    """Утка реестра для A/B: перекрывает ``roles`` вариантом промпта.

    ``model_classes`` печатается на local-полку целиком: пилот идёт в local
    ollama напрямую, и метка полки в ``on_node_usage`` должна говорить правду.
    Прочие kinds — passthrough реального реестра. ``roles.yaml`` не меняется.
    """

    def __init__(self, base: Registry, roles: dict[str, Any]) -> None:
        self._base = base
        self._roles = roles

    def get(self, kind: str) -> dict:
        if kind == "roles":
            return self._roles
        data = self._base.get(kind)
        if kind == "model_classes":
            return {name: {**spec, "shelf": "local"} for name, spec in data.items()}
        return data


def build_variant_registry(
    base: Registry, variant: str,
    *, seed_reader: Callable[[str], str] = load_seed_text,
) -> VariantRegistry:
    """Реестр-подделка варианта: empty/full/contract по метам roles.yaml.

    ``BASE_VARIANT`` ("base") — роли реестра как есть (без модификаций):
    базовый прогон probe-измерителя Ф7; тот же passthrough, что у "contract".
    """
    if variant not in VARIANTS and variant != BASE_VARIANT:
        raise ValueError(
            f"неизвестный вариант {variant!r}; ожидается {VARIANTS} или {BASE_VARIANT!r}"
        )
    roles: dict[str, Any] = {}
    for role, meta in base.get("roles").items():
        meta = dict(meta)
        if variant == "empty":
            meta["contract"] = None  # seed_loader не подключён → без префикса
        elif variant == "full":
            meta["contract"] = seed_reader(str(meta.get("seed_skill", "")))
        # contract: как есть (6a.2a)
        roles[role] = meta
    return VariantRegistry(base, roles)


# ── структурный скор ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class DocumentChecks:
    sections_ok: bool
    citation_ok: bool
    length_ok: bool
    doc_chars: int


def document_checks(
    mode_output: Mapping[str, Any], sections: Mapping[str, str],
) -> DocumentChecks:
    """Структурные критерии финального документа по ``output`` режима.

    ``sections`` — секции доски после прогона; длина меряется по ПЕРВОЙ секции
    (сам документ, без приложения-цитат citer'а).
    """
    wanted = [str(s) for s in mode_output.get("sections") or []]
    if mode_output.get("section"):
        wanted = [str(mode_output["section"])] + wanted
    if not wanted:
        wanted = ["document"]
    sections_ok = all(sections.get(name, "").strip() for name in wanted)
    composed = "\n\n".join(sections.get(name, "") for name in wanted)
    citation_ok = CITATION_MARKER in composed
    doc_main = sections.get(wanted[0], "")
    return DocumentChecks(
        sections_ok=sections_ok, citation_ok=citation_ok,
        length_ok=MIN_DOC_CHARS <= len(doc_main) <= MAX_DOC_CHARS,
        doc_chars=len(doc_main),
    )


def score_run(*, verdict_parse_ok: bool, checks: DocumentChecks) -> float:
    """Скор 0..1: взвешенная сумма четырёх структурных критериев."""
    raw = (
        W_VERDICT * verdict_parse_ok
        + W_SECTIONS * checks.sections_ok
        + W_CITATION * checks.citation_ok
        + W_LENGTH * checks.length_ok
    )
    return round(raw, 3)


# ── один прогон (вариант × задание × run) ─────────────────────────────────


@dataclass
class RunOutcome:
    variant: str
    task_id: str
    mode: str
    run: int
    status: str  # done | failed | stopped | error
    detail: str = ""
    verdict: str | None = None
    verdict_parse_ok: bool = False
    document: str = ""
    draft: str = ""
    score: float = 0.0
    job_wall_s: float = 0.0
    llm_wall_s: float = 0.0
    prompt_chars: int = 0
    tokens: int = 0
    # 2c (В2-A «Достоверность»): in/out-разбивка из usage ответов сервера —
    # журнал llm.calls несёт живой факт (OllamaClient пишет usage каждого
    # вызова); без usage (стабы/FakeLLM) остаются 0 → probe честно падает
    # обратно на нижнюю оценку «все токены входные».
    tokens_in: int = 0
    tokens_out: int = 0
    node_events: list[dict[str, Any]] = field(default_factory=list)
    live_sample: dict[str, Any] = field(default_factory=dict)
    critic_fragment: str = ""
    # В3-A 3b (Ф7, приватность I5): структурные критерии прогона — булевы
    # для partial-снапшота probe (тексты document/draft в снапшот не едут,
    # пересчёт из них невозможен — критерии несут сам прогон); error-прогон
    # оставляет None (критерии не вычислены).
    checks: DocumentChecks | None = None


def _critic_node(graph: Any) -> Node | None:
    for node in graph.nodes.values():
        if node.kind == "critic-gate":
            return node
    return None


def run_one(
    mode_path: Path,
    task: Mapping[str, Any],
    variant: str,
    run_idx: int,
    llm: LLMClient,
    *,
    base_registry: Registry | None = None,
    seed_reader: Callable[[str], str] = load_seed_text,
) -> RunOutcome:
    """Прогнать задание до терминала на выбранном варианте промпта.

    Human-gate'ы авто-approve (пилот), сторы in-memory (по одному job на
    вариант×задание×run — fx-кэш не переносится между вариантами).
    """
    base = base_registry or Registry(AI_WORKSPACE_DIR / "registry")
    # В1a.1/В1a.2 Ф7 (arch-2026-10-08-f7-calibration): активный профиль
    # калибровки для конструктора движка. Селектор — реестр model_classes
    # базового реестра (active_profile), факт полки — провайдер (ollama
    # /api/tags, Э2-2); нет профиля → (None, None) → паритет F1. Import
    # ЛОКАЛЬНЫЙ: model_facts импортирует OLLAMA_MODELS_URL из этого модуля
    # — топ-уровень дал бы циклический импорт.
    from ai_workspace.calibration.model_facts import urllib_http_get
    from ai_workspace.calibration.runtime import DEFAULT_PROFILES_DIR, active_calibration

    cal_profile, cal_facts = active_calibration(
        base.dir, DEFAULT_PROFILES_DIR, http_get=urllib_http_get,
    )
    graph = load_mode(mode_path)
    job_id = f"vp-{variant}-{task['id']}-{run_idx}"
    outcome = RunOutcome(
        variant=variant, task_id=str(task["id"]), mode=graph.doc.get("id", mode_path.stem),
        run=run_idx, status="error",
    )
    events: list[dict[str, Any]] = []
    # 2c (В2-A): журнал вызовов клиента (OllamaClient.calls) несёт usage из
    # ответов; клиент ОДИН на все прогоны probe → снапшот длины ДО прогона,
    # чтобы посчитать только вызовы этого run_one.
    journal = getattr(llm, "calls", None)
    journal_start = len(journal) if isinstance(journal, list) else 0
    t0 = time.monotonic()
    try:
        jobs = FakeJobs()
        jobs.create(job_id, zone=str(task.get("zone", "public")))
        engine = ModeEngine(
            jobs=jobs,
            boards=FakeBoards(),
            graph=graph,
            llm=llm,
            mcp=StubMCP(),
            ledger=MemoryLedger(),
            artifacts=ArtifactStore(MemoryBackend()),
            registry=build_variant_registry(base, variant, seed_reader=seed_reader),
            decoding=cf.DECODING_PIN,
            on_node_usage=events.append,
            # В1a.2 Ф7: профиль/факты калибровки (см. выше); (None, None)
            # без active_profile — паритет F1.
            calibration_profile=cal_profile,
            calibration_model_facts=cal_facts,
        )
        engine.seed(job_id, {"brief": str(task["prompt"])}, epoch=1)
        step = engine.run(job_id, epoch=1)
        guard = 0
        while step.status == "paused":
            guard += 1
            if guard > len(graph.nodes) + 2:
                break
            step = engine.resume(
                job_id, epoch=1, token=step.resume_token or "", decision="approve",
            )
        _, sections = engine.boards.read()
        outcome.status = step.status if step.status != "paused" else "stopped"
        outcome.detail = step.detail
        outcome.document = sections.get("document", "")
        outcome.draft = sections.get("draft", "")

        critic = _critic_node(graph)
        verdict_text = sections.get("verdict", "")
        if critic is None:
            outcome.verdict_parse_ok = True  # критика в режиме нет — нейтрально
        elif verdict_text.strip():
            # секция пишется только ПОСЛЕ успешного _parse_verdict (engine)
            outcome.verdict_parse_ok = True
            try:
                outcome.verdict = ModeEngine._parse_verdict(critic, verdict_text)
            except Exception:  # noqa: BLE001 — теоретически недостижимо
                outcome.verdict = None
        checks = document_checks(graph.doc.get("output") or {}, sections)
        outcome.score = score_run(
            verdict_parse_ok=outcome.verdict_parse_ok, checks=checks
        )
        outcome.checks = checks  # В3-A 3b: булевы критерии для partial-снапшота
    except Exception as exc:  # noqa: BLE001 — один сбой не валит пилот
        outcome.status = "error"
        outcome.detail = f"{type(exc).__name__}: {exc}"
    finally:
        outcome.job_wall_s = round(time.monotonic() - t0, 3)
    llm_events = [e for e in events if e["kind"] in ("llm-step", "critic-gate")]
    outcome.node_events = events
    outcome.prompt_chars = sum(int(e["prompt_chars"]) for e in llm_events)
    outcome.tokens = sum(int(e["tokens"]) for e in llm_events)
    # 2c (В2-A): in/out из usage НОВЫХ записей журнала (записи без usage —
    # стабы/FakeLLM, чужие строки-роли — пропускаются; тогда разбивки нет и
    # M6 probe идёт нижней оценкой, как до В2-A).
    if isinstance(journal, list):
        for call in journal[journal_start:]:
            usage = call.get("usage") if isinstance(call, dict) else None
            if isinstance(usage, Mapping):
                outcome.tokens_in += int(usage.get("prompt_tokens") or 0)
                outcome.tokens_out += int(usage.get("completion_tokens") or 0)
    outcome.llm_wall_s = round(sum(float(e["wall_s"]) for e in llm_events), 3)
    if isinstance(llm, OllamaClient) and llm.calls:
        outcome.live_sample = dict(llm.calls[-1])
        if not outcome.verdict_parse_ok:
            critic_calls = [c for c in llm.calls if c["role"] == "critic"]
            if critic_calls:
                outcome.critic_fragment = str(critic_calls[-1]["fragment"])
    return outcome


# ── агрегация и отчёт ─────────────────────────────────────────────────────


def _agg(outcomes: list[RunOutcome]) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        runs = [o for o in outcomes if o.variant == variant]
        if not runs:
            continue
        data[variant] = {
            "runs": len(runs),
            "done": sum(1 for o in runs if o.status == "done"),
            "failed": sum(1 for o in runs if o.status != "done"),
            "verdict_ok": sum(1 for o in runs if o.verdict_parse_ok),
            "score": round(sum(o.score for o in runs) / len(runs), 3),
            "prompt_chars": sum(o.prompt_chars for o in runs),
            "tokens": sum(o.tokens for o in runs),
            "llm_wall_s": round(sum(o.llm_wall_s for o in runs), 1),
        }
    return data


def _delta(label: str, a: float, b: float, *, unit: str = "") -> str:
    if b == 0:
        return f"{label}: {a}{unit} vs 0{unit} (деление на 0 — «—»)"
    pct = (a - b) / b * 100
    return f"{label}: {a}{unit} vs {b}{unit} ({pct:+.0f}%)"


def build_report(
    outcomes: list[RunOutcome],
    *,
    modes: list[str],
    tasks: list[Mapping[str, Any]],
    runs: int,
    provider: str,
    live: Mapping[str, Any] | None,
) -> str:
    agg = _agg(outcomes)
    by_variant: dict[str, RunOutcome] = {}
    for o in outcomes:  # пример: первый done, иначе первый любой
        by_variant.setdefault(o.variant, o)
        if o.status == "done" and by_variant[o.variant].status != "done":
            by_variant[o.variant] = o

    lines = [
        "# Ф6-a 6a.2b — живой A/B-пилот V-P: какой промпт роли лучше на слабой модели",
        "",
        f"- Дата: {datetime.now(timezone.utc).isoformat(timespec='seconds')} · trace_id: `arch-2026-10-05-ai-workspace` · план REV.15",
        "- Вопрос: `contract` (roles.yaml, 6a.2a) vs `full` (seed-скилл целиком) vs `empty` (без префикса) на слабой модели.",
        f"- Контур: **ollama напрямую** `{OLLAMA_URL.split('/v1')[0]}`, модель `{OLLAMA_MODEL}`.",
        "  LiteLLM-шлюз с хоста не опубликован (internal-only) и НЕ задействован — это A/B промпта, не тест шлюза; ext/DeepSeek не трогали.",
        f"- Провайдер: {provider}",
        (
            f"- Режимы: {', '.join(modes)} · golden-set `{DEFAULT_GOLDEN.name}`, задания: "
            f"{', '.join(str(t['id']) for t in tasks)} · runs={runs} на вариант."
        ),
        f"- Decoding-pin: {cf.DECODING_PIN.as_params()} · human-gate: авто-approve · citer: StubMCP (цитаты canned `src-*`).",
        "",
        "## Результаты: вариант × метрики",
        "",
        "Метрики — суммы по LLM-узлам (`on_node_usage`, Ф6-a 6a.1); `tokens` — оценка движка ~4 симв/токен;",
        "`score` — структурный скор 0..1 (verdict 0.2 + sections 0.4 + citation 0.2 + length 0.2).",
        "",
        "| вариант | runs | done | verdict_parse_ok | score (0..1) | prompt_chars | tokens | wall_s (LLM-узлы) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for variant in VARIANTS:
        if variant not in agg:
            continue
        r = agg[variant]
        lines.append(
            f"| {variant} | {r['runs']} | {r['done']} | {r['verdict_ok']}/{r['runs']} "
            f"| {r['score']:.3f} | {r['prompt_chars']} | {r['tokens']} | {r['llm_wall_s']:.1f} |"
        )
    lines += ["", "### Дельты", ""]
    for other in ("empty", "full"):
        if other in agg and "contract" in agg:
            c, e = agg["contract"], agg[other]
            lines += [
                (
                    f"**contract vs {other}:** score {c['score']:.3f} vs {e['score']:.3f} "
                    f"({c['score'] - e['score']:+.3f}); verdict_parse_ok "
                    f"{c['verdict_ok']}/{c['runs']} vs {e['verdict_ok']}/{e['runs']}"
                ),
                f"- {_delta('prompt_chars', c['prompt_chars'], e['prompt_chars'])}",
                f"- {_delta('tokens', c['tokens'], e['tokens'])}",
                f"- {_delta('wall_s', c['llm_wall_s'], e['llm_wall_s'], unit=' с')}",
                "",
            ]

    lines += ["## Per-node usage (вариант × узел; суммы по всем прогонам варианта)", "",
              "| вариант | узел | kind | роль | calls | cached | prompt_chars | tokens | wall_s |",
              "|---|---|---|---|---|---|---|---|---|"]
    node_order: list[tuple[str, str]] = []
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for o in outcomes:
        for ev in o.node_events:
            key = (o.variant, str(ev["node"]))
            if key not in rows:
                node_order.append(key)
                rows[key] = {
                    "kind": ev["kind"], "role": ev["role"], "calls": 0, "cached": 0,
                    "prompt_chars": 0, "tokens": 0, "wall_s": 0.0,
                }
            r = rows[key]
            r["calls"] += 1
            r["cached"] += 1 if ev["cached"] else 0
            r["prompt_chars"] += int(ev["prompt_chars"])
            r["tokens"] += int(ev["tokens"])
            r["wall_s"] += float(ev["wall_s"])
    for (variant, node) in node_order:
        r = rows[(variant, node)]
        lines.append(
            f"| {variant} | {node} | {r['kind']} | {r['role'] or '—'} | {r['calls']} "
            f"| {r['cached']} | {r['prompt_chars']} | {r['tokens']} | {r['wall_s']:.1f} |"
        )
    lines += ["", "## Детали прогонов", "",
              "| вариант | режим/задание | status | score | detail (≤140 симв.) |",
              "|---|---|---|---|---|"]
    for o in outcomes:
        detail = " ".join(o.detail.split())[:140] or "—"
        lines.append(
            f"| {o.variant} | {o.mode}/{o.task_id} | {o.status} | {o.score:.3f} | {detail} |"
        )
    lines += ["", "## Диагностика критика (фрагмент ответа при провале парсинга)", ""]
    diag = [o for o in outcomes if not o.verdict_parse_ok]
    if not diag:
        lines.append("- Все вердикты распарсились.")
    else:
        lines += ["| вариант | режим/задание | ответ критика (≤200 симв.) |", "|---|---|---|"]
        for o in diag:
            frag = " ".join(o.critic_fragment.split())[:200] or "—"
            lines.append(f"| {o.variant} | {o.mode}/{o.task_id} | {frag} |")

    lines += ["", "## Примеры вывода на вариант (обрезано до ~400 симв.)", ""]
    for variant in VARIANTS:
        o = by_variant.get(variant)
        if o is None:
            continue
        sample = o.document or o.draft or f"[нет вывода: {o.status} — {o.detail}]"
        frag = sample[:FRAGMENT_CHARS].replace("\n", " ")
        lines += [f"### {variant} · {o.mode}/{o.task_id} · status={o.status} · score={o.score:.3f}",
                  "", f"> {frag}", ""]

    lines += ["## Живой факт (прогон не стаб)", ""]
    if live and live.get("calls"):
        s = live.get("sample") or {}
        lines += [
            (
                f"- Живых вызовов ollama: {live['calls']}; usage по ответам сервера: "
                f"prompt_tokens={live.get('prompt_tokens', 0)}, "
                f"completion_tokens={live.get('completion_tokens', 0)}."
            ),
            f"- Самый долгий вызов: роль={s.get('role')}, wall_s={s.get('wall_s')}.",
            f"- Фрагмент ответа 7B: «{str(s.get('fragment', ''))[:200]}»",
        ]
    else:
        lines.append("- Живых вызовов нет (offline-прогон на скриптованном LLM).")

    verdict_line = "недостаточно данных"
    if {"contract", "empty", "full"} <= agg.keys():
        c, e, f_ = agg["contract"], agg["empty"], agg["full"]
        beats = c["score"] > e["score"] and c["score"] > f_["score"]
        done_total = sum(1 for o in outcomes if o.status == "done")
        verdict_line = (
            f"contract {'БЬЁТ' if beats else 'НЕ бьёт'} оба варианта по score "
            f"({c['score']:.3f} vs empty {e['score']:.3f} / full {f_['score']:.3f}); "
            f"verdict_parse_ok: {c['verdict_ok']}/{c['runs']} vs "
            f"{e['verdict_ok']}/{e['runs']} vs {f_['verdict_ok']}/{f_['runs']}; "
            f"полных (done) прогонов всего {done_total}/{len(outcomes)} — "
            "конвейер на 7B в основном не доходит до документа (см. «Детали прогонов»)"
        )
    lines += [
        "",
        "## Вывод",
        "",
        f"- {verdict_line}.",
        "- Details см. в таблицах выше; финальную качественную формулировку добавляет",
        "  оператор/критик по числам (механическое сравнение — здесь).",
        "",
        "## Ограничения (честно)",
        "",
        "- local-only: один провайдер ollama `qwen2.5:7b` (7B); ext/DeepSeek не задействован —",
        "  перенос выводов на сильную модель не доказан.",
        "- Шлюз LiteLLM не задействован (internal-only с хоста): wire-трансформации шлюза не покрыты.",
        "- Скор — СТРУКТУРНЫЙ (вердикт-парсинг, секции, маркер цитат, длина в коридоре);",
        "  семантику (полезность/точность текста) пилот не измеряет — это предел метода.",
        f"- N мал (заданий {len(tasks)} × runs {runs} × 3 варианта) — пилот, не статистика;",
        "  temperature=0/seed=42 снижает разброс, но детерминизм 7B не гарантирован.",
        "- `citer` — StubMCP: критерий `citation` проверяет механику приложения блока цитат,",
        "  не качество реальных цитат.",
        "- `tokens` — оценка движка (~4 симв/токен), не биллинг ollama (живой usage — в «Живом факте»).",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Живой A/B-прогон: варианты × задания × runs → отчёт-Markdown."""
    parser = argparse.ArgumentParser(
        prog="vp-ab-pilot",
        description="A/B-пилот V-P (Ф6-a 6a.2b): empty/full/contract промпт роли на local 7B",
    )
    parser.add_argument("--mode", type=Path, action="append", default=None,
                        help="путь режима (можно несколько раз); по умолчанию statya")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--tasks", type=int, default=2,
                        help="сколько первых заданий golden-set брать")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="таймаут одного вызова ollama, с")
    args = parser.parse_args(argv)

    client = OllamaClient(timeout_s=args.timeout)
    try:
        models = client.ping()
    except Exception as exc:  # noqa: BLE001 — полка недоступна = пилот невозможен
        print(f"ollama недоступна ({OLLAMA_MODELS_URL}): {exc}", file=sys.stderr)
        return 2
    if OLLAMA_MODEL not in models:
        print(f"модель {OLLAMA_MODEL!r} отсутствует на полке: {models}", file=sys.stderr)
        return 2
    provider = f"ollama /v1/models жива: {', '.join(models)}"

    golden = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    tasks = list(golden["tasks"])[: max(1, args.tasks)]
    modes = args.mode or [DEFAULT_MODE]
    outcomes: list[RunOutcome] = []
    started = time.monotonic()
    for mode_path in modes:
        for task in tasks:
            for run_idx in range(1, args.runs + 1):
                for variant in VARIANTS:
                    outcome = run_one(mode_path, task, variant, run_idx, client)
                    outcomes.append(outcome)
                    print(
                        f"[vp] {outcome.mode}/{outcome.task_id}/run{outcome.run} "
                        f"{outcome.variant:>9}: {outcome.status:<7} "
                        f"score={outcome.score:.3f} verdict_ok={int(outcome.verdict_parse_ok)} "
                        f"llm_wall={outcome.llm_wall_s:.1f}s",
                        flush=True,
                    )
    total = time.monotonic() - started
    live = client.journal_summary()
    report = build_report(
        outcomes, modes=[p.stem for p in modes], tasks=tasks, runs=args.runs,
        provider=provider, live=live,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"\nпилот: {len(outcomes)} прогонов за {total:.0f} с; живых вызовов {live.get('calls', 0)}")
    print(f"отчёт: {args.report}")
    return 0 if any(o.status == "done" for o in outcomes) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
