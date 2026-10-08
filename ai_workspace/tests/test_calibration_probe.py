"""Э3 Ф7: тесты probe-runner — композиция над golden_run (БЕЗ живого LLM).

Стаб-фабрика движка — по образцу ``golden_run._default_engine_factory``/
``test_golden_run._factory`` (in-memory fakes, детерминированный вывод
``StubShelfLLM``); ответы полок считает сам харнесс (``shelf_answer``),
поэтому per-run скаляры воспроизводимы независимо от реализации probe.

A7 — все поля ProbeReport заполнены, медиана при N=3, dispersion=max−min,
held-out в СВОЁМ поле; A8 — probe импортирует golden_run/conformance и не
заводит новых runner-классов; drift-t1/blocked и private→ext — до запуска.
"""
from __future__ import annotations

import ast
import hashlib
import statistics
from pathlib import Path

import pytest
import yaml

import ai_workspace.calibration.probe as probe_mod
from ai_workspace.calibration.model_facts import ModelFacts
from ai_workspace.calibration.probe import MIN_RUNS, ProbeAborted, ProbeReport, run_probe
from ai_workspace.orchestrator.engine import ModeEngine, load_mode
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import FakeBoards, FakeJobs, MemoryLedger
from ai_workspace.tools.golden_run import StubMCP, StubShelfLLM, shelf_answer

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

GOLDEN_TASKS = [
    {"id": "p01-structure", "zone": "public",
     "prompt": "Составь структуру статьи про MCP-RAG.",
     "expect_keywords": ["структура", "разделы", "MCP"]},
    {"id": "p02-draft", "zone": "public",
     "prompt": "Напиши черновик раздела про семафор K и VRAM.",
     "expect_keywords": ["семафор", "VRAM", "K"]},
]
HELDOUT_TASKS = [
    {"id": "h01-cite", "zone": "public",
     "prompt": "Процитируй источники по human-gate.",
     "expect_keywords": ["цитата", "источник"]},
]


def _factory(calls: list[str] | None = None):
    """Фабрика движка на in-memory fakes (тот же контур, без Redis и без LLM)."""
    def factory(shelf, job_id, artifacts, answer, zone, mode_path) -> ModeEngine:
        if calls is not None:
            calls.append(job_id)
        graph = load_mode(mode_path)
        jobs, boards = FakeJobs(), FakeBoards()
        jobs.create(job_id, zone=zone)
        return ModeEngine(
            jobs=jobs,
            boards=boards,
            graph=graph,
            llm=StubShelfLLM(shelf, answer),
            mcp=StubMCP(),
            ledger=MemoryLedger(),
            artifacts=artifacts,
            registry=Registry(REGISTRY_DIR),
            decoding=__import__("ai_workspace.conformance", fromlist=["x"]).DECODING_PIN,
        )
    return factory


def _write_golden(path: Path, tasks: list[dict]) -> Path:
    """Маленький golden-YAML БЕЗ min_runs (эффективный N задаёт RunConfig=runs)."""
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _coverage(answer: str, keywords: list[str]) -> float:
    """Независимая от probe реализация ожидаемого балла (покрытие ключевых слов)."""
    low = answer.lower()
    return sum(1 for k in keywords if k.lower() in low) / len(keywords)


def _expected_scalars(tasks: list[dict], shelves: tuple[str, ...], n: int) -> list[float]:
    """Ожидаемые per-run скаляры: ответы полок — детерминизм харнесса."""
    scalars = []
    for run_idx in range(1, n + 1):
        vals = [
            _coverage(shelf_answer(shelf, task, run_idx), task["expect_keywords"])
            for task in tasks
            for shelf in shelves
        ]
        scalars.append(sum(vals) / len(vals))
    return scalars


def _profile(*, status: str = "calibrated", digest: str = "sha256:old") -> dict:
    return {
        "schema": "calibration-profile/1",
        "model_class": "fast",
        "status": status,
        "calibrated_for": {"model_id": "qwen2.5:7b", "digest": digest},
        "evidence": {"golden_manifest": "x" * 64, "pricing_manifest": "y" * 64},
    }


# ── A7: ProbeReport заполнен, медиана N=3, dispersion, held-out отдельно ──


def test_run_probe_fills_all_fields_median_and_dispersion(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    facts = ModelFacts(model_id="qwen2.5:7b", digest="sha256:abc")

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), engine_factory=_factory(),
        runs=3, model_facts=facts, clock=lambda: 1.0,
    )

    assert isinstance(report, ProbeReport)
    assert report.run_id.startswith("probe-") and len(report.run_id) == len("probe-") + 12
    assert report.model_id == "qwen2.5:7b"
    assert report.digest == "sha256:abc"
    assert report.golden_manifest == hashlib.sha256(golden.read_bytes()).hexdigest()
    assert report.pricing_manifest == hashlib.sha256(
        probe_mod.PRICING_YAML.read_bytes()
    ).hexdigest()
    assert report.parse_rate == 1.0  # M2: данных из харнесса нет → 1.0
    assert report.rub == 0.0  # M6: local-полка без прайса by design
    assert report.wall_s == 0.0  # фиксированный clock → детерминированный M7
    assert report.n_runs == 3
    assert report.q_report is not None and report.q_report.rows

    expected = _expected_scalars(GOLDEN_TASKS, ("local", "ext"), 3)
    assert report.golden_median_score == pytest.approx(statistics.median(expected))
    assert report.golden_dispersion == pytest.approx(max(expected) - min(expected))
    assert expected[0] != expected[1]  # прогоны реально различаются (не константа)

    # F6: held-out — отдельный набор → отдельное поле со СВОИМ значением
    expected_heldout = _expected_scalars(HELDOUT_TASKS, ("local", "ext"), 3)
    assert report.heldout_score == pytest.approx(statistics.median(expected_heldout))
    assert report.heldout_score != pytest.approx(report.golden_median_score)

    # M5: локальная полка теряет ключевые слова от прогона к прогону → флаг
    assert "unstable_cell" in report.flags


def test_run_probe_reports_effective_n_when_golden_yaml_overrides(tmp_path) -> None:
    golden = tmp_path / "golden-n2.yaml"
    golden.write_text(
        yaml.safe_dump(
            {"version": 1, "min_runs": 2, "tasks": GOLDEN_TASKS},
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), engine_factory=_factory(), runs=3,
    )
    # golden-YAML перекрыл min_runs=2 → эффективный N=2, честно помечен флагом
    assert report.n_runs == 2
    assert "n_runs_lt3" in report.flags


def test_run_probe_rejects_runs_below_minimum(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    with pytest.raises(ValueError, match="runs"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), engine_factory=_factory(),
            runs=MIN_RUNS - 1,
        )


# ── A8: композиция, не копипаста — импорты golden_run/conformance, без runner'ов ──


def test_probe_is_composition_over_existing_harnesses() -> None:
    source = Path(probe_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "ai_workspace.tools.golden_run" in imported
    assert "ai_workspace.conformance" in imported

    # прогон делегирован харнессу (вызов golden_run.run_golden), не скопирован
    calls = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "run_golden" in calls and "detect" in calls

    # новых харнесс-/runner-классов нет: только отчёт и исключение
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert set(classes) == {"ProbeReport", "ProbeAborted"}


# ── drift-гейт (§6.2): t1/blocked → ProbeAborted ДО сборки движков ──


def test_drift_t1_aborts_probe_before_any_run(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    engine_calls: list[str] = []

    # T1: digest полки ≠ calibrated_for профиля (модель другая)
    with pytest.raises(ProbeAborted, match="t1"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), engine_factory=_factory(engine_calls),
            drift_profile=_profile(digest="sha256:old"),
            model_facts=ModelFacts(model_id="qwen2.5:7b", digest="sha256:new"),
        )
    assert engine_calls == []  # гейт сработал ДО запуска харнесса


def test_drift_status_divergence_blocks_probe(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    engine_calls: list[str] = []

    # fail-closed (F3): profile.status=calibrated, реестр класса — uncalibrated;
    # факты совпадают (не t1) → blocked по расхождению носителей
    with pytest.raises(ProbeAborted, match="blocked"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), engine_factory=_factory(engine_calls),
            drift_profile=_profile(digest="sha256:same"),
            model_facts=ModelFacts(model_id="qwen2.5:7b", digest="sha256:same"),
        )
    assert engine_calls == []


# ── local-first (I5/P3): private → только local; ext-класс запрещён ──


def test_private_zone_ext_class_aborts_before_run(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    engine_calls: list[str] = []

    # heavy -> shelf: ext (registry/model_classes.yaml); private на ext запрещён
    with pytest.raises(ProbeAborted, match="private->ext"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="heavy",
            registry=Registry(REGISTRY_DIR), engine_factory=_factory(engine_calls),
            zone="private",
        )
    assert engine_calls == []


def test_private_zone_local_class_runs_local_shelf_only(tmp_path) -> None:
    golden = _write_golden(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_golden(tmp_path / "heldout.yaml", HELDOUT_TASKS)

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), engine_factory=_factory(), zone="private",
    )
    assert report.n_runs == 3
    # Q-строки только local: ext-полка в private-прогоне не участвовала
    assert report.q_report is not None
    assert {r.shelf for r in report.q_report.rows} == {"local"}
    expected = _expected_scalars(GOLDEN_TASKS, ("local",), 3)
    assert report.golden_median_score == pytest.approx(statistics.median(expected))
