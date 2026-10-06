"""Тесты job-store (Ф3.1): unit — чистая статус-машина/кодек/effect_id;
integration (класс TestJobStoreRedis) — живой ws-redis: create/get/transition,
CAS-конфликт, epoch-fencing; авто-skip без WS_REDIS_URL/недоступного Redis."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_workspace.orchestrator.job import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    JobAlreadyExists,
    JobNotFound,
    JobRecord,
    JobState,
    StaleEpoch,
    VersionConflict,
    compute_effect_id,
    job_from_hash,
    job_to_hash,
    validate_transition,
)
from ai_workspace.tests.conftest import requires_redis

# ── UNIT (без Redis) ─────────────────────────────────────────────────────

# Независимая фиксация контракта: если источник меняет таблицу — тест падает
# и заставляет менять её осознанно (вместе с контрактом Mode engine).
EXPECTED_TRANSITIONS = {
    "queued": {"running", "cancelled", "failed"},
    "running": {
        "queued", "waiting_human", "sleeping", "preempted",
        "done", "failed", "cancelled",
    },
    "waiting_human": {"running", "cancelled", "failed"},
    "sleeping": {"running", "cancelled", "failed"},
    "preempted": {"running", "queued", "cancelled", "failed"},
    "done": set(),
    "failed": {"queued"},  # retry по явному решению engine
    "cancelled": set(),
}

ALL_STATES = [s.value for s in JobState]


def test_source_table_matches_contract():
    """ALLOWED_TRANSITIONS (источник) == EXPECTED_TRANSITIONS (тест-контракт)."""
    for st in JobState:
        assert ALLOWED_TRANSITIONS[st] == frozenset(
            JobState(x) for x in EXPECTED_TRANSITIONS[st.value]
        ), f"дрейф таблицы переходов для {st.value}"


def test_transition_matrix_full():
    """Каждая пара (old, new) 8×8: легальная проходит, нелегальная — отказ."""
    for old in ALL_STATES:
        for new in ALL_STATES:
            if new in EXPECTED_TRANSITIONS[old]:
                assert validate_transition(old, new) is None
            else:
                with pytest.raises(IllegalTransition):
                    validate_transition(old, new)


def test_terminal_states_are_absorbing():
    """done/cancelled терминальны: исходящих переходов нет (в т.ч. в себя)."""
    for term in ("done", "cancelled"):
        assert ALLOWED_TRANSITIONS[JobState(term)] == frozenset()
        for new in ALL_STATES:
            with pytest.raises(IllegalTransition):
                validate_transition(term, new)


def test_no_self_transitions():
    for st in ALL_STATES:
        with pytest.raises(IllegalTransition):
            validate_transition(st, st)


def test_validate_transition_coerces_strings():
    assert validate_transition("queued", "running") is None
    with pytest.raises(ValueError, match="zombie"):
        validate_transition("queued", "zombie")


def test_effect_id_deterministic():
    """I4: H(job, node, effect) без attempt — повторная доставка даёт тот же id."""
    a = compute_effect_id("job-1", "node-a", "mcp:write_knowledge")
    b = compute_effect_id("job-1", "node-a", "mcp:write_knowledge")
    assert a == b and len(a) == 64
    assert a != compute_effect_id("job-2", "node-a", "mcp:write_knowledge")
    assert a != compute_effect_id("job-1", "node-b", "mcp:write_knowledge")
    assert a != compute_effect_id("job-1", "node-a", "mcp:delete_knowledge")


def test_record_hash_roundtrip():
    rec = JobRecord(
        id="j1", user="u1", account_level="high", job_class="interactive",
        mode="review", zone="private", state=JobState.WAITING_HUMAN,
        step=3, vft=12.5, retry=1, epoch=4, attempt=2, cursor="step:3",
        board_versions={"board-a": 3, "board-b": 7},
        created="2026-10-06T00:00:00+00:00", updated="2026-10-06T00:01:00+00:00",
        version=9,
    )
    h = job_to_hash(rec)
    assert set(h) == {
        "id", "user", "account_level", "class", "mode", "zone", "state",
        "step", "vft", "retry", "epoch", "attempt", "cursor",
        "board_versions", "created", "updated", "version",
    }
    assert h["class"] == "interactive"  # python job_class ↔ HASH-поле "class"
    assert job_from_hash(h) == rec


def test_decode_defaults_for_partial_hash():
    rec = job_from_hash(
        {"id": "j1", "user": "u", "account_level": "med",
         "class": "batch", "mode": "m", "zone": "private"}
    )
    assert rec.state is JobState.QUEUED
    assert rec.version == 1
    assert rec.board_versions == {}
    assert rec.vft == 0.0


# ── INTEGRATION (живой ws-redis; авто-skip без WS_REDIS_URL) ──────────────


def _tid() -> str:
    return f"test-{uuid4().hex[:10]}"


def _mk(job_store, **kw):
    defaults = {
        "user": "u-test", "account_level": "high", "job_class": "interactive",
        "mode": "review", "zone": "private",
    }
    defaults.update(kw)
    return job_store.create(**defaults)


@pytest.mark.integration
@requires_redis
class TestJobStoreRedis:
    def test_create_get_roundtrip(self, job_store):
        rec = _mk(
            job_store, job_id=_tid(), epoch=3, vft=7.25, cursor="step:0",
            board_versions={"board-a": 3},
        )
        assert rec.state is JobState.QUEUED and rec.version == 1
        loaded = job_store.get(rec.id)
        assert loaded == rec
        assert loaded.epoch == 3 and loaded.vft == 7.25
        assert loaded.board_versions == {"board-a": 3}

    def test_create_duplicate_id_rejected(self, job_store):
        jid = _tid()
        _mk(job_store, job_id=jid)
        with pytest.raises(JobAlreadyExists):
            _mk(job_store, job_id=jid)

    def test_get_missing_raises(self, job_store):
        with pytest.raises(JobNotFound):
            job_store.get("test-nonexistent-000")

    def test_full_lifecycle_with_patch(self, job_store):
        rec = _mk(job_store, job_id=_tid())
        r1 = job_store.transition(
            rec.id, "running", expect_version=1, epoch=0,
            patch={"step": 1, "cursor": "step:1", "board_versions": {"board-a": 2}},
        )
        assert (r1.state, r1.version, r1.step, r1.cursor) == (
            JobState.RUNNING, 2, 1, "step:1",
        )
        assert r1.board_versions == {"board-a": 2}
        r2 = job_store.transition(rec.id, "waiting_human", expect_version=2, epoch=0)
        assert (r2.state, r2.version) == (JobState.WAITING_HUMAN, 3)
        r3 = job_store.transition(rec.id, "running", expect_version=3, epoch=0)
        assert r3.state is JobState.RUNNING
        r4 = job_store.transition(rec.id, "done", expect_version=4, epoch=0)
        assert (r4.state, r4.version) == (JobState.DONE, 5)

    def test_cas_conflict_two_writers(self, job_store):
        """Два писателя с одинаковым expect_version: побеждает один CAS-ом."""
        rec = _mk(job_store, job_id=_tid())
        winner = job_store.transition(
            rec.id, "running", expect_version=1, epoch=0, patch={"step": 1}
        )
        assert winner.version == 2
        with pytest.raises(VersionConflict):
            job_store.transition(rec.id, "sleeping", expect_version=1, epoch=0)
        after = job_store.get(rec.id)
        assert after.state is JobState.RUNNING  # проигравший ничего не изменил
        assert after.version == 2 and after.step == 1

    def test_epoch_fencing_rejects_stale_redelivery(self, job_store):
        """I4: коммит с меньшим epoch отвергнут; запись (и version) не меняются."""
        rec = _mk(job_store, job_id=_tid(), epoch=1)
        taken = job_store.transition(rec.id, "running", expect_version=1, epoch=2)
        assert taken.epoch == 2  # новый владелец поднял fencing-эпоху
        with pytest.raises(StaleEpoch):
            job_store.transition(rec.id, "preempted", expect_version=2, epoch=1)
        after = job_store.get(rec.id)
        assert after.epoch == 2 and after.version == 2  # отказ без записи

    def test_same_epoch_commit_allowed(self, job_store):
        """Коммит с равным epoch (тот же владелец, повтор после конфликта)."""
        rec = _mk(job_store, job_id=_tid(), epoch=5)
        r = job_store.transition(rec.id, "running", expect_version=1, epoch=5)
        assert r.epoch == 5

    def test_illegal_transition_via_store(self, job_store):
        rec = _mk(job_store, job_id=_tid())
        with pytest.raises(IllegalTransition):
            job_store.transition(rec.id, "done", expect_version=1, epoch=0)
        assert job_store.get(rec.id).state is JobState.QUEUED

    def test_protected_patch_field_rejected(self, job_store):
        rec = _mk(job_store, job_id=_tid())
        with pytest.raises(ValueError, match="не патчится"):
            job_store.transition(
                rec.id, "running", expect_version=1, epoch=0, patch={"version": 99}
            )
