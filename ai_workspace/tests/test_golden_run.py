"""Тесты golden-run «статья» e2e (Ф3.10): T/I/Q на реальном контуре (стаб-полки).

Прогон исполняет режим `statya` целиком (движок + 2 human-gate + артефакт) для каждой
полки и задания, проверяет обязательные слои (T zone→egress, I pin+parity) и формирует
Q-отчёт; отчёт сохраняется артефактом. Живые провайдеры — шаг прода (Ф6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_workspace import conformance as cf
from ai_workspace.orchestrator.engine import ModeEngine, load_mode
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import FakeBoards, FakeJobs, MemoryLedger
from ai_workspace.tools.golden_run import (
    DEFAULT_GOLDEN,
    RunConfig,
    StubMCP,
    StubShelfLLM,
    main,
    run_golden,
)

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


def _factory():
    """Фабрика движка на in-memory fakes (тот же контур, без Redis)."""
    state: dict[str, tuple[FakeJobs, FakeBoards]] = {}

    def factory(shelf, job_id, artifacts, answer, zone, mode_path) -> ModeEngine:
        graph = load_mode(mode_path)
        jobs, boards = FakeJobs(), FakeBoards()
        jobs.create(job_id, zone=zone)
        state[job_id] = (jobs, boards)
        return ModeEngine(
            jobs=jobs,
            boards=boards,
            graph=graph,
            llm=StubShelfLLM(shelf, answer),
            mcp=StubMCP(),
            ledger=MemoryLedger(),
            artifacts=artifacts,
            registry=Registry(REGISTRY_DIR),
            decoding=cf.DECODING_PIN,
        )

    factory.state = state  # type: ignore[attr-defined]
    return factory


def test_golden_run_passes_t_and_i_layers() -> None:
    result = run_golden(RunConfig(), engine_factory=_factory())

    assert result.ok, result.report_md
    assert all(report.equal for report in result.parity.values()), "parity промптов полок"
    assert set(result.parity) >= {"analyst", "critic", "editor"}
    assert result.pin_violations == []
    assert result.egress["private_ext_calls"] == 0  # private не уходит в ext
    assert result.egress["public_ext_calls"] == 1  # контроль: публичный маршрут дошёл


def test_golden_run_q_report_covers_every_task_and_shelf() -> None:
    result = run_golden(RunConfig(), engine_factory=_factory())
    golden = __import__("yaml").safe_load(DEFAULT_GOLDEN.read_text(encoding="utf-8"))
    public = [t for t in golden["tasks"] if t["zone"] == "public"]
    private = [t for t in golden["tasks"] if t["zone"] == "private"]
    expected = len(public) * len(cf.SHELVES) + len(private)  # private — только local

    assert len(result.q_report.rows) == expected
    assert set(result.q_report.by_shelf()) == {"local", "ext"}
    assert all(len(row.scores) >= 2 for row in result.q_report.rows)  # N>=2 (R6)
    assert all(r.shelf == "local" for r in result.q_report.rows if r.zone == "private")


def test_golden_run_public_rows_pass_floor_and_private_local_marked() -> None:
    result = run_golden(RunConfig(), engine_factory=_factory())

    public_rows = [r for r in result.q_report.rows if r.zone == "public" and r.shelf == "ext"]
    assert public_rows and all(r.passed and r.marker == cf.MARKER_FINAL for r in public_rows)

    private_local = [r for r in result.q_report.rows if r.zone == "private" and r.shelf == "local"]
    assert private_local and all(r.marker == cf.MARKER_DRAFT for r in private_local)  # ниже Q-floor


def test_golden_run_pin_is_checked_on_received_params() -> None:
    """P1 критика: пин проверяется на параметрах, ПОЛУЧЕННЫХ движком от движка-клиента."""
    seen: list[dict] = []

    class RecordingLLM(StubShelfLLM):
        def complete(self, *, role, model_class, prompt, inputs, params=None, job_id=None):
            seen.append(dict(params or {}))
            return super().complete(role=role, model_class=model_class, prompt=prompt,
                                    inputs=inputs, params=params, job_id=job_id)

    def factory(shelf, job_id, artifacts, answer, zone, mode_path) -> ModeEngine:
        jobs, boards = FakeJobs(), FakeBoards()
        jobs.create(job_id, zone=zone)
        return ModeEngine(
            jobs=jobs, boards=boards, graph=load_mode(mode_path), llm=RecordingLLM(shelf, answer),
            mcp=StubMCP(), ledger=MemoryLedger(), artifacts=artifacts,
            registry=Registry(REGISTRY_DIR), decoding=cf.DECODING_PIN,
        )

    run_golden(RunConfig(), engine_factory=factory)

    assert seen and all(cf.DECODING_PIN.as_params() == p for p in seen)


def test_golden_run_flags_local_variability() -> None:
    """R6: разброс между прогонами local зажигает флаг вариативности."""
    result = run_golden(RunConfig(), engine_factory=_factory())

    local_rows = [r for r in result.q_report.rows if r.shelf == "local"]
    assert any(r.variability > 0 for r in local_rows)
    assert [r.task_id for r in result.q_report.flagged]


def test_golden_run_detects_missing_zone_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 критика: если зонный гейт снять, golden-run обязан упасть."""
    from ai_workspace.orchestrator import engine as engine_mod

    monkeypatch.setattr(engine_mod.ModeEngine, "shelf_for", lambda self, mc: "local", raising=True)
    monkeypatch.setattr(engine_mod.ModeEngine, "_guard_zone", lambda self, rec, node, shelf: None, raising=True)

    with pytest.raises(cf.ConformanceError, match="зонный гейт"):
        run_golden(RunConfig(), engine_factory=_factory())


def test_golden_run_executes_real_mode_to_done() -> None:
    factory = _factory()
    run_golden(RunConfig(), engine_factory=factory)

    # оценочные job'ы дошли до done и сохранили секции режима;
    # негативные (golden-neg-*) — failed по зонному гейту, без единого вызова модели
    for job_id, (jobs, boards) in factory.state.items():  # type: ignore[attr-defined]
        state = jobs.get(job_id).state.value
        if job_id.startswith("golden-neg-"):
            assert state == "failed", job_id
            continue
        assert state == "done", job_id
        assert boards.sections["document"] and "document+refs" in boards.sections


def test_golden_run_report_has_all_sections_and_artifact(tmp_path: Path) -> None:
    result = run_golden(RunConfig(out_dir=tmp_path), engine_factory=_factory())

    for marker in ("# Golden-run", "## I — parity", "## T — zone→egress", "## Q — механика отчёта", "| задание |"):
        assert marker in result.report_md
    assert result.artifact_id, "отчёт должен сохраняться артефактом"
    exported = Path(result.export_path)
    assert exported.exists() and exported.read_text(encoding="utf-8") == result.report_md


def test_golden_run_report_has_per_node_usage_and_mode_attribution() -> None:
    """Ф6-a 6a.1b: секция per-node usage + имя режима в заголовке (не хардкод).

    Коллектор инжектируется обёрткой фабрики внутри ``run_golden`` — фабрика
    теста ничего о нём не знает (порт ставится на движок пост-сборки).
    """
    result = run_golden(RunConfig(), engine_factory=_factory())

    # атрибуция режима: заголовок несёт имя режима из --mode (statya по умолчанию)
    assert "# Golden-run режима «statya»" in result.report_md

    # секция per-node usage: строка узла analyst (llm-step, heavy→ext → cost в ₽)
    assert "## Per-node usage" in result.report_md
    section = result.report_md.split("## Per-node usage", 1)[1]
    analyst = next(ln for ln in section.splitlines() if ln.startswith("| analyst |"))
    assert "llm-step" in analyst and "₽" in analyst
    # critic на heavy (protected-принцип, L14): полка ext → стоимость в ₽
    critic = next(ln for ln in section.splitlines() if ln.startswith("| critic |"))
    assert "₽" in critic and "| — |" not in critic
    # local-полка без LLM (citer: tool-step): ₽ не определён by design → «—»
    citer = next(ln for ln in section.splitlines() if ln.startswith("| citer |"))
    assert "| — |" in citer
    # порядок строк — как узлы идут в режиме; human-gate не измеряется
    rows = [ln for ln in section.splitlines()
            if ln.startswith("| ") and not ln.startswith("| node")]
    assert [r.split("|")[1].strip() for r in rows] == ["analyst", "critic", "editor", "citer"]
    # комментарий о GPU-слот-времени для local-полки присутствует
    assert "GPU-слот-время" in section


def test_golden_run_detects_broken_parity() -> None:
    """Мутация: стаб добавляет model-specific строку в промпт → parity падает."""
    class BranchingLLM(StubShelfLLM):
        def complete(self, *, role, model_class, prompt, inputs, params=None, job_id=None):
            if self.shelf == "local":
                prompt = prompt + "\nОтвечай кратко (7B)."
            return super().complete(role=role, model_class=model_class, prompt=prompt,
                                    inputs=inputs, params=params, job_id=job_id)

    def factory(shelf, job_id, artifacts, answer, zone, mode_path) -> ModeEngine:
        jobs, boards = FakeJobs(), FakeBoards()
        jobs.create(job_id, zone=zone)
        return ModeEngine(
            jobs=jobs, boards=boards, graph=load_mode(mode_path), llm=BranchingLLM(shelf, answer),
            mcp=StubMCP(), ledger=MemoryLedger(), artifacts=artifacts,
            registry=Registry(REGISTRY_DIR), decoding=cf.DECODING_PIN,
        )

    result = run_golden(RunConfig(), engine_factory=factory)

    assert not all(report.equal for report in result.parity.values())
    assert result.ok is False


@pytest.mark.integration
def test_cli_writes_report_file_and_exports_artifact(tmp_path: Path) -> None:
    """CLI-путь на ws-redis: пишет отчёт-файл и экспортирует артефакт (Ф3.7)."""
    from ai_workspace.tests.conftest import REDIS_REACHABLE

    if not REDIS_REACHABLE:
        pytest.skip("ws-redis недоступен (unit-only запуск)")
    report = tmp_path / "golden-report.md"
    out = tmp_path / "artifact"

    rc = main(["--report", str(report), "--out", str(out)])

    assert rc == 0
    text = report.read_text(encoding="utf-8")
    assert "# Golden-run" in text and "## Q — механика отчёта" in text
    assert list(out.glob("*.md")), "артефакт-отчёт должен быть экспортирован на диск"
