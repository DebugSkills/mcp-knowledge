"""Тесты ETA панели очереди (Ф4.4a): EMA/p95/eta_range + observe + engine-хук.

Offline: ``ema`` на известной последовательности (ожидание посчитано в тесте
шаг за шагом), ``p95`` ближайшего ранга, границы ``eta_range`` (position=1
-> (0, p95); position=k -> ((k-1)*ema, k*p95)); хук ``ModeEngine.
on_job_terminal`` — терминал наблюдает wall-длительность, сбой хука не
ломает терминал, пустые штампы -> НЕТ наблюдения (не выдумываем).

Integration (ws-redis): ``observe`` — ZADD+окно+обрезка+JSON-агрегат.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
from ai_workspace.scheduler.eta import (
    ETAStore,
    ema,
    eta_key,
    eta_range,
    p95,
)
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis
from ai_workspace.tests.test_engine import (
    EPOCH,
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

SCRIPT = {"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}


# ── offline: чистые функции ──────────────────────────────────────────────


def test_ema_known_sequence():
    """EMA alpha=0.3 по хронологии [10, 20, 40], шаги явно:
    e0 = 10; e1 = 0.3*20 + 0.7*10 = 6 + 7 = 13; e2 = 0.3*40 + 0.7*13 =
    12 + 9.1 = 21.1 (прежний комментарий «19.0» врал арифметику;
    авторитетна формула ema = a*x + (1-a)*prev, а не число в комментарии)."""
    e = 10.0
    e = 0.3 * 20.0 + 0.7 * e  # 6 + 7 = 13.0
    e = 0.3 * 40.0 + 0.7 * e  # 12 + 9.1 = 21.1
    assert e == pytest.approx(21.1)
    assert ema([10.0, 20.0, 40.0]) == pytest.approx(e)
    assert ema([]) is None
    assert ema([5.0]) == pytest.approx(5.0)  # первое наблюдение — как есть


def test_p95_nearest_rank():
    """Квантиль ближайшего ранга: sort asc, индекс ceil(0.95*n)-1, без
    интерполяции (n=1 -> само значение; пусто -> None)."""
    assert p95([]) is None
    assert p95([7.0]) == pytest.approx(7.0)
    vals = [float(i) for i in range(1, 21)]  # 1..20: rank=ceil(19)=19 -> 19.0
    assert p95(vals) == pytest.approx(19.0)
    assert p95([30.0, 10.0, 20.0]) == pytest.approx(30.0)  # ceil(2.85)=3 -> max


def test_eta_range_bounds():
    """Диапазон R5: lower=(pos-1)*ema (позиция 1 -> 0), upper=pos*p95."""
    snap = {"ema_s": 10.0, "p95_s": 100.0, "n": 5, "updated_at": 1.0}
    assert eta_range(1, snap) == (0.0, 100.0)
    assert eta_range(3, snap) == (20.0, 300.0)
    assert eta_range(0, snap) is None      # не в очереди
    assert eta_range(2, None) is None      # ETA ещё не наблюдалась
    assert eta_range(2, {}) is None        # битый снимок — не падаем


# ── offline: engine -> on_job_terminal (Ф4.4a, best-effort) ──────────────


class StampJobs(FakeJobs):
    """FakeJobs + ISO-штампы created/updated (как живой JobStore, job.py):
    движок берёт wall-длительность из rec.created/rec.updated."""

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def create(self, job_id: str, **kw):
        super().create(job_id, **kw)
        self.records[job_id] = replace(
            self.records[job_id], created=self._now(), updated=self._now()
        )
        return self.records[job_id]

    def _cas(self, job_id, expect_version, epoch, patch):
        self.records[job_id] = replace(self.records[job_id], updated=self._now())
        return super()._cas(job_id, expect_version, epoch, patch)


def _engine(hook, jobs=None) -> ModeEngine:
    engine = ModeEngine(
        jobs=jobs if jobs is not None else StampJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM(SCRIPT),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        on_job_terminal=hook,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=EPOCH)
    return engine


def test_engine_terminal_observes_wall_duration():
    """done-терминал -> on_job_terminal(job_id, seconds>=0) ровно один раз;
    пауза waiting_human — НЕ терминал, не наблюдается."""
    seen: list[tuple[str, float]] = []
    engine = _engine(lambda job_id, seconds: seen.append((job_id, seconds)))

    paused = engine.run("j1", epoch=EPOCH)
    assert paused.status == "paused"
    assert seen == []

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    assert res.status == "done"
    assert len(seen) == 1
    assert seen[0][0] == "j1"
    assert seen[0][1] >= 0.0


def test_engine_terminal_broken_hook_keeps_terminal():
    """Сломанный хук (панель упала) — терминал всё равно done (display-only)."""
    def boom(job_id: str, seconds: float) -> None:
        raise RuntimeError("eta panel down")

    engine = _engine(boom)
    paused = engine.run("j1", epoch=EPOCH)
    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    assert res.status == "done"


def test_engine_without_stamps_skips_observation():
    """Пустые created/updated (штампов нет) -> хук НЕ зовётся (не выдумываем)."""
    seen: list[tuple[str, float]] = []
    engine = _engine(lambda job_id, seconds: seen.append((job_id, seconds)),
                     jobs=FakeJobs())  # без штампов: created=""
    paused = engine.run("j1", epoch=EPOCH)
    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    assert res.status == "done"
    assert seen == []


# ── integration: observe/snapshot на живом ws-redis ──────────────────────


@pytest.fixture()
def env():
    """Изолированная полка test-f44a-* + ETAStore; уборка своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    shelf = f"{WS_TEST_ID_PREFIX}f44a-{uuid4().hex[:8]}"
    yield client, shelf, ETAStore(client)
    keys = list(client.scan_iter(match=f"ws:*{shelf}*"))
    if keys:
        client.delete(*keys)


@requires_redis
@pytest.mark.integration
def test_observe_writes_snapshot_json(env):
    """[10, 20, 40] на t=100..102: ema=21.1 (10 -> 13 -> 21.1, см.
    test_ema_known_sequence), p95=40, n=3; snapshot читает JSON-агрегат
    (round-trip без потерь); updated_at = now наблюдения."""
    _, shelf, eta = env
    eta.observe(shelf, 10.0, now=100.0)
    eta.observe(shelf, 20.0, now=101.0)
    snap = eta.observe(shelf, 40.0, now=102.0)

    assert snap["n"] == 3
    assert snap["ema_s"] == pytest.approx(21.1)
    assert snap["p95_s"] == pytest.approx(40.0)
    assert snap["updated_at"] == 102.0
    assert eta.snapshot(shelf) == snap  # JSON round-trip
    assert set(snap) == {"ema_s", "p95_s", "n", "updated_at"}


@requires_redis
@pytest.mark.integration
def test_observe_window_trims_old(env):
    """Наблюдение вне окна ]now-window_s, now] выбрасывается ZREMRANGEBYSCORE."""
    _, shelf, eta = env
    eta.observe(shelf, 7.0, now=899.0, window_s=100.0)
    assert eta.snapshot(shelf)["n"] == 1
    snap = eta.observe(shelf, 5.0, now=1000.0, window_s=100.0)  # cutoff=900
    assert snap["n"] == 1  # 899-е выброшено, живо только 5.0
    assert snap["ema_s"] == pytest.approx(5.0)


@requires_redis
@pytest.mark.integration
def test_observe_max_n_trims_tail(env):
    """Сверх max_n выкидываются СТАРЕЙШИЕ (ZREMRANGEBYRANK): 5 наблюдений
    при max_n=3 -> остались [3,4,5]."""
    _, shelf, eta = env
    for i, seconds in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
        eta.observe(shelf, seconds, now=100.0 + i, max_n=3)
    snap = eta.snapshot(shelf)
    assert snap["n"] == 3
    # ema по [3,4,5]: 3 -> 0.3*4+0.7*3=3.3 -> 0.3*5+0.7*3.3=3.81
    assert snap["ema_s"] == pytest.approx(3.81)
    assert snap["p95_s"] == pytest.approx(5.0)


@requires_redis
@pytest.mark.integration
def test_snapshot_missing_shelf_is_none(env):
    """Полка без наблюдений -> None (UI показывает «нет оценки»)."""
    _, shelf, eta = env
    assert eta.snapshot(shelf) is None
    assert client_has_no_eta(env) is True


def client_has_no_eta(env) -> bool:
    client, shelf, _ = env
    return client.get(eta_key(shelf)) is None
