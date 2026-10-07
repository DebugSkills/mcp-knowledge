"""Тесты лестницы распознавания вердикта критика (Ф6-a 6a.2c, fail-closed).

Живой пилот 6a.2b (``plans/_provenance/arch-2026-10-05-ai-workspace/
Ф6a-2b-vp-pilot.md``) показал: слабая модель не выдаёт литеральный
PASS/REVISE первой строкой — near-miss ``**VERDICT: REVISE**`` валит
``engine._parse_verdict`` → секция verdict не пишется → документ не
достигается (done=0 во всех вариантах). 6a.2c расширяет парсер лестницей
из 3 шагов при полном сохранении fail-closed: синонимов сверх ``verdicts``
узла не изобретается, проза рубрики не «выдёргивается».
"""

from __future__ import annotations

import pytest

from ai_workspace.orchestrator.engine import ModeEngine, NodeFailure
from ai_workspace.orchestrator.graph import Node

NODE = Node(id="critic", kind="critic-gate", spec={"verdicts": ["PASS", "REVISE"]})


def parse(text: str) -> str:
    return ModeEngine._parse_verdict(NODE, text)


# ── шаг 1: первый токен строки (поведение до 6a.2c сохранено) ──────────────


def test_step1_bare_token_first_line() -> None:
    assert parse("PASS\nРУБРИКА: полнота 1.0") == "PASS"


def test_step1_markdown_wrapped_token() -> None:
    assert parse("**PASS**\nрубрика") == "PASS"


def test_step1_verdict_word_opens_line() -> None:
    assert parse("REVISE — согласованность 0.5") == "REVISE"


# ── шаг 2: «VERDICT:/ВЕРДИКТ: <токен>» — near-miss живого пилота 6a.2b ─────


def test_step2_english_verdict_prefix() -> None:
    """Живой near-miss 7B из 6a.2b (metodichka/g02): «**VERDICT: REVISE** …»."""
    assert parse("**VERDICT: REVISE**\n### Рубрика | Критерий | Балл |") == "REVISE"


def test_step2_russian_prefix_mixed_case() -> None:
    assert parse("Вердикт: PASS\nРУБРИКА: …") == "PASS"


def test_step2_heading_and_dash_separator() -> None:
    assert parse("### ВЕРДИКТ — REVISE") == "REVISE"


def test_step2_returns_canonical_register() -> None:
    assert parse("verdict: revise") == "REVISE"
    assert parse("**Verdict: PaSs**") == "PASS"


def test_step2_unknown_token_fails_closed() -> None:
    with pytest.raises(NodeFailure):
        parse("VERDICT: PASSED")  # длинное слово — не PASS
    with pytest.raises(NodeFailure):
        parse("VERDICT: APPROVE")  # синонимы сверх verdicts не изобретаются


# ── шаг 3: ровно один отдельно стоящий токен (фолбэк всего вывода) ─────────


def test_step3_decorated_standalone_token() -> None:
    assert parse("Конечно, вот разбор.\n\n- PASS") == "PASS"


def test_step3_two_different_standalone_tokens_ambiguous() -> None:
    with pytest.raises(NodeFailure):
        parse("разбор\n- PASS\n— REVISE")


# ── гварды: проза рубрики не выдёргивается шагами 2–3 ──────────────────────


def test_rubric_prose_revise_does_not_beat_standalone_pass() -> None:
    """REVISE упомянут в прозе РАНЬШЕ отдельного PASS — побеждает PASS."""
    text = (
        "| Согласованность | 0.5 | раздел 2 противоречит введению, нужен REVISE |\n"
        "\n"
        "PASS\n"
        "| Полнота | 1.0 | всё на месте |"
    )
    assert parse(text) == "PASS"


def test_midline_verdict_prefix_in_prose_ignored() -> None:
    """«VERDICT: REVISE» в середине строки прозы — не вердиктная строка."""
    text = (
        "Замечание: в прошлом отзыве было VERDICT: REVISE, теперь устранено.\n"
        "\n"
        "PASS"
    )
    assert parse(text) == "PASS"


# ── fail-closed: отказы ─────────────────────────────────────────────────────


def test_preamble_without_verdict_fails() -> None:
    with pytest.raises(NodeFailure):
        parse(
            "Конечно! Вот улучшенная версия раздела с учётом ваших предложений:\n"
            "— текст без вердикта"
        )


def test_empty_output_fails() -> None:
    with pytest.raises(NodeFailure):
        parse("")
    with pytest.raises(NodeFailure):
        parse("\n \n")


def test_verdict_marker_without_token_fails() -> None:
    """Живой near-miss 7B из 6a.2b (statya, metodichka): «**VERDICT**» без токена."""
    with pytest.raises(NodeFailure):
        parse("**VERDICT**\n### РУБРИКА | Критерий | Балл 0–1 | Комментарий |")


def test_marker_line_then_bare_token_on_later_line() -> None:
    """«**VERDICT**»-заголовок + токен отдельной строкой ниже = валидный вердикт."""
    assert parse("**VERDICT**\n\nREVISE\n### Рубрика") == "REVISE"
