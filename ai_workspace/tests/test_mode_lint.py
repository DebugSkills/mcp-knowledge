"""Offline-тесты линт-правил L1–L11 (Ф3.5a-3, L11 — Ф3.9).

Невакуумность: на каждый код — свой фикстур-нарушитель (ровно один код);
валидный режим даёт пустой список; отдельные inline-мутации покрывают
ветви правил (citer запрещён при verdict; private требует local-only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.orchestrator.mode_lint import validate_lint
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


def _codes(doc: dict, registry: Registry) -> set[str]:
    return {f.code for f in validate_lint(doc, registry, base_dir=REPO_ROOT)}


def test_valid_mode_is_clean(registry: Registry):
    """Валидный режим: и схема, и линт пусты (иначе правило слишком строгое)."""
    doc = _load(VALID)
    assert validate_schema(doc, registry) == []
    assert validate_lint(doc, registry, base_dir=REPO_ROOT) == []


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("lint_L1_cycle.yaml", "L1"),
        ("lint_L2_unknown_role.yaml", "L2"),
        ("lint_L3_unknown_tool.yaml", "L3"),
        ("lint_L4_output_input_gap.yaml", "L4"),
        ("lint_L5_no_human_gate.yaml", "L5"),
        ("lint_L6_missing_citer.yaml", "L6"),
        ("lint_L7_bad_model_class.yaml", "L7"),
        ("lint_L8_no_max_iter.yaml", "L8"),
        ("lint_L9_fork_unbalanced.yaml", "L9"),
        ("lint_L10_double_writer.yaml", "L10"),
        ("lint_L11_model_specific.yaml", "L11"),
    ],
)
def test_each_rule_fires_exactly_its_code(registry: Registry, fixture: str, code: str):
    """Каждый фикстур-нарушитель даёт РОВНО свой код (не «пачку» замечаний)."""
    codes = _codes(_load(FIXTURES / fixture), registry)
    assert codes == {code}, f"{fixture}: ожидался {code}, получено {sorted(codes)}"


def test_model_agnostic_prompts_rule(registry: Registry):
    """L11: пустой prompt_overrides и отсутствие model-specific упоминаний."""
    doc = _load(VALID)
    doc["prompt_overrides"] = {"qwen2.5-7b": "кратко"}
    doc["nodes"][0]["prompt"] = "используй модель deepseek-v4-pro"
    assert "L11" in _codes(doc, registry)


def test_citer_forbidden_for_verdict(registry: Registry):
    """Ветвь L6: contract=verdict с citer → находка."""
    doc = _load(VALID)
    doc["contract"] = "verdict"
    doc["shape"] = "analyst-critic"
    assert "L6" in _codes(doc, registry)


def test_private_requires_local_only(registry: Registry):
    """Ветвь L7: zone=private и shelf-класс (heavy) → находка."""
    doc = _load(VALID)
    doc["zone"] = "private"
    assert "L7" in _codes(doc, registry)
    for n in doc["nodes"]:
        if n.get("model_class"):
            n["model_class"] = "local-only"
    assert "L7" not in _codes(doc, registry)


def test_cli_reports_lint_errors():
    """CLI видит L-ошибки (exit 1) и пропускает валидный (exit 0)."""
    from ai_workspace.tools.modes_validate import main

    assert main(["--file", str(FIXTURES / "lint_L1_cycle.yaml")]) == 1
    assert main(["--file", str(VALID)]) == 0
