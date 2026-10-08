"""Ф6-a 6a.4 Ф1 (В3/в): дельта-контекст в REVISE — структурное правило достаточности.

Стоп-сигнал Ф1.0 (escalation_rate=0.00, ложный PASS на дельте): достаточность
дельты решает ДВИЖОК детерминированно, без опроса модели. Три контура:

1. unit на чистое правило ``decide_context_mode`` (referenced ⊆ changed,
   fail-safe на любой неопределённости);
2. unit на извлечение охвата критики ``_critique_scope`` (структурно:
   inputs критика; правка человека → неопределимо → full);
3. integration на движок (offline-фейки): REVISE-повторный вход узла с
   ``context: delta`` получает изменённые секции + критику; недостаточность,
   правка человека, потеря снапшота, первый прогон → полный контекст.
"""

from __future__ import annotations

from ai_workspace.orchestrator.board import BoardError
from ai_workspace.orchestrator.context_delta import (
    CONTEXT_DELTA,
    CONTEXT_FULL,
    decide_context_mode,
)
from ai_workspace.orchestrator.engine import LLMResult, MemoryLedger, ModeEngine
from ai_workspace.orchestrator.graph import ModeGraph
from ai_workspace.tests.test_engine import FakeBoards, FakeJobs, FakeMCP

EPOCH = 1


# ── фейки (offline) ──────────────────────────────────────────────────────


class DiffingBoards(FakeBoards):
    """FakeBoards + ``diff`` (контракт BoardStore; зеркалит board.py:181)."""

    def diff(self, v_from: int, v_to: int) -> dict[str, tuple[str | None, str | None]]:
        a, b = self.read_version(v_from), self.read_version(v_to)
        names = set(a) | set(b)
        return {n: (a.get(n), b.get(n)) for n in names if a.get(n) != b.get(n)}


class SnapshotLostBoards(DiffingBoards):
    """diff падает (снапшот версии утерян) — движок обязан упасть в full."""

    def diff(self, v_from: int, v_to: int) -> dict[str, tuple[str | None, str | None]]:
        raise BoardError("нет снапшота версии (fake: потеря)")


class RecordingLLM:
    """Скриптованный LLM с фиксацией промпта и inputs каждого вызова."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []

    def complete(self, *, role, model_class, prompt, inputs, params=None,
                 job_id=None) -> LLMResult:
        self.calls.append({"role": role, "prompt": prompt, "inputs": dict(inputs)})
        return LLMResult(output=self.script[role].pop(0))


def delta_mode(*, analyst_context: str | None = "delta",
               critic_inputs: tuple[str, ...] = ("draft",)) -> dict:
    """Минимальный режим analyst→critic→editor с опт-ином дельты на analyst."""
    analyst = {
        "id": "analyst", "kind": "llm-step", "role": "analyst", "model_class": "heavy",
        "inputs": ["brief"], "outputs": ["draft"],
    }
    if analyst_context is not None:
        analyst["context"] = analyst_context
    return {
        "id": "delta-pilot", "version": 1, "shape": "analyst-critic-editor",
        "contract": "document",
        "nodes": [
            analyst,
            {"id": "critic", "kind": "critic-gate", "role": "critic", "model_class": "heavy",
             "inputs": list(critic_inputs), "verdicts": ["PASS", "REVISE"],
             "max_iterations": 3, "on_revise": "analyst"},
            {"id": "editor", "kind": "llm-step", "role": "editor", "model_class": "heavy",
             "inputs": ["draft", "verdict"], "outputs": ["document"]},
        ],
        "edges": ["analyst->critic", "critic|PASS->editor"],
    }


def gated_mode() -> dict:
    """Режим формы statya-private: structure-gate с on_edit → analyst."""
    doc = delta_mode()
    doc["nodes"].insert(1, {
        "id": "structure", "kind": "human-gate", "actor": "operator",
        "prompt": "Утвердить структуру?", "timeout": "24h", "on_timeout": "sleep",
        "on_approve": "critic", "on_edit": "analyst",
    })
    doc["edges"] = ["analyst->structure", "structure->critic", "critic|PASS->editor"]
    return doc


def make_engine(doc: dict, script: dict[str, list[str]], *, seed: dict[str, str] | None = None,
                boards=None):
    jobs, board_store, ledger = FakeJobs(), boards or DiffingBoards(), MemoryLedger()
    engine = ModeEngine(
        jobs=jobs, boards=board_store, graph=ModeGraph(doc),
        llm=RecordingLLM(script), mcp=FakeMCP(), ledger=ledger,
    )
    engine.jobs.create("j1")
    engine.seed("j1", seed or {"brief": "тема: дельта"}, epoch=EPOCH)
    engine._test = (jobs, board_store, ledger)  # type: ignore[attr-defined]
    return engine


def analyst_calls(engine: ModeEngine):
    return [c for c in engine.llm.calls if c["role"] == "analyst"]


# ── 1. чистое правило достаточности ──────────────────────────────────────


def test_rule_uncovered_reference_means_full() -> None:
    """referenced={a,c}, changed={a} → full (критика касается неизменённого)."""
    assert decide_context_mode(frozenset({"a"}), frozenset({"a", "c"})) == CONTEXT_FULL


def test_rule_exact_cover_means_delta() -> None:
    """referenced={a,c}, changed={a,c} → delta."""
    assert decide_context_mode(frozenset({"a", "c"}), frozenset({"a", "c"})) == CONTEXT_DELTA


def test_rule_extra_changes_still_delta() -> None:
    """referenced={a}, changed={a,b}: лишние изменения не мешают дельте."""
    assert decide_context_mode(frozenset({"a", "b"}), frozenset({"a"})) == CONTEXT_DELTA


def test_rule_no_previous_state_is_full() -> None:
    """changed=None (нет предыдущей версии доски) → full при любом referenced."""
    assert decide_context_mode(None, frozenset({"a"})) == CONTEXT_FULL


def test_rule_unparseable_reference_is_full() -> None:
    """referenced=None (охват критики не извлекается) → full (fail-safe)."""
    assert decide_context_mode(frozenset({"a"}), None) == CONTEXT_FULL


def test_rule_empty_reference_is_full() -> None:
    """referenced=∅ → full: «вакуумная истинность» исключена (нет критики — нет дельты)."""
    assert decide_context_mode(frozenset({"a"}), frozenset()) == CONTEXT_FULL


def test_rule_empty_changed_is_full() -> None:
    """changed=∅ при непустом referenced → full."""
    assert decide_context_mode(frozenset(), frozenset({"a"})) == CONTEXT_FULL


# ── 2. извлечение охвата критики (структурное, консервативное) ───────────


def test_critique_scope_is_critic_inputs() -> None:
    """Охват критики = inputs узла-критика (структурный факт графа)."""
    engine = make_engine(delta_mode(), {"analyst": ["v1"], "critic": ["PASS"], "editor": ["doc"]})
    analyst = engine.graph.node("analyst")
    assert engine._critique_scope(analyst, frozenset({"verdict"})) == frozenset({"draft"})


def test_critique_scope_critic_without_inputs_is_none() -> None:
    """Критик без объявленных inputs видел весь борд → охват неопределим → full."""
    engine = make_engine(
        delta_mode(critic_inputs=()),
        {"analyst": ["v1"], "critic": ["PASS"], "editor": ["doc"]},
    )
    analyst = engine.graph.node("analyst")
    assert engine._critique_scope(analyst, frozenset({"verdict"})) is None


def test_critique_scope_human_edit_is_none() -> None:
    """Правка человека — свободная форма, ссылок на секции нет → full."""
    engine = make_engine(gated_mode(), {"analyst": ["v1"], "critic": ["PASS"], "editor": ["doc"]})
    analyst = engine.graph.node("analyst")
    assert engine._critique_scope(analyst, frozenset({"structure"})) is None


def test_critique_scope_stale_critique_ignored() -> None:
    """Критика вне changed (уже потреблённая) не ограничивает дельту."""
    engine = make_engine(gated_mode(), {"analyst": ["v1"], "critic": ["PASS"], "editor": ["doc"]})
    analyst = engine.graph.node("analyst")
    assert engine._critique_scope(analyst, frozenset({"draft", "verdict"})) == frozenset({"draft"})


# ── 3. движок: REVISE-повторный вход с context: delta ────────────────────


def test_delta_revise_gets_changed_sections_plus_critique() -> None:
    """Достаточная дельта: повторный вход analyst видит {draft, verdict}, НЕ brief."""
    engine = make_engine(
        delta_mode(),
        {"analyst": ["v1", "v2"], "critic": ["REVISE — слабо", "PASS"], "editor": ["doc"]},
    )
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "done"
    calls = analyst_calls(engine)
    assert len(calls) == 2
    assert calls[0]["inputs"] == {"brief": "тема: дельта"}  # первый прогон — полный
    assert set(calls[1]["inputs"]) == {"draft", "verdict"}  # дельта: изменённые + критика
    assert "## brief" not in calls[1]["prompt"]
    assert "## draft" in calls[1]["prompt"] and "## verdict" in calls[1]["prompt"]
    _, _, ledger = engine._test  # type: ignore[attr-defined]
    assert ledger.get("j1", "board_seen:analyst") is not None  # маркер чтения доски


def test_delta_insufficient_scope_falls_back_to_full() -> None:
    """Критика охватывает неизменённую секцию (refs) → полный контекст (HD1-паттерн)."""
    engine = make_engine(
        delta_mode(critic_inputs=("draft", "refs")),
        {"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"], "editor": ["doc"]},
        seed={"brief": "тема", "refs": "источники v1"},
    )
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "done"
    calls = analyst_calls(engine)
    assert "brief" in calls[1]["inputs"]  # full: refs не изменилась → дельты нет
    assert "refs" not in calls[1]["inputs"]  # полный контекст = declared inputs + критика


def test_delta_first_run_is_full() -> None:
    """Нет предыдущего состояния доски → первый вход полный (0 изменений)."""
    engine = make_engine(
        delta_mode(), {"analyst": ["v1"], "critic": ["PASS"], "editor": ["doc"]},
    )
    engine.run("j1", epoch=EPOCH)
    assert analyst_calls(engine)[0]["inputs"] == {"brief": "тема: дельта"}


def test_delta_off_by_default() -> None:
    """Без поля context — текущее поведение: повторный вход получает declared inputs."""
    engine = make_engine(
        delta_mode(analyst_context=None),
        {"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"], "editor": ["doc"]},
    )
    engine.run("j1", epoch=EPOCH)
    calls = analyst_calls(engine)
    assert "brief" in calls[1]["inputs"]  # режим по умолчанию = full


def test_delta_snapshot_loss_fails_safe_to_full() -> None:
    """diff падает (снапшот утерян) → полный контекст, прогон не рушится."""
    engine = make_engine(
        delta_mode(),
        {"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"], "editor": ["doc"]},
        boards=SnapshotLostBoards(),
    )
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "done"
    calls = analyst_calls(engine)
    assert "brief" in calls[1]["inputs"]  # fail-safe: полный контекст


def test_delta_after_human_edit_is_full_then_delta_after_revise() -> None:
    """Правка человека → full; следующая REVISE-итерация → дельта (правка потреблена)."""
    engine = make_engine(
        gated_mode(),
        {"analyst": ["v1", "v2", "v3"], "critic": ["REVISE", "PASS"], "editor": ["doc"]},
    )
    paused = engine.run("j1", epoch=EPOCH)  # analyst v1 → structure-gate

    res_edit = engine.resume("j1", epoch=EPOCH, token=paused.resume_token,
                             decision="edit", edit="правка оператора")
    assert res_edit.status == "paused"  # analyst v2 → structure-gate снова
    calls = analyst_calls(engine)
    assert set(calls[1]["inputs"]) == {"brief", "structure"}  # правка человека → full

    res_done = engine.resume("j1", epoch=EPOCH, token=res_edit.resume_token, decision="approve")
    assert res_done.status == "done"  # critic REVISE → analyst v3 (дельта) → PASS → editor
    calls = analyst_calls(engine)
    assert set(calls[2]["inputs"]) == {"draft", "verdict"}  # дельта: старая правка не мешает
    assert "## brief" not in calls[2]["prompt"]


def test_context_mode_changes_effect_cache_namespace() -> None:
    """I4-факт: digest эффекта включает inputs ⇒ delta/full не делят кэш эффектов."""
    script = {"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"], "editor": ["doc"]}
    engine_delta = make_engine(delta_mode(), dict(script))
    engine_full = make_engine(delta_mode(analyst_context=None), dict(script))
    engine_delta.run("j1", epoch=EPOCH)
    engine_full.run("j1", epoch=EPOCH)

    fx_delta = {k[1] for k in engine_delta.ledger.kv if k[1].startswith("fx:")}
    fx_full = {k[1] for k in engine_full.ledger.kv if k[1].startswith("fx:")}
    assert fx_delta != fx_full  # повторный вход analyst дал разные эффекты


def test_critic_gate_never_uses_delta() -> None:
    """critic-gate всегда на полном контексте (protected-принцип, стоп-сигнал Ф1.0)."""
    doc = delta_mode()
    doc["nodes"][1]["context"] = "delta"  # незаконный опт-ин (L15) — движок глух к нему
    engine = make_engine(doc, {"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"],
                               "editor": ["doc"]})
    engine.run("j1", epoch=EPOCH)

    critic_calls = [c for c in engine.llm.calls if c["role"] == "critic"]
    assert len(critic_calls) == 2
    for call in critic_calls:
        assert set(call["inputs"]) == {"draft"}  # полный declared-контекст критика
