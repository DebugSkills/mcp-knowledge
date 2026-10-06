"""Integration-тесты scheduler/slots.lua (Ф3.3a) — живой ws-redis.

Ключевые доказательства:
- K-инвариант: K acquire ок, K+1 = ОТКАЗ без побочных эффектов (holders/lease/
  stream не изменились) — «отказ, не очередь» (I1);
- ровно ОДНО событие на успешный acquire (I3);
- release/heartbeat/reclaim_expired; совместимость с consumer-group.
"""

from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest

from ai_workspace.scheduler.slots import Slots
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


@pytest.fixture()
def slots():
    """Slots на изолированной полке test-f33a-*; уборка только своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    shelf = f"{WS_TEST_ID_PREFIX}f33a-{uuid4().hex[:8]}"
    s = Slots(make_ws_redis(), shelf=shelf, k=2, lease_ttl_ms=5_000)
    yield s
    keys = list(s.client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        s.client.delete(*keys)


def _xlen(client, key: str) -> int:
    """XLEN с 0 для отсутствующего стрима (redis-py кидает на missing key)."""
    return int(client.xlen(key)) if client.exists(key) else 0


def test_k_invariant_refusal_has_no_side_effects(slots: Slots):
    """K acquire проходят; K+1 — False, при этом holders/lease/stream
    НЕ изменились (отказ ДО любых записей)."""
    assert slots.acquire("j1:0:0") is True
    assert slots.acquire("j1:1:0") is True
    used, xl = slots.used(), _xlen(slots.client, slots.stream_key)
    assert used == 2 and xl == 2
    assert slots.acquire("j1:2:0") is False  # сверх K
    assert slots.used() == used  # holders не вырос
    assert _xlen(slots.client, slots.stream_key) == xl  # события НЕ писалось
    assert slots.client.exists(slots.lease_key("j1:2:0")) == 0  # lease не создан


def test_release_frees_and_is_idempotent(slots: Slots):
    """release освобождает слот; повторный release — no-op без события."""
    assert slots.acquire("a:0:0") is True
    assert slots.acquire("b:0:0") is True
    assert slots.release("a:0:0") is True
    assert slots.used() == 1
    xl = _xlen(slots.client, slots.stream_key)
    assert slots.release("a:0:0") is False  # уже не держим
    assert _xlen(slots.client, slots.stream_key) == xl  # no-op не пишет событие
    assert slots.acquire("c:0:0") is True  # освобождённый слот переиспользован


def test_heartbeat_extends_only_own_lease(slots: Slots):
    """heartbeat продлевает свой lease (TTL растёт) и отвергает чужой call."""
    assert slots.acquire("a:0:0") is True
    t1 = slots.client.pttl(slots.lease_key("a:0:0"))
    time.sleep(0.05)
    assert slots.heartbeat("a:0:0") is True
    t2 = slots.client.pttl(slots.lease_key("a:0:0"))
    assert t2 > t1 - 40  # TTL восстановлен, не истёк
    assert slots.heartbeat("other:0:0") is False


def test_reclaim_expired_returns_slot(slots: Slots):
    """Мёртвый lease (истёк) → reclaim возвращает слот + событие; повторно — False."""
    from ai_workspace.redis_client import make_ws_redis

    s = Slots(make_ws_redis(), shelf=slots.shelf, k=1, lease_ttl_ms=1)
    assert s.acquire("dead:0:0") is True
    time.sleep(0.02)  # lease (1 ms) истёк
    assert s.client.exists(s.lease_key("dead:0:0")) == 0
    assert s.reclaim_expired("dead:0:0") is True
    assert s.used() == 0
    assert s.reclaim_expired("dead:0:0") is False  # уже не в holders


def test_exactly_one_event_per_acquire(slots: Slots):
    """Успешный acquire пишет ровно одно корректное событие (не 0 и не 2)."""
    before = _xlen(slots.client, slots.stream_key)
    assert slots.acquire("j9:3:1", job="j9", epoch=2) is True
    after = _xlen(slots.client, slots.stream_key)
    assert after - before == 1
    _id, fields = slots.client.xrange(slots.stream_key)[-1]
    ev = json.loads(fields["event"])
    assert ev["type"] == "acquired" and ev["call"] == "j9:3:1"
    assert ev["job"] == "j9" and ev["epoch"] == 2 and ev["shelf"] == slots.shelf


def test_stream_maxlen_approx():
    """XADD MAXLEN ~: после многих событий XLEN ограничен (с запасом)."""
    from ai_workspace.redis_client import make_ws_redis

    shelf = f"{WS_TEST_ID_PREFIX}f33a-ml-{uuid4().hex[:6]}"
    s = Slots(make_ws_redis(), shelf=shelf, k=1, stream_maxlen=10)
    try:
        for i in range(60):
            assert s.acquire(f"j:{i}:0") is True
            assert s.release(f"j:{i}:0") is True
        # `MAXLEN ~` — ПРИБЛИЗИТЕЛЬНАЯ обрезка по макро-узлам (дефолт
        # stream-node-max-entries=100): XLEN сильно меньше 120 записанных
        # событий, но не обязан равняться ~10.
        xl = _xlen(s.client, s.stream_key)
        assert xl < 120, f"обрезка не сработала: XLEN={xl}"
    finally:
        keys = list(s.client.scan_iter(match=f"ws:*{shelf}*"))
        if keys:
            s.client.delete(*keys)


def test_event_stream_consumer_group_compatible(slots: Slots):
    """Producer совместим с потребителем: XGROUP CREATE + XREADGROUP + XACK."""
    assert slots.acquire("cg:0:0") is True
    group, consumer = "engine", "engine-1"
    slots.client.xgroup_create(slots.stream_key, group, id="0", mkstream=True)
    resp = slots.client.xreadgroup(group, consumer, {slots.stream_key: ">"}, count=10)
    assert resp, "XREADGROUP не вернул события"
    _stream, entries = resp[0]
    ids = [eid for eid, _fields in entries]
    assert ids, "нет доставленных событий"
    assert slots.client.xack(slots.stream_key, group, *ids) == len(ids)
