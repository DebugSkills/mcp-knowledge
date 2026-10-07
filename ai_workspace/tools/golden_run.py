"""Golden-run режима (по ``--mode``): T/I/Q-прогон + отчёт для Critic-вердикта.

Запуск: ``python -m ai_workspace.tools.golden_run [--golden PATH] [--out DIR] [--report PATH]``
(или ``make golden-run``). Выход: ``0`` — гейт пройден, ``1`` — нарушение T/I
(Q-слой — отчёт, не ассерт: слабые строки помечаются маркером ``local-draft``).

Что проверяется на РЕАЛЬНОМ контуре (mode engine + режим statya + artifact-store):

- **T**: zone→egress — отдельное задание под прогон приватного маршрута (двойной ассерт:
  отказ при `private` вне local И счётчик ext-egress == 0).
- **I**: decoding-pin каждого вызова + **parity промптов** обеих полок на одинаковых
  стаб-ответах (одинаковые входы → одинаковые хэши резолвнутых промптов; любая
  model-specific ветка в контуре ломает parity).
- **Q**: golden-set, N прогонов на полку → Q-отчёт (таблица), порог по зоне,
  маркер ``local-draft`` ниже порога; отчёт сохраняется артефактом.

**Честная граница:** провайдеры здесь — детерминированные стабы полок (валидируется
КОНТУР: маршрут, parity, пин, Q-механика, маркеры). Живой прогон на local/ext
(реальные ollama/DeepSeek) — шаг прода (Ф6): требует поднятых LiteLLM + MCP и времени
на N×заданий; конфиг гейта (пин, пороги, golden-set) здесь уже зафиксирован.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_workspace import conformance as cf
from ai_workspace.artifacts import ArtifactStore, MemoryBackend
from ai_workspace.orchestrator.engine import LLMResult, ModeEngine, load_mode
from ai_workspace.registry import Registry
from ai_workspace.registry.pricing import MICRO_PER_UNIT, PricingRegistry

AI_WORKSPACE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AI_WORKSPACE_DIR.parent
DEFAULT_GOLDEN = AI_WORKSPACE_DIR / "tests" / "golden" / "golden-set.yaml"
DEFAULT_MODE = AI_WORKSPACE_DIR / "modes" / "statya.yaml"
DEFAULT_PRIVATE_MODE = AI_WORKSPACE_DIR / "modes" / "statya-private.yaml"
"""Приватный вариант режима (все узлы ``local-only``): зонный гейт не пускает private в ext."""
DEFAULT_REPORT = REPO_ROOT / "plans" / "_provenance" / "arch-2026-10-05-ai-workspace" / "Ф3.10-golden-run-report.md"

def shelf_answer(shelf: str, task: Mapping[str, Any], run_idx: int = 1) -> str:
    """Детерминированный стаб-ответ полки.

    ext покрывает все ключевые слова; local — теряет ОДНО при первом прогоне и ДВА
    при втором (Q ниже порога → маркер ``local-draft``; разброс между прогонами
    зажигает флаг вариативности R6). Это механика Q-слоя, НЕ измерение качества.
    """
    keywords = [str(k) for k in task["expect_keywords"]]
    if shelf == "ext":
        return " ".join(keywords) + " — развёрнутый ответ со ссылками и структурой."
    lost = min(run_idx, max(1, len(keywords) - 1))
    keep = keywords[: max(1, len(keywords) - lost)]
    return " ".join(keep) + " — компактный вариант локального прогона."


class StubShelfLLM:
    """Детерминированный стаб полки: пишет промпты вызовов + пин-параметры (I/T)."""

    VERDICT = "PASS — соответствует критериям"

    def __init__(self, shelf: str, answer: str) -> None:
        self.shelf = shelf
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, *, role: str, model_class: str, prompt: str, inputs: Mapping[str, str],
        params: Mapping[str, Any] | None = None, job_id: str | None = None,
    ) -> LLMResult:
        self.calls.append({"role": role, "prompt": prompt, "shelf": self.shelf,
                           "params": dict(params or {})})
        # Стаб без наблюдаемости шлюза: request_id/usage = None → движок
        # fallback'ит токены на оценку (числа golden-прогонов не меняются)
        if role == "critic":
            return LLMResult(output=self.VERDICT)  # critic-gate ждёт вердикт (PASS|REVISE)
        return LLMResult(output=self.answer)

    def params(self) -> dict[str, Any]:
        """Параметры декодинга, ФАКТИЧЕСКИ полученные от движка (P1-1: не вакуумно)."""
        return dict(self.calls[0]["params"]) if self.calls else {}

    def prompt_hashes(self) -> dict[str, str]:
        """Хэши промптов по ролям (для parity обеих полок)."""
        return {str(c["role"]): cf.prompt_hash(str(c["prompt"])) for c in self.calls}


class StubMCP:
    """Стаб citer'а: возвращает блок цитат (детерминированно)."""

    def call(self, *, tool: str, args: Mapping[str, Any]) -> Any:
        return {"tool": tool, "refs": ["src-0123456789abcdef"]}


class NodeUsageCollector:
    """Коллектор событий ``on_node_usage`` текущего golden-прогона (Ф6-a 6a.1).

    Копит события ВСЕХ движков прогона (parity + Q + негативные) по узлам;
    таблица отчёта идёт по порядку первого появления узла — это порядок, в
    котором узлы идут в режиме (human-gate не эмитируется). Агрегаты
    ``usage:{node}`` из ledger не дублируются — коллектора достаточно.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def observe(self, event: dict[str, Any]) -> None:
        """Принять событие движка (движок вызывает best-effort)."""
        self.events.append(dict(event))

    def attach(self, engine: ModeEngine) -> ModeEngine:
        """Инжекция коллектора при сборке движка (порт — публичный атрибут).

        Ставится обёрткой фабрики в ``run_golden`` на КАЖДЫЙ движок прогона —
        включая кастомные фабрики тестов (без смены их сигнатуры).
        """
        engine.on_node_usage = self.observe
        return engine


EngineFactory = Callable[[str, str, ArtifactStore, str, str, Path], ModeEngine]
"""Фабрика движка: (полка, job_id, artifact-store, стаб-ответ, zone, путь режима)."""


@dataclass
class RunConfig:
    """Конфигурация прогона: golden-set, режим, полки, каталоги вывода."""

    golden: Path = DEFAULT_GOLDEN
    mode: Path = DEFAULT_MODE
    private_mode: Path = DEFAULT_PRIVATE_MODE
    shelves: tuple[str, ...] = cf.SHELVES
    out_dir: Path | None = None
    min_runs: int = 2


@dataclass
class GoldenRunResult:
    """Результат прогона: отчёт-Markdown + машинные части (для ассертов и Critic)."""

    ok: bool
    report_md: str
    q_report: cf.QReport
    parity: dict[str, cf.ParityReport] = field(default_factory=dict)
    egress: dict[str, Any] = field(default_factory=dict)
    pin_violations: list[str] = field(default_factory=list)
    artifact_id: str | None = None
    export_path: str = ""


def _coverage(answer: str, keywords: Sequence[str]) -> float:
    low = answer.lower()
    return sum(1 for k in keywords if k.lower() in low) / len(keywords)


def _per_node_rows(collector: NodeUsageCollector, pricing: PricingRegistry) -> list[str]:
    """Строки таблицы per-node usage: суммы событий по узлам режима.

    - ``cost`` — ТОЛЬКО полка ``ext``: ``price_for("ext").cost_micro`` по
      ``registry/pricing``. Для ``local`` прайса нет by design и
      ``price_for("local")`` НЕ вызывается (fail-loud: отчёт не должен
      падать на полке без прайса) — колонка «—», единица local-полки —
      GPU-слот-время (``wall_s``).
    - токены in/out для прайса — оценка свежих (не кэш) вызовов по той же
      эвристике движка ~4 симв/токен; кэш-попадание LLM не вызывает →
      денег не тратит (в ``tokens`` таких вызовов 0).
    """
    order: list[str] = []
    rows: dict[str, dict[str, Any]] = {}
    for ev in collector.events:
        node = str(ev["node"])
        if node not in rows:
            order.append(node)
            rows[node] = {
                "kind": ev["kind"], "role": ev["role"], "model_class": ev["model_class"],
                "shelf": ev["shelf"], "calls": 0, "cached": 0, "tokens": 0,
                "prompt_chars": 0, "output_chars": 0, "wall_s": 0.0,
                "fresh_in_chars": 0, "fresh_out_chars": 0,
            }
        row = rows[node]
        row["calls"] += 1
        row["cached"] += 1 if ev["cached"] else 0
        row["tokens"] += int(ev["tokens"])
        row["prompt_chars"] += int(ev["prompt_chars"])
        row["output_chars"] += int(ev["output_chars"])
        row["wall_s"] += float(ev["wall_s"])
        if not ev["cached"]:
            row["fresh_in_chars"] += int(ev["prompt_chars"])
            row["fresh_out_chars"] += int(ev["output_chars"])

    lines: list[str] = []
    for node in order:
        r = rows[node]
        if r["shelf"] == "ext":
            micro = pricing.price_for("ext").cost_micro(
                (r["fresh_in_chars"] + 3) // 4, (r["fresh_out_chars"] + 3) // 4,
            )
            cost = f"{micro / MICRO_PER_UNIT:.4f} ₽"
        else:
            cost = "—"  # local: ₽ не определён by design
        lines.append(
            f"| {node} | {r['kind']} | {r['role'] or '—'} | {r['model_class'] or '—'} "
            f"| {r['shelf']} | {r['calls']} | {r['cached']} | {r['tokens']} "
            f"| {r['prompt_chars']} | {r['output_chars']} | {cost} | {r['wall_s']:.4f} |"
        )
    return lines


def _run_pipeline(
    engine_factory: EngineFactory,
    shelf: str,
    task: Mapping[str, Any],
    artifacts: ArtifactStore,
    *,
    run_idx: int,
    answer: str,
    zone: str = "public",
    mode_path: Path | None = None,
) -> tuple[ModeEngine, StubShelfLLM]:
    """Прогнать одно задание до done через human-gate'ы (HITL авто-approve в прогоне)."""
    job_id = f"golden-{task['id']}-{shelf}-{run_idx}"
    engine = engine_factory(shelf, job_id, artifacts, answer, zone, mode_path or DEFAULT_MODE)
    stub: StubShelfLLM = engine.llm  # type: ignore[assignment]
    engine.seed(job_id, {"brief": str(task["prompt"])}, epoch=1)

    step = engine.run(job_id, epoch=1)
    guard = 0
    while step.status == "paused":
        guard += 1
        if guard > len(engine.graph.nodes) + 1:
            raise RuntimeError(f"golden-run: гейты не завершаются ({job_id})")
        step = engine.resume(job_id, epoch=1, token=step.resume_token or "", decision="approve")
    if step.status != "done":
        raise RuntimeError(f"golden-run: {job_id} завершился {step.status}: {step.detail}")
    return engine, stub


def _run_pipeline_expect_egress_block(
    engine_factory: EngineFactory,
    shelf: str,
    task: Mapping[str, Any],
    artifacts: ArtifactStore,
    *,
    zone: str,
    mode_path: Path | None = None,
) -> tuple[ModeEngine | None, StubShelfLLM]:
    """Негативный прогон private×ext: движок обязан отказать ДО вызова модели."""
    job_id = f"golden-neg-{task['id']}-{shelf}"
    engine = engine_factory(
        shelf, job_id, artifacts, "НЕ ДОЛЖНО ВЫЗЫВАТЬСЯ", zone, mode_path or DEFAULT_MODE
    )
    stub: StubShelfLLM = engine.llm  # type: ignore[assignment]
    engine.seed(job_id, {"brief": str(task["prompt"])}, epoch=1)
    step = engine.run(job_id, epoch=1)
    if step.status != "failed" or "zone=private" not in step.detail:
        raise cf.ConformanceError(
            f"зонный гейт не сработал для {task['id']}/{shelf}: "
            f"status={step.status}, detail={step.detail!r}"
        )
    return engine, stub


def run_golden(config: RunConfig, *, engine_factory: EngineFactory) -> GoldenRunResult:
    """Полный golden-run: T (egress) + I (pin/parity) + Q (отчёт) → отчёт-Markdown."""
    golden = yaml.safe_load(config.golden.read_text(encoding="utf-8"))
    tasks: list[dict[str, Any]] = list(golden["tasks"])
    floors = dict(golden.get("floor") or cf.Q_FLOOR)
    artifacts = ArtifactStore(MemoryBackend())
    q_report = cf.QReport(min_runs=int(golden.get("min_runs", config.min_runs)))
    pin_violations: list[str] = []
    mode_name = config.mode.stem  # атрибуция отчёта: имя режима, не хардкод
    pricing = PricingRegistry(Registry(AI_WORKSPACE_DIR / "registry"))
    usage = NodeUsageCollector()
    raw_factory = engine_factory

    def observed_factory(
        shelf: str, job_id: str, artifacts: ArtifactStore, answer: str,
        zone: str, mode_path: Path,
    ) -> ModeEngine:
        """Сборка движка с инжекцией on_node_usage-коллектора (Ф6-a 6a.1b)."""
        return usage.attach(raw_factory(shelf, job_id, artifacts, answer, zone, mode_path))

    engine_factory = observed_factory

    # ── I: parity на ОДИНАКОВЫХ стаб-ответах (промпты model-agnostic) ──
    parity: dict[str, cf.ParityReport] = {}
    identical = "структура и разделы статьи про MCP"
    same_shelf_hashes: dict[str, dict[str, str]] = {}
    for shelf in config.shelves:
        _, stub = _run_pipeline(
            engine_factory, shelf, tasks[0], artifacts, run_idx=90, answer=identical,
            zone=str(tasks[0]["zone"]), mode_path=config.mode,
        )
        assert isinstance(stub, StubShelfLLM)
        same_shelf_hashes[shelf] = stub.prompt_hashes()
        try:
            cf.assert_decoding_pin(stub.params())
        except cf.DecodingPinViolation as exc:
            pin_violations.append(f"{shelf}: {exc}")
    roles = sorted({role for hashes in same_shelf_hashes.values() for role in hashes})
    for role in roles:
        hashes = {shelf: same_shelf_hashes[shelf][role] for shelf in config.shelves if role in same_shelf_hashes[shelf]}
        parity[role] = cf.ParityReport(hashes=hashes, equal=len(set(hashes.values())) <= 1)
    parity_ok = all(p.equal for p in parity.values())

    # ── Q: N прогонов на (задание, полку) → отчёт.
    #    private-задания: local — прогон, ext — НЕГАТИВ (зонный гейт обязан отказать).
    zone_violations: list[str] = []
    for task in tasks:
        zone = str(task["zone"])
        for shelf in config.shelves:
            if zone == "private" and shelf != "local":
                continue  # private×ext — негативная проверка ниже (на public-режиме)
            mode_path = config.private_mode if zone == "private" else config.mode
            scores: list[float] = []
            for run_idx in range(1, q_report.min_runs + 1):
                _, stub = _run_pipeline(
                    engine_factory, shelf, task, artifacts,
                    run_idx=run_idx, answer=shelf_answer(shelf, task, run_idx), zone=zone,
                    mode_path=mode_path,
                )
                scores.append(_coverage(stub.answer, task["expect_keywords"]))
            q_report.add(str(task["id"]), shelf, zone, scores, floors=floors)

    # Негативная e2e-проверка зонного гейта: PUBLIC-режим (heavy→ext) с zone=private
    # обязан упасть ДО вызова модели, ext-полка не получает ни одного вызова.
    public_task = next((t for t in tasks if t["zone"] == "public"), tasks[0])
    _, neg_stub = _run_pipeline_expect_egress_block(
        engine_factory, "ext", public_task, artifacts, zone="private", mode_path=config.mode
    )
    if neg_stub.calls:
        zone_violations.append(
            f"{public_task['id']}/private×ext: зонный гейт НЕ сработал, "
            f"ext получил {len(neg_stub.calls)} вызовов"
        )

    # ── T: zone→egress (private — только local; счётчик ext == 0) ──
    private_tasks = [t for t in tasks if t["zone"] == "private"]
    private_ext = cf.ExtStub()
    for task in private_tasks:
        cf.assert_no_ext_egress("private", "local", private_ext)
        cf.assert_no_ext_egress("private", "ext", cf.ExtStub())  # запрещённый маршрут → отказ
    public_ext = cf.ExtStub()
    cf.assert_no_ext_egress("public", "ext", public_ext)  # контроль: счётчик работает
    egress = {
        "private_tasks": [t["id"] for t in private_tasks],
        "private_ext_calls": private_ext.calls,
        "public_ext_calls": public_ext.calls,
    }

    # ── отчёт ──
    ok = (
        parity_ok
        and not pin_violations
        and not zone_violations
        and all(r.marker != cf.MARKER_DRAFT or r.shelf == "local" for r in q_report.rows)
    )
    artifact_id: str | None = None
    export_path = ""
    sections = [
        f"# Golden-run режима «{mode_name}»",
        "",
        f"- Режим: `{config.mode.name}` (атрибуция: имя режима из --mode)",
        f"- Golden-set: `{config.golden.name}` (version {golden.get('version')}), N={q_report.min_runs} на полку",
        "- Провайдеры: **детерминированные стабы полок** (валидируется контур; живой прогон — шаг прода, Ф6)",
        f"- Decoding-pin: {cf.DECODING_PIN.as_params()}",
        f"- Q-floor: {floors} (владелец {cf.FLOOR_OWNER}, decide_by {cf.FLOOR_DECIDE_BY})",
        "",
        "## I — parity промптов обеих полок (обязательна)",
        "| роль | local | ext | parity |",
        "|---|---|---|---|",
    ]
    for role, report in parity.items():
        sections.append(
            f"| {role} | `{report.hashes.get('local', '')[:12]}` | `{report.hashes.get('ext', '')[:12]}` | "
            f"{'✅ equal' if report.equal else '❌ mismatch'} |"
        )
    sections += [
        "",
        f"Пин: {'✅ все вызовы запинены' if not pin_violations else '❌ ' + '; '.join(pin_violations)}",
        "",
        "## T — zone→egress",
        (
            f"- private-задания: {egress['private_tasks']} — отказ вне local; "
            f"egress в ext при private: {egress['private_ext_calls']} (нужно 0)"
        ),
        f"- контроль публичного маршрута: ext получил {egress['public_ext_calls']} вызов (счётчик работает)",
        "",
        "## Q — механика отчёта, НЕ измерение качества (стаб-полки)",
        q_report.to_table(),
        "",
        f"Средние по полкам: { {k: round(v, 3) for k, v in q_report.by_shelf().items()} }",
        f"Вариативность > {q_report.variability_flag}: {[r.task_id for r in q_report.flagged] or '—'}",
        "",
        (
            f"**Итог гейта:** {'✅ T/I пройдены' if (parity_ok and not pin_violations and not zone_violations) else '❌ нарушение T/I'}; "
            f"Q — **НЕ измерение качества** (стаб-полки): проверена только механика "
            f"(маркеров `local-draft`: {sum(1 for r in q_report.rows if r.marker == cf.MARKER_DRAFT)}, "
            f"флаг вариативности: {[r.task_id for r in q_report.flagged] or '—'})"
        ),
        "",
        f"- Зон: private-задания идут только на local; нарушений зонного гейта: {len(zone_violations)}"
        + (f" — {zone_violations}" if zone_violations else ""),
        "",
        "## Per-node usage",
        "",
        "События `on_node_usage` (Ф6-a 6a.1) всех движков прогона, сгруппированы по узлам;",
        "порядок строк — как узлы идут в режиме. `cached` — попадания в fx-кэш (LLM не",
        "вызывался: `tokens` за такой вызов 0).",
        "",
        "| node | kind | role | model_class | shelf | calls | cached | tokens | prompt_chars | output_chars | cost | wall_s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *_per_node_rows(usage, pricing),
        "",
        "local: ₽ не определён by design, единица — GPU-слот-время (`wall_s`);",
        "`cost` (₽) — только полка `ext` по `registry/pricing` (`price_for`).",
        "",
        "## Не покрыто (чек-лист Ф6, по ревью критика)",
        "- живые провайдеры (LiteLLM local/ext): wire-параметры, gateway-трансформации промптов;",
        "- REVISE-петля критика и `max_iterations` (стаб всегда PASS), retry узлов;",
        "- валидность цитат (`StubMCP` возвращает canned `src-…`) и tool-loop 3+ на 7B;",
        "- `max_latency_s` из golden-set не читается (нет замеров wall-clock).",
    ]
    report_md = "\n".join(sections) + "\n"

    if config.out_dir is not None:
        config.out_dir.mkdir(parents=True, exist_ok=True)
        record = artifacts.save(
            report_md, user="golden-run", job_id=f"golden-run-{mode_name}",
            type="golden-report", zone="public", title=f"golden-run «{mode_name}»",
        )
        artifact_id = record.id
        export_path = str(artifacts.export("golden-run", record.id, config.out_dir))
    return GoldenRunResult(
        ok=ok, report_md=report_md, q_report=q_report, parity=parity,
        egress=egress, pin_violations=pin_violations, artifact_id=artifact_id,
        export_path=export_path,
    )


def _default_engine_factory(config: RunConfig) -> EngineFactory:
    """Фабрика движка на реальных store'ах (ws-redis) — путь ``make golden-run``."""
    from ai_workspace.artifacts import RedisBackend
    from ai_workspace.orchestrator.board import BoardStore
    from ai_workspace.orchestrator.job import JobStore
    from ai_workspace.orchestrator.ledger import RedisLedger
    from ai_workspace.redis_client import make_ws_redis

    registry = Registry(AI_WORKSPACE_DIR / "registry")

    def factory(
        shelf: str, job_id: str, artifacts: ArtifactStore, answer: str,
        zone: str, mode_path: Path,
    ) -> ModeEngine:
        graph = load_mode(mode_path)
        client = make_ws_redis()
        # Идемпотентность повторных прогонов: снести ключи этого job'а (job/board/fx/resume).
        stale = [f"ws:job:{job_id}", f"ws:board:{job_id}", f"ws:board:{job_id}:owner",
                 f"ws:fx:{job_id}", f"ws:resume:{job_id}"]
        stale += list(client.scan_iter(match=f"ws:board:{job_id}:v:*"))
        client.delete(*stale)
        jobs = JobStore(client)
        jobs.create(user="golden-run", account_level="basic", job_class="interactive",
                    mode=graph.doc["id"], zone=zone, job_id=job_id)
        return ModeEngine(
            jobs=jobs,
            boards=BoardStore(client, job_id),
            graph=graph,
            llm=StubShelfLLM(shelf, answer),
            mcp=StubMCP(),
            ledger=RedisLedger(client),
            artifacts=ArtifactStore(RedisBackend(client)),
            registry=registry,
            decoding=cf.DECODING_PIN,
        )

    return factory


def main(argv: list[str] | None = None) -> int:
    """CLI golden-run: пишет отчёт в файл; 0 — T/I пройдены, 1 — нарушение."""
    parser = argparse.ArgumentParser(
        prog="golden-run", description="Golden-run режима (T/I/Q; режим — по --mode)",
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--mode", type=Path, default=DEFAULT_MODE)
    parser.add_argument("--private-mode", type=Path, default=DEFAULT_PRIVATE_MODE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out", type=Path, default=None, help="каталог экспорта артефакта-отчёта")
    args = parser.parse_args(argv)

    config = RunConfig(golden=args.golden, mode=args.mode, private_mode=args.private_mode,
                       out_dir=args.out)
    result = run_golden(config, engine_factory=_default_engine_factory(config))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(result.report_md, encoding="utf-8")
    print(result.report_md)
    print(f"отчёт: {args.report}")
    if result.artifact_id:
        print(f"артефакт: {result.artifact_id}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
