"""Схема-валидатор режимов (Ф3.5a-2, контур (а)): offline, без Redis.

Невакуумность: каждая фикстура-нарушитель даёт РОВНО свой код
(codes == {"S3"} и т.п.), валидный режим — пустой список.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.orchestrator.mode_schema import Finding, validate_schema
from ai_workspace.registry import Registry
from ai_workspace.tools.modes_validate import main as cli_main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "modes"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

VIOLATIONS = [
    ("bad_missing_field.yaml", "S1"),
    ("bad_kind.yaml", "S2"),
    ("bad_shape_contract.yaml", "S3"),
    ("bad_dup_id.yaml", "S4"),
    ("bad_dangling_edge.yaml", "S5"),
    ("bad_empty_nodes.yaml", "S6"),
]


def _load(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def _codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def test_valid_statya_has_no_findings() -> None:
    findings = validate_schema(_load("valid_statya.yaml"), Registry(REGISTRY_DIR))
    assert findings == []


@pytest.mark.parametrize(("fixture", "code"), VIOLATIONS)
def test_violator_gives_exactly_its_code(fixture: str, code: str) -> None:
    findings = validate_schema(_load(fixture), Registry(REGISTRY_DIR))
    assert _codes(findings) == {code}, [str(f) for f in findings]
    assert all(f.severity == "error" for f in findings)


def test_s7_unknown_contract_inline() -> None:
    doc = _load("valid_statya.yaml")
    doc["contract"] = "video"
    assert _codes(validate_schema(doc, Registry(REGISTRY_DIR))) == {"S7"}


def test_s8_non_positive_version_inline() -> None:
    doc = _load("valid_statya.yaml")
    doc["version"] = 0
    assert _codes(validate_schema(doc, Registry(REGISTRY_DIR))) == {"S8"}


def test_non_mapping_document_is_s1() -> None:
    findings = validate_schema(["not", "a", "mapping"], Registry(REGISTRY_DIR))
    assert _codes(findings) == {"S1"}


def test_cli_file_valid_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--file", str(FIXTURES / "valid_statya.yaml")]) == 0
    assert "✅" in capsys.readouterr().out


def test_cli_file_violator_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--file", str(FIXTURES / "bad_kind.yaml")]) == 1
    assert "S2" in capsys.readouterr().out


def test_cli_empty_dir_exits_zero(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli_main(["--dir", str(tmp_path)]) == 0
    assert "режимов нет" in capsys.readouterr().out
