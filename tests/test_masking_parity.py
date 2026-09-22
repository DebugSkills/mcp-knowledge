"""Паритет маскирования (006, P2-5/P2-new-5): дубликат в tools/errors_query.py
≡ scripts/errors_collect.py:mask_secrets (:99-106).

Дубликат ОБЯЗАТЕЛЕН: scripts/ не копируется в образ (Dockerfile: только
src/+tests/), импорт в туле невозможен. Дрейф ловит ЭТОТ тест (G4 preflight):
(i) структурный паритет наборов паттернов; (ii) кейс на каждое из 5 семейств;
(iii) идемпотентность; (iv) RED-инъекция (расхождение → красный).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("errors_collect", ROOT / "scripts" / "errors_collect.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

# имя модуля tools.errors_query затенено одноимённой функцией в пакете → sys.modules
import mcp_server.tools  # noqa: F401  (регистрирует пакет в sys.modules)

eq_tool = sys.modules["mcp_server.tools.errors_query"]

# ── (i) Структурный паритет: канонические 5 паттернов, источник — collector ──
# Канон извлекается ПОВЕДЕНЧЕСКИ из mask_secrets (см. test_family_cases ниже),
# а здесь фиксируем точный набор дубликата: дрейф любого элемента = красный.

CANONICAL_PATTERNS = [
    r"\bmcp_[a-z]{1,3}_[A-Za-z0-9]+\b",
    r"\b[0-9a-fA-F]{32,}\b",
    r"(?i)Authorization:\s*.*",
    r"(?i)\bBearer\s+\S+",
    r"(?i)\b(password|token|api[_-]?key|secret)\s*[=:]\s*\S+",
]


class TestStructuralParity:
    def test_duplicate_has_exactly_five_patterns(self):
        assert len(eq_tool._SECRET_PATTERNS) == 5

    def test_duplicate_pattern_sources_match_canon(self):
        sources = [p.pattern for p, _ in eq_tool._SECRET_PATTERNS]
        assert sorted(sources) == sorted(CANONICAL_PATTERNS)

    def test_replacements_are_secret_markers(self):
        for _, repl in eq_tool._SECRET_PATTERNS:
            assert "<secret>" in repl


# ── (ii) Поведенческий паритет: ≥1 кейс на каждое из 5 семейств ──

FAMILY_CASES = [
    # (семейство, входящая строка)
    ("mcp-key", "key=mcp_ab_SuperSecret1"),
    ("hex32", "digest=0123456789abcdef0123456789abcdef"),
    ("authorization", "header Authorization: Basic dXNlcjpwYXNz"),
    ("bearer", "token Bearer eyJhbGciOiJIUzI1NiJ9"),
    ("password-eq", "password=hunter2"),
    ("token-colon", "token: abc123xyz"),
    ("api-key", "api_key=sk-12345"),
    ("secret-eq", "secret=s3cr3tvalue"),
]


class TestFamilyParity:
    @pytest.mark.parametrize("family,case", FAMILY_CASES, ids=[f for f, _ in FAMILY_CASES])
    def test_both_mask_identically(self, family, case):
        # collector маскирует на записи; тул — на отдаче; результат обязан совпасть
        assert ec.mask_secrets(case) == eq_tool.mask_output(case), family

    @pytest.mark.parametrize("family,case", FAMILY_CASES, ids=[f for f, _ in FAMILY_CASES])
    def test_collector_actually_masks(self, family, case):
        out = ec.mask_secrets(case)
        assert out != case, f"семейство {family} перестало маскироваться в collector"

    def test_plain_text_unchanged_by_both(self):
        plain = "Timeout after 30 seconds in /app/data/logs"
        assert ec.mask_secrets(plain) == plain
        assert eq_tool.mask_output(plain) == plain


# ── (iii) Идемпотентность: дважды = один раз ──

IDEMPOTENCE_CASES = [c for _, c in FAMILY_CASES] + [
    "mcp_x_key1 and 0123456789abcdef0123456789abcdef together",
    "password=<secret> уже замаскирован",
]


class TestIdempotence:
    @pytest.mark.parametrize("case", IDEMPOTENCE_CASES)
    def test_collector_idempotent(self, case):
        once = ec.mask_secrets(case)
        assert ec.mask_secrets(once) == once

    @pytest.mark.parametrize("case", IDEMPOTENCE_CASES)
    def test_tool_idempotent(self, case):
        once = eq_tool.mask_output(case)
        assert eq_tool.mask_output(once) == once


# ── (iv) RED-инъекция: расхождение наборов обязано ронять тест ──

class TestRedInjection:
    def test_reduced_duplicate_fails_parity(self):
        """Симуляция дрейфа: убрать паттерн у дубликата → паритет красный.

        Проверяем сам механизм: обрезанный набор НЕ равен канону и ведёт
        себя иначе хотя бы на одном семейном кейсе.
        """
        reduced = list(eq_tool._SECRET_PATTERNS)[:4]  # «потеряли» 5-й паттерн

        def mask_reduced(text: str) -> str:
            for rx, repl in reduced:
                text = rx.sub(repl, text)
            return text

        assert len(reduced) < 5  # структурная проверка поймала бы
        diverged = [case for _, case in FAMILY_CASES
                    if ec.mask_secrets(case) != mask_reduced(case)]
        assert diverged, "инъекция обязана расходиться хотя бы на одном кейсе"
