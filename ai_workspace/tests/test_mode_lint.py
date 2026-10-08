"""Offline-тесты линт-правил L1–L15 (Ф3.5a-3; L11 — Ф3.9; L13 — Ф6-a 6a.4; L14 — protected-принцип критика; L15 — Ф1 дельта-контекст).

Невакуумность: на каждый код — свой фикстур-нарушитель (ровно один код);
валидный режим даёт пустой список; отдельные inline-мутации покрывают
ветви правил (citer запрещён при verdict; private требует local-only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.orchestrator.mode_lint import lint_l9, validate_lint
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
        # L9 вынесен из «ровно одного кода» (Ф6-a 6a.4): фикстура содержит
        # fork → вместе с L9 срабатывает L13; см. test_l9_fixture_l13_before_l9.
        ("lint_L10_double_writer.yaml", "L10"),
        ("lint_L11_model_specific.yaml", "L11"),
        ("lint_L13_fork_join_unsupported.yaml", "L13"),
        ("lint_L14_critic_on_fast.yaml", "L14"),
        ("lint_L15_delta_on_critic.yaml", "L15"),
    ],
)
def test_each_rule_fires_exactly_its_code(registry: Registry, fixture: str, code: str):
    """Каждый фикстур-нарушитель даёт РОВНО свой код (не «пачку» замечаний)."""
    codes = _codes(_load(FIXTURES / fixture), registry)
    assert codes == {code}, f"{fixture}: ожидался {code}, получено {sorted(codes)}"


def test_l9_fixture_l13_before_l9(registry: Registry):
    """Фикстура L9 содержит fork → с L13 (Ф6-a 6a.4) срабатывают ОБА кода.

    Механизма подавления L13 в линте нет, поэтому фикстура больше не даёт
    «ровно один код». Порядок детерминирован и зафиксирован: L13
    («не поддержано движком») раньше L9 («не сбалансированы»). Сущность
    L9 проверяется прямым вызовом lint_l9 (парность fork=1, join=0).
    """
    doc = _load(FIXTURES / "lint_L9_fork_unbalanced.yaml")
    findings = validate_lint(doc, registry, base_dir=REPO_ROOT)
    codes = [f.code for f in findings]
    assert set(codes) == {"L9", "L13"}, f"ожидались L9+L13, получено {codes}"
    assert codes.index("L13") < codes.index("L9"), f"L13 должен идти раньше L9: {codes}"
    parity = lint_l9(doc, registry, None)
    assert [f.code for f in parity] == ["L9"]
    assert "fork=1, join=0" in parity[0].message


def test_l13_message_points_to_engine_and_plan(registry: Registry):
    """L13: сообщение называет узел+kind, ссылку engine.py:548 и план 6a.4."""
    findings = validate_lint(
        _load(FIXTURES / "lint_L13_fork_join_unsupported.yaml"),
        registry,
        base_dir=REPO_ROOT,
    )
    msgs = {f.path: f.message for f in findings}
    assert "узел 'fan' (kind=fork) не поддержан движком (engine.py:548)" in msgs["nodes.fan"]
    assert "plans/arch-2026-10-05-ai-workspace-f6a4-plan.md" in msgs["nodes.fan"]
    assert "узел 'merge' (kind=join) не поддержан движком (engine.py:548)" in msgs["nodes.merge"]


def test_l14_message_names_protected_principle(registry: Registry):
    """L14: сообщение называет слабый класс, сильный класс и зонную альтернативу."""
    findings = validate_lint(
        _load(FIXTURES / "lint_L14_critic_on_fast.yaml"),
        registry,
        base_dir=REPO_ROOT,
    )
    l14 = [f for f in findings if f.code == "L14"]
    assert len(l14) == 1, f"ожидался ровно один L14, получено {[f.code for f in findings]}"
    assert l14[0].path == "nodes.critic.model_class"
    assert "critic-gate на слабом классе `fast`" in l14[0].message
    assert "`heavy` (protected-принцип)" in l14[0].message
    assert "класс `local-only`" in l14[0].message


def test_l14_allows_heavy_and_local_only(registry: Registry):
    """L14: heavy (public) и local-only (private) легальны; fast — ошибка."""
    doc = _load(VALID)
    critic = next(n for n in doc["nodes"] if n.get("kind") == "critic-gate")
    critic["model_class"] = "fast"
    assert "L14" in _codes(doc, registry)
    critic["model_class"] = "heavy"
    assert "L14" not in _codes(doc, registry)
    doc["zone"] = "private"  # зонный режим I5: всем узлам local-only
    for n in doc["nodes"]:
        if n.get("model_class"):
            n["model_class"] = "local-only"
    assert "L14" not in _codes(doc, registry)


def test_valid_fixtures_do_not_fire_l13(registry: Registry):
    """Валидные фикстуры не дают L13 (правило не ломает валидные режимы)."""
    paths = sorted(FIXTURES.glob("valid_*.yaml"))
    assert paths, "ожидалась хотя бы одна valid_* фикстура"
    for path in paths:
        assert "L13" not in _codes(_load(path), registry), f"{path.name}: неожиданный L13"


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


def test_l15_message_names_protected_principle(registry: Registry):
    """L15: сообщение называет protected-принцип и допустимые kind-ы."""
    findings = validate_lint(
        _load(FIXTURES / "lint_L15_delta_on_critic.yaml"),
        registry,
        base_dir=REPO_ROOT,
    )
    l15 = [f for f in findings if f.code == "L15"]
    assert len(l15) == 1, f"ожидался ровно один L15, получен {[f.code for f in findings]}"
    assert l15[0].path == "nodes.critic.context"
    assert "protected-принцип" in l15[0].message
    assert "llm-step/tool-step" in l15[0].message


def test_l15_allows_consumer_kinds(registry: Registry):
    """L15: context: delta легален на llm-step и tool-step (узлы-потребители)."""
    doc = _load(VALID)
    analyst = next(n for n in doc["nodes"] if n.get("id") == "analyst")
    citer = next(n for n in doc["nodes"] if n.get("id") == "citer")
    analyst["context"] = "delta"
    citer["context"] = "delta"
    assert "L15" not in _codes(doc, registry)
    assert validate_schema(doc, registry) == []  # схема принимает delta (S9 чист)
    human = next(n for n in doc["nodes"] if n.get("kind") == "human-gate")
    human["context"] = "delta"
    assert "L15" in _codes(doc, registry)
