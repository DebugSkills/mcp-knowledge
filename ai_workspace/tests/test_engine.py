"""Тесты Mode engine (Ф3.5b-2): граф, human-gate, CAS, идемпотентные эффекты.

Offline-часть работает на фейках (jobs/boards/llm/mcp) + ``MemoryLedger``;
integration-часть (``-m integration``) поднимает JobStore/BoardStore/RedisLedger
на живом ws-redis (``make ws-up-test`` → 127.0.0.1:6390).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ai_workspace.orchestrator.board import BoardError, StaleBoard
from ai_workspace.orchestrator.engine import (
    MAX_STEPS,
    MemoryLedger,
    ModeEngine,
    TokenInvalid,
    load_mode,
)
from ai_workspace.orchestrator.job import (
    JobRecord,
    JobState,
    JobStore,
    StaleEpoch,
    VersionConflict,
    compute_effect_id,
    job_from_hash,
    job_to_hash,
    validate_transition,
)
from ai_workspace.tests.conftest import requires_redis

FIXTURES = Path(__file__).parent / "fixtures" / "modes"
VALID = FIXTURES / "valid_statya.yaml"
EPOCH = 1


# ── фейки (offline) ──────────────────────────────────────────────────────


class FakeJobs:
    """Мини-реализация job-store с CAS по version и patchable-полями."""

    def __init__(self) -> None:
        self.records: dict[str, JobRecord] = {}

    def create(self, job_id: str, **kw) -> JobRecord:
        rec = JobRecord(
            id=job_id,
            user=kw.get("user", "u1"),
            account_level=kw.get("account_level", "basic"),
            job_class=kw.get("job_class", "interactive"),
            mode=kw.get("mode", "statya"),
            zone=kw.get("zone", "public"),
            state=JobState.QUEUED,
            version=1,
        )
        self.records[job_id] = rec
        return rec

    def get(self, job_id: str) -> JobRecord:
        return self.records[job_id]

    def _cas(self, job_id: str, expect_version: int, epoch: int, patch: dict | None):
        rec = self.records[job_id]
        if rec.version != expect_version:
            raise VersionConflict("fake: version mismatch")
        if epoch < rec.epoch:
            raise StaleEpoch("fake: stale epoch")
        updates = dict(patch or {})
        updates["version"] = rec.version + 1
        updates["epoch"] = epoch
        self.records[job_id] = replace(rec, **updates)
        return self.records[job_id]

    def transition(self, job_id, new_state, *, expect_version, epoch, patch=None) -> JobRecord:
        rec = self.records[job_id]
        validate_transition(rec.state, new_state)
        return self._cas(job_id, expect_version, epoch, {**(patch or {}), "state": JobState(new_state)})

    def patch(self, job_id, *, expect_version, epoch, patch=None) -> JobRecord:
        return self._cas(job_id, expect_version, epoch, patch)


class FakeBoards:
    """Board-store с CAS-версией, владельцами секций и опциональным stale-сбоем."""

    def __init__(self, *, stale_once: bool = False) -> None:
        self.version = 0
        self.sections: dict[str, str] = {}
        self.owners: dict[str, str] = {}
        self.snapshots: dict[int, dict[str, str]] = {}
        self.stale_once = stale_once

    def read(self) -> tuple[int, dict[str, str]]:
        return self.version, dict(self.sections)

    def read_version(self, version: int) -> dict[str, str]:
        if version not in self.snapshots:
            raise BoardError(f"нет снапшота версии {version}")
        return dict(self.snapshots[version])

    def write_sections(self, sections, *, expect_version, writer_node, single_writer=True) -> int:
        if self.stale_once:
            self.stale_once = False
            self.version += 1  # конкурент пишет раньше нас → наш CAS устарел
            raise StaleBoard("fake: stale")
        if expect_version != self.version:
            raise StaleBoard(f"fake: stale (expect={expect_version} cur={self.version})")
        if single_writer:
            for name in sections:
                owner = self.owners.get(name)
                if owner and owner != writer_node:
                    raise BoardError(f"SECTION:{name}")
        self.sections.update(sections)
        for name in sections:
            self.owners[name] = writer_node
        self.version += 1
        self.snapshots[self.version] = dict(self.sections)
        return self.version


class FakeLLM:
    """Скриптованный LLM: role -> очередь ответов; фиксирует вызовы."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def complete(self, *, role, model_class, prompt, inputs) -> str:
        self.calls.append(role)
        queue = self.script.get(role)
        if not queue:
            raise AssertionError(f"нет скриптованного ответа для роли {role!r}")
        return queue.pop(0)


class FakeMCP:
    def __init__(self, result=None) -> None:
        self.result = result if result is not None else {"refs": ["src-deadbeef"]}
        self.calls: list[str] = []

    def call(self, *, tool, args):
        self.calls.append(tool)
        return self.result


def make_engine(script, *, boards=None, ledger=None, mcp=None, graph=None):
    jobs = FakeJobs()
    records = jobs.records
    llm = FakeLLM(script)
    mcp = mcp or FakeMCP()
    ledger = ledger or MemoryLedger()
    boards = boards or FakeBoards()
    engine = ModeEngine(
        jobs=jobs,
        boards=boards,
        graph=graph or load_mode(VALID),
        llm=llm,
        mcp=mcp,
        ledger=ledger,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=EPOCH)
    engine._test = (records, llm, mcp, ledger, boards)  # type: ignore[attr-defined]
    return engine


# ── offline: основной контур ─────────────────────────────────────────────


def test_happy_path_pauses_at_human_gate_and_writes_sections() -> None:
    engine = make_engine({"analyst": ["черновик"], "critic": ["PASS — годно"], "editor": ["документ"]})
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused"
    assert res.node == "publish"
    assert res.resume_token
    assert res.detail == "Утвердить статью?"
    _, _, _, _, boards = engine._test  # type: ignore[attr-defined]
    assert boards.sections["draft"] == "черновик"
    assert boards.sections["verdict"] == "PASS — годно"
    assert boards.sections["document"] == "документ"
    assert boards.sections["document+refs"] == '{"refs": ["src-deadbeef"]}'
    assert engine.jobs.get("j1").state is JobState.WAITING_HUMAN
    assert res.board_version == boards.version


def test_resume_approve_completes_job() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token, decision="approve")

    assert res.status == "done"
    assert engine.jobs.get("j1").state is JobState.DONE


def test_resume_token_is_single_use() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)
    engine.resume("j1", epoch=EPOCH, token=paused.resume_token)

    with pytest.raises(TokenInvalid):
        engine.resume("j1", epoch=EPOCH, token=paused.resume_token)


def test_resume_with_edit_writes_gate_section_via_cas() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token, decision="edit", edit="правка оператора")

    assert res.status == "done"
    _, _, _, _, boards = engine._test  # type: ignore[attr-defined]
    assert boards.sections["publish"] == "правка оператора"
    assert boards.owners["publish"] == "publish"
    assert res.board_version == boards.version


def test_resume_reject_fails_job() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token, decision="reject")

    assert res.status == "failed"
    assert engine.jobs.get("j1").state is JobState.FAILED


def test_done_job_rerun_is_noop() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)
    engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    _, llm, _, _, _ = engine._test  # type: ignore[attr-defined]
    calls_before = len(llm.calls)

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "done"
    assert len(llm.calls) == calls_before  # терминальный job не переигрывается


# ── offline: ревизионная петля критика ───────────────────────────────────


def test_critic_revise_loops_to_analyst_then_passes() -> None:
    engine = make_engine(
        {"analyst": ["черновик v1", "черновик v2"], "critic": ["REVISE — мало фактов", "PASS"], "editor": ["doc"]}
    )
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.node == "publish"
    _, llm, _, ledger, boards = engine._test  # type: ignore[attr-defined]
    assert llm.calls.count("analyst") == 2  # петля вернула черновик на доработку
    assert llm.calls.count("critic") == 2
    assert boards.sections["draft"] == "черновик v2"
    assert ledger.get("j1", "iter:critic") == {"n": 1}


def test_critic_max_iterations_fails_job() -> None:
    doc = load_mode(VALID).doc
    for node in doc["nodes"]:
        if node["id"] == "critic":
            node["max_iterations"] = 1
    from ai_workspace.orchestrator.graph import ModeGraph

    engine = make_engine({"analyst": ["d"], "critic": ["REVISE"], "editor": ["doc"]},
                         graph=ModeGraph(doc))
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "failed"
    assert "max_iterations=1" in res.detail
    assert engine.jobs.get("j1").state is JobState.FAILED


def test_revise_rerun_uses_new_effect_id_and_calls_llm_again() -> None:
    """Идемпотентность не должна «замораживать» ревизию: критика меняет effect_id."""
    ledger = MemoryLedger()
    engine = make_engine({"analyst": ["v1", "v2"], "critic": ["REVISE", "PASS"], "editor": ["doc"]},
                         ledger=ledger)
    engine.run("j1", epoch=EPOCH)

    feedback_changed = any(k[1].startswith("fx:") for k in ledger.kv)
    assert feedback_changed
    # эффект первого прогона analyst кэширован, второй — другой ключ
    keys = [k[1] for k in ledger.kv if k[1].startswith("fx:")]
    assert len(keys) >= 4  # analyst×2 + critic×2 (уникальные effect_id)


# ── offline: отказы и устойчивость ───────────────────────────────────────


def test_missing_input_section_fails_closed() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    engine.jobs.get("j1")
    engine._test[4].sections.pop("brief")  # type: ignore[attr-defined]

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "failed"
    assert "brief" in res.detail


def test_stale_board_retried_once() -> None:
    boards = FakeBoards()
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}, boards=boards)
    boards.stale_once = True  # конкурент пишет ровно перед первым шагом движка

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused"
    assert boards.sections["draft"] == "d"


def test_tool_failure_fails_job() -> None:
    class BoomMCP:
        def call(self, *, tool, args):
            raise RuntimeError("MCP недоступен")

    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}, mcp=BoomMCP())
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "failed"
    assert "citation_attach упал" in res.detail


def test_unsupported_fork_is_fail_loud() -> None:
    from ai_workspace.orchestrator.graph import ModeGraph

    doc = {
        "id": "forky", "version": 1, "shape": "s", "contract": "document",
        "nodes": [
            {"id": "fan", "kind": "fork"},
            {"id": "a", "kind": "llm-step", "role": "analyst", "inputs": [], "outputs": ["x"]},
        ],
        "edges": ["fan->a"],
    }
    engine = make_engine({}, graph=ModeGraph(doc))
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "failed"
    assert "Ф3.8+" in res.detail


def test_max_steps_guard_stops_runaway_graph() -> None:
    from ai_workspace.orchestrator.graph import ModeGraph

    doc = {
        "id": "loop", "version": 1, "shape": "s", "contract": "document",
        "nodes": [{"id": "a", "kind": "llm-step", "role": "analyst", "inputs": [], "outputs": ["x"]}],
        "edges": ["a->a"],
    }
    engine = make_engine({"analyst": ["x"] * (MAX_STEPS + 2)}, graph=ModeGraph(doc))
    res = engine.run("j1", epoch=EPOCH, max_steps=3)

    assert res.status == "stopped"
    assert "max_steps=3" in res.detail


def test_effect_id_is_stable_without_attempt() -> None:
    assert compute_effect_id("j", "n", "llm:x") == compute_effect_id("j", "n", "llm:x")
    assert compute_effect_id("j", "n", "llm:x") != compute_effect_id("j", "n", "llm:y")


def test_paused_job_run_returns_paused_without_llm_calls() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]})
    engine.run("j1", epoch=EPOCH)
    _, llm, _, _, _ = engine._test  # type: ignore[attr-defined]
    before = len(llm.calls)

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.resume_token is None
    assert len(llm.calls) == before


# ── integration: живой ws-redis (job + board + ledger) ───────────────────


@pytest.mark.integration
@requires_redis
def test_integration_engine_full_cycle_on_redis() -> None:
    from ai_workspace.orchestrator.board import BoardStore
    from ai_workspace.orchestrator.ledger import RedisLedger
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    job_id = "test-engine-j1"
    client.delete(f"ws:job:{job_id}", f"ws:board:{job_id}", f"ws:board:{job_id}:owner",
                  f"ws:fx:{job_id}", f"ws:resume:{job_id}")
    for key in client.scan_iter(match=f"ws:board:{job_id}:v:*"):
        client.delete(key)

    jobs = JobStore(client)
    jobs.create(user="u1", account_level="basic", job_class="interactive", mode="statya",
                zone="public", job_id=job_id)
    engine = ModeEngine(
        jobs=jobs,
        boards=BoardStore(client, job_id),
        graph=load_mode(VALID),
        llm=FakeLLM({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}),
        mcp=FakeMCP(),
        ledger=RedisLedger(client),
    )
    engine.seed(job_id, {"brief": "тема"}, epoch=EPOCH)

    paused = engine.run(job_id, epoch=EPOCH)
    assert paused.status == "paused"
    assert job_from_hash(client.hgetall(f"ws:job:{job_id}")).state is JobState.WAITING_HUMAN

    # immutable снапшот версии доски доступен (replay/аудит)
    version, sections = engine.boards.read()
    assert engine.boards.read_version(version)["draft"] == "d"

    done = engine.resume(job_id, epoch=EPOCH, token=paused.resume_token)
    assert done.status == "done"
    assert job_from_hash(client.hgetall(f"ws:job:{job_id}")).state is JobState.DONE

    with pytest.raises(TokenInvalid):
        engine.resume(job_id, epoch=EPOCH, token=paused.resume_token)

    client.delete(f"ws:job:{job_id}", f"ws:board:{job_id}", f"ws:board:{job_id}:owner",
                  f"ws:fx:{job_id}", f"ws:resume:{job_id}")
    for key in client.scan_iter(match=f"ws:board:{job_id}:v:*"):
        client.delete(key)
    assert sections  # каскад секций прочитан (нет ложного пустого прогона)


@pytest.mark.integration
@requires_redis
def test_integration_job_hash_roundtrip_helper() -> None:
    """job_to_hash/job_from_hash — круговой рейс (используется движком в Redis)."""
    rec = JobRecord(id="x", user="u", account_level="l", job_class="c", mode="m", zone="public")
    assert job_from_hash(job_to_hash(rec)).id == "x"
