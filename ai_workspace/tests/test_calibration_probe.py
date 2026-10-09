"""Э3-4a Ф7 (fix §10 LIVE-PROBE-1): probe — живой измеритель ``vp_ab_pilot.run_one``.

Живого LLM нет: ``probe_mod.run_one`` подменяется fake'ом, возвращающим
``RunOutcome`` с нужными скалярами (score/verdict_parse_ok/tokens/job_wall_s/
status). A7 — все поля ProbeReport: медиана по ВСЕМ (задание, прогон)
golden-скoram, dispersion=max−min, held-out в СВОЁМ поле (отдельный набор),
parse_rate — доля True, error-прогон не валит замер (скор 0), ₽ по
``PricingRegistry`` полки класса, ``n_runs_lt3`` при runs=2; A8 — probe
импортирует ``vp_ab_pilot``/``conformance`` и НЕ импортирует ``golden_run``,
новых runner-классов нет; drift-t1/blocked и private→ext — ДО первого прогона.
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
from ai_workspace.calibration.probe import ProbeAborted, ProbeReport, run_probe
from ai_workspace.registry import Registry
from ai_workspace.registry.pricing import MICRO_PER_UNIT, PricingRegistry
from ai_workspace.tools.vp_ab_pilot import RunOutcome

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

GOLDEN_TASKS = [
    {"id": "g01-structure", "zone": "public",
     "prompt": "Составь структуру статьи про MCP-RAG.",
     "expect_keywords": ["структура", "разделы", "MCP"]},
    {"id": "g02-draft", "zone": "public",
     "prompt": "Напиши черновик раздела про семафор K и VRAM.",
     "expect_keywords": ["семафор", "VRAM", "K"]},
]
HELDOUT_TASKS = [
    {"id": "h01-cite", "zone": "public",
     "prompt": "Процитируй источники по human-gate.",
     "expect_keywords": ["цитата", "источник"]},
]


def _write_set(path: Path, tasks: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _outcome(
    task_id: str, run_idx: int, *, score: float = 0.5, parse_ok: bool = True,
    tokens: int = 0, wall_s: float = 0.1, status: str = "done",
) -> RunOutcome:
    """RunOutcome измерителя с нужными скалярами (форма — из vp_ab_pilot :303)."""
    return RunOutcome(
        variant="base", task_id=task_id, mode="statya", run=run_idx,
        status=status, verdict_parse_ok=parse_ok, score=score,
        tokens=tokens, job_wall_s=wall_s,
    )


def _fake_run_one(
    script: dict | None = None, calls: list | None = None,
):
    """``probe_mod.run_one`` подмена: (task_id, run_idx) → параметры прогона."""
    def fake(mode_path, task, variant, run_idx, llm, *,
             base_registry=None, seed_reader=None) -> RunOutcome:
        if calls is not None:
            calls.append(
                {"task": task["id"], "variant": variant, "run": run_idx,
                 "llm": llm, "registry": base_registry,
                 "mode": Path(mode_path).name}
            )
        spec = (script or {}).get((task["id"], run_idx), {})
        return _outcome(task["id"], run_idx, **spec)
    return fake


def _profile(*, status: str = "calibrated", digest: str = "sha256:old") -> dict:
    return {
        "schema": "calibration-profile/1",
        "model_class": "fast",
        "status": status,
        "calibrated_for": {"model_id": "qwen2.5:7b", "digest": digest},
        "evidence": {"golden_manifest": "x" * 64, "pricing_manifest": "y" * 64},
    }


# ── A7: ProbeReport заполнен, медиана N=3, dispersion, held-out отдельно ──


def test_run_probe_fills_all_fields_median_dispersion_heldout(
    tmp_path, monkeypatch,
) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    facts = ModelFacts(model_id="qwen2.5:7b", digest="sha256:abc")
    llm = object()  # sentinel: измеритель обязан получить ЭТОТ клиент
    calls: list[dict] = []
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(
        {
            ("g01-structure", 1): {"score": 0.2},
            ("g01-structure", 2): {"score": 0.5, "parse_ok": False},
            ("g01-structure", 3): {"score": 0.8},
            ("g02-draft", 1): {"score": 0.4},
            ("g02-draft", 2): {"score": 0.5},
            ("g02-draft", 3): {"score": 0.6},
            ("h01-cite", 1): {"score": 0.9},
            ("h01-cite", 2): {"score": 0.9},
            ("h01-cite", 3): {"score": 0.9},
        },
        calls,
    ))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), llm=llm, runs=3,
        model_facts=facts, clock=lambda: 1.0,
    )

    assert isinstance(report, ProbeReport)
    assert report.run_id.startswith("probe-") and len(report.run_id) == len("probe-") + 12
    assert report.model_id == "qwen2.5:7b"
    assert report.digest == "sha256:abc"
    assert report.golden_manifest == hashlib.sha256(golden.read_bytes()).hexdigest()
    assert report.pricing_manifest == hashlib.sha256(
        probe_mod.PRICING_YAML.read_bytes()
    ).hexdigest()
    assert report.n_runs == 3

    # M1/M5: медиана/разброс по ВСЕМ (задание, прогон) скораm golden
    expected = [0.2, 0.5, 0.8, 0.4, 0.5, 0.6]
    assert report.golden_median_score == pytest.approx(statistics.median(expected))
    assert report.golden_dispersion == pytest.approx(max(expected) - min(expected))
    assert report.golden_dispersion > 0.15
    assert "unstable_cell" in report.flags

    # M2: доля распарсенных вердиктов по прогонам (один False из шести)
    assert report.parse_rate == pytest.approx(5 / 6)
    assert "parse_fail" in report.flags

    # F6: held-out — отдельный набор → отдельное поле со СВОИМ значением
    assert report.heldout_score == pytest.approx(0.9)
    assert report.heldout_score != pytest.approx(report.golden_median_score)

    # M7: сумма job_wall_s прогонов golden (6 × 0.1)
    assert report.wall_s == pytest.approx(0.6)

    # M6: fast → local-полка → 0 ₽
    assert report.rub == 0.0

    # Q-агрегация (conformance): по строке на задачу golden, полка класса
    assert report.q_report is not None
    assert [r.task_id for r in report.q_report.rows] == ["g01-structure", "g02-draft"]
    assert {r.shelf for r in report.q_report.rows} == {"local"}  # fast → local
    assert report.q_report.rows[0].scores == (0.2, 0.5, 0.8)

    # измерение пошло через run_one с контрактом живого измерителя
    assert len(calls) == 9  # golden 2×3 + heldout 1×3
    for c in calls:
        assert c["variant"] == "base"
        assert c["llm"] is llm
        assert isinstance(c["registry"], Registry)
        assert c["mode"] == "statya.yaml"


def test_error_outcome_scores_zero_and_does_not_abort(tmp_path, monkeypatch) -> None:
    """status=error → скор прогона 0 (даже если score>0), замер не падает."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(
        {
            ("g01-structure", 1): {"score": 0.8},
            ("g01-structure", 2): {"score": 0.9, "status": "error", "tokens": 0},
            ("g01-structure", 3): {"score": 0.8},
            ("g02-draft", 1): {"score": 0.8},
            ("g02-draft", 2): {"score": 0.8},
            ("g02-draft", 3): {"score": 0.8},
            ("h01-cite", 1): {"score": 0.8},
            ("h01-cite", 2): {"score": 0.8},
            ("h01-cite", 3): {"score": 0.8},
        },
    ))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), llm=object(), runs=3,
    )

    # error-прогон честно дал 0: медиана [0.8, 0.0, 0.8, 0.8, 0.8, 0.8] = 0.8
    assert report.golden_median_score == pytest.approx(0.8)
    assert report.golden_dispersion == pytest.approx(0.8)
    assert "n_runs_lt3" not in report.flags


def test_runs_below_three_allowed_with_flag(tmp_path, monkeypatch) -> None:
    """runs=2 — контурный прогон: НЕ ValueError, а флаг n_runs_lt3."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one())

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), llm=object(), runs=2,
    )
    assert report.n_runs == 2
    assert "n_runs_lt3" in report.flags
    # Q-отчёт при N=2 строится (min_runs=runs), по строке на задачу
    assert report.q_report is not None and len(report.q_report.rows) == 2


def test_runs_below_one_rejected(tmp_path, monkeypatch) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one())
    with pytest.raises(ValueError, match="runs"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), llm=object(), runs=0,
        )


def test_heavy_class_rub_via_pricing_registry(tmp_path, monkeypatch) -> None:
    """heavy → ext: ₽ по прайсу полки × токены golden (все — входные)."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    # 1M токенов на прогон: 6 golden-прогонов → 6M; heldout (3M) в ₽ НЕ входит
    script = {
        (t["id"], r): {"score": 0.9, "tokens": 1_000_000}
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(script))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="heavy",
        registry=Registry(REGISTRY_DIR), llm=object(), runs=3,
    )

    price = PricingRegistry(Registry(REGISTRY_DIR)).price_for("ext")
    # независимо от cost_micro: 6M токенов по входной цене → ₽
    expected_rub = 6 * price.input_per_1m_micro / MICRO_PER_UNIT
    assert report.rub == pytest.approx(expected_rub)
    # Q-строки помечены полкой ext (полка класса heavy)
    assert report.q_report is not None
    assert {r.shelf for r in report.q_report.rows} == {"ext"}


# ── A8: измерение через vp_ab_pilot.run_one, golden_run не импортируется ──


def test_probe_measures_via_vp_ab_pilot_not_golden_run() -> None:
    source = Path(probe_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert "ai_workspace.tools.vp_ab_pilot" in imported
    assert "ai_workspace.conformance" in imported
    assert "ai_workspace.tools.golden_run" not in imported

    # измерение делегировано живому измерителю (+ drift-предчек)
    calls = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "run_one" in calls and "detect" in calls
    assert "run_golden" not in calls

    # новых харнесс-/runner-классов нет: только отчёт и исключение
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert set(classes) == {"ProbeReport", "ProbeAborted"}


# ── drift-гейт (§6.2): t1/blocked → ProbeAborted ДО первого прогона ──


def test_drift_t1_aborts_probe_before_any_run(tmp_path, monkeypatch) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    calls: list[dict] = []
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(None, calls))

    # T1: digest полки ≠ calibrated_for профиля (модель другая)
    with pytest.raises(ProbeAborted, match="t1"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), llm=object(),
            drift_profile=_profile(digest="sha256:old"),
            model_facts=ModelFacts(model_id="qwen2.5:7b", digest="sha256:new"),
        )
    assert calls == []  # гейт сработал ДО первого прогона измерителя


def test_drift_status_divergence_blocks_probe(tmp_path, monkeypatch) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    calls: list[dict] = []
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(None, calls))

    # fail-closed (F3): profile.status=calibrated, реестр класса — uncalibrated;
    # факты совпадают (не t1) → blocked по расхождению носителей
    with pytest.raises(ProbeAborted, match="blocked"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="fast",
            registry=Registry(REGISTRY_DIR), llm=object(),
            drift_profile=_profile(digest="sha256:same"),
            model_facts=ModelFacts(model_id="qwen2.5:7b", digest="sha256:same"),
        )
    assert calls == []


# ── local-first (I5/P3): private → только local; ext-класс запрещён ──


def test_private_zone_ext_class_aborts_before_run(tmp_path, monkeypatch) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    calls: list[dict] = []
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(None, calls))

    # heavy -> shelf: ext (registry/model_classes.yaml); private на ext запрещён
    with pytest.raises(ProbeAborted, match="private->ext"):
        run_probe(
            mode=MODE, golden=golden, heldout=heldout, model_class="heavy",
            registry=Registry(REGISTRY_DIR), llm=object(), zone="private",
        )
    assert calls == []


def test_private_zone_local_class_measures_local_shelf(tmp_path, monkeypatch) -> None:
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(
        {(t["id"], r): {"score": 0.8} for t in GOLDEN_TASKS for r in (1, 2, 3)}
    ))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), llm=object(), zone="private",
    )
    assert report.n_runs == 3
    # Q-строки только local: ext-полка в private-прогоне не участвовала
    assert report.q_report is not None
    assert {r.shelf for r in report.q_report.rows} == {"local"}
    assert report.golden_median_score == pytest.approx(0.8)
    assert "unstable_cell" not in report.flags  # разброс 0 ≤ 0.15
