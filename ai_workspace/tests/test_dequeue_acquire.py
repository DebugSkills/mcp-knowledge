"""Integration-тесты атомарного DEQUEUE_ACQUIRE (Ф3.3b) — живой ws-redis.

Ключевые доказательства:
- АТОМАРНОСТЬ ОТКАЗА: при занятых слотах очередь (q и starve) НЕ изменяется и
  событий нет — отказ происходит ДО любого снятия (не «полуснятие»);
- в одном Lua: снятие из двух индексов + слот + ровно одно событие (I2/I3);
- PARITY: порядок выдачи совпадает с чистым `Queue.dequeue` на той же
  последовательности (правила выбора не разъехались);
- lease записан в формате, который читает `Slots.heartbeat`.
"""

from __future__ import annotations

import random
from uuid import uuid4

import pytest

from ai_workspace.scheduler import Queue
from ai_workspace.scheduler.slots import Slots
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


def _shelf(tag: str) -> str:
    return f"{WS_TEST_ID_PREFIX}{tag}-{uuid4().hex[:8]}"


def _cleanup(client, shelf: str) -> None:
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


def test_refusal_when_slots_full_keeps_queue_intact():
    """Слотов нет → [] и НИ очереди, ни starve-индекса не тронуто, событий нет."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("f33b-full")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    slots = Slots(client, shelf=shelf, k=1)
    try:
        q.enqueue("a:0:0", prio="med", call_class="interactive", cost_est=1.0, now=1.0)
        q.enqueue("b:0:0", prio="med", call_class="interactive", cost_est=1.0, now=2.0)
        assert slots.acquire("occupy:0:0") is True  # слот занят
        q_before, s_before = q.size(), int(client.zcard(q.starve_key))
        xl_before = int(client.xlen(f"ws:events:{shelf}")) if client.exists(
            f"ws:events:{shelf}"
        ) else 0
        assert q.dequeue_and_acquire(k_max=1, now=10.0, limit=2) == []
        assert q.size() == q_before and int(client.zcard(q.starve_key)) == s_before
        xl_after = int(client.xlen(f"ws:events:{shelf}")) if client.exists(
            f"ws:events:{shelf}"
        ) else 0
        assert xl_after == xl_before  # отказ не эмитит событий
    finally:
        _cleanup(client, shelf)


def test_atomic_take_slot_and_event():
    """limit=2 при k_max=1 → взят ровно 1; q/starve уменьшились; слот+lease+
    ровно одно событие."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("f33b-one")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        q.enqueue("a:0:0", prio="high", call_class="interactive", cost_est=1.0, now=1.0)
        q.enqueue("b:0:0", prio="low", call_class="interactive", cost_est=1.0, now=2.0)
        taken = q.dequeue_and_acquire(k_max=1, now=99.0, limit=2)  # без просрочек
        assert taken == ["a:0:0"]  # WFQ: high раньше low
        assert q.size() == 1
        assert client.zcard(q.starve_key) == 1
        assert client.scard(f"ws:slots:{shelf}") == 1
        assert client.get(f"ws:lease:{shelf}:a:0:0") == "a:0:0"
        assert int(client.xlen(f"ws:events:{shelf}")) == 1
    finally:
        _cleanup(client, shelf)


def test_parity_with_plain_dequeue():
    """Порядок DEQUEUE_ACQUIRE == порядок Queue.dequeue на той же (свежей)
    последовательности (правила выбора не разъехались)."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf_a, shelf_b = _shelf("f33b-par"), _shelf("f33b-ref")
    qa = Queue(client, shelf=shelf_a, clock=lambda: 0.0)
    qb = Queue(client, shelf=shelf_b, clock=lambda: 0.0)
    rnd = random.Random(20261006)
    specs = []
    now = 1000.0
    for i in range(24):
        prio = rnd.choice(["high", "med", "low"])
        cls = rnd.choice(["interactive", "batch", "background"])
        now += rnd.random() * 0.5
        specs.append((f"j{i}:0:0", prio, cls, now))
    try:
        for call, prio, cls, ts in specs:
            qa.enqueue(call, prio=prio, call_class=cls, cost_est=1.0, now=ts)
            qb.enqueue(call, prio=prio, call_class=cls, cost_est=1.0, now=ts)
        # 24 < окна 32 (спека §3) → за один проход выдаются все.
        got = qa.dequeue_and_acquire(k_max=24, now=now + 1.0, limit=24)
        ref = qb.dequeue(now=now + 1.0, limit=24)
        assert got == ref and len(got) == 24
    finally:
        _cleanup(client, shelf_a)
        _cleanup(client, shelf_b)


def test_lease_readable_by_slots_heartbeat():
    """lease, записанный DEQUEUE_ACQUIRE, продлевается Slots.heartbeat."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("f33b-hb")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    slots = Slots(client, shelf=shelf, k=1, lease_ttl_ms=5_000)
    try:
        q.enqueue("a:0:0", prio="med", call_class="interactive", cost_est=1.0, now=1.0)
        assert q.dequeue_and_acquire(k_max=1, now=2.0, limit=1) == ["a:0:0"]
        assert slots.client.pttl(slots.lease_key("a:0:0")) > 0
        assert slots.heartbeat("a:0:0") is True
        assert slots.used() == 1
    finally:
        _cleanup(client, shelf)
