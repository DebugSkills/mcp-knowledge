"""В2-B 2a/2e (arch-2026-10-08-f7-calibration): needle-различимость M4 + гейты.

2a: needle-набор — ОТДЕЛЬНЫЙ файл (``--needle``), golden_manifest и
golden-метрики M1/M5/M6/M7 не трогаются; детект — grep ``expect_needle`` в
выводе измерителя (``RunOutcome.document``/``draft``, без LLM-оценщика);
``needle_rate`` — доля найденных (задание × прогон), ``None`` без набора.
2e (F-3i): ``propose_scalars`` — ``needle_rate < policy.NEEDLE_RATE_FLOOR`` →
violations (None → гейт молчит, обратная совместимость);
``evaluate_promotion`` — promoted требует ``needle_rate >= порога`` у
варианта. α (2026-10-09, оператор P5, 2f негатив-3): ceiling-флаг больше
НЕ авто-reject — структурный скор насыщен по построению (свойство меры);
при ceiling (у любого плеча) решение ПО NEEDLE: база недостаточна ⇔
needle базы < пола (α-достройка 3c: структурный пол golden насыщен
всегда), вариант ≥ пола И > базы на ≥ 1 квант 1/(tasks×runs); needle
отсутствует (у любой руки) → не promoted («нечем решать»). Профиль:
``needle_rate`` —
additive ключ ``evidence.metrics`` (Г6: presence-валидация не режет).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import ai_workspace.calibration.probe as probe_mod
from ai_workspace.calibration import policy, profiles
from ai_workspace.calibration.probe import ProbeReport, run_probe
from ai_workspace.registry import Registry
from ai_workspace.tools import probe_run
from ai_workspace.tools.vp_ab_pilot import RunOutcome

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
NEEDLE_SET_YAML = Path(__file__).parent / "golden" / "needle-set.yaml"

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
NEEDLE_TASKS = [
    {"id": "n01-budget", "zone": "public",
     "prompt": "Длинный вход с разделами; код заявки TG-7741-DELTA в середине.",
     "expect_needle": "TG-7741-DELTA"},
    {"id": "n02-metric", "zone": "public",
     "prompt": "Длинный вход; коэффициент деградации 0.4173 в третьей секции.",
     "expect_needle": "0.4173"},
]


def _write_set(path: Path, tasks: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _outcome(
    task_id: str, run_idx: int, *, score: float = 0.8, parse_ok: bool = True,
    tokens: int = 0, wall_s: float = 0.1, status: str = "done",
    document: str = "", draft: str = "",
) -> RunOutcome:
    """RunOutcome измерителя (форма — vp_ab_pilot :303); document/draft — вывод."""
    return RunOutcome(
        variant="base", task_id=task_id, mode="statya", run=run_idx,
        status=status, verdict_parse_ok=parse_ok, score=score,
        tokens=tokens, job_wall_s=wall_s, document=document, draft=draft,
    )


def _fake_run_one(script: dict | None = None, calls: list | None = None):
    """``probe_mod.run_one`` подмена: (task_id, run_idx) → параметры прогона."""
    def fake(mode_path, task, variant, run_idx, llm, *,
              base_registry=None, seed_reader=None) -> RunOutcome:
        if calls is not None:
            calls.append({"task": task["id"], "variant": variant, "run": run_idx})
        spec = (script or {}).get((task["id"], run_idx), {})
        return _outcome(task["id"], run_idx, **spec)
    return fake


def _report(**over) -> ProbeReport:
    """ProbeReport выше пола и без капов (по умолчанию проходит рамку D6)."""
    base: dict = {
        "run_id": "probe-abc123def456",
        "model_id": "qwen2.5:7b",
        "digest": "sha256:abc",
        "golden_manifest": "g" * 64,
        "pricing_manifest": "p" * 64,
        "golden_median_score": 0.9,
        "golden_dispersion": 0.05,
        "heldout_score": 0.88,
        "parse_rate": 1.0,
        "rub": 1.0,
        "wall_s": 10.0,
        "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


# ── 2a: needle_rate считается (found/missed), golden не трогается ─────────


def test_needle_rate_found_missed_fraction(tmp_path, monkeypatch) -> None:
    """Доля найденных needle: 3 из 4 (задание × прогон), включая found только
    в draft и missed при пустом выводе; golden-метрики от needle не зависят."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    needle = _write_set(tmp_path / "needle.yaml", NEEDLE_TASKS)
    calls: list[dict] = []
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(
        {
            ("g01-structure", 1): {"score": 0.8}, ("g01-structure", 2): {"score": 0.8},
            ("g02-draft", 1): {"score": 0.8}, ("g02-draft", 2): {"score": 0.8},
            ("h01-cite", 1): {"score": 0.8}, ("h01-cite", 2): {"score": 0.8},
            # needle: found в document / missed (вывод пуст) / found / found в draft
            ("n01-budget", 1): {"document": "итог: заявка TG-7741-DELTA одобрена"},
            ("n01-budget", 2): {"document": "", "draft": ""},
            ("n02-metric", 1): {"document": "коэффициент 0.4173 — норма"},
            ("n02-metric", 2): {"draft": "в приложении: 0.4173"},
        },
        calls,
    ))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, needle=needle,
        model_class="fast", registry=Registry(REGISTRY_DIR), llm=object(),
        runs=2,
    )

    assert report.needle_rate == pytest.approx(3 / 4)
    # golden не тронут: медиана только по golden-скорам, прогонов 2×2+1×2+2×2
    assert report.golden_median_score == pytest.approx(0.8)
    assert report.wall_s == pytest.approx(0.4)  # 4 golden-прогона × 0.1
    assert [c["task"] for c in calls].count("n01-budget") == 2
    assert len(calls) == 10


def test_needle_absent_rate_none_gate_silent(tmp_path, monkeypatch) -> None:
    """Без --needle: needle_rate=None, предложение скаляров работает как раньше."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one())

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, model_class="fast",
        registry=Registry(REGISTRY_DIR), llm=object(), runs=2,
    )
    assert report.needle_rate is None
    # обратная совместимость гейта: None не роняет рамку D6
    result = policy.propose_scalars(report, quality_floor=0.8)
    assert result["applied"] is True


def test_needle_error_run_counts_missed(tmp_path, monkeypatch) -> None:
    """error-прогон needle-задания — честный missed (fail-closed, скор 0)."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    needle = _write_set(tmp_path / "needle.yaml", [NEEDLE_TASKS[0]])
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(
        {
            ("n01-budget", 1): {"status": "error", "document": "TG-7741-DELTA"},
            ("n01-budget", 2): {"document": "заявка TG-7741-DELTA"},
        },
    ))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, needle=needle,
        model_class="fast", registry=Registry(REGISTRY_DIR), llm=object(),
        runs=2,
    )
    # error-статус: вывод не принимается, even though document non-empty
    assert report.needle_rate == pytest.approx(1 / 2)


# ── 2a: committed needle-set.yaml — валидный черновик на рецензию ─────────


def test_needle_set_yaml_draft_contract() -> None:
    doc = yaml.safe_load(NEEDLE_SET_YAML.read_text(encoding="utf-8"))
    assert doc["owner"] == "operator"
    tasks = doc["tasks"]
    assert isinstance(tasks, list) and len(tasks) >= 3
    needles = [str(t["expect_needle"]).strip() for t in tasks]
    assert all(needles) and len(set(needles)) == len(needles)
    for task, needle in zip(tasks, needles):
        prompt = str(task["prompt"])
        # длинный многосекционный вход: needle спрятан в толще текста
        assert len(prompt) >= 400, task["id"]
        assert prompt.count("##") >= 4, task["id"]
        assert needle in prompt, task["id"]


# ── 2e: propose_scalars — needle_rate в violations рамки D6 ───────────────


def test_propose_violation_low_needle_rate() -> None:
    result = policy.propose_scalars(_report(needle_rate=0.25), quality_floor=0.8)
    assert result["applied"] is False
    assert result["scalars"] == {}
    assert "needle_rate=0.2500" in result["reason"]
    assert "NEEDLE_RATE_FLOOR" in result["reason"]


def test_propose_applied_when_needle_rate_meets_floor() -> None:
    result = policy.propose_scalars(
        _report(needle_rate=policy.NEEDLE_RATE_FLOOR), quality_floor=0.8
    )
    assert result["applied"] is True
    assert "needle_rate" in result["reason"]


def test_propose_needle_rate_documented_threshold() -> None:
    """Порог — документированная константа (draft, уточнит 2f-контроль CC1)."""
    assert isinstance(policy.NEEDLE_RATE_FLOOR, float)
    assert 0.0 < policy.NEEDLE_RATE_FLOOR <= 1.0


# ── 2e: evaluate_promotion — promoted требует needle; ceiling не applied ──


def _vreport(golden: float = 0.9, heldout: float = 0.88, **over) -> ProbeReport:
    return _report(
        golden_median_score=golden, golden_dispersion=0.05,
        heldout_score=heldout, **over,
    )


def test_promotion_variant_low_needle_not_promoted() -> None:
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7), _vreport(needle_rate=0.2),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "needle_rate" in res["reasons"][0]


def test_promotion_variant_needle_ok_passes() -> None:
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7), _vreport(needle_rate=policy.NEEDLE_RATE_FLOOR),
        quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_variant_ceiling_not_promoted() -> None:
    """α (осознанное обновление 2026-10-09): прежде ceiling ⇒ авто-reject
    («замер не различает конфигурации»); по решению оператора P5 (2f
    негатив-3: структурный скор насыщен ПО ПОСТРОЕНИЮ — свойство меры, не
    контента) авто-reject снят: при ceiling решение ПО NEEDLE. Needle нет
    (needle_rate=None) → не promoted — структурно нечем решать."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7), _vreport(flags=("ceiling",)),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert any("ceiling" in r for r in res["reasons"])
    assert any("решение по needle" in r for r in res["reasons"])


def test_promotion_ceiling_needle_decides_promoted() -> None:
    """α: ceiling у обеих рук + needle варианта ≥ пола и > базы на ≥ 1
    кванта 1/(tasks×runs) → promoted (живой кейс 2f v3: full 10/18 vs
    compressed 0/18 — маржа 10 квантов)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.0),
        _vreport(flags=("ceiling",), needle_rate=10 / 18),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_ceiling_base_flag_needle_absent_not_promoted() -> None:
    """α: ceiling только у БАЗЫ + needle ВАРИАНТА не прогонялся → не
    promoted: гейт решает по needle варианта, его нет — нечем решать
    (needle базы 0.9 внизу — не спасает: смотрит вариант)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.9),
        _vreport(),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert any("нечем решать" in r for r in res["reasons"])


def test_promotion_ceiling_base_flag_variant_needle_ok_promoted() -> None:
    """α: ceiling только у базы (вариант различим структурно) — α-путь всё
    равно включён: база недостаточна по needle (0.3 < пола), вариант ≥
    пола (маржа строго > 0 — квант не задан)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.3),
        _vreport(needle_rate=policy.NEEDLE_RATE_FLOOR + 0.1),
        quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_ceiling_needle_below_floor_not_promoted() -> None:
    """α: ceiling + needle варианта < policy.NEEDLE_RATE_FLOOR → не promoted;
    причина называет насыщение структурного скора."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.0),
        _vreport(flags=("ceiling",), needle_rate=policy.NEEDLE_RATE_FLOOR - 0.05),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert any(
        "структурный скор насыщен" in r and "NEEDLE_RATE_FLOOR" in r
        for r in res["reasons"]
    )


def test_promotion_ceiling_needle_margin_required() -> None:
    """α: needle варианта ≥ пола, но НЕ лучше базы (равенство) → не
    promoted: без превосходства по retention различие рук не доказано
    (негатив-2 CC1: 0.833/0.833)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.6),
        _vreport(flags=("ceiling",), needle_rate=0.6),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res["passed"] is False
    assert any("не лучше базы" in r for r in res["reasons"])


def test_promotion_ceiling_needle_margin_below_quantum_not_promoted() -> None:
    """α: маржа строго положительна, но < 1 кванта 1/(tasks×runs) → не
    promoted: различие объяснимо единичным срабатыванием (граничный
    негатив-1 CC1: Δ=1/9 ровно один квант)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.5),
        _vreport(flags=("ceiling",), needle_rate=0.51),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res["passed"] is False
    assert any("маржа" in r and "квант" in r for r in res["reasons"])


def test_promotion_ceiling_needle_strict_margin_without_quantum() -> None:
    """α: needle_quantum не задан → маржа — строгое превосходство
    (0.51 > 0.4 базы проходит; база 0.4 < пола — недостаточна); паритет
    для вызовов без кванта."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",), needle_rate=0.4),
        _vreport(flags=("ceiling",), needle_rate=0.51),
        quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_ceiling_base_needle_unknown_not_promoted() -> None:
    """α-достройка: ceiling + needle базы неизвестен (база без --needle) →
    не promoted: adequacy базы по needle нечем решать (структурный скор
    насыщен, golden не различает)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, flags=("ceiling",)),
        _vreport(flags=("ceiling",), needle_rate=policy.NEEDLE_RATE_FLOOR),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res["passed"] is False
    assert any(
        "ceiling" in r and "needle_rate базы неизвестен" in r
        for r in res["reasons"]
    )


# ── α-достройка (2026-10-09, живой 3c): при ceiling база недостаточна ПО
# NEEDLE — структурный пол golden насыщен всегда, прежняя ветка «база
# проваливает пол» по golden делала promotion недостижимым ───────────────


def test_promotion_ceiling_base_by_needle_promoted_live_3c() -> None:
    """α-достройка (живой 3c: base statya.local golden=1.0 needle=0.00 vs
    variant statya.full.local needle=0.56): ceiling у обеих рук ⇒ adequacy
    базы по needle — 0.0 < пола, вариант 10/18 ≥ пола, маржа 10 квантов →
    promoted (прежде блокировалось «база не проваливает quality_floor:
    golden=1.0000 >= 0.8000»)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(1.0, 1.0, flags=("ceiling",), needle_rate=0.0),
        _vreport(1.0, 1.0, flags=("ceiling",), needle_rate=10 / 18),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_ceiling_base_needle_above_floor_not_promoted() -> None:
    """α-достройка: ceiling + база достаточна по needle (0.56 ≥ пола 0.5) →
    не promoted с ЕДИНСТВЕННОЙ причиной «база достаточна по needle» —
    вариант имеет смысл только у недостаточной базы."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(1.0, 1.0, flags=("ceiling",), needle_rate=0.56),
        _vreport(1.0, 1.0, flags=("ceiling",), needle_rate=0.9),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "база достаточна по needle" in res["reasons"][0]


def test_promotion_no_ceiling_parity_old_needle_gate() -> None:
    """Паритет F1: без ceiling — прежняя логика 2e (гейт только по needle
    варианта; needle базы не участвует, авто-reject по ceiling нет)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, needle_rate=0.0),
        _vreport(needle_rate=1.0),
        quality_floor=0.8, needle_quantum=1 / 18,
    )
    assert res == {"passed": True, "reasons": []}


# ── 2e/α: propose_scalars — ceiling НЕ блокирует D6, needle < пола — да ────


def test_propose_ceiling_does_not_block() -> None:
    """α: флаг ceiling замера (структурный скор насыщен по построению)
    НЕ блокирует предложение скаляров Сам По Себе — различимость даёт
    needle; содержательные гейты — needle/пол/капы."""
    result = policy.propose_scalars(
        _report(flags=("ceiling",), needle_rate=policy.NEEDLE_RATE_FLOOR),
        quality_floor=0.8,
    )
    assert result["applied"] is True
    assert result["scalars"] == policy.DEFAULT_PROPOSED_SCALARS


def test_propose_ceiling_with_low_needle_blocks_on_needle() -> None:
    """α: ceiling + needle ниже пола → NOT applied именно ПО NEEDLE
    (нарушение рамки D6), не по ceiling."""
    result = policy.propose_scalars(
        _report(flags=("ceiling",), needle_rate=0.25), quality_floor=0.8,
    )
    assert result["applied"] is False
    assert "needle_rate=0.2500" in result["reason"]
    assert "ceiling" not in result["reason"]


def test_promotion_base_needle_irrelevant() -> None:
    """Гейт — по needle ВАРИАНТА; needle базы не участвует (None/низкий ок)."""
    from ai_workspace.calibration.variants import evaluate_promotion
    res = evaluate_promotion(
        _vreport(0.7, 0.7, needle_rate=0.0),
        _vreport(needle_rate=1.0), quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


# ── 2a/Г6: needle_rate — additive-ключ evidence.metrics профиля ───────────


def test_profile_metrics_needle_rate_additive() -> None:
    doc = profiles.build_draft_profile(
        _report(needle_rate=0.75), model_class="fast",
        scalars=dict(policy.DEFAULT_PROPOSED_SCALARS), quality_floor=0.8,
    )
    assert doc["evidence"]["metrics"]["needle_rate"] == pytest.approx(0.75)
    assert profiles.validate_profile(doc) == []  # Г6: extra-ключ не режет


def test_profile_metrics_needle_absent_parity() -> None:
    doc = profiles.build_draft_profile(
        _report(), model_class="fast",
        scalars=dict(policy.DEFAULT_PROPOSED_SCALARS), quality_floor=0.8,
    )
    assert "needle_rate" not in doc["evidence"]["metrics"]
    assert profiles.validate_profile(doc) == []


# ── CLI: --needle доезжает до run_probe, метрика публикуется ──────────────


def test_cli_needle_flag_passed_and_reported(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    needle = _write_set(tmp_path / "needle.yaml", NEEDLE_TASKS)
    seen: dict[str, object] = {}

    def fake_run_probe(**kwargs) -> ProbeReport:
        seen.update(kwargs)
        return _report(needle_rate=0.75)

    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast", "--confirm-live",
         "--needle", str(needle), "--profiles-dir", str(tmp_path / "profiles")],
        llm=object(),
    )
    assert code == 0
    assert seen["needle"] == needle
    out = capsys.readouterr().out
    assert "needle_rate" in out and "0.75" in out
    written = list((tmp_path / "profiles").glob("*.yaml"))
    assert len(written) == 1
    doc = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert doc["evidence"]["metrics"]["needle_rate"] == pytest.approx(0.75)


def test_cli_dry_run_plan_mentions_needle(tmp_path, capsys) -> None:
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    needle = _write_set(tmp_path / "needle.yaml", NEEDLE_TASKS)
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast", "--needle", str(needle)],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "needle" in out


# ── 2a v3: needle-set.yaml v3 (инверсия CC1, подготовка повтора 2f) ────────


def _material_sections(prompt: str) -> list[str]:
    """Куски промпта по '## '-заголовкам (заголовок — часть куска)."""
    return [c for c in re.split(r"(?m)^(?=## )", prompt) if c.startswith("## ")]


def _fast_section_budget() -> int:
    """Бюджет сжатия секции класса fast — из реестра (SSOT, не константа)."""
    classes = Registry(REGISTRY_DIR).get("model_classes") or {}
    return int(classes["fast"]["max_chars_per_section"])


def test_needle_set_yaml_v3_contract() -> None:
    """v3 (корень негатива-2 CC1): 6 заданий; в каждом — секция материала
    ДОЛЬШЕ бюджета сжатия 4000 (иначе shaping compressed — no-op: секция в
    бюджете идёт байт-в-байт, engine.shape_section, и руки CC1 идентичны —
    0.833/0.833). Needle — ровно один раз, в средней трети [0.3, 0.7]
    длинной секции; блок «## Задание» несёт ЯВНУЮ инструкцию дословного
    переноса и НЕ содержит литерал needle (иначе recency-копирование из
    задания сравняет руки)."""
    doc = yaml.safe_load(NEEDLE_SET_YAML.read_text(encoding="utf-8"))
    assert doc["version"] == 3
    assert doc["status"] == "draft"
    assert doc["owner"] == "operator"
    assert doc["min_runs"] == 3
    tasks = doc["tasks"]
    assert len(tasks) == 6
    assert [t["id"][:3] for t in tasks] == [f"n0{k}" for k in range(1, 7)]
    needles = [str(t["expect_needle"]).strip() for t in tasks]
    assert all(needles) and len(set(needles)) == 6
    for task, needle in zip(tasks, needles):
        prompt = str(task["prompt"])
        # весь вход — длинный: сжатие промпта-секции точно не no-op
        assert len(prompt) >= 7000, task["id"]
        sections = _material_sections(prompt)
        assert len(sections) >= 4, task["id"]  # материал + «## Задание»
        material = [s for s in sections if not s.startswith("## Задание")]
        long_sec = max(material, key=len)
        assert len(long_sec) > 4000, task["id"]
        # needle спрятан в толще длинной секции: один раз, средняя треть
        assert prompt.count(needle) == 1, task["id"]
        frac = long_sec.index(needle) / len(long_sec)
        assert 0.3 <= frac <= 0.7, (task["id"], frac)
        # задание: явная инструкция дословного переноса, без литерала
        instr = prompt.split("## Задание", 1)[1]
        assert "дословно" in instr, task["id"]
        assert "раздел" in instr, task["id"]
        assert needle not in instr, task["id"]


def test_needle_set_yaml_v3_compressed_actually_cuts_needle() -> None:
    """Механика инверсии CC1: промпт задания едет в движок ОДНОЙ секцией-
    входом brief (vp_ab_pilot.run_one: engine.seed({"brief": prompt})), и
    compressed-рука класса fast режет его по схеме «голова+хвост»
    (head_share 0.8: head 3200 / tail 800 при бюджете 4000). Проверка на
    самом engine.shape_section: needle ЦЕЛИКОМ в зоне среза — full-рука
    сохраняет факт, compressed-рука теряет (различие рук возможно)."""
    from ai_workspace.orchestrator.engine import SHAPING_HEAD_SHARE, shape_section

    budget = _fast_section_budget()
    head = max(1, int(budget * SHAPING_HEAD_SHARE))
    tail = max(1, budget - head)
    doc = yaml.safe_load(NEEDLE_SET_YAML.read_text(encoding="utf-8"))
    for task in doc["tasks"]:
        needle = str(task["expect_needle"])
        prompt = str(task["prompt"])
        assert needle in prompt, task["id"]
        pos = prompt.index(needle)
        assert pos >= head, task["id"]  # вне сохраняемой головы
        assert pos + len(needle) <= len(prompt) - tail, task["id"]  # вне хвоста
        # ground truth: сжатая секция НЕ содержит needle дословно
        assert needle not in shape_section(prompt, budget=budget), task["id"]


def test_needle_set_yaml_v3_needle_types_diverse() -> None:
    """Типы needle разведены (преемственность v2): код, десятичное число,
    имя, ISO-дата, semver, hex — по одному на задание."""
    doc = yaml.safe_load(NEEDLE_SET_YAML.read_text(encoding="utf-8"))
    needles = [str(t["expect_needle"]) for t in doc["tasks"]]
    joined = " ".join(needles)
    for needle in (
        "TG-7741-DELTA",   # код бюджетной заявки
        "0.4173",          # десятичное число (коэффициент)
        "Синяя-нить-7",    # имя протокола
        "2027-03-14",      # ISO-дата
        "v3.9.2-rc1",      # semver релиз-кандидата
        "0xDEADBEEF",      # hex-маркер формата
    ):
        assert needle in joined, needle


def test_needle_set_v3_load_tasks_and_detect(tmp_path, monkeypatch) -> None:
    """_load_tasks + grep-детект на committed v3-наборе (синтетический
    вывод, без LLM): needle_rate = found/total по (задание × прогон),
    found учитывается и в document, и в draft; пустой вывод — missed."""
    tasks = probe_mod._load_tasks(NEEDLE_SET_YAML)
    assert len(tasks) == 6
    assert all(str(t.get("expect_needle") or "").strip() for t in tasks)

    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    script = {
        # found в document / только в draft / missed (пустой вывод) — итого 5/6
        ("n01-budget-code", 1): {"document": "заявка TG-7741-DELTA одобрена"},
        ("n02-metric-decimal", 1): {"draft": "коэффициент: 0.4173"},
        ("n03-failover-protocol", 1): {"document": "", "draft": ""},
        ("n04-migration-date", 1): {"document": "старт переноса: 2027-03-14"},
        ("n05-release-semver", 1): {"document": "кандидат v3.9.2-rc1 готов"},
        ("n06-marker-hex", 1): {"draft": "маркер записи 0xDEADBEEF"},
    }
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(script))

    report = run_probe(
        mode=MODE, golden=golden, heldout=heldout, needle=NEEDLE_SET_YAML,
        model_class="fast", registry=Registry(REGISTRY_DIR), llm=object(),
        runs=1,
    )

    assert report.needle_rate == pytest.approx(5 / 6)
    # golden-метрики от needle-набора не зависят (M1 — только golden-скоры)
    assert report.golden_median_score == pytest.approx(0.8)
