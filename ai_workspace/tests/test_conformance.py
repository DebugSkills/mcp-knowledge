"""Тесты conformance-гейта (Ф3.9): T/I/Q, decoding-pin, parity, zone→egress (red-first).

Слои паттерна «Local-First Conformance Gate»:
- **I**: decoding-pin (temp=0/seed/thinking=off) и prompt-hash parity обеих полок;
- **Q**: golden-set → ОТЧЁТ (порог Q-floor по зоне, маркер `local-draft` ниже порога, N≥2);
- **T**: zone→egress unit — двойной ассерт (отказ + счётчик ext==0) + детекция мутации.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.conformance import (
    DECODING_PIN,
    FLOOR_DECIDE_BY,
    FLOOR_OWNER,
    MARKER_DRAFT,
    MARKER_FINAL,
    Q_FLOOR,
    ConformanceError,
    DecodingPin,
    DecodingPinViolation,
    EgressViolation,
    ExtStub,
    QReport,
    assert_decoding_pin,
    assert_no_ext_egress,
    guarded_route,
    prompt_hash,
    prompt_parity,
    q_floor_for,
)

GOLDEN = Path(__file__).parent / "golden" / "golden-set.yaml"


def _golden() -> dict:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def _coverage(answer: str, keywords: list[str]) -> float:
    """Детерминированный скорер golden-задания: доля покрытых ключевых слов."""
    low = answer.lower()
    hit = sum(1 for k in keywords if k.lower() in low)
    return hit / len(keywords)


# ── I: decoding-pin (P1-1) ───────────────────────────────────────────────


def test_decoding_pin_accepts_canonical_params() -> None:
    assert_decoding_pin(DECODING_PIN.as_params())  # не бросает


@pytest.mark.parametrize(
    "params",
    [
        {"temperature": 0.7, "seed": 42, "thinking": False},
        {"temperature": 0.0, "seed": None, "thinking": False},
        {"temperature": 0.0, "seed": 42, "thinking": True},
        {"temperature": 0.0},  # seed/thinking отсутствуют
    ],
)
def test_decoding_pin_rejects_unpinned_params(params: dict) -> None:
    with pytest.raises(DecodingPinViolation):
        assert_decoding_pin(params)


def test_custom_pin_is_honoured() -> None:
    pin = DecodingPin(temperature=0.2, seed=7, thinking=True)
    assert_decoding_pin(pin.as_params(), pin=pin)
    with pytest.raises(DecodingPinViolation):
        assert_decoding_pin(DECODING_PIN.as_params(), pin=pin)


# ── I: prompt-hash parity (P1-3, R2) ─────────────────────────────────────


def test_prompt_parity_holds_for_model_agnostic_prompt() -> None:
    report = prompt_parity("Ты — редактор. Собери статью из черновика.")

    assert report.equal
    assert set(report.hashes) == {"local", "ext"}
    assert report.mismatch() == []


def test_prompt_hash_normalizes_trailing_whitespace() -> None:
    assert prompt_hash("строка  \nвторая\t\n\n") == prompt_hash("строка\nвторая")


def test_model_specific_branch_breaks_parity() -> None:
    """Мутация: 7B-хак только для local → parity падает (иначе гейт бесполезен)."""
    def resolve(shelf: str, text: str) -> str:
        return text + ("\nОтвечай кратко." if shelf == "local" else "")

    report = prompt_parity("Собери статью.", resolve=resolve)

    assert not report.equal
    assert report.mismatch() == ["ext"]


def test_prompt_parity_over_golden_set() -> None:
    """Каждый промпт golden-set model-agnostic (parity обеих полок)."""
    for task in _golden()["tasks"]:
        assert prompt_parity(task["prompt"]).equal, task["id"]


# ── Q: golden-set + Q-floor + маркер (P0-1, R3, R6) ──────────────────────


def test_golden_set_is_versioned_and_well_formed() -> None:
    doc = _golden()

    assert isinstance(doc["version"], int) and doc["version"] >= 1
    assert 5 <= len(doc["tasks"]) <= 10
    ids = [t["id"] for t in doc["tasks"]]
    assert len(ids) == len(set(ids))
    for task in doc["tasks"]:
        assert task["zone"] in {"public", "private"}
        assert task["prompt"] and task["expect_keywords"]
    assert doc["min_runs"] >= 2


def test_golden_floors_match_conformance_defaults() -> None:
    """SSOT порогов: golden-set и conformance не должны расходиться (R3)."""
    doc = _golden()

    assert doc["floor"] == Q_FLOOR
    assert doc["owner"] == FLOOR_OWNER
    assert doc["decide_by"] == FLOOR_DECIDE_BY


def test_q_report_marks_local_draft_below_floor() -> None:
    report = QReport()
    weak = report.add("g01-structure", "local", "public", [0.4, 0.5])   # < 0.80
    strong = report.add("g01-structure", "ext", "public", [0.9, 0.95])
    private_weak = report.add("g05-private-article", "local", "private", [0.7, 0.75])  # < 0.85

    assert weak.marker == MARKER_DRAFT and not weak.passed
    assert strong.marker == MARKER_FINAL and strong.passed
    assert private_weak.marker == MARKER_DRAFT
    table = report.to_table()
    assert "local-draft" in table and "| задание |" in table and "| final |" in table


def test_q_floor_is_zone_specific() -> None:
    assert q_floor_for("public") < q_floor_for("private")
    assert q_floor_for("неизвестная") == max(Q_FLOOR.values())  # строгий по умолчанию


def test_q_report_requires_at_least_two_runs() -> None:
    report = QReport()

    with pytest.raises(ConformanceError):
        report.add("g01", "local", "public", [0.9])


def test_q_report_flags_variability() -> None:
    report = QReport()
    report.add("g01", "local", "public", [0.5, 0.95])   # разброс 0.45
    report.add("g02", "local", "public", [0.90, 0.91])

    flagged = [r.task_id for r in report.flagged]
    assert flagged == ["g01"]


def test_q_report_is_data_not_assert() -> None:
    """Q — non-parity: слабый local НЕ бросает, а помечается маркером."""
    report = QReport()
    row = report.add("g05-private-article", "local", "private", [0.2, 0.3])

    assert row.marker == MARKER_DRAFT
    assert report.by_shelf()["local"] == pytest.approx(0.25)


def test_golden_run_produces_q_report_for_both_shelves() -> None:
    """Прогон golden-set стаб-ответами: отчёт по обеим полкам + маркеры."""
    doc = _golden()
    answers = {"local": "структура и разделы MCP", "ext": "структура, разделы, MCP, семафор, VRAM, K"}
    report = QReport(min_runs=doc["min_runs"], variability_flag=0.15)

    for task in doc["tasks"]:
        for shelf in ("local", "ext"):
            score = _coverage(answers[shelf], task["expect_keywords"])
            report.add(task["id"], shelf, task["zone"], [score, score])

    assert len(report.rows) == len(doc["tasks"]) * 2
    assert set(report.by_shelf()) == {"local", "ext"}
    assert all(r.marker in {MARKER_FINAL, MARKER_DRAFT} for r in report.rows)


# ── T: zone→egress (P1-4, R1) — red-first ────────────────────────────────


def test_private_never_egresses_to_ext() -> None:
    ext = ExtStub()

    assert_no_ext_egress("private", "ext", ext)  # отказ + счётчик 0

    assert ext.calls == 0 and ext.seen == []
    with pytest.raises(EgressViolation):
        guarded_route("ext", "private", ext=ext)


def test_private_on_local_is_allowed_without_egress() -> None:
    ext = ExtStub()

    assert_no_ext_egress("private", "local", ext)

    assert ext.calls == 0


def test_public_route_reaches_ext_counter() -> None:
    ext = ExtStub()

    assert_no_ext_egress("public", "ext", ext)

    assert ext.calls == 1  # egress состоялся — до счётчика


def test_mutation_without_zone_predicate_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red-first: если зонный предикат снят, ассерт обязан упасть."""
    from ai_workspace import conformance

    ext = ExtStub()

    def unguarded_route(shelf: str, zone: str, *, ext: ExtStub | None = None) -> str:
        if ext is not None and shelf != "local":
            ext.call(shelf=shelf)
        return shelf  # зональной проверки НЕТ — мутация предиката

    monkeypatch.setattr(conformance, "guarded_route", unguarded_route, raising=True)

    with pytest.raises(EgressViolation, match="мутация зонного предиката"):
        assert_no_ext_egress("private", "ext", ext)
    assert ext.calls == 1  # мутация действительно «утянула» egress в ext
