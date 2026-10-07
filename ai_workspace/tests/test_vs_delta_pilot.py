"""Offline-тесты пилота «дельта vs 7B» (Ф0b плана 6a.4): vs_delta_pilot.

Ноль сетевых вызовов: LLM-клиент инжектируется (FakeChatClient под
Protocol ChatClient; Protocol не runtime_checkable, поэтому совместимость
структурная — метод chat(prompt) -> str), реальный make_client либо
подменён, либо в dry-run вообще не вызывается. Живой прогон qwen2.5:7b
на 127.0.0.1:11435 в юнит-тесты не входит.

Попутно зафиксировано ограничение parse_verdict: однострочный код-блок
с вердиктом парсится как None — см. xfail-тест и отчёт Ф0b.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ai_workspace.tools import vs_delta_pilot as vdp
from ai_workspace.tools.vs_delta_pilot import (
    PAIRS,
    ROLE_CONTRACT,
    VARIANTS,
    Pair,
    aggregate,
    diff_sections,
    hypothesis_gate,
    pair_prompt,
    parse_verdict,
    run_variant,
)

if TYPE_CHECKING:
    from pathlib import Path

# Уникальные фрагменты тел ground-truth пары «fixed» (PAIRS[0]):
# unchanged-секции «Обзор» и «Зоны доступа» (тела идентичны в v1/v2),
# changed-секция «Переиндексация» (фраза есть только в теле v2).
UNCHANGED_OVERVIEW = "поисковый индекс Qdrant строится из чанков этих файлов"
UNCHANGED_ZONES = "Поиск по умолчанию ограничен зоной токена"
CHANGED_REINDEX = "затем старая коллекция удаляется"
CRITIQUE_MARK = "Требуется описать blue-green схему"


# ── двойник клиента: очередь заготовленных ответов + счётчик вызовов ──────


class FakeChatClient:
    """Офлайн-двойник ChatClient: ответы выдаются по очереди, вызовы считаются."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []

    def chat(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._replies.pop(0)


# ── строение промптов по вариантам ────────────────────────────────────────


def test_full_prompt_contains_all_section_bodies() -> None:
    """full: неизменённые тела в промпте присутствуют целиком."""
    prompt = pair_prompt("full", PAIRS[0])
    assert UNCHANGED_OVERVIEW in prompt
    assert UNCHANGED_ZONES in prompt
    assert CHANGED_REINDEX in prompt
    assert CRITIQUE_MARK in prompt  # предыдущая критика — во всех вариантах
    assert "### Обзор" in prompt and "### Зоны доступа" in prompt


def test_delta_prompt_excludes_unchanged_section_bodies() -> None:
    """delta: тел неизменённых секций нет; дельта и критика — есть."""
    prompt = pair_prompt("delta", PAIRS[0])
    assert UNCHANGED_OVERVIEW not in prompt
    assert UNCHANGED_ZONES not in prompt
    assert CHANGED_REINDEX in prompt  # тело изменённой секции (версия v2)
    assert CRITIQUE_MARK in prompt
    assert "Секции, не вошедшие в дельту, не менялись." in prompt


def test_delta_map_has_unchanged_headings_without_bodies() -> None:
    """delta-map: заголовки всех секций есть, тел неизменённых — нет."""
    prompt = pair_prompt("delta-map", PAIRS[0])
    assert "- Обзор" in prompt and "- Зоны доступа" in prompt  # карта v2
    assert UNCHANGED_OVERVIEW not in prompt
    assert UNCHANGED_ZONES not in prompt
    assert CHANGED_REINDEX in prompt  # дельта рендерится как в delta


def test_diff_sections_classifies_added_changed_removed() -> None:
    """Секционная дельта: added/changed (тело v2), removed (сортировка)."""
    v1 = {"A": "same", "B": "old", "C": "gone", "E": "e"}
    v2 = {"A": "same", "B": "new", "D": "fresh"}
    assert diff_sections(v1, v2) == {
        "added": {"D": "fresh"},
        "changed": {"B": "new"},
        "removed": ["C", "E"],  # sorted, хотя E добавлена в v1 после C
    }
    assert diff_sections({"X": "x"}, {"X": "x"}) == {
        "added": {},
        "changed": {},
        "removed": [],
    }


@pytest.mark.parametrize(
    ("variant", "pair"),
    [(v, p) for v in VARIANTS for p in PAIRS],
    ids=[f"{v}-{p.name}" for v in VARIANTS for p in PAIRS],
)
def test_prompt_first_line_is_role_contract(variant: str, pair: Pair) -> None:
    """Контракт роли — первая строка: инструкции раньше данных."""
    assert pair_prompt(variant, pair).splitlines()[0] == ROLE_CONTRACT


# ── parse_verdict: контракт первой строки ─────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PASS", "PASS"),
        ("REVISE", "REVISE"),
        ("pass", "PASS"),
        ("Revise", "REVISE"),
        ("  PASS  \n\nОбоснование со второй строки.", "PASS"),
        ("PASS: все замечания учтены", "PASS"),
        ("REVISE — осталась секция «Токены доступа»", "REVISE"),
    ],
)
def test_parse_verdict_plain_tokens(text: str, expected: str) -> None:
    assert parse_verdict(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("```\nPASS\n```", "PASS"),
        ("```text\nREVISE\nОбоснование.\n```", "REVISE"),
        ("```\nPASS\n\nОбоснование после пустой строки.\n```", "PASS"),
        ("`PASS`", "PASS"),
        ("**REVISE**", "REVISE"),
        ("**PASS — ok**", "PASS"),
        ("# PASS", "PASS"),
        ("> REVISE", "REVISE"),
        ("- PASS", "PASS"),
    ],
)
def test_parse_verdict_markdown_wrappers(text: str, expected: str) -> None:
    """Обёртки (блок кода, инлайн-код, жирный, заголовок, цитата) сняты."""
    assert parse_verdict(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Нужны правки в секции «Токены доступа».",
        "MAYBE",
        "",
        "   \n\t\n",
        "```\n```",
        "ПРОШЛО\nPASS",  # токен не в первой строке — контракт нарушен
    ],
)
def test_parse_verdict_rejects_non_verdict_text(text: str) -> None:
    assert parse_verdict(text) is None


@pytest.mark.xfail(
    reason="ограничение parse_verdict: однострочный код-блок ```PASS``` "
    "даёт None — первая строка блока съедается целиком (отчёт Ф0b)",
    strict=True,
)
def test_parse_verdict_single_line_code_fence_limitation() -> None:
    assert parse_verdict("```PASS```") == "PASS"


# ── прогон на fake-клиенте и агрегаты ─────────────────────────────────────


def test_run_variant_with_fake_client_counts_correct() -> None:
    """correct считается против expected; записи полны; вызовы — в fake."""
    pair = PAIRS[0]  # fixed, expected=PASS
    fake = FakeChatClient(["PASS", "REVISE"])
    records = run_variant("full", pair, fake, n=2)
    assert len(records) == 2
    assert len(fake.calls) == 2  # каждый «вызов LLM» ушёл в fake: 0 сети
    prompt_chars = len(pair_prompt("full", pair))
    first, second = records
    assert (first.variant, first.pair, first.expected) == ("full", "fixed", "PASS")
    assert first.verdict == "PASS" and first.correct is True
    assert second.verdict == "REVISE" and second.correct is False
    assert all(rec.prompt_chars == prompt_chars for rec in records)
    assert all(rec.wall_s >= 0.0 for rec in records)

    broke = FakeChatClient(["не знаю, что сказать"])
    rec = run_variant("delta", pair, broke, n=1)[0]
    assert rec.verdict is None and rec.correct is False


def test_aggregate_computes_manual_expectations() -> None:
    """compliance/accuracy/delta_ratio посчитаны вручную и совпадают."""
    pair = PAIRS[0]  # fixed, expected=PASS
    # full: PASS + REVISE -> accuracy 1/2, compliance 2/2
    # delta: PASS + PASS -> accuracy 2/2, compliance 2/2
    records = run_variant("full", pair, FakeChatClient(["PASS", "REVISE"]), n=2)
    records += run_variant("delta", pair, FakeChatClient(["PASS", "PASS"]), n=2)
    agg = aggregate(records)
    full_chars = len(pair_prompt("full", pair))
    delta_chars = len(pair_prompt("delta", pair))
    assert agg["full"]["verdict_compliance"] == pytest.approx(1.0)
    assert agg["full"]["accuracy"] == pytest.approx(0.5)
    assert agg["full"]["mean_prompt_chars"] == pytest.approx(full_chars)
    assert agg["delta"]["verdict_compliance"] == pytest.approx(1.0)
    assert agg["delta"]["accuracy"] == pytest.approx(1.0)
    assert agg["delta"]["mean_prompt_chars"] == pytest.approx(delta_chars)
    assert agg["delta_ratio"] == pytest.approx(delta_chars / full_chars)
    assert agg["delta_ratio"] < 1.0  # дельта компактнее полного документа

    # not-fixed (expected=REVISE): PASS некорректен, «мусор» ломает контракт;
    # без варианта full delta_ratio не появляется вовсе
    solo = run_variant("delta", PAIRS[1], FakeChatClient(["PASS", "мусор"]), n=2)
    agg2 = aggregate(solo)
    assert agg2["delta"]["verdict_compliance"] == pytest.approx(0.5)
    assert agg2["delta"]["accuracy"] == pytest.approx(0.0)
    assert "full" not in agg2
    assert "delta_ratio" not in agg2


def test_hypothesis_gate_branches() -> None:
    """Обе ветки провала -> 2; границы 0.2/0.8 и отсутствие delta -> None."""

    def stats(accuracy: float, compliance: float) -> dict[str, float]:
        return {"accuracy": accuracy, "verdict_compliance": compliance}

    # ветка 1: accuracy(delta) хуже accuracy(full) более чем на 0.2
    assert hypothesis_gate({"full": stats(1.0, 1.0), "delta": stats(0.7, 1.0)}) == 2
    # ветка 2: контракт вердикта delta нарушается чаще 20%
    assert hypothesis_gate({"full": stats(1.0, 1.0), "delta": stats(1.0, 0.79)}) == 2
    # ровно на границах (0.2 и 0.8) гипотеза ещё держится
    assert hypothesis_gate({"full": stats(1.0, 1.0), "delta": stats(0.8, 0.8)}) is None
    # без full база accuracy = 1.0; delta в норме -> гейт пройден
    assert hypothesis_gate({"delta": stats(0.9, 1.0)}) is None
    # нет delta — гейту нечего проверять
    assert hypothesis_gate({"full": stats(1.0, 1.0)}) is None


# ── main: dry-run без сети и полный цикл на инжектированном клиенте ───────


def _refuse_network(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("make_client вызван: сетевой путь запрещён тестом")


def test_main_dry_run_no_network_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run: make_client не вызывается, промпты печатаются, отчёт честный."""
    monkeypatch.setattr(vdp, "make_client", _refuse_network)
    out = tmp_path / "dry-run.json"
    assert vdp.main(["--dry-run", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.count("[PROMPT]") == len(VARIANTS) * len(PAIRS)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert set(payload) >= {"model", "base_url", "dry_run", "n", "records", "aggregate"}
    assert len(payload["records"]) == len(VARIANTS) * len(PAIRS)  # по 1 прогону
    for rec in payload["records"]:
        assert set(rec) == {
            "variant",
            "pair",
            "expected",
            "verdict",
            "correct",
            "prompt_chars",
            "wall_s",
            "kind",
            "escalated",
            "fallback_verdict",
            "delta_sufficient",
        }
        assert rec["kind"] in {"easy", "hard"}
        assert rec["escalated"] is False and rec["fallback_verdict"] is None
        assert rec["verdict"] is None and rec["correct"] is False
        assert rec["wall_s"] == 0.0
    assert set(payload["aggregate"]) == {"full", "delta", "delta-map", "delta_ratio"}
    # delta_ratio есть и в dry-run: mean_prompt_chars не зависит от вердиктов


def test_main_end_to_end_with_injected_fake_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Полный цикл main на fake-клиенте: метрики, correct, JSON, exit 0."""
    # --pairs easy: happy-path exit 0 (PASS-факер на hard-парах дал бы exit 3)
    easy_pairs = [pair for pair in PAIRS if pair.kind == "easy"]
    calls_total = len(VARIANTS) * len(easy_pairs) * 2  # --n по умолчанию = 2
    fake = FakeChatClient(["PASS"] * calls_total)
    monkeypatch.setattr(vdp, "make_client", lambda _model, _base_url: fake)
    out = tmp_path / "e2e.json"
    assert vdp.main(["--pairs", "easy", "--out", str(out)]) == 0
    assert len(fake.calls) == calls_total  # все вызовы LLM — через fake
    payload = json.loads(out.read_text(encoding="utf-8"))
    records = payload["records"]
    assert len(records) == calls_total
    assert all(rec["verdict"] == "PASS" for rec in records)
    fixed = [rec for rec in records if rec["pair"] == "fixed"]
    not_fixed = [rec for rec in records if rec["pair"] == "not-fixed"]
    assert all(rec["correct"] for rec in fixed)  # expected=PASS
    assert not any(rec["correct"] for rec in not_fixed)  # expected=REVISE
    agg = payload["aggregate"]
    for name in VARIANTS:  # fake везде врёт PASS -> accuracy 0.5, контракт 1.0
        assert agg[name]["accuracy"] == pytest.approx(0.5)
        assert agg[name]["verdict_compliance"] == pytest.approx(1.0)
    n_easy = len(easy_pairs)  # delta_ratio считается только по easy-парам
    delta_mean = sum(len(pair_prompt("delta", p)) for p in easy_pairs) / n_easy
    full_mean = sum(len(pair_prompt("full", p)) for p in easy_pairs) / n_easy
    assert agg["delta_ratio"] == pytest.approx(delta_mean / full_mean)

# ── доработка пилота: hard-пары, ESCALATE/fallback, гейты ─────────────────


def test_hard_pairs_present_and_delta_insufficient() -> None:
    """HD1/HD2 входят в hard: kind=hard, delta_sufficient=False, REVISE."""
    hard = vdp.select_pairs("hard")
    assert {p.name for p in hard} == {"hd1-unchanged-defect", "hd2-cross-section"}
    for pair in hard:
        assert pair.kind == "hard"
        assert pair.delta_sufficient is False
        assert pair.expected == "REVISE"
        assert len(pair.doc_v2) == 3  # компактные самодостаточные данные
    easy = vdp.select_pairs("easy")
    assert {p.name for p in easy} == {"fixed", "not-fixed"}
    assert all(p.kind == "easy" and p.delta_sufficient for p in easy)
    assert vdp.select_pairs("all") == PAIRS
    # HD1: дефектная секция не попадает в дельту (правится другая секция)
    hd1_delta = diff_sections(hard[0].doc_v1, hard[0].doc_v2)
    assert set(hd1_delta["changed"]) == {"Артефакты"}
    assert "User Gate" not in hd1_delta["changed"]
    # HD2: конфликтующие секции не в дельте — правится третья секция
    hd2_delta = diff_sections(hard[1].doc_v1, hard[1].doc_v2)
    assert set(hd2_delta["changed"]) == {"Сжатие контекста"}


def test_escalate_token_prompt_rules_and_parser() -> None:
    """ESCALATE: инструкция в delta/delta-map, запрет в full, парсер строг."""
    pair = PAIRS[0]
    for variant in ("delta", "delta-map"):
        prompt = pair_prompt(variant, pair)
        assert "ESCALATE" in prompt
        assert "Если дельты недостаточно для вердикта" in prompt
    full_prompt = pair_prompt("full", pair)
    assert "ESCALATE" in full_prompt and "запрещён" in full_prompt
    # парсер: ESCALATE — не вердикт; принимается только с allow_escalate
    assert vdp.parse_verdict("ESCALATE") is None
    assert vdp.parse_answer("ESCALATE", allow_escalate=True) == "ESCALATE"
    assert vdp.parse_answer("escalate\nмало данных", allow_escalate=True) == "ESCALATE"
    assert vdp.parse_answer("ESCALATE") is None  # full: токен запрещён
    assert vdp.parse_answer("PASS", allow_escalate=True) == "PASS"


def test_escalate_accepted_in_delta_rejected_in_full() -> None:
    """run_variant: ESCALATE в delta -> fallback; в full -> контракт нарушен."""
    pair = vdp.select_pairs("hard")[0]  # hd1: дельта недостаточна по построению
    fake = FakeChatClient(["ESCALATE\nне видно секции «User Gate»", "REVISE"])
    rec = vdp.run_variant("delta", pair, fake, n=1)[0]
    assert rec.escalated is True
    assert len(fake.calls) == 2  # дельта-промпт + full-промпт fallback'а
    assert "## Документ v2 (полностью)" in fake.calls[1]
    assert rec.fallback_verdict == "REVISE" and rec.verdict == "REVISE"
    assert rec.correct is True and rec.kind == "hard"
    assert rec.delta_sufficient is False

    stubborn = FakeChatClient(["ESCALATE\nне хватает контекста"])
    rec_full = vdp.run_variant("full", pair, stubborn, n=1)[0]
    assert rec_full.escalated is False  # full не умеет эскалировать
    assert rec_full.verdict is None  # ESCALATE в full запрещён контрактом
    assert rec_full.fallback_verdict is None


def test_fallback_metrics_in_aggregate() -> None:
    """escalation_rate/accuracy_raw/accuracy_after_fallback после fallback."""
    records = vdp.run_variant(  # fixed (PASS): ESCALATE -> fallback PASS
        "delta", PAIRS[0], FakeChatClient(["ESCALATE", "PASS"]), n=1
    )
    records += vdp.run_variant(  # not-fixed (REVISE): без эскалации
        "delta", PAIRS[1], FakeChatClient(["REVISE"]), n=1
    )
    stats = aggregate(records)["delta"]
    assert stats["escalation_rate"] == pytest.approx(0.5)
    assert stats["accuracy_raw"] == pytest.approx(0.5)  # эскалация = raw-промах
    assert stats["accuracy_after_fallback"] == pytest.approx(1.0)
    assert stats["accuracy"] == pytest.approx(1.0)  # итог = после fallback


def test_delta_ratio_null_when_pairs_hard_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--pairs hard: easy-пар нет -> aggregate delta_ratio = None; exit 0."""
    monkeypatch.setattr(vdp, "make_client", _refuse_network)
    out = tmp_path / "hard.json"
    argv = ["--dry-run", "--variant", "all", "--pairs", "hard", "--out", str(out)]
    assert vdp.main(argv) == 0
    printed = capsys.readouterr().out
    assert printed.count("[PROMPT]") == len(VARIANTS) * 2  # только hd1/hd2
    assert "kind=hard" in printed
    assert "kind=easy" not in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pairs"] == "hard"
    assert payload["aggregate"]["delta_ratio"] is None


def test_exit3_when_full_inaccurate_on_hard_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hard_pair_gate: accuracy(full) на hard-парах < 0.8 -> exit 3."""

    class WrongOnFull:
        """full -> PASS (неверно: hard ждёт REVISE); дельта -> REVISE."""

        def chat(self, prompt: str) -> str:
            is_full = "## Документ v2 (полностью)" in prompt
            return "PASS" if is_full else "REVISE"

    monkeypatch.setattr(vdp, "make_client", lambda _m, _b: WrongOnFull())
    code = vdp.main(["--variant", "all", "--pairs", "hard", "--n", "1"])
    assert code == 3  # full на обеих hard-парах неправ: accuracy 0.0 < 0.8
