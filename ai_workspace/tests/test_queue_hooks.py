"""Хуки панели очереди в Queue/ParkControl/wiring (Ф4.4a).

on_queue_change(shelf) стреляет после enqueue/requeue/preempt/park_call
(preempt — через requeue: одна операция, один выстрел); при dequeue/
dequeue_and_acquire/complete — дополнительно DEL ws:pos:{call} снятого
вызова; ЛЮБОЙ сбой хука/DEL глотается с warning (display-only: панель
очереди не ломает планирование). Wiring-фабрики связывают хуки с
PositionStore/ETAStore (прод-проводка Ф4.4a).
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from ai_workspace.scheduler.eta import ETAStore
from ai_workspace.scheduler.position import PositionStore
from ai_workspace.scheduler.queue import Queue
from ai_workspace.scheduler.wiring import (
    QuotaWiring,
    make_on_job_terminal,
    make_on_queue_change,
)
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis


@pytest.fixture()
def env():
    """Изолированная полка test-f44a-* на живом ws-redis; уборка своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = f"{WS_TEST_ID_PREFIX}f44a-{uuid4().hex[:8]}"
    yield client, shelf
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


# ── выстрелы хука + best-effort ──────────────────────────────────────────


@requires_redis
@pytest.mark.integration
def test_hook_fires_on_enqueue_requeue_preempt_park(env):
    client, shelf = env
    fired: list[str] = []
    q = Queue(client, shelf=shelf, clock=lambda: 100.0, on_queue_change=fired.append)
    call = f"{shelf}:1:0"

    q.enqueue(call, prio="med", call_class="batch", cost_est=100.0)
    assert fired == [shelf]
    q.dequeue(now=100.0, limit=1)  # снятие тоже меняет очередь -> хук
    assert fired == [shelf, shelf]
    assert q.requeue(call, now=100.0)
    assert fired.count(shelf) == 3
    assert q.preempt(call, cost_done=10.0, cost_est=100.0)  # через requeue
    assert fired.count(shelf) == 4
    assert q.park_call(call, job=shelf, event_json="") >= 1
    assert fired.count(shelf) == 5


@requires_redis
@pytest.mark.integration
def test_broken_hook_does_not_break_enqueue(env, caplog):
    """Хук падает -> enqueue всё равно успешен (best-effort), warning в лог."""
    client, shelf = env

    def boom(shelf_arg: str) -> None:
        raise RuntimeError("panel down")

    q = Queue(client, shelf=shelf, clock=lambda: 100.0, on_queue_change=boom)
    with caplog.at_level(logging.WARNING, logger="ai_workspace.scheduler.queue"):
        vft = q.enqueue(f"{shelf}:9:0", prio="high", call_class="interactive",
                        cost_est=10.0)
    assert vft > 0.0
    assert q.size() == 1  # постановка реально прошла мимо сбоя панели
    assert any("on_queue_change" in r.getMessage() for r in caplog.records)


@requires_redis
@pytest.mark.integration
def test_dequeue_and_acquire_dels_pos_and_recalculates(env):
    """Снятый вызов теряет ws:pos сразу; хук пересчитал: следующий стал 1-м."""
    client, shelf = env
    positions = PositionStore(client, clock=lambda: 0.0)
    q = Queue(client, shelf=shelf, clock=lambda: 100.0,
              on_queue_change=lambda s: positions.update_pos(s, now=100.0))
    c1, c2 = f"{shelf}:1:0", f"{shelf}:2:0"
    q.enqueue(c1, prio="med", call_class="batch", cost_est=10.0)  # vft=0.5
    q.enqueue(c2, prio="med", call_class="batch", cost_est=10.0)  # vft=1.0
    positions.update_pos(shelf, now=100.0)
    assert positions.position_of(c1) == 1

    taken = q.dequeue_and_acquire(k_max=4, now=100.0, limit=1)

    assert taken == [c1]
    assert client.exists(f"ws:pos:{c1}") == 0
    assert positions.position_of(c1) is None
    assert positions.position_of(c2) == 1
    assert int(client.get(f"ws:posq:{shelf}")) == 1


@requires_redis
@pytest.mark.integration
def test_complete_with_call_dels_pos(env):
    """complete(call=...) гасит ws:pos завершённого вызова (ранг снят)."""
    client, shelf = env
    positions = PositionStore(client, clock=lambda: 0.0)
    q = Queue(client, shelf=shelf, clock=lambda: 100.0,
              on_queue_change=lambda s: positions.update_pos(s, now=100.0))
    call = f"{shelf}:5:0"
    q.enqueue(call, prio="med", call_class="batch", cost_est=10.0)
    positions.update_pos(shelf, now=100.0)
    assert client.exists(f"ws:pos:{call}") == 1

    q.complete(prio="med", call_class="batch", cost_actual=12.0, call=call)

    assert client.exists(f"ws:pos:{call}") == 0


@requires_redis
@pytest.mark.integration
def test_park_call_dels_pos(env):
    """park_call гасит ws:pos припаркованного (панель не ждёт его в очереди)."""
    client, shelf = env
    positions = PositionStore(client, clock=lambda: 0.0)
    q = Queue(client, shelf=shelf, clock=lambda: 100.0,
              on_queue_change=lambda s: positions.update_pos(s, now=100.0))
    call = f"{shelf}:7:0"
    q.enqueue(call, prio="med", call_class="batch", cost_est=10.0)
    positions.update_pos(shelf, now=100.0)
    assert client.exists(f"ws:pos:{call}") == 1

    q.park_call(call, job=shelf, event_json="")

    assert client.exists(f"ws:pos:{call}") == 0


# ── прод-проводка (wiring-фабрики, Ф4.4a) ────────────────────────────────


@requires_redis
@pytest.mark.integration
def test_make_on_queue_change_updates_positions(env):
    """Фабрика: Queue(on_queue_change=make_on_queue_change(client)) —
    enqueue уже материализует ws:pos/ws:posq."""
    client, shelf = env
    q = Queue(client, shelf=shelf, clock=lambda: 100.0,
              on_queue_change=make_on_queue_change(client))
    call = f"{shelf}:w:0"
    q.enqueue(call, prio="low", call_class="background", cost_est=5.0)
    assert client.exists(f"ws:pos:{call}") == 1
    assert int(client.get(f"ws:posq:{shelf}")) == 1


@requires_redis
@pytest.mark.integration
def test_make_on_job_terminal_observes_eta(env):
    """Фабрика: полка известна проводке (не движку) — observe(shelf, sec)."""
    client, shelf = env
    hook = make_on_job_terminal(client, shelf)
    hook("job-x", 12.5)
    snap = ETAStore(client).snapshot(shelf)
    assert snap is not None and snap["n"] == 1
    assert snap["ema_s"] == pytest.approx(12.5)


@requires_redis
@pytest.mark.integration
def test_parkcontrol_and_wiring_forward_hook(env):
    """ParkControl/QuotaWiring прокидывают on_queue_change в свой Queue."""
    from ai_workspace.orchestrator.job import JobStore
    from ai_workspace.scheduler.park import ParkControl

    client, shelf = env
    fired: list[str] = []
    store = JobStore(client)
    job_id = f"{WS_TEST_ID_PREFIX}f44a-park-{uuid4().hex[:6]}"
    try:
        store.create(user="u1", account_level="basic", job_class="batch",
                     mode="statya", zone="public", job_id=job_id)
        pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 100.0,
                         on_queue_change=fired.append)
        assert pc.park(job_id, call=f"{shelf}:1:0", reason="command")
        assert fired == [shelf]

        wiring = QuotaWiring(client, registry=object(), store=store, shelf=shelf,
                             on_queue_change=fired.append)
        # bound method: каждый доступ list.append — НОВЫЙ объект метода,
        # ``is`` всегда False; ``==`` сравнивает __self__/__func__ (тот же
        # список + та же функция) — корректная проверка проброса хука.
        assert wiring.park.queue.on_queue_change == fired.append
    finally:
        client.delete(f"ws:job:{job_id}")
