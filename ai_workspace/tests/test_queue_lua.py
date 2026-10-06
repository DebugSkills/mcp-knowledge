"""Integration-тесты scheduler/queue.lua (Ф3.2) — живой ws-redis.

Ключевое доказательство: PARITY — порядок dequeue из Lua совпадает с
эталоном ``policy.pick_best`` (N=60 случайных вызовов, фиксированный seed);
двух-индексное снятие (q + starve); aging e2e (просроченный background
побеждает свежий поток interactive, вне окна ZRANGE 32); идемпотентный
ZREM; атомарный complete.
"""

from __future__ import annotations

import random
from uuid import uuid4

import pytest

from ai_workspace.scheduler import Queue, policy
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


@pytest.fixture()
def queue():
    """Queue на изолированной полке test-f32-*; уборка только своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    shelf = f"{WS_TEST_ID_PREFIX}f32-{uuid4().hex[:8]}"
    q = Queue(make_ws_redis(), shelf=shelf, clock=lambda: 0.0)  # now инъектируем явно
    yield q
    keys = list(q.client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        q.client.delete(*keys)


def test_enqueue_vft_chain_matches_policy(queue: Queue):
    """vft-цепочка внутри очереди = policy.virtual_finish; чужая очередь
    (другой p,c) стартует от нуля, а не от vft соседей."""
    v1 = queue.enqueue("j1:0:0", prio="med", call_class="interactive",
                       cost_est=200.0, now=100.0)
    assert v1 == pytest.approx(policy.virtual_finish(0.0, 0.0, 200.0, 200.0))
    v2 = queue.enqueue("j2:0:0", prio="med", call_class="interactive",
                       cost_est=100.0, now=101.0)
    assert v2 == pytest.approx(v1 + 100.0 / 200.0)
    v3 = queue.enqueue("j3:0:0", prio="low", call_class="background",
                       cost_est=10.0, now=102.0)
    assert v3 == pytest.approx(10.0 / 1.0)
    assert queue.size() == 3


def _mirror_order(state: list[dict], now: float) -> list[str]:
    """Эталон: повторяет алгоритм dequeue(limit=1) через policy.pick_best.

    Видимость итерации: все просроченные (dl <= now) ∪ окно 32 младших vft
    — ровно то, что видит Lua (starve-индекс + ZRANGE 0 31).
    """
    order = []
    alive = list(state)
    while alive:
        expired = [c for c in alive if c["starve_deadline"] <= now]
        window = sorted(alive, key=lambda c: (c["vft"], c["call"]))[:32]
        visible = {c["call"]: c for c in window}
        visible.update({c["call"]: c for c in expired})
        best = policy.pick_best(list(visible.values()), now)
        order.append(best)
        alive = [c for c in alive if c["call"] != best]
    return order


def test_dequeue_parity_with_policy(queue: Queue):
    """N=60 случайных вызовов (seed): порядок Lua == порядок эталона."""
    rng = random.Random(20261006)
    base = 1_000_000.0
    state: list[dict] = []
    for i in range(60):
        prio = rng.choice(["high", "med", "low"])
        cls = rng.choice(["interactive", "batch", "background"])
        cost = rng.choice([10.0, 50.0, 200.0, 1500.0])
        enq_now = base + rng.uniform(0.0, 7200.0)  # разброс свежести
        call = f"job{i}:0:0"
        queue.enqueue(call, prio=prio, call_class=cls, cost_est=cost,
                      now=enq_now, starve_deadline=enq_now + policy.T_STARVE[cls])
        # vft берём ФАКТИЧЕСКИЙ (посчитан Lua) — parity проверяет порядок
        vft = float(queue.client.zscore(queue.q_key, call))
        state.append({"call": call, "vft": vft, "class": cls, "prio": prio,
                      "starve_deadline": enq_now + policy.T_STARVE[cls]})
    # interactive (60s) к этому моменту просрочены все, batch (30m) — частично,
    # background (2h) — свежие: проверяются обе ветки правила.
    final_now = base + 7200.0 + 1800.0
    expected = _mirror_order(state, final_now)
    got: list[str] = []
    while queue.size() > 0:
        got.extend(queue.dequeue(now=final_now, limit=1))
    assert got == expected
    assert len(got) == 60 and len(set(got)) == 60


def test_dequeue_batch_limit_order(queue: Queue):
    """dequeue(limit=5) одной пачкой — порядок argmin vft (та же арифметика
    через policy.virtual_finish); повторный dequeue пуст."""
    now = 500.0
    combos = [("low", "background"), ("med", "batch"), ("high", "interactive"),
              ("low", "interactive"), ("high", "background")]
    vfts = {}
    for i, (prio, cls) in enumerate(combos):
        queue.enqueue(f"c{i}:0:0", prio=prio, call_class=cls,
                      cost_est=100.0, now=now)
        vfts[f"c{i}:0:0"] = policy.virtual_finish(
            0.0, 0.0, policy.weight(prio, cls), 100.0
        )
    expected = sorted(vfts, key=lambda name: (vfts[name], name))
    got = queue.dequeue(now=now + 1.0, limit=5)
    assert got == expected
    assert queue.dequeue(now=now + 1.0, limit=5) == []
    assert queue.size() == 0


def test_two_index_removal(queue: Queue):
    """Снятие удаляет вызов из ОБОИХ индексов (q и starve) — одной Lua."""
    now = 100.0
    for i in range(3):
        queue.enqueue(f"r{i}:0:0", prio="med", call_class="batch",
                      cost_est=50.0, now=now)
    assert int(queue.client.zcard(queue.starve_key)) == 3
    got = queue.dequeue(now=now, limit=2)
    assert got == ["r0:0:0", "r1:0:0"]
    assert int(queue.client.zcard(queue.q_key)) == 1
    assert int(queue.client.zcard(queue.starve_key)) == 1
    for call in got:  # снятого нет ни в одном индексе
        assert queue.client.zscore(queue.q_key, call) is None
        assert queue.client.zscore(queue.starve_key, call) is None


def test_aging_e2e_background_beats_fresh_crowd(queue: Queue):
    """Aging e2e: background ждал T_starve под потоком interactive —
    просроченный побеждает ВСЕХ свежих, хотя его vft ВНЕ окна 32 младших
    (абсолютное право I2 обеспечено starve-индексом, не окном WFQ)."""
    t0 = 2_000_000.0
    queue.enqueue("bg:0:0", prio="med", call_class="background",
                  cost_est=1e6, now=t0)  # vft = 5e5 — больше любых interactive
    fresh_t = t0 + policy.T_STARVE["background"] - 30.0
    for i in range(32):  # ровно окно: bg в него НЕ попадает
        queue.enqueue(f"in{i}:0:0", prio="high", call_class="interactive",
                      cost_est=10.0, now=fresh_t)
    expired_now = t0 + policy.T_STARVE["background"] + 1.0
    assert queue.dequeue(now=expired_now, limit=1) == ["bg:0:0"]
    assert queue.client.zscore(queue.q_key, "bg:0:0") is None
    assert queue.client.zscore(queue.starve_key, "bg:0:0") is None
    assert queue.size() == 32  # толпа осталась ждать своей очереди


def test_zrem_missing_is_idempotent(queue: Queue):
    """ZREM отсутствующего члена = 0 без ошибки; dequeue пустой очереди = []."""
    assert int(queue.client.zrem(queue.q_key, "ghost:0:0")) == 0
    assert queue.dequeue(now=1.0, limit=3) == []
    assert queue.size() == 0


def test_complete_advances_vt_atomic(queue: Queue):
    """complete: vt += cost_actual/w, аккумулируется; новый enqueue не
    раньше vt (max(V, last) в enqueue)."""
    vt1 = queue.complete(prio="high", call_class="interactive",
                         cost_actual=1000.0)  # w=400 → +2.5
    assert vt1 == pytest.approx(2.5)
    vt2 = queue.complete(prio="med", call_class="batch",
                         cost_actual=100.0)  # w=20 → +5
    assert vt2 == pytest.approx(7.5)
    vft = queue.enqueue("after:0:0", prio="low", call_class="background",
                        cost_est=1.0, now=10.0)  # max(7.5, 0) + 1/1
    assert vft == pytest.approx(8.5)


def test_enqueue_fail_closed_bad_weight(queue: Queue):
    """weight<=0 → redis.error_reply → ResponseError (fail-closed)."""
    from redis.exceptions import ResponseError

    with pytest.raises(ResponseError):
        queue.enqueue("x:0:0", prio="med", call_class="batch",
                      cost_est=10.0, now=1.0, weight=0.0)
