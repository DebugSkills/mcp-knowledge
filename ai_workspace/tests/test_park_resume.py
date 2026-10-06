"""Integration-тесты budget-hard-stop park/resume (Ф4.3, I10/D5) — живой ws-redis.

Доказательства (контракт Ф4.3):
- park освобождает слот/lease, изымает вызов из ОБОИХ индексов, снимает
  ws:pos, состояние parked (НЕ failed), ws:fx не тронут, событие parked;
- parked не занимает conc пользователя (admit того же юзера проходит);
- resume НЕ теряет приоритет: вызов возвращается с исходным vft — впереди
  одноуровневых, вставших за время парковки;
- resume при всё ещё исчерпанном бюджете → False, job остаётся parked
  (без записей); после возврата бюджета → True, conc учтён ровно один раз;
- идемпотентность park×2 / resume×2 — без дублей в очереди и событиях;
- epoch-fencing (I4): допарковые эффекты (меньший epoch) не применяются ни
  до, ни после resume; stale requeue по per-call epoch отвергнут.

Паттерн — test_requeue_preempt.py (полка = изолированный namespace) и
test_admission.py (боевой реестр read-only, снапшот ws:budget:global).
Полка 'ext' —.literal: admission гейтит бюджет только при shelf == 'ext';
очистка ext-ключей — явным списком (общий namespace, scan по подстроке
переложил бы чужие ключи).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from ai_workspace.orchestrator.job import JobState, JobStore, StaleEpoch
from ai_workspace.registry import Registry
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler import policy
from ai_workspace.scheduler.admission import (
    BUDGET_GLOBAL_KEY,
    admit,
    conc_exit,
    conc_key,
)
from ai_workspace.scheduler.park import JobNotParked, ParkControl, pos_key
from ai_workspace.scheduler.queue import Queue
from ai_workspace.scheduler.slots import Slots
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


def _shelf(tag: str) -> str:
    return f"{WS_TEST_ID_PREFIX}f43-{tag}-{uuid4().hex[:8]}"


def _user() -> str:
    return f"{WS_TEST_ID_PREFIX}f43u-{uuid4().hex[:8]}"


def _cleanup_shelf(client, shelf: str, calls: list[str], jobs: list[str]) -> None:
    """Точная уборка ключей полки (ext — общий namespace: без scan-паттерна)."""
    keys = [
        f"ws:q:{shelf}",
        f"ws:starve:{shelf}",
        f"ws:vt:{shelf}",
        f"ws:slots:{shelf}",
        f"ws:events:{shelf}",
        f"ws:vftlast:{shelf}:med:interactive",
    ]
    keys += [f"ws:call:{shelf}:{c}" for c in calls]
    keys += [f"ws:lease:{shelf}:{c}" for c in calls]
    keys += [pos_key(j) for j in jobs]
    keys += [f"ws:fx:{j}" for j in jobs]
    client.delete(*keys)


def _events(client, shelf: str) -> list[dict]:
    out = []
    for entry in client.xrange(f"ws:events:{shelf}"):
        out.append(json.loads(entry[1]["event"]))
    return out


@pytest.fixture()
def ws():
    """(client, JobStore, QuotaRegistry, user): уникальный user; уборка."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    yield client, JobStore(client), QuotaRegistry(Registry(REGISTRY_DIR)), _user()
    for pattern in (
        f"ws:job:{WS_TEST_ID_PREFIX}f43-*",
        f"ws:quota:*{WS_TEST_ID_PREFIX}f43u-*",
    ):
        keys = list(client.scan_iter(match=pattern))
        if keys:
            client.delete(*keys)


@pytest.fixture()
def budget_global(ws):
    """Снапшот/восстановление ws:budget:global (общий ключ тестового redis)."""
    client = ws[0]
    prev = client.get(BUDGET_GLOBAL_KEY)
    yield client
    if prev is None:
        client.delete(BUDGET_GLOBAL_KEY)
    else:
        client.set(BUDGET_GLOBAL_KEY, prev)


def _mk_job(store: JobStore, user: str, *, level: str = "member", epoch: int = 0):
    return store.create(
        user=user,
        account_level=level,
        job_class="interactive",
        mode="review",
        zone="private",
        job_id=f"{WS_TEST_ID_PREFIX}f43-{uuid4().hex[:10]}",
        epoch=epoch,
    )


# ── 1. park: слот/очередь/статус/событие/pos ───────────────────────────


def test_park_frees_slot_removes_from_queue_not_failed():
    """Running-путь: dequeue_and_acquire взял слот → park вернул слот+lease,
    изъял из q/starve, снял ws:pos, state=parked (НЕ failed), ws:fx целы,
    событие parked с reason; queued-путь: ZREM из обоих индексов."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    store = JobStore(client)
    shelf = _shelf("park")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    slots = Slots(client, shelf=shelf, k=1)
    user = _user()
    j1 = _mk_job(store, user)
    j2 = _mk_job(store, user)
    calls = ["p1:0:0", "p2:0:0"]
    try:
        vft = q.enqueue(
            "p1:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=1.0, job=j1.id, epoch=0,
        )
        assert q.dequeue_and_acquire(k_max=1, now=2.0, limit=1) == ["p1:0:0"]
        assert slots.used() == 1
        client.set(pos_key(j1.id), "1")  # панель показывает позицию
        client.hset(f"ws:fx:{j1.id}", "sentinel", "keep")  # ledger-эффекты

        pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 3.0)
        assert pc.park(j1.id, call="p1:0:0", reason="budget_ext_exhausted") is True

        rec = store.get(j1.id)
        assert rec.state is JobState.PARKED
        assert rec.state is not JobState.FAILED
        assert slots.used() == 0  # слот возвращён
        assert client.exists(slots.lease_key("p1:0:0")) == 0
        assert client.zrange(q.q_key, 0, -1) == []
        assert client.zrange(q.starve_key, 0, -1) == []
        assert client.exists(pos_key(j1.id)) == 0  # панель: parked не ждущий
        assert client.hget(f"ws:fx:{j1.id}", "sentinel") == "keep"  # НЕ failure
        parked = [e for e in _events(client, shelf) if e["type"] == "parked"]
        assert len(parked) == 1
        assert parked[0]["job"] == j1.id
        assert parked[0]["reason"] == "budget_ext_exhausted"
        assert parked[0]["state"] == "parked"
        # durable-кредиты в хеше job (vft + starve из per-call записи)
        assert rec.vft == pytest.approx(vft)
        assert rec.starve_deadline == pytest.approx(1.0 + policy.T_STARVE["interactive"])

        # queued-путь: вызов в очереди (слот не брался) → park изымает из ZSET
        q.enqueue(
            "p2:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=3.0, job=j2.id, epoch=0,
        )
        assert pc.park(j2.id, call="p2:0:0", reason="command") is True
        assert store.get(j2.id).state is JobState.PARKED
        assert client.zrange(q.q_key, 0, -1) == []
        assert client.zrange(q.starve_key, 0, -1) == []
        assert slots.used() == 0
    finally:
        _cleanup_shelf(client, shelf, calls, [j1.id, j2.id])
        conc_exit(user, redis=client)


def test_parked_does_not_hold_user_conc(ws):
    """guest conc=1: admit взял резерв → park освободил → повторный admit
    проходит (счётчик не течёт)."""
    client, store, book, user = ws
    assert admit(user, "guest", registry=book, redis=client).action == "allow"
    assert int(client.get(conc_key(user))) == 1

    job = _mk_job(store, user, level="guest")
    pc = ParkControl(client, shelf=_shelf("conc"), store=store)
    assert pc.park(job.id, reason="command") is True  # парк до постановки

    assert int(client.get(conc_key(user))) == 0  # резерв отдан
    assert admit(user, "guest", registry=book, redis=client).action == "allow"
    conc_exit(user, redis=client)


# ── 3. resume не теряет приоритет ──────────────────────────────────────


def test_resume_preserves_priority_over_late_same_class_arrivals(ws):
    """A (interactive) запаркован; за время парковки встали B, C того же
    класса → resume A → A обслуживается ПЕРВЫМ (исходный vft сохранён)."""
    client, store, book, user = ws
    shelf = _shelf("prio")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    ja, jb, jc = (_mk_job(store, user) for _ in range(3))
    pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 0.0)
    try:
        vft_a = q.enqueue(
            "a:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=0.0, job=ja.id, epoch=0,
        )
        q.enqueue(
            "b:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=1.0, job=jb.id, epoch=0,
        )
        q.enqueue(
            "c:0:0", prio="med", call_class="interactive", cost_est=1.0,
            now=2.0, job=jc.id, epoch=0,
        )
        assert pc.park(ja.id, call="a:0:0", reason="budget_ext_exhausted") is True
        assert client.zscore(q.q_key, "a:0:0") is None  # из очереди изъят

        assert pc.resume(ja.id, registry=book, call="a:0:0") is True
        assert store.get(ja.id).state is JobState.QUEUED
        # исходный vft (меньше всех позже вставших) — впереди B и C
        assert client.zscore(q.q_key, "a:0:0") == pytest.approx(vft_a)
        assert q.dequeue(now=3.0, limit=3) == ["a:0:0", "b:0:0", "c:0:0"]
    finally:
        _cleanup_shelf(
            client, shelf, ["a:0:0", "b:0:0", "c:0:0"], [ja.id, jb.id, jc.id]
        )
        conc_exit(user, redis=client)


# ── 4/5. resume против бюджета (полка ext) ─────────────────────────────


def _ext_setup(client, store):
    shelf = "ext"
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 0.0)
    user = _user()
    job = _mk_job(store, user)
    call = f"{job.id}:0:0"
    q.enqueue(
        call, prio="med", call_class="interactive", cost_est=1.0,
        now=1.0, job=job.id, epoch=0,
    )
    return pc, q, user, job, call


def test_resume_with_budget_still_exhausted_stays_parked(ws, budget_global):
    """Бюджет на уровне лимита → resume=False, state остался parked, вызова
    в очереди нет, conc-резерв НЕ взят, resumed-события нет."""
    client, store, book, _ = ws
    pc, q, user, job, call = _ext_setup(client, store)
    limit = book.budgets["ext"].limit
    try:
        client.set(BUDGET_GLOBAL_KEY, limit)
        assert pc.park(job.id, call=call, reason="budget_ext_exhausted") is True

        assert pc.resume(job.id, registry=book, call=call) is False

        rec = store.get(job.id)
        assert rec.state is JobState.PARKED  # остался в парке
        assert client.zscore(q.q_key, call) is None
        assert client.zcard(q.starve_key) == 0
        assert not client.exists(conc_key(user))  # резерв не берётся
        assert [e for e in _events(client, "ext") if e["type"] == "resumed"] == []
    finally:
        _cleanup_shelf(client, "ext", [call], [job.id])
        conc_exit(user, redis=client)


def test_resume_after_budget_restored_requeues_with_single_conc(ws, budget_global):
    """Бюджет вернулся (ниже лимита) → resume=True: queued + вызов в очереди
    с исходным vft; conc учтён РОВНО один раз (резерв admit(allow), без
    второго conc_enter); вызов сервируется следующим dequeue."""
    client, store, book, _ = ws
    pc, q, user, job, call = _ext_setup(client, store)
    limit = book.budgets["ext"].limit
    try:
        client.set(BUDGET_GLOBAL_KEY, limit)
        assert pc.park(job.id, call=call, reason="budget_ext_exhausted") is True
        vft = q.call_record(call)["vft"]

        client.set(BUDGET_GLOBAL_KEY, limit - 1)  # nightly reconcile вернул
        assert pc.resume(job.id, registry=book, call=call) is True

        assert store.get(job.id).state is JobState.QUEUED
        assert client.zscore(q.q_key, call) == pytest.approx(vft)
        assert int(client.get(conc_key(user))) == 1  # ровно один учёт
        assert q.dequeue(now=2.0, limit=1) == [call]  # сервируемость
    finally:
        _cleanup_shelf(client, "ext", [call], [job.id])
        conc_exit(user, redis=client)


# ── 6. идемпотентность ─────────────────────────────────────────────────


def test_park_and_resume_are_idempotent_no_duplicate_side_effects(ws):
    """park×2: второй no-op (False), ровно 1 событие parked; resume×2: второй
    JobNotParked, в очереди ОДИН член, ровно 1 событие resumed."""
    client, store, book, user = ws
    shelf = _shelf("idem")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    job = _mk_job(store, user)
    call = f"{job.id}:0:0"
    pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 0.0)
    try:
        q.enqueue(
            call, prio="med", call_class="interactive", cost_est=1.0,
            now=0.0, job=job.id, epoch=0,
        )
        assert pc.park(job.id, call=call, reason="command") is True
        assert pc.park(job.id, call=call, reason="command") is False  # no-op
        assert store.get(job.id).state is JobState.PARKED
        assert len([e for e in _events(client, shelf) if e["type"] == "parked"]) == 1

        assert pc.resume(job.id, registry=book, call=call) is True
        with pytest.raises(JobNotParked):
            pc.resume(job.id, registry=book, call=call)
        assert q.size() == 1  # без дублей (ZSET-член один)
        assert client.zcard(q.starve_key) == 1
        assert len([e for e in _events(client, shelf) if e["type"] == "resumed"]) == 1
        assert store.get(job.id).state is JobState.QUEUED
    finally:
        _cleanup_shelf(client, shelf, [call], [job.id])
        conc_exit(user, redis=client)


# ── 7. epoch-fencing: допарковые эффекты не применяются после resume ──


def test_stale_pre_park_epoch_rejected_before_and_after_resume(ws):
    """park бампит epoch (забирает владение): коммит эффекта с допарковым
    epoch → StaleEpoch и ДО, и ПОСЛЕ resume; актуальный — проходит;
    stale requeue по per-call epoch отвергнут без записей."""
    client, store, book, user = ws
    shelf = _shelf("epoch")
    q = Queue(client, shelf=shelf, clock=lambda: 0.0)
    job = _mk_job(store, user, epoch=5)
    call = f"{job.id}:0:0"
    pc = ParkControl(client, shelf=shelf, store=store, clock=lambda: 0.0)
    try:
        running = store.transition(
            job.id, JobState.RUNNING, expect_version=1, epoch=5
        )
        assert running.epoch == 5
        q.enqueue(
            call, prio="med", call_class="interactive", cost_est=1.0,
            now=0.0, job=job.id, epoch=5,
        )

        assert pc.park(job.id, call=call, reason="budget_ext_exhausted") is True
        parked = store.get(job.id)
        assert parked.epoch == 6  # владение у park

        # допарковой владелец (epoch=5) коммитит эффект → отказ, без записи
        with pytest.raises(StaleEpoch):
            store.patch(
                job.id, expect_version=parked.version, epoch=5, patch={"cursor": "stale"}
            )

        assert pc.resume(job.id, registry=book, call=call) is True
        resumed = store.get(job.id)

        # и ПОСЛЕ resume допарковой коммит не проходит (fencing не откатывается)
        with pytest.raises(StaleEpoch):
            store.patch(
                job.id, expect_version=resumed.version, epoch=5, patch={"cursor": "stale"}
            )
        assert store.get(job.id).cursor != "stale"
        # актуальный владелец (epoch=6) — проходит
        assert store.patch(
            job.id, expect_version=resumed.version, epoch=6, patch={"cursor": "ok"}
        ).cursor == "ok"
        # queue-level fence: requeue со stale per-call epoch — без записей
        assert q.size() == 1
        assert q.requeue(call, now=1.0, epoch=4) is False
        assert q.size() == 1  # дубля не появилось
    finally:
        _cleanup_shelf(client, shelf, [call], [job.id])
        conc_exit(user, redis=client)
