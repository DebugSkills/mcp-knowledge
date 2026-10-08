"""Э2-1 Ф7 (arch-2026-10-08-f7-calibration): схемы профиля и вариантов (A4).

Невакуумность (образец test_mode_schema.py): каждый нарушитель даёт РОВНО
свой код и путь; валидные документы — пустой список findings.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration.profiles import (
    PROFILE_SCHEMA,
    Finding,
    list_profiles,
    load_profile,
    validate_profile,
)
from ai_workspace.calibration.variants import VARIANT_SCHEMA, validate_variants


def _codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


VALID_PROFILE: dict = {
    "schema": PROFILE_SCHEMA,
    "profile_id": "cal-heavy-qwen25-7b-0002",
    "model_class": "heavy",  # A4: привязка применения, НЕ мутация узлов
    "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "sha256:abc123"},
    "status": "calibrated",
    "version": 2,
    "evidence": {
        "probe_run": "probe-2026-10-20-001",
        "golden_manifest": "sha256:golden",
        "pricing_manifest": "sha256:pricing",
        "metrics": {  # F6: golden и held-out — раздельные обязательные поля
            "golden_median_score": 0.83,
            "heldout_score": 0.79,
            "parse_rate": 1.0,
            "rub": 12.4,
            "wall_s": 610,
        },
    },
    "scalars": {
        "retries": 2,
        "max_iterations": 3,
        "shaping": "full-context",
        "context_mode": "full",
    },
    "constraints": {"quality_floor": 0.75, "rub_cap": None, "wall_cap_s": None},
    "created_at": "2026-10-20T12:00:00Z",
    "updated_at": "2026-10-20T12:00:00Z",
}


def _profile() -> dict:
    return deepcopy(VALID_PROFILE)


# ── профили: fail-closed (A4) ─────────────────────────────────────────────


def test_valid_profile_has_no_findings() -> None:
    assert validate_profile(_profile()) == []


def test_unknown_top_level_field_is_cp4() -> None:
    doc = _profile()
    doc["temperature"] = 0.7
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP4"}, [str(f) for f in findings]
    assert findings[0].path == "temperature"


@pytest.mark.parametrize("field", ["zone", "decoding", "q_floor"])
def test_forbidden_field_is_cp5(field: str) -> None:
    doc = _profile()
    doc[field] = "public"
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP5"}, (field, [str(f) for f in findings])
    assert findings[0].path == field


def test_missing_heldout_score_is_cp6() -> None:
    doc = _profile()
    del doc["evidence"]["metrics"]["heldout_score"]
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP6"}, [str(f) for f in findings]
    assert findings[0].path == "evidence.metrics.heldout_score"


def test_missing_golden_median_score_is_cp6_separately() -> None:
    """F6: отсутствие golden-среды ловится отдельно от held-out."""
    doc = _profile()
    del doc["evidence"]["metrics"]["golden_median_score"]
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP6"}, [str(f) for f in findings]
    assert findings[0].path == "evidence.metrics.golden_median_score"


def test_unknown_status_is_cp2() -> None:
    doc = _profile()
    doc["status"] = "x"
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP2"}, [str(f) for f in findings]
    assert findings[0].path == "status"


def test_schema_mismatch_is_cp3() -> None:
    doc = _profile()
    doc["schema"] = "calibration-profile/2"
    assert _codes(validate_profile(doc)) == {"CP3"}


def test_missing_required_field_is_cp1() -> None:
    doc = _profile()
    del doc["version"]
    findings = validate_profile(doc)
    assert _codes(findings) == {"CP1"}, [str(f) for f in findings]
    assert findings[0].path == "version"


def test_non_mapping_profile_is_cp0() -> None:
    assert _codes(validate_profile(["not", "a", "mapping"])) == {"CP0"}


# ── load_profile / list_profiles (I/O-хелперы) ────────────────────────────


def test_load_profile_roundtrip(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "cal-heavy-0001.yaml").write_text(
        yaml.safe_dump(VALID_PROFILE, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    loaded = load_profile(profiles, "cal-heavy-0001")
    assert isinstance(loaded, dict)
    assert validate_profile(loaded) == []


def test_load_profile_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_profile(tmp_path, "nope") is None


def test_list_profiles_sorted_ids_only_yaml(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for name in ("cal-b-0001.yaml", "cal-a-0002.yaml", "notes.txt", "cal-c.yaml"):
        (profiles / name).write_text("{}", encoding="utf-8")
    assert list_profiles(profiles) == ["cal-a-0002", "cal-b-0001", "cal-c"]


def test_list_profiles_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_profiles(tmp_path / "nope") == []


# ── варианты (§7.4, F8) ───────────────────────────────────────────────────


def _modes_dir(tmp_path: Path) -> Path:
    modes = tmp_path / "modes"
    modes.mkdir()
    (modes / "statya.yaml").write_text("id: statya\n", encoding="utf-8")
    (modes / "statya.deep.yaml").write_text("id: statya.deep\n", encoding="utf-8")
    return modes


def _variants_doc() -> dict:
    return {
        "schema": VARIANT_SCHEMA,
        "entries": [
            {
                "variant": "statya.deep",
                "variant_of": "statya",
                "status": "baseline",
                "decided_by": "operator",
                "decided_at": "2026-11-01T18:00:00Z",
            }
        ],
    }


def test_variants_valid_baseline_ok(tmp_path: Path) -> None:
    assert validate_variants(_variants_doc(), _modes_dir(tmp_path)) == []


def test_variants_promoted_without_probe_pair_is_cv7(tmp_path: Path) -> None:
    doc = _variants_doc()
    doc["entries"][0]["status"] = "promoted"
    findings = validate_variants(doc, _modes_dir(tmp_path))
    assert _codes(findings) == {"CV7"}, [str(f) for f in findings]
    assert findings[0].path == "entries[0].probe_pair"


def test_variants_promoted_with_probe_pair_ok(tmp_path: Path) -> None:
    doc = _variants_doc()
    doc["entries"][0]["status"] = "promoted"
    doc["entries"][0]["probe_pair"] = {"base": "probe-001", "variant": "probe-002"}
    assert validate_variants(doc, _modes_dir(tmp_path)) == []


def test_variants_variant_of_missing_mode_is_cv6(tmp_path: Path) -> None:
    modes = _modes_dir(tmp_path)
    (modes / "statya.yaml").unlink()
    findings = validate_variants(_variants_doc(), modes)
    assert _codes(findings) == {"CV6"}, [str(f) for f in findings]
    assert findings[0].path == "entries[0].variant_of"


def test_variants_variant_file_missing_is_cv5(tmp_path: Path) -> None:
    modes = _modes_dir(tmp_path)
    (modes / "statya.deep.yaml").unlink()
    findings = validate_variants(_variants_doc(), modes)
    assert _codes(findings) == {"CV5"}, [str(f) for f in findings]


def test_variants_unknown_status_is_cv4(tmp_path: Path) -> None:
    doc = _variants_doc()
    doc["entries"][0]["status"] = "maybe"
    assert _codes(validate_variants(doc, _modes_dir(tmp_path))) == {"CV4"}


def test_variants_schema_mismatch_is_cv1(tmp_path: Path) -> None:
    doc = _variants_doc()
    doc["schema"] = "calibration-variants/2"
    assert _codes(validate_variants(doc, _modes_dir(tmp_path))) == {"CV1"}
