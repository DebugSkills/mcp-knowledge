"""Тесты wiring-контура квот (P1-5 ревизии Ф4): машина квот ↔ движок.

Offline-часть (FakeQuota + фейки test_engine): политика пауз waiting_human
(освобождение + re-admit), heartbeat на шагах, финализация на ВСЕХ
терминалах, компенсация битого токена, отсутствие двойного списания.

Integration-часть (``-m integration``, живой ws-redis): постановка
deny/park/allow ДО создания job, e2e-нить admit→run→charge→release со
сходящимися счётчиками, продление lease heartbeat'ом, глобальный свип
sweep_all, деградация redis (fail-closed на постановке).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ai_workspace.orchestrator.engine import (
    MemoryLedger,
    ModeEngine,
    TokenInvalid,
    load_mode,
)
from ai_workspace.orchestrator.job import JobNotFound, JobState, JobStore
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


# ── фейк порта квот (offline) ───────────────────────────────────────────


class FakeQuota:
    """Offline-двойник QuotaPort: записи всех вызовов, скриптуемые ответы."""

    def __init__(self, *, readmit_result: str = "allow", lease_alive: bool = True) -> None:
        self.readmit_result = readmit_result
        self.lease_alive = lease_alive
        self.readmit_calls: list[tuple[str, str, str]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.charges: list[tuple[str, int]] = []
        self.releases: list[tuple[str, str]] = []

    def readmit(self, user: str, role: str, job_id: str) -> str:
        self.readmit_calls.append((user, role, job_id))
        return self.readmit_result

    def heartbeat(self, user: str, job_id: str) -> bool:
        self.heartbeats.append((user, job_id))
        return self.lease_alive

    def charge(self, user: str, tokens: int) -> None:
        self.charges.append((user, tokens))

    def release(self, user: str, job_id: str) -> None:
        self.releases.append((user, job_id))


def make_quoted_engine(quota: FakeQuota, *, mcp=None):
    jobs = FakeJobs()
    boards = FakeBoards()
    ledger = MemoryLedger()
    engine = ModeEngine(
        jobs=jobs,
        boards=boards,
        graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)),
        mcp=mcp or FakeMCP(),
        ledger=ledger,
        quota=quota,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: квоты"}, epoch=EPOCH)
    return engine, ledger


def run_to_pause(engine: ModeEngine):
    res = engine.run("j1", epoch=EPOCH)
    assert res.status == "paused" and res.resume_token
    return res


# ── offline: политика пауз + финализация ────────────────────────────────


def test_done_charges_usage_once_and_releases_reserve() -> None:
    quota = FakeQuota()
    engine, ledger = make_quoted_engine(quota)
    paused = run_to_pause(engine)

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)

    assert res.status == "done"
    used = ledger.get("j1", "usage")
    assert used and used["tokens"] > 0
    assert quota.charges == [("u1", used["tokens"])]  # списание РОВНО по факту
    assert ("u1", "j1") in quota.releases  # терминал вернул резерв


def test_waiting_human_releases_slot_and_resume_readmits() -> None:
    """Политика P1-5: пауза ОСВОБОЖДАЕТ резерв; resume берёт заново.

    До паузы резерв жив (heartbeat прошёл) → повторного взятия нет
    (readmit не звался) — двойной INCR исключён.
    """
    quota = FakeQuota()
    engine, _ = make_quoted_engine(quota)
    paused = run_to_pause(engine)

    assert ("u1", "j1") in quota.releases  # пауза освободила слот
    assert quota.readmit_calls == []  # резерв при старте жив — re-admit не нужен
    assert quota.heartbeats  # lease продлевался на шагах

    engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    assert quota.readmit_calls == [("u1", "basic", "j1")]  # resume взял заново


def test_resume_denied_defers_with_fresh_token() -> None:
    """Квота занята → resume отложен: состояние ждёт, предъявленный токен
    отработан (single-use), гейт жив — выдан СВЕЖИЙ токен того же узла."""
    quota = FakeQuota(readmit_result="deny")
    engine, _ = make_quoted_engine(quota)
    paused = run_to_pause(engine)

    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)

    assert res.status == "paused"
    assert "admission=deny" in res.detail
    assert res.resume_token and res.resume_token != paused.resume_token
    assert engine.jobs.get("j1").state is JobState.WAITING_HUMAN

    quota.readmit_result = "allow"
    res2 = engine.resume("j1", epoch=EPOCH, token=res.resume_token)
    assert res2.status == "done"


def test_invalid_token_rejected_before_readmit() -> None:
    """Битый токен отсекается ДО readmit: резерв вообще не берётся —
    компенсировать нечего (порядок: токен → состояние → readmit)."""
    quota = FakeQuota()
    engine, _ = make_quoted_engine(quota)
    run_to_pause(engine)

    with pytest.raises(TokenInvalid):
        engine.resume("j1", epoch=EPOCH, token="bogus-token")

    assert quota.readmit_calls == []  # резерв не брался
    assert engine.jobs.get("j1").state is JobState.WAITING_HUMAN


def test_run_start_lease_lost_readmits_then_beats() -> None:
    """Lease истёк в очереди → стартовый re-admit; мид-ран утеря — fail-loud."""
    quota = FakeQuota(lease_alive=False)
    engine, _ = make_quoted_engine(quota)

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "failed"
    assert "conc-lease" in res.detail
    assert quota.readmit_calls == [("u1", "basic", "j1")]  # старт взял заново…
    assert ("u1", "j1") in quota.releases  # …но lease мёртв и внутри шага — терминал
    assert engine.jobs.get("j1").state is JobState.FAILED


def test_done_rerun_does_not_double_charge() -> None:
    quota = FakeQuota()
    engine, _ = make_quoted_engine(quota)
    paused = run_to_pause(engine)
    engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    charges_after_done = len(quota.charges)

    res = engine.run("j1", epoch=EPOCH)  # терминальный no-op

    assert res.status == "done"
    assert len(quota.charges) == charges_after_done  # заряд НЕ идемпотентен — один раз


def test_no_quota_keeps_ledger_contract() -> None:
    """Quota=None (старый контур): usage-счётчик не ведётся — контракт ledger
    прежний (P1-5 не меняет поведение неподключённых прогонов)."""
    jobs = FakeJobs()
    ledger = MemoryLedger()
    engine = ModeEngine(
        jobs=jobs, boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=ledger,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "т"}, epoch=EPOCH)
    engine.run("j1", epoch=EPOCH)
    assert ledger.get("j1", "usage") is None


# ── offline: параметризованный возврат резерва по исходам ───────────────


class _BoomMCP:
    def call(self, *, tool, args):
        raise RuntimeError("MCP недоступен")


def _drive(engine: ModeEngine, outcome: str):
    if outcome in ("failed_tool", "lease_lost"):
        return engine.run("j1", epoch=EPOCH)
    if outcome == "done":
        paused = run_to_pause(engine)
        return engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    paused = run_to_pause(engine)
    if outcome == "reject":
        return engine.resume("j1", epoch=EPOCH, token=paused.resume_token, decision="reject")
    if outcome == "cancel":
        return engine.cancel("j1", epoch=EPOCH, reason="test")
    if outcome == "gate_timeout":
        return engine.gate_timeout("j1", epoch=EPOCH)
    raise AssertionError(outcome)


@pytest.mark.parametrize(
    "outcome,state",
    [
        ("done", JobState.DONE),
        ("failed_tool", JobState.FAILED),
        ("reject", JobState.FAILED),
        ("cancel", JobState.CANCELLED),
        ("gate_timeout", JobState.FAILED),
        ("lease_lost", JobState.FAILED),
    ],
)
def test_every_terminal_outcome_returns_reserve(outcome: str, state: JobState) -> None:
    quota = FakeQuota(lease_alive=(outcome != "lease_lost"))
    engine, _ = make_quoted_engine(quota, mcp=_BoomMCP() if outcome == "failed_tool" else None)

    res = _drive(engine, outcome)

    assert res.status == "failed" or (res.status == "done" and outcome == "done")
    assert engine.jobs.get("j1").state is state
    assert ("u1", "j1") in quota.releases, f"{outcome}: резерв не возвращён"


# ── integration: живой ws-redis ─────────────────────────────────────────

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


@pytest.fixture()
def ws():
    from ai_workspace.redis_client import make_ws_redis
    from ai_workspace.registry import Registry
    from ai_workspace.registry.quotas import QuotaRegistry

    client = make_ws_redis()
    user = f"{WS_TEST_ID_PREFIX}f45-{uuid4().hex[:8]}"
    yield client, QuotaRegistry(Registry(REGISTRY_DIR)), user
    keys = list(client.scan_iter(match=f"*{user}*"))
    if keys:
        client.delete(*keys)


@pytest.fixture()
def budget_global(ws):
    from ai_workspace.scheduler.admission import budget_global_key

    key = budget_global_key()
    client = ws[0]
    prev = client.get(key)
    yield client
    if prev is None:
        client.delete(key)
    else:
        client.set(key, prev)


def _wiring(client, book, user, *, shelf="local", lease_ttl_ms=90_000, pricing=None):
    from ai_workspace.scheduler.wiring import QuotaWiring

    return QuotaWiring(
        client, registry=book, shelf=shelf, lease_ttl_ms=lease_ttl_ms, pricing=pricing,
    )


def _pricing():
    from ai_workspace.registry import Registry
    from ai_workspace.registry.pricing import PricingRegistry

    return PricingRegistry(Registry(REGISTRY_DIR))


def _engine(client, job_id, port):
    from ai_workspace.orchestrator.board import BoardStore
    from ai_workspace.orchestrator.ledger import RedisLedger

    return ModeEngine(
        jobs=JobStore(client),
        boards=BoardStore(client, job_id),
        graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)),
        mcp=FakeMCP(),
        ledger=RedisLedger(client),
        quota=port,
    )


def _submit(client, book, user, job_id, *, account_level="member", shelf="local",
            lease_ttl_ms=90_000, zone="public", pricing=None):
    return _wiring(
        client, book, user, shelf=shelf, lease_ttl_ms=lease_ttl_ms, pricing=pricing,
    ).submit(
        user=user, account_level=account_level, job_class="interactive",
        mode="statya", zone=zone, job_id=job_id,
    )


@pytest.mark.integration
@requires_redis
def test_submit_allow_creates_queued_job_and_reserves_conc(ws) -> None:
    from ai_workspace.scheduler.admission import conc_key, conchold_key, conclease_key

    client, book, user = ws
    jid = f"{user}-j1"
    rec = _submit(client, book, user, jid)

    assert rec.state is JobState.QUEUED
    assert int(client.get(conc_key(user))) == 1  # резерв взят ОДИН раз (admit)
    assert client.sismember(conchold_key(user), jid)  # per-job владение (P1-2)
    assert client.exists(conclease_key(user, jid))  # lease для свипера (P1-3)
    assert client.exists(f"ws:job:{jid}")


@pytest.mark.integration
@requires_redis
def test_submit_deny_tokens_creates_nothing_and_touches_no_counters(ws) -> None:
    from ai_workspace.scheduler.admission import (
        AdmissionDenied,
        charge_tokens,
        conc_key,
        conchold_key,
    )

    client, book, user = ws
    limit = book.quota_for("member").tokens_per_day
    assert charge_tokens(user, limit, redis=client) == limit  # день исчерпан

    with pytest.raises(AdmissionDenied) as ei:
        _submit(client, book, user, f"{user}-j1")

    assert ei.value.code == "quota_tokens_exhausted"
    assert ei.value.message
    with pytest.raises(JobNotFound):
        JobStore(client).get(f"{user}-j1")  # job НЕ создан
    assert client.get(conc_key(user)) is None  # conc не тронут
    assert client.smembers(conchold_key(user)) == set()


@pytest.mark.integration
@requires_redis
def test_submit_deny_conc_holds_personal_slot(ws) -> None:
    from ai_workspace.scheduler.admission import AdmissionDenied, conc_key

    client, book, user = ws
    _submit(client, book, user, f"{user}-j1")
    _submit(client, book, user, f"{user}-j2")  # member conc=2: оба в резерве

    with pytest.raises(AdmissionDenied) as ei:
        _submit(client, book, user, f"{user}-j3")

    assert ei.value.code == "quota_conc_exceeded"
    assert int(client.get(conc_key(user))) == 2  # deny счётчик не сдвинул
    assert not client.exists(f"ws:job:{user}-j3")


@pytest.mark.integration
@requires_redis
def test_submit_budget_park_parks_job_without_reserve(ws, budget_global) -> None:
    from ai_workspace.scheduler.admission import (
        budget_global_key,
        conc_key,
        conchold_key,
    )

    client, book, user = ws
    limit_micro = book.budgets["ext"].limit_micro  # микро-₽ (P0-1)
    client.set(budget_global_key(), limit_micro)  # бюджет исчерпан (D4/D5)

    rec = _submit(
        client, book, user, f"{user}-j1", shelf="ext", pricing=_pricing()
    )

    assert rec.state is JobState.PARKED  # создан и СРАЗУ запаркован (Ф4.3)
    assert client.get(conc_key(user)) is None  # park ничего не резервирует
    assert client.smembers(conchold_key(user)) == set()
    assert client.exists(f"ws:job:{user}-j1")


@pytest.mark.integration
@requires_redis
def test_e2e_admit_run_charge_release_counters_converge(ws) -> None:
    """Нить P1-5: submit(admit) → run(paused→resume→done) → charge → release;
    счётчики сходятся: токены == usage ledger, conc == 0, маркеров нет."""
    from ai_workspace.orchestrator.ledger import RedisLedger
    from ai_workspace.scheduler.admission import conc_key, conchold_key, tok_key

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user)
    rec = wiring.submit(user=user, account_level="member", job_class="interactive",
                        mode="statya", zone="public", job_id=jid)
    assert rec.state is JobState.QUEUED
    assert int(client.get(conc_key(user))) == 1

    engine = _engine(client, jid, wiring.make_port())
    engine.seed(jid, {"brief": "тема"}, epoch=EPOCH)
    paused = engine.run(jid, epoch=EPOCH)
    assert paused.status == "paused" and paused.resume_token
    assert int(client.get(conc_key(user))) == 0  # waiting_human освободил (P1-5)
    assert client.smembers(conchold_key(user)) == set()

    done = engine.resume(jid, epoch=EPOCH, token=paused.resume_token)
    assert done.status == "done"

    usage = RedisLedger(client).get(jid, "usage")
    assert usage and usage["tokens"] > 0
    day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    assert int(client.get(tok_key(user, day))) == usage["tokens"]  # списан факт
    assert int(client.get(conc_key(user))) == 0  # резерв возвращён
    assert client.smembers(conchold_key(user)) == set()

    # повторный release — no-op по владению: в минус не уходит (P2-пол)
    wiring.make_port().release(user, jid)
    assert int(client.get(conc_key(user))) == 0


@pytest.mark.integration
@requires_redis
def test_waiting_human_frees_slot_for_second_job_and_resume_retries(ws) -> None:
    """Диспропорция критика (E11) закрыта: гость (conc=1) в waiting_human
    НЕ держит слот — второй job ставится; resume первого при занятом слоте
    откладывается с СОХРАНЕНИЕМ токена и проходит после освобождения."""
    from ai_workspace.scheduler.admission import conc_key

    client, book, user = ws
    j1, j2 = f"{user}-j1", f"{user}-j2"
    wiring = _wiring(client, book, user)
    wiring.submit(user=user, account_level="guest", job_class="interactive",
                  mode="statya", zone="public", job_id=j1)
    e1 = _engine(client, j1, wiring.make_port())
    e1.seed(j1, {"brief": "т"}, epoch=EPOCH)
    paused = e1.run(j1, epoch=EPOCH)
    assert paused.status == "paused"
    assert int(client.get(conc_key(user))) == 0  # слот свободен, ждёт человека

    wiring.submit(user=user, account_level="guest", job_class="interactive",
                  mode="statya", zone="public", job_id=j2)
    assert int(client.get(conc_key(user))) == 1  # слот занял второй job

    deferred = e1.resume(j1, epoch=EPOCH, token=paused.resume_token)
    assert deferred.status == "paused" and "admission=deny" in deferred.detail
    assert deferred.resume_token  # свежий токен — гейт жив
    assert e1.jobs.get(j1).state is JobState.WAITING_HUMAN  # ждёт человека

    e2 = _engine(client, j2, wiring.make_port())
    e2.seed(j2, {"brief": "т"}, epoch=EPOCH)
    e2.run(j2, epoch=EPOCH)  # тоже пауза → слот снова свободен
    assert int(client.get(conc_key(user))) == 0

    done = e1.resume(j1, epoch=EPOCH, token=deferred.resume_token)  # свежий токен
    assert done.status == "done"


@pytest.mark.integration
@requires_redis
def test_heartbeat_extends_lease_then_sweep_reclaims_dead_reserve(ws) -> None:
    from ai_workspace.scheduler.admission import (
        QUOTA_EVENTS_KEY,
        conc_key,
        conchold_key,
        conclease_key,
    )

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user, lease_ttl_ms=200)
    wiring.submit(user=user, account_level="member", job_class="interactive",
                  mode="statya", zone="public", job_id=jid)
    port = wiring.make_port()

    first_ttl = client.pttl(conclease_key(user, jid))
    assert 0 < first_ttl <= 200
    assert port.heartbeat(user, jid) is True  # воркер жив — продлили
    assert client.pttl(conclease_key(user, jid)) > first_ttl

    import time as time_mod

    time_mod.sleep(0.3)  # воркер умер: lease истёк без продлений
    assert port.heartbeat(user, jid) is False  # резерва нет — стоп работу

    reclaimed = wiring.sweep_all()  # reconcile-tick (P1-3/P1-5)
    assert reclaimed.get(user) == [jid]
    assert int(client.get(conc_key(user))) == 0
    assert client.smembers(conchold_key(user)) == set()
    events = [
        # хвост стрима (аудит Ф4.4a): ws:quota:events общий и НЕ чистится между
        # прогонами — чтение с головы (xrange, count=100) при накоплении >100
        # возвращало старые события и не видело свежее; xrevrange читает
        # последние записи, jid (= uuid-user) отсекает события чужих прогонов
        json.loads(fields["event"])
        for _mid, fields in client.xrevrange(QUOTA_EVENTS_KEY, count=200)
    ]
    assert any(
        e.get("type") == "conc_reservation_reclaimed" and e.get("job") == jid
        for e in events
    )


@pytest.mark.integration
@requires_redis
def test_submit_fail_closed_when_ws_redis_down(ws, caplog) -> None:
    """P1-4 на постановке: QuotaRedisUnavailable — понятный отказ (job не
    создаётся), не трейс; ALARM quota_degraded в logging."""
    import logging

    import redis

    from ai_workspace.scheduler.admission import QuotaRedisUnavailable

    _client, book, user = ws

    class _DeadCall:
        def __init__(self, exc):
            self._exc = exc

        def __call__(self, *a, **k):
            raise self._exc

    class _DeadRedis:
        def __init__(self, exc):
            self._exc = exc

        def register_script(self, _s):
            return _DeadCall(self._exc)

        def xadd(self, *a, **k):
            raise self._exc

        def hsetnx(self, *a, **k):
            raise AssertionError("create не должен зваться при отказе admission")

        def hset(self, *a, **k):
            raise AssertionError("create не должен зваться при отказе admission")

    dead = _DeadRedis(redis.exceptions.ConnectionError("connection refused"))
    from ai_workspace.scheduler.wiring import QuotaWiring

    wiring = QuotaWiring(dead, registry=book)
    with caplog.at_level(logging.ERROR, logger="ai_workspace.scheduler.admission"), \
            pytest.raises(QuotaRedisUnavailable) as ei:
        wiring.submit(user=user, account_level="member", job_class="interactive",
                      mode="statya", zone="public", job_id=f"{user}-j1")
    assert isinstance(ei.value.__cause__, redis.exceptions.ConnectionError)
    degraded = [r for r in caplog.records if "quota_degraded" in r.getMessage()]
    assert degraded and "admit" in degraded[0].getMessage()


# ── offline: P1-C1 iter2 — finalize crash/repeat-safe ───────────────────


class _ChargeBoomQuota(FakeQuota):
    """Charge отказал (деградация redis на терминале) — порт поднять нельзя."""

    def charge(self, user: str, tokens: int) -> None:
        raise RuntimeError("ws-redis упал на charge")


def test_finalize_charge_failure_still_releases_reserve() -> None:
    """Отказ charge на терминале НЕ съедает резерв: release выполняется
    ВСЕГДА (try/finally); сам отказ — fail-loud наружу (терминал уже
    зафиксирован в сторе)."""
    quota = _ChargeBoomQuota()
    engine, _ = make_quoted_engine(quota)
    paused = run_to_pause(engine)
    releases_at_pause = len(quota.releases)  # релиз паузы НЕ считаем

    with pytest.raises(RuntimeError, match="charge"):
        engine.resume("j1", epoch=EPOCH, token=paused.resume_token)

    # резерв возвращён ИМЕННО финализацией, несмотря на отказ charge
    assert len(quota.releases) == releases_at_pause + 1
    assert engine.jobs.get("j1").state is JobState.DONE  # терминал зафиксирован
    assert quota.charges == []  # заряд не прошёл — маркер не поставлен


def test_failed_requeued_terminal_charges_usage_once() -> None:
    """Легальный retry-путь FAILED→QUEUED→terminal (эффекты из кэша, 0 новых
    токенов) — суммарно ОДИН заряд: пробник критика списывал 50 токенов
    дважды (50 → 100). Маркер ``usage.charged`` доводится до факта."""
    quota = FakeQuota()
    engine, ledger = make_quoted_engine(quota, mcp=_BoomMCP())
    failed = engine.run("j1", epoch=EPOCH)
    assert failed.status == "failed"
    used = ledger.get("j1", "usage")
    assert used and used["tokens"] > 0
    assert quota.charges == [("u1", used["tokens"])]  # первый терминал списал

    rec = engine.jobs.get("j1")
    engine.jobs.transition("j1", JobState.QUEUED, expect_version=rec.version, epoch=EPOCH)
    engine.mcp = FakeMCP()  # причина сбоя устранена — retry
    paused = engine.run("j1", epoch=EPOCH)
    assert paused.status == "paused" and paused.resume_token
    done = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)
    assert done.status == "done"

    assert quota.charges == [("u1", used["tokens"])]  # НЕТ второго заряда
    final = ledger.get("j1", "usage")
    assert final["charged"] == final["tokens"]  # маркер == факт
    assert ("u1", "j1") in quota.releases


# ── offline: P2-5 iter2 — гонка cancel() с живым воркером ───────────────


def test_cancel_race_store_error_returns_terminal_status() -> None:
    """Воркер проиграл CAS параллельному cancel/gate_timeout: run() ПЕРЕЖИВАЕТ
    JobStoreError (VersionConflict/IllegalTransition), перечитывает job и
    возвращает терминальный статус — вместо необработанного исключения в
    воркер-цикле. Квоты финализированы параллельным вызовом (без дубля)."""
    from dataclasses import replace as _replace

    from ai_workspace.orchestrator.job import VersionConflict

    class _RacingCancelJobs(FakeJobs):
        """Первый patch курсора сталкивается с параллельным cancel:
        store поднял VersionConflict, актуальное состояние — cancelled."""

        def patch(self, job_id, *, expect_version, epoch, patch=None):
            if patch is not None and "cursor" in patch and "board_versions" not in patch:
                rec = self.records[job_id]
                self.records[job_id] = _replace(
                    rec, state=JobState.CANCELLED, version=rec.version + 1
                )
                raise VersionConflict("race: cancel выиграл CAS")
            return super().patch(
                job_id, expect_version=expect_version, epoch=epoch, patch=patch
            )

    quota = FakeQuota()
    engine = ModeEngine(
        jobs=_RacingCancelJobs(), boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=MemoryLedger(), quota=quota,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "т"}, epoch=EPOCH)

    res = engine.run("j1", epoch=EPOCH)  # без фикса: VersionConflict наружу

    assert res.status == "failed"  # терминальное отображение CANCELLED
    assert "параллельным" in res.detail
    assert engine.jobs.get("j1").state is JobState.CANCELLED


# ── integration: P1-A/P1-B/P2-1/P1-3 iter2 ──────────────────────────────


@pytest.mark.integration
@requires_redis
def test_admin_conc_null_e2e_submit_run_done(ws) -> None:
    """P1-A: admin (conc=null, tokens_per_day=null) — полный цикл
    submit→run→done: lease жив на heartbeat (QuotaLeaseLost не возникает),
    usage списан (личного лимита нет — charge не гейтится), владение
    возвращено на терминале. Пробник критика: run падал «conc-lease утерян»."""
    from ai_workspace.scheduler.admission import conc_key, conchold_key, tok_key

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user)
    rec = wiring.submit(user=user, account_level="admin", job_class="interactive",
                        mode="statya", zone="public", job_id=jid)
    assert rec.state is JobState.QUEUED
    assert client.get(conc_key(user)) is None  # счётчика нет (conc=null)
    assert client.sismember(conchold_key(user), jid)  # но владение есть
    assert client.exists(f"ws:quota:conclease:{user}:{jid}")  # и lease

    engine = _engine(client, jid, wiring.make_port())
    engine.seed(jid, {"brief": "тема"}, epoch=EPOCH)
    paused = engine.run(jid, epoch=EPOCH)
    assert paused.status == "paused" and paused.resume_token  # дошёл до гейта
    done = engine.resume(jid, epoch=EPOCH, token=paused.resume_token)
    assert done.status == "done"  # НЕ failed «conc-lease истёк/утерян»

    day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    assert int(client.get(tok_key(user, day)) or 0) > 0  # usage списан
    assert client.get(conc_key(user)) is None  # декремента не было и не нужно
    assert client.smembers(conchold_key(user)) == set()  # владение возвращено


@pytest.mark.integration
@requires_redis
def test_submit_retry_same_job_id_no_double_count_and_released(ws) -> None:
    """P2-1 (fresh-резерв): ретрай по job БЕЗ маркера (резерв снят паузой
    waiting_human / терминалом, job жив в сторе) — admit берёт НОВЫЙ резерв,
    create падает JobAlreadyExists → компенсация release'ом ОБЯЗАТЕЛЬНА
    (иначе утеча: терминал этот резерв уже не вернёт)."""
    from ai_workspace.orchestrator.job import JobAlreadyExists
    from ai_workspace.scheduler.admission import conc_key, conc_release, conchold_key

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user)
    wiring.submit(user=user, account_level="member", job_class="interactive",
                  mode="statya", zone="public", job_id=jid)
    assert int(client.get(conc_key(user))) == 1

    # пауза сняла резерв, job остался в сторе (waiting_human → re-queue)
    assert conc_release(user, jid, redis=client) is True
    assert int(client.get(conc_key(user))) == 0

    with pytest.raises(JobAlreadyExists):
        wiring.submit(user=user, account_level="member", job_class="interactive",
                      mode="statya", zone="public", job_id=jid)

    # fresh-резерв этого вызова компенсирован (0, не 1 — утеча закрыта)
    assert int(client.get(conc_key(user))) == 0
    assert client.smembers(conchold_key(user)) == set()
    assert JobStore(client).get(jid).state is JobState.QUEUED  # живой job цел


@pytest.mark.integration
@requires_redis
def test_submit_retry_live_marker_keeps_reserve_for_queued_job(ws) -> None:
    """N1 (reopen Ф4.2e), QUEUED-ветка: маркер жив (job в очереди, резерв
    при нём) — ретрай НЕ компенсирует: reused-допуск ничего не резервировал,
    резерв принадлежит job'у (движок возьмёт его же readmit'ом на старте —
    без повторного INCR, терминал вернёт). Было: release снимал резерв
    ожидающего job'а."""
    from ai_workspace.orchestrator.job import JobAlreadyExists
    from ai_workspace.scheduler.admission import conc_key, conchold_key, conclease_key

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user)
    wiring.submit(user=user, account_level="member", job_class="interactive",
                  mode="statya", zone="public", job_id=jid)
    assert int(client.get(conc_key(user))) == 1

    with pytest.raises(JobAlreadyExists):
        wiring.submit(user=user, account_level="member", job_class="interactive",
                      mode="statya", zone="public", job_id=jid)

    # ретрай не удвоил счётчик И НЕ снял резерв ожидающего job'а
    assert int(client.get(conc_key(user))) == 1
    assert client.sismember(conchold_key(user), jid)
    assert client.exists(conclease_key(user, jid))
    assert JobStore(client).get(jid).state is JobState.QUEUED  # живой job цел


@pytest.mark.integration
@requires_redis
def test_submit_retry_running_job_keeps_reserve_and_finishes(ws) -> None:
    """N1 (reopen Ф4.2e), точный сценарий пробника критика: submit →
    RUNNING → повторный submit тем же job_id. Было: компенсация release
    снимала резерв/lease ЖИВОГО job'а (conc 1→0, marker=[], lease=0) →
    первый же _beat = QuotaLeaseLost → ложный FAILED (потеря работы).
    Стало: reused-допуск не компенсируется — job дорабатывает до DONE."""
    from ai_workspace.orchestrator.job import JobAlreadyExists
    from ai_workspace.scheduler.admission import conc_key, conchold_key, conclease_key

    client, book, user = ws
    jid = f"{user}-j1"
    wiring = _wiring(client, book, user)
    wiring.submit(user=user, account_level="member", job_class="interactive",
                  mode="statya", zone="public", job_id=jid)
    store = JobStore(client)
    rec = store.get(jid)
    store.transition(jid, JobState.RUNNING, expect_version=rec.version, epoch=EPOCH)
    assert int(client.get(conc_key(user))) == 1  # резерв живого job'а

    with pytest.raises(JobAlreadyExists):  # ретрай постановки тем же job_id
        wiring.submit(user=user, account_level="member", job_class="interactive",
                      mode="statya", zone="public", job_id=jid)

    # резерв/владение/lease ЖИВОГО job'а не тронуты (было: 0/пусто/нет)
    assert int(client.get(conc_key(user))) == 1
    assert client.sismember(conchold_key(user), jid)
    assert client.exists(conclease_key(user, jid))
    assert wiring.make_port().heartbeat(user, jid) is True  # _beat пройдёт

    engine = _engine(client, jid, wiring.make_port())
    engine.seed(jid, {"brief": "тема"}, epoch=EPOCH)
    paused = engine.run(jid, epoch=EPOCH)  # RUNNING → шаги → гейт (НЕ failed)
    assert paused.status == "paused" and paused.resume_token
    done = engine.resume(jid, epoch=EPOCH, token=paused.resume_token)
    assert done.status == "done"  # НЕ failed «conc-lease утерян»
    assert store.get(jid).state is JobState.DONE


@pytest.mark.integration
@requires_redis
def test_ws_quota_sweep_script_reclaims_dead_reserve(ws) -> None:
    """P1-3 (остаток): у sweep_all есть реальный вызов — запуск СКРИПТА
    scripts/ws_quota_sweep.py (make ws-quota-sweep): мёртвый резерв снят
    именно этим вызовом, JSON-отчёт называет снятый job."""
    import os
    import subprocess
    import sys
    import time as time_mod
    from pathlib import Path

    client, book, user = ws
    jid = f"{user}-j1"
    _submit(client, book, user, jid, lease_ttl_ms=150)  # резерв с коротким lease
    time_mod.sleep(0.25)  # воркер «умер», свип ещё не ходил

    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "ws_quota_sweep.py")],
        capture_output=True, text=True, env=dict(os.environ), timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["reclaimed"].get(user) == [jid]
    assert int(client.get(f"ws:quota:conc:{user}") or 0) == 0
    assert client.smembers(f"ws:quota:conchold:{user}") == set()
