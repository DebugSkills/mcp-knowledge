"""Тесты per-node наблюдения Ф6-a 6a.1: on_node_usage + usage:{node} + max_output_tokens.

Инструмент-минимум измеряемости (план REV.15, секция «Ф6-a»): для каждого узла
режима видны вызовы, cache-hit, токены, символы промпта/выхода, время. Семантика
движка НЕ меняется — только аддитивные наблюдения:

- порт ``on_node_usage`` (best-effort, НЕ валит узел) для llm/tool/critic-узлов;
- персистентный агрегат ``usage:{node_id}`` в ledger (read-modify-write сумм);
- ``max_output_tokens`` в ``DecodingPin`` (единый для обеих полок, parity T/I).

Offline: фейки из ``test_engine.py`` (FakeJobs/FakeBoards/FakeLLM/FakeMCP) +
``MemoryLedger`` — как офлайн-часть test_engine.
"""

from __future__ import annotations

from ai_workspace.conformance import DECODING_PIN, DecodingPin, assert_decoding_pin
from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
from ai_workspace.tests.test_engine import (
    EPOCH,
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

EVENT_KEYS = {
    "job", "node", "kind", "role", "model_class", "shelf", "cached",
    "prompt_chars", "output_chars", "tokens", "wall_s",
}
"""Контракт события on_node_usage (Ф6-a 6a.1): ровно эти ключи, без сюрпризов."""


def make_observed(script, events, *, ledger=None, on_node_usage=None):
    """Движок на фейках + наблюдатель on_node_usage (как make_engine в test_engine)."""
    jobs = FakeJobs()
    llm = FakeLLM(script)
    ledger = ledger or MemoryLedger()
    boards = FakeBoards()
    engine = ModeEngine(
        jobs=jobs,
        boards=boards,
        graph=load_mode(VALID),
        llm=llm,
        mcp=FakeMCP(),
        ledger=ledger,
        on_node_usage=on_node_usage or events.append,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=EPOCH)
    engine._test = (jobs, llm, ledger, boards)  # type: ignore[attr-defined]
    return engine


# ── порт наблюдения: событие на каждый измеримый узел ────────────────────


def test_on_node_usage_collects_events_per_node() -> None:
    events: list[dict] = []
    engine = make_observed(
        {"analyst": ["черновик"], "critic": ["PASS — годно"], "editor": ["документ"]}, events
    )

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.node == "publish"
    by_node = {e["node"]: e for e in events}
    # измеряются llm-step/tool-step/critic-gate; human-gate (publish) — НЕ эмитится
    assert set(by_node) == {"analyst", "critic", "editor", "citer"}
    assert all(set(e) == EVENT_KEYS for e in events)  # контракт формы события

    a = by_node["analyst"]
    assert (a["job"], a["node"], a["kind"]) == ("j1", "analyst", "llm-step")
    assert a["role"] == "analyst" and a["model_class"] == "heavy" and a["shelf"] == "local"
    assert a["cached"] is False
    assert a["tokens"] > 0  # свежий вызов: оценка по факту объёма (~4 симв/токен)
    assert a["prompt_chars"] > 0 and a["output_chars"] == len("черновик")
    assert a["wall_s"] >= 0

    c = by_node["critic"]
    assert c["kind"] == "critic-gate" and c["role"] == "critic"
    assert c["model_class"] == "heavy" and c["tokens"] > 0

    t = by_node["citer"]
    assert t["kind"] == "tool-step" and t["cached"] is False
    assert t["role"] is None and t["model_class"] is None
    assert t["tokens"] == 0  # MCP-вызов не тратит LLM-токены
    assert t["output_chars"] == len('{"refs": ["src-deadbeef"]}')


# ── cache-hit: повторный прогон узла ──────────────────────────────────────


def test_cache_hit_second_visit_emits_cached_true_and_zero_tokens() -> None:
    """Повторный вход в узел с теми же входами: эффект из ledger → cached=True, tokens=0."""
    events: list[dict] = []
    engine = make_observed({"analyst": ["черновик"], "critic": ["PASS"], "editor": ["doc"]}, events)
    node = engine.graph.node("analyst")

    engine._run_node("j1", engine.jobs.get("j1"), node, EPOCH)  # свежий вызов
    engine._run_node("j1", engine.jobs.get("j1"), node, EPOCH)  # повторный: кэш эффекта

    assert len(events) == 2
    first, second = events
    assert first["cached"] is False and first["tokens"] > 0
    assert second["cached"] is True and second["tokens"] == 0  # LLM не вызывался
    assert second["prompt_chars"] == first["prompt_chars"] > 0  # промпт считаем и на кэше
    assert second["output_chars"] == first["output_chars"]

    agg = engine.ledger.get("j1", "usage:analyst")
    assert agg is not None
    assert agg["calls"] == 2 and agg["cached_calls"] == 1  # счётчики суммируются
    assert agg["tokens"] == first["tokens"]  # кэш-попадание токенов не добавляет
    assert agg["prompt_chars"] == first["prompt_chars"] * 2
    assert agg["output_chars"] == first["output_chars"] * 2
    assert agg["wall_s_last"] >= 0
    assert agg["role"] == "analyst" and agg["model_class"] == "heavy" and agg["shelf"] == "local"


# ── персистентный агрегат в ledger ────────────────────────────────────────


def test_ledger_persists_per_node_usage_aggregates() -> None:
    events: list[dict] = []
    engine = make_observed({"analyst": ["черновик"], "critic": ["PASS"], "editor": ["doc"]}, events)

    engine.run("j1", epoch=EPOCH)

    for node_id in ("analyst", "critic", "editor", "citer"):
        agg = engine.ledger.get("j1", f"usage:{node_id}")
        assert agg is not None, node_id
        assert agg["calls"] == 1 and agg["cached_calls"] == 0
        assert agg["prompt_chars"] >= 0 and agg["output_chars"] >= 0
        assert agg["wall_s_last"] >= 0 and agg["shelf"] == "local"
    critic_agg = engine.ledger.get("j1", "usage:critic")
    assert critic_agg["role"] == "critic" and critic_agg["model_class"] == "heavy"
    # агрегат и событие сходятся по токенам единственного вызова
    critic_event = next(e for e in events if e["node"] == "critic")
    assert critic_agg["tokens"] == critic_event["tokens"]
    # job-level счётчик квот не затронут (контур выключен) и fx-кэш на месте
    assert engine.ledger.get("j1", "usage") is None
    assert any(key.startswith("fx:") for _, key in engine.ledger.kv)


# ── best-effort: наблюдатель не валит прогон ─────────────────────────────


def test_callback_exception_does_not_fail_run() -> None:
    def boom(event: dict) -> None:
        raise RuntimeError("наблюдатель упал")

    engine = make_observed(
        {"analyst": ["черновик"], "critic": ["PASS"], "editor": ["doc"]}, [], on_node_usage=boom
    )

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.node == "publish"
    _, llm, ledger, boards = engine._test  # type: ignore[attr-defined]
    assert boards.sections["draft"] == "черновик"  # узлы отработали
    assert llm.calls.count("analyst") == 1
    # персистентный агрегат пишется и при падающем колбэке (независимые каналы)
    assert ledger.get("j1", "usage:analyst")["calls"] == 1


# ── max_output_tokens в DecodingPin (decoding-pin, parity T/I) ───────────


def test_decoding_pin_has_max_output_tokens() -> None:
    params = DECODING_PIN.as_params()
    assert params["max_output_tokens"] == 2048  # консервативный дефолт
    assert_decoding_pin(params)  # канонический пин сам себе соответствует

    custom = DecodingPin(max_output_tokens=512)
    assert custom.as_params()["max_output_tokens"] == 512
    assert_decoding_pin(custom.as_params(), pin=custom)


def test_decoding_pin_params_identical_for_both_shelves() -> None:
    """Parity: heavy (ext-полка) и fast (local-полка) узлы получают ОДИНАКОВЫЕ params.

    Единое значение пина для обеих полок — без per-node веток (иначе parity
    недостоверен). Проверяем на параметрах, ФАКТИЧЕСКИ записанных фейком LLM.
    """
    jobs = FakeJobs()
    llm = FakeLLM({"analyst": ["черновик"], "critic": ["PASS"], "editor": ["doc"]})
    engine = ModeEngine(
        jobs=jobs,
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=llm,
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        decoding=DECODING_PIN,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема"}, epoch=EPOCH)

    engine.run("j1", epoch=EPOCH)

    assert len(llm.params_seen) == 3  # analyst(heavy) + critic(fast) + editor(heavy)
    assert all(p == DECODING_PIN.as_params() for p in llm.params_seen)
    assert all("max_output_tokens" in p for p in llm.params_seen)
