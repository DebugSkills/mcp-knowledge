"""Integration-тесты REQUEUE / preempt / sweep_expired (Ф3.4) — живой ws-redis.

Доказательства:
- requeue возвращает вызов в ОБА индекса, vft и starve-дедлайн сохранены
  (I2 — anti-livelock: aging-пол не сбрасывается вытеснением);
- preempt-циклы НЕ сдвигают starve-дедлайн (vft тает на cost_done/w);
- epoch-fencing (I4): stale epoch отвергнут БЕЗ записей, актуальный — принят;
- preempt-кредит == vft − cost_done/w(p,c) (сверка с policy.weight);
- сервируемость: requeued-вызов реально выдаётся следующим dequeue;
- sweeper: мёртвый lease → слот возвращён, requeue возвращает вызов в очередь.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_workspace.scheduler import Queue, policy
from ai_workspace.scheduler.slots import Slots
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


def _shelf(tag: str) -> str:
    return f"{WS_TEST_ID_PREFIX}f34-{tag}-{uuid4().hex[:8]}"


def _cleanup(client, shelf: str) -> None:
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


def test_enqueue_writes_call_record_hash():
    """enqueue пишет per-call HASH {prio, class, job, epoch, attempt, vft,
    starve_deadline} — источник для requeue/preempt (Ф3.4)."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("hash")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        vft = q.enqueue(
            "a:0:0",
            prio="med",
            call_class="batch",
            cost_est=3.0,
            now=10.0,
            job="a",
            epoch=7,
            attempt=2,
        )
        rec = q.call_record("a:0:0")
        assert rec == {
            "prio": "med",
            "class": "batch",
            "job": "a",
            "epoch": 7,
            "attempt": 2,
            "vft": vft,
            "starve_deadline": 10.0 + policy.T_STARVE["batch"],
        }
    finally:
        _cleanup(client, shelf)


def test_requeue_preserves_both_indices():
    """requeue после dequeue: ZSCORE q == исходный vft, ZSCORE starve ==
    исходный дедлайн (НЕ сдвинулся); attempt++ (I2 + retry)."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("keep")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        vft = q.enqueue(
            "a:0:0", prio="med", call_class="batch", cost_est=3.0,
            now=10.0, epoch=2,
        )
        dl_before = client.zscore(q.starve_key, "a:0:0")
        assert q.dequeue(now=11.0) == ["a:0:0"]
        assert client.zscore(q.q_key, "a:0:0") is None  # снят из ОБЕИХ
        assert client.zscore(q.starve_key, "a:0:0") is None

        assert q.requeue("a:0:0", now=12.0, epoch=2) is True
        assert q.size() == 1
        assert client.zscore(q.q_key, "a:0:0") == vft
        assert client.zscore(q.starve_key, "a:0:0") == dl_before  # I2
        assert q.call_record("a:0:0")["attempt"] == 1
    finally:
        _cleanup(client, shelf)


def test_preempt_cycles_do_not_shift_starve_deadline():
    """Anti-livelock: 3 цикла dequeue → preempt(cost_done>0) → starve-дедлайн
    стабилен; vft монотонно тает ровно на cost_done/w."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("live")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        vft = q.enqueue(
            "b:0:0", prio="low", call_class="batch", cost_est=5.0,
            now=0.0, epoch=3,
        )
        dl0 = client.zscore(q.starve_key, "b:0:0")
        w = policy.weight("low", "batch")
        for i in range(3):
            assert q.dequeue(now=i + 1.0) == ["b:0:0"]
            assert q.preempt("b:0:0", cost_done=1.0, cost_est=5.0) is True
            # дедлайн НЕ изменился ни в одном цикле (иначе preempt-livelock)
            assert client.zscore(q.starve_key, "b:0:0") == dl0
            vft -= 1.0 / w
            assert client.zscore(q.q_key, "b:0:0") == pytest.approx(
                vft, rel=1e-12
            )
            assert q.call_record("b:0:0")["attempt"] == i + 1
    finally:
        _cleanup(client, shelf)


def test_epoch_fencing_rejects_stale_without_writes():
    """I4: requeue(epoch=1) при stored epoch=2 → False и НИ одной записи;
    requeue(epoch=2) → True."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("epoch")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        q.enqueue(
            "c:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=5.0, epoch=2,
        )
        assert q.dequeue(now=6.0) == ["c:0:0"]

        assert q.requeue("c:0:0", now=7.0, epoch=1) is False  # stale
        # отказ БЕЗ записи: очередь/индексы/запись не тронуты
        assert q.size() == 0
        assert client.zcard(q.starve_key) == 0
        rec = q.call_record("c:0:0")
        assert rec["epoch"] == 2 and rec["attempt"] == 0

        assert q.requeue("c:0:0", now=8.0, epoch=2) is True  # актуальный
        assert q.size() == 1
    finally:
        _cleanup(client, shelf)


def test_preempt_credit_matches_policy_weight():
    """Кредит: vft' = vft − cost_done/w(p,c), w — policy.weight по prio/class
    из per-call HASH (в очереди и в записи — одно и то же значение)."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("credit")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        vft0 = q.enqueue(
            "d:0:0", prio="high", call_class="background", cost_est=7.0,
            now=0.0, epoch=1,
        )
        assert q.dequeue(now=1.0) == ["d:0:0"]
        cost_done = 4.0
        assert q.preempt("d:0:0", cost_done=cost_done, cost_est=7.0) is True
        expected = vft0 - cost_done / policy.weight("high", "background")
        assert client.zscore(q.q_key, "d:0:0") == pytest.approx(
            expected, rel=1e-12
        )
        assert q.call_record("d:0:0")["vft"] == pytest.approx(
            expected, rel=1e-12
        )
    finally:
        _cleanup(client, shelf)


def test_requeued_call_is_served_by_next_dequeue():
    """Сервируемость: requeued-вызов (исходный меньший vft) реально выдаётся
    следующим dequeue — опережая более поздний вызов."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("serve")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    try:
        q.enqueue(
            "e:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=0.0, epoch=0,
        )
        q.enqueue(
            "f:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=0.5, epoch=0,
        )
        assert q.dequeue(now=1.0, limit=1) == ["e:0:0"]
        assert q.requeue("e:0:0", now=2.0, epoch=0) is True
        # e вернулся с исходным (меньшим) vft → следующий dequeue отдаёт e
        assert q.dequeue(now=3.0, limit=1) == ["e:0:0"]
    finally:
        _cleanup(client, shelf)


def test_sweep_expired_reclaims_dead_lease_and_requeue_returns_call():
    """Sweeper: dequeue_and_acquire (lease жив) → DEL lease (имитация крэша)
    → sweep_expired() вернул вызов, used() упал, requeue вернул в очередь,
    следующий конвейер снова берёт вызов."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = _shelf("sweep")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    slots = Slots(client, shelf=shelf, k=1)
    try:
        q.enqueue(
            "g:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=1.0, epoch=5,
        )
        assert q.dequeue_and_acquire(k_max=1, now=2.0, limit=1) == ["g:0:0"]
        assert slots.used() == 1

        client.delete(slots.lease_key("g:0:0"))  # крэш воркера: lease исчез
        assert slots.sweep_expired() == ["g:0:0"]
        assert slots.used() == 0
        assert q.size() == 0  # свипер возвращает слот; requeue — шаг caller'а

        assert q.requeue("g:0:0", now=3.0, epoch=5) is True
        assert q.size() == 1
        assert q.dequeue_and_acquire(k_max=1, now=4.0, limit=1) == ["g:0:0"]
    finally:
        _cleanup(client, shelf)
