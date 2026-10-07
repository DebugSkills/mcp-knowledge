"""Ф6 TODO 8 (I12/D2): события приоритет-контура устойчивы к OOM ws-redis.

trace_id: arch-2026-10-05-ai-workspace. При ``--maxmemory 200mb`` +
``noeviction`` (compose.workspace.yml, Ф6 TODO 8) исчерпание памяти ws-redis
отказывает на записывающих командах: ``OOM command not allowed when used
memory > 'maxmemory'``; redis-py маппит это в ``OutOfMemoryError`` — наследник
``RedisError`` (redis-py 5.3.1). До фикса ``emit_event`` глотал только
Connection/Timeout → OOM на XADD ``ws:quota:events`` валил ``QuotaWiring.submit``
ПОСЛЕ взятия conc-резерва admit'ом (резерв утекал до свипа). Теперь любое
``RedisError`` на событии — warning-лог + проглатывание (best-effort:
наблюдение не валит постановку, докстрока wiring._resolve_priority).

Offline-фейк: реальный контракт ``admission.admit`` (Lua-кортеж
``(action, code, detail, reused)``) + ``JobStore`` (hsetnx/hset/hgetall);
живой ws-redis не нужен (интеграция OOM-ветки — test_quota_wiring на живом
redis с maxmemory не гоняется, юнит покрывает точку отказа).
"""

from __future__ import annotations

import logging
from uuid import uuid4

import redis

from ai_workspace.scheduler.prio import emit_event, prio_key
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX
from ai_workspace.tests.test_prio import REGISTRY_DIR


class _OomXaddRedis:
    """Offline ws-redis: admit/create работают, XADD событий → OOM.

    Только методы submit-пути (реальные вызовы admission/job/prio, не
    выдуманный протокол): get (ws:prio override), register_script (ADMIT),
    xadd (ws:quota:events), hsetnx/hset/hgetall (ws:job:{id}).
    """

    def __init__(self, exc: redis.exceptions.RedisError) -> None:
        self._exc = exc
        self.prio: dict[str, str] = {}
        self.jobs: dict[str, dict[str, str]] = {}
        self.xadd_calls = 0

    # ── prio: override-ключ + стрим событий ──────────────────────────────
    def get(self, key: str):
        return self.prio.get(key)

    def xadd(self, *a, **k):
        self.xadd_calls += 1
        raise self._exc

    # ── admission.admit: Lua ADMIT → кортеж (action, code, detail, reused) ─
    def register_script(self, _source):
        return lambda *a, **k: ("allow", None, "", "0")

    # ── JobStore: create (hsetnx + hset) / get (hgetall) ──────────────────
    def hsetnx(self, key, field, value):
        if key in self.jobs:
            return 0
        self.jobs[key] = {field: str(value)}
        return 1

    def hset(self, key, mapping=None, **k):
        slot = self.jobs.setdefault(key, {})
        slot.update({f: str(v) for f, v in (mapping or {}).items()})
        return len(mapping or {})

    def hgetall(self, key):
        return dict(self.jobs.get(key, {}))


def test_emit_event_swallows_oom(caplog) -> None:
    """OOM (redis-py ``OutOfMemoryError`` — реальный класс серверного
    отказа) на XADD глотается с warning: emit_event остаётся best-effort."""
    client = _OomXaddRedis(
        redis.exceptions.OutOfMemoryError(
            "OOM command not allowed when used memory > 'maxmemory'"
        )
    )
    with caplog.at_level(logging.WARNING, logger="ai_workspace.scheduler.prio"):
        emit_event(client, "job_priority_applied", job="j-oom", prio="high")

    assert client.xadd_calls == 1  # XADD звался и упёрся в OOM
    assert "не записано" in caplog.text  # отказ зафиксирован логом, не тилшиной


def test_submit_survives_oom_on_priority_event(caplog) -> None:
    """Ф6 TODO 8: OOM на событии ``job_priority_applied`` (XADD
    ws:quota:events) НЕ валит ``QuotaWiring.submit`` — job создан и
    возвращён; отказ наблюдения — warning в лог."""
    from ai_workspace.registry import Registry
    from ai_workspace.registry.quotas import QuotaRegistry
    from ai_workspace.scheduler.wiring import QuotaWiring

    client = _OomXaddRedis(
        redis.exceptions.RedisError(
            "OOM command not allowed when used memory > 'maxmemory'"
        )
    )
    user = f"{WS_TEST_ID_PREFIX}f6t8-{uuid4().hex[:8]}"
    job = f"{user}-j1"
    client.prio[prio_key(job)] = "high"  # override → source="job" → emit_event
    wiring = QuotaWiring(client, registry=QuotaRegistry(Registry(REGISTRY_DIR)))

    with caplog.at_level(logging.WARNING, logger="ai_workspace.scheduler.prio"):
        rec = wiring.submit(
            user=user,
            account_level="member",
            job_class="interactive",
            mode="statya",
            zone="public",
            job_id=job,
        )

    assert rec.id == job
    assert client.xadd_calls >= 1  # emit ДЕЙСТВИТЕЛЬНО упёрся в OOM (не «не звался»)
    assert "не записано" in caplog.text
