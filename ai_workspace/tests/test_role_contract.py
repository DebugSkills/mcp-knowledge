"""Тесты контракта роли (Ф6-a 6a.2a): roles.yaml → contract → стабильный префикс промпта.

Контракт — компактная инструкция роли (вместо полного seed-скилла): рендерится
в ``engine._prompt`` ПЕРВОЙ частью, до ``# РОЛЬ/# УЗЕЛ`` и секций-``inputs``.
Требования плана REV.15: стабильный cache-friendly префикс; одинаков для обеих
полок (parity L11); у всех ролей реестра есть контракт в границах объёма;
контракт критика требует вердикт первой значащей строкой (совместимость с
``engine._parse_verdict``) и нигде не упоминает модели.
"""

from __future__ import annotations

from pathlib import Path

from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
from ai_workspace.orchestrator.graph import Node
from ai_workspace.orchestrator.mode_lint import MODEL_NAME_MARKERS
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import (
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
ROLES = ("analyst", "critic", "editor", "researcher")
MIN_CHARS, MAX_CHARS = 200, 6000
"""Тест-лок объёма: нижняя граница отсекает пустышку, верхняя — «полный скилл»."""

FORBIDDEN_MARKERS = frozenset(MODEL_NAME_MARKERS) | {"gpt"}
"""Маркеры model-specific лексики (список плана + L11): qwen/deepseek/gpt/glm/llama/mistral/..."""


class FakeRegistry:
    """Утка реестра для fallback-тестов: только ``get(kind)``."""

    def __init__(self, roles: dict) -> None:
        self._roles = roles

    def get(self, kind: str) -> dict:
        return self._roles


def make_engine(*, registry=None, seed_loader=None) -> ModeEngine:
    """Движок на фейках: ``_prompt`` не требует живого контура (offline)."""
    return ModeEngine(
        jobs=FakeJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM({}),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        registry=registry,
        seed_loader=seed_loader,
    )


def role_node(role: str, model_class: str = "heavy") -> Node:
    spec = {"id": f"{role}-x", "kind": "llm-step", "role": role, "model_class": model_class}
    return Node(id=spec["id"], kind="llm-step", spec=spec)


# ── рендер: контракт в промпте, до # РОЛЬ (стабильный префикс) ────────────


def test_prompt_starts_with_contract_before_role_header() -> None:
    registry = Registry(REGISTRY_DIR)
    engine = make_engine(registry=registry)

    prompt = engine._prompt(role_node("analyst"), {"brief": "тема"})

    contract = registry.get("roles")["analyst"]["contract"].strip()
    assert contract in prompt
    assert prompt.startswith(contract), "контракт обязан быть самой первой частью промпта"
    assert prompt.index(contract) < prompt.index("# РОЛЬ"), "контракт — ДО # РОЛЬ"
    assert prompt.index("# РОЛЬ") < prompt.index("## brief"), "# РОЛЬ — ДО секций-входов"
    assert "# УЗЕЛ: analyst-x (llm-step)" in prompt


def test_prompt_independent_of_model_class_shelf() -> None:
    """Parity (L11): heavy и fast получают одинаковый промпт — до секций и целиком."""
    engine = make_engine(registry=Registry(REGISTRY_DIR))
    inputs = {"brief": "тема"}

    heavy = engine._prompt(role_node("editor", model_class="heavy"), inputs)
    fast = engine._prompt(role_node("editor", model_class="fast"), inputs)

    assert heavy.split("## brief", 1)[0] == fast.split("## brief", 1)[0]
    assert heavy == fast


# ── реестр: контракт у всех ролей, в границах объёма ──────────────────────


def test_all_roles_have_contract_within_bounds() -> None:
    roles = Registry(REGISTRY_DIR).get("roles")
    for role in ROLES:
        contract = roles.get(role, {}).get("contract")
        assert isinstance(contract, str) and contract.strip(), f"{role}: нет контракта"
        assert MIN_CHARS <= len(contract) <= MAX_CHARS, (
            f"{role}: len={len(contract)} вне [{MIN_CHARS}, {MAX_CHARS}]"
        )


# ── контракт критика: вердикт первой строкой, совместимость с парсером ───


def test_critic_contract_demands_first_line_verdict() -> None:
    contract = Registry(REGISTRY_DIR).get("roles")["critic"]["contract"]
    low = contract.lower()

    assert "перв" in low and "строк" in low, "нет явной инструкции про первую строку"
    assert "PASS" in contract and "REVISE" in contract
    assert "РУБРИКА" in contract

    node = Node(id="critic", kind="critic-gate",
                spec={"verdicts": ["PASS", "REVISE"]})
    assert ModeEngine._parse_verdict(node, "PASS\nруБрика: …") == "PASS"
    assert ModeEngine._parse_verdict(node, "REVISE — согласованность 0.5") == "REVISE"


def test_critic_contract_tight_one_word_with_few_shots() -> None:
    """6a.2c: первая строка — ровно одно слово PASS/REVISE; few-shot ✅/❌.

    Живой пилот 6a.2b: 7B игнорирует мягкую формулировку и пишет near-miss
    ``VERDICT: X`` / пояснение до вердикта. Контракт обязан (а) требовать
    ровно одно слово-вердикт первой строкой, (б) явно запрещать префикс
    VERDICT и пояснения, (в) показывать мини-few-shot «верно/неверно».
    """
    contract = Registry(REGISTRY_DIR).get("roles")["critic"]["contract"]

    assert "ровно одно слово" in contract, "требование «ровно одно слово» явно"
    assert "✅" in contract and "❌" in contract, "мини-few-shot пара обязательна"
    assert "VERDICT" in contract, "явный запрет VERDICT-префикса"
    low = contract.lower()
    assert "пояснение до вердикта" in low, "пояснения до вердикта — ошибка (few-shot ❌)"


# ── model-agnostic: ни одного упоминания моделей ──────────────────────────


def test_contracts_have_no_model_names() -> None:
    roles = Registry(REGISTRY_DIR).get("roles")
    for role, meta in roles.items():
        contract = str(meta.get("contract") or "")
        low = contract.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker not in low, f"{role}: model-specific упоминание {marker!r}"


# ── приоритет и fallback: контракт > seed_loader > ничего ─────────────────


def test_contract_suppresses_seed_loader() -> None:
    calls: list[str] = []
    engine = make_engine(
        registry=Registry(REGISTRY_DIR),
        seed_loader=lambda path: calls.append(path) or f"SEED:{path}",
    )

    prompt = engine._prompt(role_node("critic"), {"draft": "текст"})

    assert calls == [], "контракт есть → seed_loader вызываться не обязан"
    assert "SEED:" not in prompt
    assert "НАЗНАЧЕНИЕ" in prompt  # реальный контракт, а не seed


def test_seed_loader_fallback_only_without_contract() -> None:
    engine = make_engine(
        registry=FakeRegistry({"nobody": {"seed_skill": "skills/x/SKILL.md"}}),
        seed_loader=lambda path: f"SEED({path})",
    )

    prompt = engine._prompt(role_node("nobody"), {})

    assert prompt.startswith("SEED(skills/x/SKILL.md)"), "нет контракта → seed-скилл в префиксе"
    assert "# РОЛЬ: nobody" in prompt


def test_role_without_contract_and_seed_prompts_as_before() -> None:
    engine = make_engine(
        registry=FakeRegistry({"bare": {}}),
        seed_loader=lambda path: f"SEED({path})",
    )

    prompt = engine._prompt(role_node("bare"), {"brief": "x"})

    assert prompt.startswith("# РОЛЬ: bare"), "нет контракта/seed → промпт как до 6a.2a"
