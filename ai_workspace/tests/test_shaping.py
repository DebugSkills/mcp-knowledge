"""Offline-тесты шейпинг-гигиены (Ф6-a 6a.3): оживление ``shaping`` в ``_prompt``.

Невакуумность: сжатие проверяется на РЕАЛЬНОМ ``engine._prompt`` (фейки только
для стора — как в test_role_contract), конфигурация shaping — на РЕАЛЬНОМ
реестре проекта; малый бюджет для compressed подменяется обёрткой реестра
(реестр-SSOT не правится ради теста — тот же приём, что в vp_ab_pilot).
L12 — на реальном ``validate_lint`` с фикстурой valid_statya.

Ключевая гарантия: **no-op** — секция в пределах бюджета уходит в промпт
байт-в-байт без маркера (0 изменения поведения на коротких документах);
контракт роли и хедер ``# РОЛЬ/# УЗЕЛ`` не сжимаются никогда.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ai_workspace.orchestrator.engine import (
    SHAPING_DEFAULT_BUDGET,
    SHAPING_FLOOR_CHARS,
    SHAPING_HEAD_SHARE,
    MemoryLedger,
    ModeEngine,
    load_mode,
    shape_section,
)
from ai_workspace.orchestrator.graph import Node
from ai_workspace.orchestrator.mode_lint import validate_lint
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import VALID, FakeBoards, FakeJobs, FakeLLM, FakeMCP

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
REPO_ROOT = Path(__file__).resolve().parents[2]

LONG = ("абзац про MCP-RAG с деталями. " * 250)  # ~7000 символов


class BudgetRegistry:
    """Обёртка реестра: fast → compressed с малым бюджетом (SSOT не трогаем)."""

    def __init__(self, base: Registry, budget: int) -> None:
        self._base = base
        self._budget = budget

    def get(self, kind: str) -> dict:
        data = self._base.get(kind)
        if kind == "model_classes":
            return {
                name: (
                    {**spec, "shaping": "compressed", "max_chars_per_section": self._budget}
                    if name == "fast" else spec
                )
                for name, spec in data.items()
            }
        return data


def make_engine(*, registry=None) -> ModeEngine:
    """Движок на фейках: ``_prompt`` не требует живого контура (offline)."""
    return ModeEngine(
        jobs=FakeJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM({}),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        registry=registry,
    )


def role_node(model_class: str) -> Node:
    spec = {"id": "editor-x", "kind": "llm-step", "role": "editor", "model_class": model_class}
    return Node(id=spec["id"], kind="llm-step", spec=spec)


# ── shape_section: чистая функция среза ────────────────────────────────────


def test_shape_section_noop_within_budget() -> None:
    """Секция ≤ бюджета → байт-в-байт, без маркера (0 изменения поведения)."""
    text = "короткая секция"
    assert shape_section(text, budget=4000) == text
    assert shape_section("x" * 4000, budget=4000) == "x" * 4000  # ровно бюджет


def test_shape_section_compresses_head_tail_with_marker() -> None:
    """Сжатие: маркер [срезано N%], голова и хвост сохранены, длина ≈ бюджет."""
    text = "Г" * 800 + "с" * 4800 + "Х" * 800  # 6400 симв; голова/хвост длиннее своих долей
    budget = 800
    out = shape_section(text, budget=budget)

    assert re.search(r"срезано \d+%", out), "маркер процента среза обязателен"
    percent = round((len(text) - budget) * 100 / len(text))
    assert f"срезано {percent}%" in out
    head = int(budget * SHAPING_HEAD_SHARE)
    tail = budget - head
    assert out.startswith("Г" * head), "голова (80% бюджета) сохранена"
    assert out.endswith("Х" * tail), "хвост (20% бюджета) сохранён"
    # длина = голова + хвост + маркер с разделителями — бюджет не раздувается
    assert len(out) <= budget + 60, f"итог длиннее бюджета+маркера: {len(out)}"


def test_shape_section_floor_guard() -> None:
    """Бюджет ≤ пола (200) → no-op: ниже пола сжатие теряет смысл."""
    assert shape_section(LONG, budget=SHAPING_FLOOR_CHARS) == LONG
    assert shape_section(LONG, budget=10) == LONG


# ── shaping_for: реестр → (режим, бюджет) ──────────────────────────────────


def test_shaping_for_reads_real_registry() -> None:
    engine = make_engine(registry=Registry(REGISTRY_DIR))
    assert engine.shaping_for("heavy") == ("full-context", 0)
    assert engine.shaping_for("fast") == ("compressed", 4000)
    assert engine.shaping_for("local-only") == ("full-context", 0)  # без shaping-поля
    assert engine.shaping_for("unknown-class") == ("full-context", 0)


def test_shaping_for_defaults_without_registry() -> None:
    """Реестр не задан → full-context (поведение до 6a.3, старые фейки)."""
    engine = make_engine()
    assert engine.shaping_for("fast") == ("full-context", 0)
    assert SHAPING_DEFAULT_BUDGET == 4000  # PLACEHOLDER, калибровка позже


# ── _prompt: данные сжимаются, контракт/хедер — никогда ────────────────────


def test_prompt_full_context_byte_identical() -> None:
    """full-context: длинная секция уходит в промпт байт-в-байт."""
    engine = make_engine(registry=Registry(REGISTRY_DIR))
    prompt = engine._prompt(role_node("heavy"), {"draft": LONG})
    assert f"## draft\n{LONG}" in prompt
    assert len(prompt) > len(LONG)  # секция не срезана


def test_prompt_compressed_trims_only_data_sections() -> None:
    """compressed: секция сжимается с маркером; префикс (контракт+хедер)
    байт-в-байт равен префиксу full-context — инструкции не тронуты."""
    real = Registry(REGISTRY_DIR)
    full_engine = make_engine(registry=real)
    comp_engine = make_engine(registry=BudgetRegistry(real, budget=800))

    inputs = {"draft": LONG}
    full = full_engine._prompt(role_node("heavy"), inputs)
    comp = comp_engine._prompt(role_node("fast"), inputs)

    # контракт роли + # РОЛЬ/# УЗЕЛ идентичны до первой секции данных
    assert comp.split("## draft", 1)[0] == full.split("## draft", 1)[0]
    # данные сжаты, маркер присутствует, хвост цел
    assert len(comp) < len(full)
    assert re.search(r"срезано \d+%", comp)
    assert comp.endswith(LONG[-160:])  # хвост секции (20% бюджета) сохранён
    # полная секция НЕ попала в промпт целиком
    assert LONG not in comp


def test_prompt_compressed_noop_on_short_sections() -> None:
    """compressed + короткая секция → промпт БАЙТ-В-БАЙТ как full-context."""
    real = Registry(REGISTRY_DIR)
    full_engine = make_engine(registry=real)
    comp_engine = make_engine(registry=BudgetRegistry(real, budget=800))

    inputs = {"draft": "Черновик: пара абзацев."}
    assert comp_engine._prompt(role_node("fast"), inputs) == full_engine._prompt(
        role_node("heavy"), inputs
    )


# ── L12: inputs объявлен и непуст у потребителей ───────────────────────────


def _load_valid() -> dict:
    return yaml.safe_load(Path(VALID).read_text(encoding="utf-8"))


def _lint_codes(doc: dict) -> set[str]:
    return {f.code for f in validate_lint(doc, Registry(REGISTRY_DIR), base_dir=REPO_ROOT)}


def test_l12_fires_on_missing_inputs_key() -> None:
    doc = _load_valid()
    critic = next(n for n in doc["nodes"] if n["id"] == "critic")
    del critic["inputs"]
    assert "L12" in _lint_codes(doc)


def test_l12_fires_on_empty_inputs_list() -> None:
    doc = _load_valid()
    citer = next(n for n in doc["nodes"] if n["id"] == "citer")
    citer["inputs"] = []
    assert "L12" in _lint_codes(doc)


def test_l12_human_gate_without_inputs_is_exempt() -> None:
    """human-gate без inputs НЕ зажигает L12 (его _inputs не вызывается)."""
    doc = _load_valid()
    gates = [n for n in doc["nodes"] if n["kind"] == "human-gate"]
    assert gates, "в valid_statya обязаны быть human-gate"
    assert all("inputs" not in n for n in gates), "фикстура: у gate нет inputs"
    assert "L12" not in _lint_codes(doc)
