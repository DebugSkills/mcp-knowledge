"""Тесты позиций панели очереди (Ф4.4a): rank_positions + update_pos.

Offline: parity ``rank_positions`` <-> ``policy.pick_best`` — порядок рангов
равен пошаговому drain'у через pick_best (ручные кейсы: просроченные FIFO
по дедлайну / смешанные / тай-брейк по имени; 40 seeded-наборов со
связками vft и граничным ``dl == now``).

Integration (ws-redis, ``make ws-up-test``): ``update_pos`` — идемпотентность,
DEL устаревших через ``ws:posidx``-diff, ``ws:posq`` == depth, защита
``max_detail`` (детальные ранги не пишутся + warning).
"""

from __future__ import annotations

import logging
import random
from uuid import uuid4

import pytest

from ai_workspace.scheduler import policy
from ai_workspace.scheduler.position import (
    PositionStore,
    pos_key,
    posidx_key,
    posq_key,
    rank_positions,
)
from ai_workspace.scheduler.queue import Queue
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

NOW = 1_000.0  # фиксированное «сейчас» (детерминизм, паттерн test_queue_policy)


# ── offline: parity rank_positions <-> policy.pick_best ──────────────────


def _drain_pick_best(cands: list[dict], now: float) -> list[str]:
    """Эталон: повторный pick_best с удалением выбранного (полный порядок)."""
    alive = list(cands)
    order: list[str] = []
    while alive:
        best = policy.pick_best(alive, now)
        order.append(best)
        alive = [c for c in alive if c["call"] != best]
    return order


def _rank_order(ranks: dict[str, int]) -> list[str]:
    """Порядок вызовов по рангу (1 — первый)."""
    return [call for call, _ in sorted(ranks.items(), key=lambda kv: kv[1])]


def test_rank_expired_first_fifo_by_deadline():
    """Просроченные раньше всех, среди них FIFO по дедлайну (не по vft)."""
    cands = [
        {"call": "fresh-low-vft", "vft": 1.0, "starve_deadline": NOW + 60.0},
        {"call": "expired-late", "vft": 0.5, "starve_deadline": NOW - 5.0},
        {"call": "expired-early", "vft": 999.0, "starve_deadline": NOW - 300.0},
    ]
    ranks = rank_positions(cands, NOW)
    assert _rank_order(ranks) == ["expired-early", "expired-late", "fresh-low-vft"]
    assert ranks == {"expired-early": 1, "expired-late": 2, "fresh-low-vft": 3}


def test_rank_fresh_argmin_vft_name_tiebreak():
    """Без просроченных: argmin vft; равный vft — лекс. имя (как pick_best)."""
    cands = [
        {"call": "bbb", "vft": 5.0, "starve_deadline": NOW + 10.0},
        {"call": "aaa", "vft": 5.0, "starve_deadline": NOW + 20.0},
        {"call": "ccc", "vft": 1.0, "starve_deadline": NOW + 30.0},
    ]
    ranks = rank_positions(cands, NOW)
    assert _rank_order(ranks) == ["ccc", "aaa", "bbb"]


def test_rank_expired_background_beats_lowest_vft():
    """Aging-пол — абсолютное право: просроченный background (vft=1e9) первый."""
    cands = [
        {"call": "in", "vft": 1.0, "starve_deadline": NOW + 60.0},
        {"call": "bg", "vft": 1e9, "starve_deadline": NOW - 1.0},
    ]
    assert _rank_order(rank_positions(cands, NOW)) == ["bg", "in"]


def test_rank_boundary_deadline_equal_now_counts_expired():
    """``dl == now`` — просрочен (контракт pick_best: ``starve_deadline <= now``)."""
    cands = [
        {"call": "edge", "vft": 100.0, "starve_deadline": NOW},
        {"call": "fresh", "vft": 1.0, "starve_deadline": NOW + 1.0},
    ]
    assert _rank_order(rank_positions(cands, NOW)) == ["edge", "fresh"]


def test_rank_missing_deadline_treated_as_fresh():
    """Член q без starve («под-снят»): aging-права НЕТ — чистый WFQ
    (fail-safe, как queue.lua/dequeue)."""
    cands = [
        {"call": "sub-taken", "vft": 1.0, "starve_deadline": None},
        {"call": "normal", "vft": 2.0, "starve_deadline": NOW + 60.0},
    ]
    assert _rank_order(rank_positions(cands, NOW)) == ["sub-taken", "normal"]


def test_rank_parity_pick_best_40_seeded_sets():
    """40 случайных наборов (seed): порядок рангов == drain через pick_best,
    ранги 1-based подряд (1..n без дыр и дублей)."""
    rng = random.Random(20261006)
    for case in range(40):
        n = rng.randint(1, 25)
        cands = [
            {
                "call": f"job{case}-{i}:0:0",
                # повторы значений vft -> тай-брейк по имени
                "vft": float(rng.choice([1.0, 2.0, 2.0, 7.0, 50.0])),
                # 0.0 -> dl == now: граничная просрочка; -100/-1: просрочен;
                # 5/600: свежий
                "starve_deadline": NOW + rng.choice([-100.0, -1.0, 0.0, 5.0, 600.0]),
            }
            for i in range(n)
        ]
        expected = _drain_pick_best([dict(c) for c in cands], NOW)
        ranks = rank_positions(cands, NOW)
        assert _rank_order(ranks) == expected, f"case {case}"
        assert sorted(ranks.values()) == list(range(1, n + 1)), f"case {case}: ранги не 1..n"


# ── integration: update_pos на живом ws-redis ────────────────────────────


@pytest.fixture()
def env():
    """Изолированная полка test-f44a-* + PositionStore; уборка своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = f"{WS_TEST_ID_PREFIX}f44a-{uuid4().hex[:8]}"
    yield (
        client,
        shelf,
        Queue(client, shelf=shelf, clock=lambda: 50.0),
        PositionStore(client, clock=lambda: 0.0),
    )
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


@requires_redis
@pytest.mark.integration
def test_update_pos_writes_ranks_and_depth(env):
    """Ранги по правилу планировщика: просроченный первый, затем по vft;
    posq == depth; position_of читает кэш панели."""
    client, shelf, q, positions = env
    c1, c2, c3 = f"{shelf}:1:0", f"{shelf}:2:0", f"{shelf}:3:0"
    q.enqueue(c1, prio="low", call_class="background", cost_est=10.0,
              starve_deadline=200.0)  # к моменту now=500 просрочен
    # ЯВНЫЙ будущий дедлайн: дефолтный now(50)+T_starve[interactive]=110
    # тоже истёк бы к now=500 -> все трое просрочены, FIFO по dl — это
    # НЕ замысел теста (замысел: просроченный первый, затем свежие по vft;
    # порядок «все просрочены» покрыт offline parity-наборами pick_best).
    q.enqueue(c2, prio="high", call_class="interactive", cost_est=100.0,
              starve_deadline=1_000.0)  # vft=0.25, к now=500 свежий
    q.enqueue(c3, prio="high", call_class="interactive", cost_est=100.0,
              starve_deadline=1_000.0)  # vft=0.5, к now=500 свежий

    depth = positions.update_pos(shelf, now=500.0)

    assert depth == 3
    assert int(client.get(posq_key(shelf))) == 3
    assert (positions.position_of(c1), positions.position_of(c2),
            positions.position_of(c3)) == (1, 2, 3)
    assert client.get(pos_key(c1)) == "1"


@requires_redis
@pytest.mark.integration
def test_update_pos_idempotent(env):
    """Двойной вызов — те же значения (SET/пересчёт без дрейфа)."""
    client, shelf, q, positions = env
    for i in range(3):
        q.enqueue(f"{shelf}:j{i}:0:0", prio="med", call_class="batch", cost_est=10.0)
    first = positions.update_pos(shelf, now=100.0)
    snap1 = {c: client.get(pos_key(c)) for c in client.smembers(posidx_key(shelf))}
    idx1 = client.smembers(posidx_key(shelf))

    second = positions.update_pos(shelf, now=100.0)
    snap2 = {c: client.get(pos_key(c)) for c in client.smembers(posidx_key(shelf))}

    assert (first, second) == (3, 3)
    assert snap1 == snap2
    assert idx1 == client.smembers(posidx_key(shelf))
    assert int(client.get(posq_key(shelf))) == 3


@requires_redis
@pytest.mark.integration
def test_update_pos_dels_stale_via_posidx_diff(env):
    """Вызов ушёл из очереди -> DEL его ws:pos + SREM из posidx (панель не
    показывает снятого как ждущего)."""
    client, shelf, q, positions = env
    a, b, c = f"{shelf}:a:0:0", f"{shelf}:b:0:0", f"{shelf}:c:0:0"
    q.enqueue(a, prio="low", call_class="background", cost_est=1.0,
              starve_deadline=10.0)  # просрочен к now=100
    q.enqueue(b, prio="med", call_class="batch", cost_est=10.0)
    q.enqueue(c, prio="med", call_class="batch", cost_est=10.0)
    positions.update_pos(shelf, now=100.0)
    assert positions.position_of(a) == 1

    taken = q.dequeue(now=100.0, limit=1)
    assert taken == [a]
    positions.update_pos(shelf, now=100.0)

    assert client.exists(pos_key(a)) == 0
    assert positions.position_of(a) is None
    assert client.smembers(posidx_key(shelf)) == {b, c}
    assert int(client.get(posq_key(shelf))) == 2
    assert positions.position_of(b) == 1  # ранги сдвинулись


@requires_redis
@pytest.mark.integration
def test_update_pos_depth_over_max_detail_skips_positions(env, caplog):
    """depth > max_detail: только posq, детальные ранги не пишутся (+warning);
    прежде написанные ws:pos гасятся (не показываем устаревшие ранги)."""
    client, shelf, q, positions = env
    for i in range(3):
        q.enqueue(f"{shelf}:d{i}:0:0", prio="med", call_class="batch", cost_est=10.0)
    positions.update_pos(shelf, now=100.0)  # детально: 3 ключа
    assert all(client.exists(pos_key(f"{shelf}:d{i}:0:0")) for i in range(3))

    with caplog.at_level(logging.WARNING, logger="ai_workspace.scheduler.position"):
        depth = positions.update_pos(shelf, now=100.0, max_detail=2)

    assert depth == 3
    assert int(client.get(posq_key(shelf))) == 3
    assert all(not client.exists(pos_key(f"{shelf}:d{i}:0:0")) for i in range(3))
    assert client.smembers(posidx_key(shelf)) == set()
    assert any("max_detail" in r.getMessage() for r in caplog.records)


@requires_redis
@pytest.mark.integration
def test_position_of_missing_is_none(env):
    """Нет ключа (вызов вне очереди) -> None, не исключение."""
    _, _, _, positions = env
    assert positions.position_of("no-such-call") is None
