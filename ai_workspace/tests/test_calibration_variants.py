"""A6 (Ф7 Э4-1, arch-2026-10-08-f7-calibration): конвенция mode-variant.

Покрытие: S11 (схема — тройка variant_of/variant_axis/variant_rationale),
L16 (линт — база существует + zone-наследование I5), ``evaluate_promotion``
(§7.3 — каждое условие отдельным кейсом на реальном ProbeReport) и
``record_decision`` (fail-closed CV7 → ValueError; запись на носителе
проходит ``validate_variants``; upsert не плодит дубли).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.calibration.variants import (
    evaluate_promotion,
    record_decision,
    validate_variants,
)
from ai_workspace.orchestrator.mode_lint import lint_l16, validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry

FIXTURES = Path(__file__).parent / "fixtures" / "modes"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = FIXTURES / "valid_statya.yaml"


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry(REGISTRY_DIR)
    reg.load()
    return reg


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _variant_doc(**extra) -> dict:
    doc = _load(VALID)
    doc.update(
        {
            "variant_of": "statya",
            "variant_axis": "decomposition",
            "variant_rationale": "глубже декомпозиция для слабой модели",
        }
    )
    doc.update(extra)
    return doc


# ── S11: схема mode-variant ──────────────────────────────────────────────────


def test_s11_no_variant_of_not_triggered(registry: Registry):
    """Режим без variant_of — обычный режим: S11 молчит (нет ложных срабатываний)."""
    assert "S11" not in {f.code for f in validate_schema(_load(VALID), registry)}


def test_s11_valid_variant_clean(registry: Registry):
    """Корректная тройка полей не даёт ни S11, ни других S-ошибок."""
    assert validate_schema(_variant_doc(), registry) == []


def test_s11_axis_outside_enum(registry: Registry):
    findings = validate_schema(_variant_doc(variant_axis="tone"), registry)
    assert [f.code for f in findings] == ["S11"]
    assert findings[0].path == "variant_axis"


def test_s11_empty_variant_of(registry: Registry):
    findings = validate_schema(_variant_doc(variant_of=""), registry)
    assert [f.code for f in findings] == ["S11"]
    assert findings[0].path == "variant_of"


def test_s11_missing_rationale(registry: Registry):
    doc = _variant_doc()
    del doc["variant_rationale"]
    findings = validate_schema(doc, registry)
    assert [f.code for f in findings] == ["S11"]
    assert findings[0].path == "variant_rationale"


# ── L16: база варианта + zone-наследование ───────────────────────────────────


def _write_mode(modes: Path, name: str, content: str = "id: x\n") -> None:
    (modes / f"{name}.yaml").write_text(content, encoding="utf-8")


def test_l16_zone_match_ok(tmp_path: Path):
    """База существует, зоны совпадают (обе public по умолчанию) — чисто."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "base")
    assert lint_l16({"variant_of": "base"}, None, tmp_path) == []


def test_l16_private_inherits_private_ok(tmp_path: Path):
    """I5: private-база → private-вариант допустим."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "base", "id: x\nzone: private\n")
    assert lint_l16({"variant_of": "base", "zone": "private"}, None, tmp_path) == []


def test_l16_base_missing(tmp_path: Path):
    modes = tmp_path / "modes"
    modes.mkdir()
    findings = lint_l16({"variant_of": "ghost"}, None, tmp_path)
    assert [f.code for f in findings] == ["L16"]
    assert findings[0].path == "variant_of"


def test_l16_zone_mismatch(tmp_path: Path):
    """Private-база + public-вариант — утечка зоны: ровно L16 на zone."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "base", "id: x\nzone: private\n")
    findings = lint_l16({"variant_of": "base"}, None, tmp_path)
    assert [f.code for f in findings] == ["L16"]
    assert findings[0].path == "zone"


def test_l16_wired_into_validate_lint(registry: Registry):
    """L16 в общем lint(): сломанный вариант ловится, валидный — нет."""
    codes = {
        f.code
        for f in validate_lint(_variant_doc(variant_of="ghost-mode"), registry, base_dir=REPO_ROOT)
    }
    assert "L16" in codes
    # Нет регрессии: валидный режим с реальной базой чист по L16.
    codes_ok = {
        f.code for f in validate_lint(_variant_doc(), registry, base_dir=REPO_ROOT)
    }
    assert "L16" not in codes_ok


# ── evaluate_promotion: критерий §7.3 ────────────────────────────────────────


def _report(
    golden: float = 0.9,
    dispersion: float = 0.05,
    heldout: float = 0.88,
    rub: float = 1.0,
    wall_s: float = 10.0,
) -> ProbeReport:
    return ProbeReport(
        run_id="r1",
        model_id="test/model",
        digest="deadbeef",
        golden_manifest="g",
        pricing_manifest="p",
        golden_median_score=golden,
        golden_dispersion=dispersion,
        heldout_score=heldout,
        parse_rate=1.0,
        rub=rub,
        wall_s=wall_s,
        n_runs=3,
    )


def test_promotion_all_conditions_passed():
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.88, rub=1.0, wall_s=10.0),
        quality_floor=0.8,
        rub_cap=2.0,
        wall_cap_s=20.0,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_base_above_floor():
    """База не проваливается — вариант не обоснован (§7.3 условие 1)."""
    res = evaluate_promotion(
        _report(golden=0.85),
        _report(golden=0.9),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "база" in res["reasons"][0]


def test_promotion_variant_below_floor():
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.75, heldout=0.74),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "вариант" in res["reasons"][0]


def test_promotion_rub_cap_exceeded():
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.88, rub=3.0),
        quality_floor=0.8,
        rub_cap=2.0,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "rub_cap" in res["reasons"][0]


def test_promotion_wall_cap_exceeded():
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.88, wall_s=30.0),
        quality_floor=0.8,
        wall_cap_s=20.0,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "wall_cap_s" in res["reasons"][0]


def test_promotion_caps_none_not_checked():
    """Кап не задан — не проверяется (рамка D6 опциональна)."""
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.88, rub=999.0, wall_s=999.0),
        quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


def test_promotion_heldout_diverges():
    """Held-out за пределами дисперсии — golden не подтверждён (F6)."""
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.7, dispersion=0.05),
        quality_floor=0.8,
    )
    assert res["passed"] is False
    assert len(res["reasons"]) == 1
    assert "held-out" in res["reasons"][0]


def test_promotion_heldout_zero_dispersion_exact():
    """Дисперсия 0: совпадение точно (допуск 1e-9) — проходит."""
    res = evaluate_promotion(
        _report(golden=0.7, heldout=0.7),
        _report(golden=0.9, heldout=0.9, dispersion=0.0),
        quality_floor=0.8,
    )
    assert res == {"passed": True, "reasons": []}


# ── record_decision: реестр variants.yaml (§7.4) ─────────────────────────────


@pytest.fixture()
def variants_path(tmp_path: Path) -> Path:
    """tmp-дерево layout'а SSOT: <ws>/modes + <ws>/calibration/variants.yaml."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya")
    _write_mode(modes, "statya.deep")
    return tmp_path / "calibration" / "variants.yaml"


_PROBE_PAIR = {"base": "runs/base.json", "variant": "runs/deep.json"}


def test_record_decision_promoted_requires_probe_pair(variants_path: Path):
    """Promoted без пары отчётов — CV7 → ValueError, файл НЕ записан (fail-closed)."""
    with pytest.raises(ValueError, match="probe_pair"):
        record_decision(
            variants_path, "statya.deep", "promoted", None, "operator", "2026-10-08T18:00:00Z"
        )
    assert not variants_path.exists()


def test_record_decision_invalid_status(variants_path: Path):
    with pytest.raises(ValueError, match="status"):
        record_decision(
            variants_path, "statya.deep", "unknown", _PROBE_PAIR, "operator", "2026-10-08T18:00:00Z"
        )


def test_record_decision_writes_valid_entry(variants_path: Path):
    record_decision(
        variants_path, "statya.deep", "promoted", _PROBE_PAIR, "operator", "2026-10-08T18:00:00Z"
    )
    doc = yaml.safe_load(variants_path.read_text(encoding="utf-8"))
    assert doc["schema"] == "calibration-variants/1"
    assert len(doc["entries"]) == 1
    entry = doc["entries"][0]
    assert entry["variant"] == "statya.deep"
    assert entry["variant_of"] == "statya"  # derived из конвенции <mode>.<variant>
    assert entry["status"] == "promoted"
    assert entry["probe_pair"] == _PROBE_PAIR
    assert entry["decided_by"] == "operator"
    assert validate_variants(doc, variants_path.parent.parent / "modes") == []


def test_record_decision_upsert_and_baseline(variants_path: Path):
    """Baseline без пары легитимен; повторная запись — upsert (одна запись)."""
    record_decision(
        variants_path, "statya.deep", "baseline", None, "operator", "2026-10-08T10:00:00Z"
    )
    record_decision(
        variants_path, "statya.deep", "promoted", _PROBE_PAIR, "operator", "2026-10-08T18:00:00Z"
    )
    doc = yaml.safe_load(variants_path.read_text(encoding="utf-8"))
    entries = doc["entries"]
    assert len(entries) == 1
    assert entries[0]["status"] == "promoted"
    assert "probe_pair" in entries[0]
    assert validate_variants(doc, variants_path.parent.parent / "modes") == []


# ── CC1 (В2-B 2f): ось shaping (S11) + dash→dot резолв базы (L16) ───────────


def test_s11_shaping_axis_accepted(registry: Registry):
    """Ось «shaping» (CC1, В2-B 2f) в enum VARIANT_AXES — тройка валидна."""
    assert validate_schema(_variant_doc(variant_axis="shaping"), registry) == []


def test_l16_dash_id_resolves_dot_file(tmp_path: Path):
    """variant_of — id режима (S11); id с дефисами → файл с точками
    (statya-local → statya.local.yaml): база найдена, ошибки нет."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.local", "id: statya-local\n")
    assert lint_l16({"variant_of": "statya-local"}, None, tmp_path) == []


def test_l16_dash_id_zone_check_via_fallback(tmp_path: Path):
    """Зона сверяется с базой, найденной по dash→dot (public-база +
    private-вариант → ошибка зоны I5, а НЕ «база не найдена»)."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.local", "id: statya-local\n")
    findings = lint_l16({"variant_of": "statya-local", "zone": "private"}, None, tmp_path)
    assert [f.code for f in findings] == ["L16"]
    assert findings[0].path == "zone"


# ── P1-fix (критика 3c-promotion, REVISE 2026-10-09): variant_of из mode-файла
#    + CV6 dash→dot (паритет L16) ──────────────────────────────────────────────


@pytest.fixture()
def shape_variants_path(tmp_path: Path) -> Path:
    """tmp-дерево shape-варианта CC1: statya.full.local (variant_of:
    statya-local) поверх базы statya.local — как реальные mode-файлы."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.local", "id: statya-local\n")
    _write_mode(
        modes,
        "statya.full.local",
        "id: statya-full-local\nvariant_of: statya-local\n",
    )
    return tmp_path / "calibration" / "variants.yaml"


def test_record_decision_shape_variant_executable(shape_variants_path: Path):
    """P1: запись для statya.full.local ИСПОЛНИМА — variant_of из mode-файла
    (statya-local), CV6 резолвит dash→dot в statya.local.yaml; CV0–CV7
    чистые, запись на носителе валидна (tmp-реестр, не реальный)."""
    record_decision(
        shape_variants_path,
        "statya.full.local",
        "promoted",
        _PROBE_PAIR,
        "operator",
        "2026-10-09T12:00:00Z",
    )
    doc = yaml.safe_load(shape_variants_path.read_text(encoding="utf-8"))
    entry = doc["entries"][0]
    assert entry["variant"] == "statya.full.local"
    assert entry["variant_of"] == "statya-local"  # из mode-файла, не rsplit
    assert entry["status"] == "promoted"
    assert validate_variants(doc, shape_variants_path.parent.parent / "modes") == []


def test_record_decision_cv6_fail_closed_missing_base(tmp_path: Path):
    """CV6 fail-closed НЕ ослаблен: база не найдена после ОБОИХ кандидатов
    имени (ghost-base / ghost.base.yaml нет) → ValueError, реестр не тронут."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(
        modes, "statya.full.local", "id: statya-full-local\nvariant_of: ghost-base\n"
    )
    variants_path = tmp_path / "calibration" / "variants.yaml"
    with pytest.raises(ValueError, match="CV6"):
        record_decision(
            variants_path,
            "statya.full.local",
            "promoted",
            _PROBE_PAIR,
            "operator",
            "2026-10-09T12:00:00Z",
        )
    assert not variants_path.exists()


def test_record_decision_variant_of_field_over_filename(tmp_path: Path):
    """Источник истины — ПОЛЕ mode-файла: имя и поле расходятся, ОБЕ базы
    существуют (rsplit-кандидат statya.full валиден, но НЕ база) —
    записывается variant_of из mode-файла (statya-local)."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.full")  # rsplit-кандидат: существует, но не база
    _write_mode(modes, "statya.local", "id: statya-local\n")
    _write_mode(
        modes,
        "statya.full.local",
        "id: statya-full-local\nvariant_of: statya-local\n",
    )
    variants_path = tmp_path / "calibration" / "variants.yaml"
    record_decision(
        variants_path,
        "statya.full.local",
        "baseline",
        None,
        "operator",
        "2026-10-09T12:00:00Z",
    )
    doc = yaml.safe_load(variants_path.read_text(encoding="utf-8"))
    assert doc["entries"][0]["variant_of"] == "statya-local"


def test_cv6_dash_id_resolves_dot_file(tmp_path: Path):
    """CV6 напрямую: variant_of — id с дефисами (statya-local) → файл с
    точками (statya.local.yaml) резолвится — паритет L16 (dash→dot)."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.local", "id: statya-local\n")
    _write_mode(modes, "statya.full.local", "id: statya-full-local\n")
    doc = {
        "schema": "calibration-variants/1",
        "entries": [
            {
                "variant": "statya.full.local",
                "variant_of": "statya-local",
                "status": "baseline",
                "decided_by": "operator",
                "decided_at": "2026-10-09",
            }
        ],
    }
    assert validate_variants(doc, modes) == []


def test_record_decision_legacy_mode_without_field_keeps_convention(
    variants_path: Path,
):
    """Паритет F1: mode-файл без variant_of (legacy/не-вариант) — прежняя
    цепочка (запись реестра → конвенция rsplit) работает без изменений."""
    record_decision(
        variants_path, "statya.deep", "baseline", None, "operator", "2026-10-09T12:00:00Z"
    )
    doc = yaml.safe_load(variants_path.read_text(encoding="utf-8"))
    assert doc["entries"][0]["variant_of"] == "statya"


def test_record_decision_mode_file_broken_yaml_fail_closed(tmp_path: Path):
    """Битый YAML mode-файла → fail-closed ValueError (variant_of невыводим),
    а НЕ молчаливый rsplit-вывод возможной неверной базы."""
    modes = tmp_path / "modes"
    modes.mkdir()
    _write_mode(modes, "statya.full.local", "id: [unclosed\n  variant_of: statya-local\n")
    variants_path = tmp_path / "calibration" / "variants.yaml"
    with pytest.raises(ValueError, match="не читается"):
        record_decision(
            variants_path,
            "statya.full.local",
            "baseline",
            None,
            "operator",
            "2026-10-09T12:00:00Z",
        )
    assert not variants_path.exists()
