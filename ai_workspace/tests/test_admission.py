"""Integration-тесты scheduler/admission (Ф4.2) — живой ws-redis.

Ключевые доказательства (контракт Ф4.2):
- deny-коды D3/D6 (токены дня / личный параллелизм) и park D5 (ext-бюджет
  исчерпан — НЕ deny);
- атомарность conc-резерва: N параллельных admit guest (conc=1) → ровно
  1 allow — проверка и INCR в одной Lua;
- TTL дневного ключа до локальной полуночи (D7), повторный charge не
  продлевает жизнь ключа за полночь;
- фолбэк неизвестной роли на квоту defaults.role (Ф4.1);
- P1-2 (ревизия критика Ф4): списание conc-резерва ПО ФАКТУ ВЛАДЕНИЯ —
  conc_release(user, job) атомарно решает SREM-маркером, чужой/повторный
  вызов резерв не трогает;
- P1-3: свипер мёртвых резервов — lease истёк → снят (+событие), живой
  lease/heartbeat продлевают владение, вечного deny после краха воркера нет;
- P1-4: деградация ws-redis — fail-closed (QuotaRedisUnavailable) + ALARM
  quota_degraded в logging, не трейс/зависание;
- P2-9: битое значение ключа квот → человекочитаемый отказ.

Реестр — боевой (read-only, не мутируется); redis — изолированные
пользователи test-f42-* (паттерн test_slots_lua).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as time_mod
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import redis

from ai_workspace.registry import Registry
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler.admission import (
    QUOTA_EVENTS_KEY,
    AdmissionDenied,
    Decision,
    QuotaRedisUnavailable,
    admit,
    budget_global_key,
    charge_tokens,
    conc_exit,
    conc_heartbeat,
    conc_key,
    conc_reclaim_expired,
    conc_release,
    conchold_key,
    conclease_key,
    seconds_to_local_midnight,
    sweep_expired_conc,
    tok_key,
)
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


@pytest.fixture()
def ws():
    """(client, QuotaRegistry, user): уникальный user; уборка своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    user = f"{WS_TEST_ID_PREFIX}f42-{uuid4().hex[:8]}"
    yield client, QuotaRegistry(Registry(REGISTRY_DIR)), user
    keys = list(client.scan_iter(match=f"ws:quota:*{user}*"))
    keys += list(client.scan_iter(match=f"ws:budget:user:{user}:*"))
    if keys:
        client.delete(*keys)


@pytest.fixture()
def budget_global(ws):
    """Снапшот/восстановление бюджетного счётчика месяца (общий ключ
    тестового redis; месяц-скоуп + микро-₽ — P0-1)."""
    key = budget_global_key()
    client = ws[0]
    prev = client.get(key)
    yield client
    if prev is None:
        client.delete(key)
    else:
        client.set(key, prev)

def _naive(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    """Naive-локальный datetime без DTZ001 (datetime.combine не флагается)."""
    return datetime.combine(date(y, mo, d), time(h, mi, s))


# ── 1. allow при нулевом расходе ──────────────────────────────────────


def test_member_allow_at_zero_spend(ws):
    client, book, user = ws
    d = admit(user, "member", registry=book, redis=client)
    assert d.action == "allow"
    assert d.code is None and d.reason is None and d.allowed
    assert int(client.get(conc_key(user))) == 1  # резерв взят (режим РЕЗЕРВ)


# ── 2. токены: charge до лимита → deny ────────────────────────────────


def test_tokens_exhausted_after_charge_to_limit(ws):
    client, book, user = ws
    limit = book.quota_for("member").tokens_per_day
    assert charge_tokens(user, 100, redis=client) == 100
    assert charge_tokens(user, limit - 100, redis=client) == limit  # кумулятивно
    d = admit(user, "member", registry=book, redis=client)
    assert d.action == "deny"
    assert d.code == "quota_tokens_exhausted"
    with pytest.raises(AdmissionDenied) as ei:
        d.raise_if_denied()
    assert ei.value.code == "quota_tokens_exhausted"
    assert ei.value.message  # человекочитаемый reason пробрасывается


# ── 3. guest conc=1: первый allow, второй deny, exit освобождает ──────


def test_guest_conc_1_and_exit_frees_reservation(ws):
    client, book, user = ws
    assert admit(user, "guest", registry=book, redis=client).action == "allow"
    d = admit(user, "guest", registry=book, redis=client)
    assert d.action == "deny"
    assert d.code == "quota_conc_exceeded"
    assert conc_exit(user, redis=client) == 0  # резерв 1 → 0
    assert admit(user, "guest", registry=book, redis=client).action == "allow"
    assert conc_exit(user, redis=client) == 0
    assert conc_exit(user, redis=client) == 0  # лишний exit — no-op (пол 0)
    assert int(client.get(conc_key(user))) == 0


# ── 4. member conc=2: два allow, третий deny ──────────────────────────


def test_member_conc_2_third_denies(ws):
    client, book, user = ws
    assert admit(user, "member", registry=book, redis=client).action == "allow"
    assert admit(user, "member", registry=book, redis=client).action == "allow"
    d = admit(user, "member", registry=book, redis=client)
    assert d.action == "deny"
    assert d.code == "quota_conc_exceeded"


# ── 5. admin: личных лимитов нет — всегда allow ───────────────────────


def test_admin_without_personal_limits_always_allows(ws):
    client, book, user = ws
    quota = book.quota_for("admin")
    assert quota.tokens_per_day is None and quota.conc is None
    for _ in range(5):
        assert admit(user, "admin", registry=book, redis=client).action == "allow"
    assert not client.exists(conc_key(user))  # резерв НЕ берётся (только общий K)
    charge_tokens(user, 10**9, redis=client)  # личного токен-лимита нет
    assert admit(user, "admin", registry=book, redis=client).action == "allow"


# ── 6. атомарность: N параллельных admit guest → ровно 1 allow ────────


def test_parallel_admits_exactly_one_allow(ws):
    """16 параллельных admit (threads через to_thread) guest c conc=1:
    ровно 1 allow — проверка и INCR атомарны в одной Lua, обе гонки
    пройти не могут («наблюдательный» режим дал бы 16 allow)."""
    client, book, user = ws

    async def _race() -> list[Decision]:
        return await asyncio.gather(
            *(
                asyncio.to_thread(admit, user, "guest", registry=book, redis=client)
                for _ in range(16)
            )
        )

    results = asyncio.run(_race())
    assert sum(1 for d in results if d.action == "allow") == 1
    denied = [d for d in results if d.action == "deny"]
    assert len(denied) == 15
    assert all(d.code == "quota_conc_exceeded" for d in denied)
    assert int(client.get(conc_key(user))) == 1


# ── 7. TTL дневного ключа = до локальной полуночи ─────────────────────


def test_daily_key_ttl_until_midnight(ws):
    client, _, user = ws
    charge_tokens(user, 42, redis=client)
    now = datetime.now(timezone.utc).astimezone()
    key = tok_key(user, now.strftime("%Y-%m-%d"))
    expect = seconds_to_local_midnight(now)
    ttl = int(client.ttl(key))
    assert 0 < ttl <= 86400
    assert expect - 5 <= ttl <= expect + 2  # ход теста — единицы секунд
    charge_tokens(user, 1, redis=client)  # повторный charge не продлевает
    assert int(client.ttl(key)) <= expect + 2  # EXPIREAT абсолютный


def test_seconds_to_local_midnight_edges():
    assert seconds_to_local_midnight(_naive(2026, 10, 6)) == 86400
    assert seconds_to_local_midnight(_naive(2026, 10, 6, 12)) == 43200
    assert seconds_to_local_midnight(_naive(2026, 10, 6, 23, 59, 59)) == 1


# ── 8. ext-бюджет исчерпан → park (НЕ deny) ───────────────────────────


def test_ext_budget_exhausted_parks_not_denies(ws, budget_global):
    client, book, user = ws
    limit_micro = book.budgets["ext"].limit_micro  # единица счётчика (P0-1)
    assert limit_micro == book.budgets["ext"].limit * 1_000_000  # ₽ → микро-₽
    client.set(budget_global_key(), limit_micro)  # ровно лимит → исчерпан (>=)
    d = admit(user, "member", registry=book, redis=client, shelf="ext")
    assert d.action == "park"
    assert d.code == "budget_ext_exhausted"
    assert d.reason and "парк" in d.reason
    assert not client.exists(conc_key(user))  # парк ничего не резервирует
    d.raise_if_denied()  # парк — НЕ исключение (D5: сигнал вызывающему)
    # локальная полка бюджетом не governed
    assert (
        admit(user, "member", registry=book, redis=client, shelf="local").action
        == "allow"
    )
    # ниже лимита ext пропускает (микро-₽: −1 от порога)
    client.set(budget_global_key(), limit_micro - 1)
    assert (
        admit(user, "member", registry=book, redis=client, shelf="ext").action
        == "allow"
    )
    # за лимитом — тоже парк (жёсткий стоп в обе стороны от порога)
    client.set(budget_global_key(), limit_micro + 1_000_000)
    assert (
        admit(user, "member", registry=book, redis=client, shelf="ext").action
        == "park"
    )


# ── 9. неизвестная роль → квота defaults.role (guest) ─────────────────


def test_unknown_role_falls_back_to_guest_quota(ws):
    client, book, user = ws
    assert book.quota_for("space-tourist").role == "guest"
    assert admit(user, "space-tourist", registry=book, redis=client).action == "allow"
    d = admit(user, "space-tourist", registry=book, redis=client)
    assert d.action == "deny"  # guest conc=1 — второй уже отказан
    assert d.code == "quota_conc_exceeded"


# ── хелперы Decision (offline-семантика, вне контракта 1-9) ───────────


def test_decision_park_and_allow_are_not_exceptions():
    Decision(action="park", code="budget_ext_exhausted").raise_if_denied()
    Decision(action="allow").raise_if_denied()
    assert Decision(action="allow").allowed
    assert not Decision(action="park").allowed


# ── 10. P1-2: списание conc-резерва по факту владения ──────────────────


def test_conc_release_by_ownership_no_foreign_decrement(ws):
    """release чужого job'а и повторный release — no-op; счётчик ==
    число живых маркеров; агрегат не уводится в минус/чужую сторону."""
    client, book, user = ws
    assert (
        admit(user, "member", registry=book, redis=client, job="job-a").action
        == "allow"
    )
    assert (
        admit(user, "member", registry=book, redis=client, job="job-b").action
        == "allow"
    )
    assert int(client.get(conc_key(user))) == 2

    assert conc_release(user, "job-unknown", redis=client) is False  # не брал
    assert int(client.get(conc_key(user))) == 2  # чужой резерв не тронут

    assert conc_release(user, "job-a", redis=client) is True
    assert conc_release(user, "job-a", redis=client) is False  # идемпотентен
    assert int(client.get(conc_key(user))) == 1
    assert client.smembers(conchold_key(user)) == {"job-b"}


# ── 11. P1-3: свипер мёртвых резервов ──────────────────────────────────


def test_sweep_reclaims_expired_reservation_leases_alive(ws):
    """Мёртвый воркер (lease истёк, heartbeat нет) → sweep снимает его
    резерв и пишет событие conc_reservation_reclaimed; живой lease не
    трогается; после снятия admit снова проходит (вечного deny нет)."""
    client, book, user = ws
    # уникальные job-id на прогон (аудит Ф4.4a): фиксированное имя ловило
    # устаревший матч по событиям прошлых прогонов в общем стриме
    dead, alive = f"{user}-job-dead", f"{user}-job-alive"
    assert (
        admit(
            user, "member", registry=book, redis=client, job=dead,
            lease_ttl_ms=150,
        ).action
        == "allow"
    )
    assert (
        admit(
            user, "member", registry=book, redis=client, job=alive,
            lease_ttl_ms=60_000,
        ).action
        == "allow"
    )
    assert int(client.get(conc_key(user))) == 2

    assert conc_reclaim_expired(user, alive, redis=client) is False
    assert conc_heartbeat(user, dead, redis=client, lease_ttl_ms=150) is True

    time_mod.sleep(0.25)  # lease job-dead истёк (heartbeat больше не продлевал)
    assert sweep_expired_conc(user, redis=client) == [dead]
    assert int(client.get(conc_key(user))) == 1
    assert client.smembers(conchold_key(user)) == {alive}
    assert conc_heartbeat(user, dead, redis=client) is False  # резерва нет

    # хвост общего стрима (аудит Ф4.4a): прежде полный xrange головы — O(n) по
    # всем накопленным событиям; теперь окно с хвоста + уникальный dead
    events = [
        json.loads(entry[1]["event"])
        for entry in client.xrevrange(QUOTA_EVENTS_KEY, count=200)
    ]
    assert any(
        e.get("type") == "conc_reservation_reclaimed" and e.get("job") == dead
        for e in events
    )


# ── 12. P1-4: деградация ws-redis — fail-closed + ALARM ────────────────


class _DeadCall:
    """Вызов зарегистрированного скрипта падает (реальный тип исключения
    redis-py — контракт, а не выдуманный атрибут)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        raise self._exc


class _DeadRedis:
    """Заглушка ws-redis: недоступен (connection refused / чёрная дыра)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def register_script(self, _source: str) -> _DeadCall:
        return _DeadCall(self._exc)

    def xadd(self, *_args: object, **_kwargs: object) -> None:
        raise self._exc


def test_admit_degrades_fail_closed_with_alarm(ws, caplog):
    """redis недоступен → QuotaRedisUnavailable (понятный отказ, cause
    сохранён), а не трейс; ALARM quota_degraded в logging; best-effort
    XADD не роняет обработку."""
    _, book, user = ws
    dead = _DeadRedis(redis.exceptions.ConnectionError("connection refused"))
    with caplog.at_level(
        logging.ERROR, logger="ai_workspace.scheduler.admission"
    ), pytest.raises(QuotaRedisUnavailable) as ei:
        admit(user, "member", registry=book, redis=dead)
    assert isinstance(ei.value.__cause__, redis.exceptions.ConnectionError)
    assert "fail-closed" in str(ei.value)
    degraded = [r for r in caplog.records if "quota_degraded" in r.getMessage()]
    assert degraded and "admit" in degraded[0].getMessage()


def test_charge_tokens_timeout_degrades_fail_closed():
    """«Чёрная дыра» (TimeoutError) → отказ fail-closed, не зависание."""
    dead = _DeadRedis(redis.exceptions.TimeoutError("black hole"))
    with pytest.raises(QuotaRedisUnavailable):
        charge_tokens("u-degraded", 5, redis=dead)


# ── 13. P2-9: битое значение ключа квот — читаемый отказ ───────────────


def test_corrupt_conc_value_fails_readable_not_runtime(ws):
    client, book, user = ws
    client.set(conc_key(user), "not-a-number")
    with pytest.raises(redis.exceptions.ResponseError, match="нечисловое"):
        admit(user, "guest", registry=book, redis=client)


# ── 14. P1-A iter2: conc=null (admin) — владение/lease безусловны ──────


def test_admin_job_admit_sets_ownership_and_lease_without_counter(ws):
    """admin (conc=null) с job=: маркер владения + lease ставятся БЕЗУСЛОВНО
    (heartbeat воркера жив — _beat не валит job), счётчик conc НЕ
    инкрементируется (личного лимита нет); release снимает владение без
    декремента (симметрия: роль без лимита счётчик не трогает ни при взятии,
    ни при возврате)."""
    client, book, user = ws
    assert admit(user, "admin", registry=book, redis=client, job="job-adm").action == "allow"
    assert client.get(conc_key(user)) is None  # счётчик не тронут (P1-A)
    assert client.sismember(conchold_key(user), "job-adm")  # владение есть
    assert client.exists(conclease_key(user, "job-adm"))  # lease есть
    assert conc_heartbeat(user, "job-adm", redis=client) is True  # _beat жив
    assert conc_release(user, "job-adm", redis=client) is True
    assert client.get(conc_key(user)) is None  # декремента не было (и не надо)
    assert client.smembers(conchold_key(user)) == set()


# ── 15. P1-B iter2: admit идемпотентен по (user, job) ──────────────────


def test_admit_same_job_idempotent_different_jobs_counted(ws):
    """Повторный admit того же job — allow БЕЗ повторного INCR (двойное
    взятие = перманентная утечка без маркера — свипер не снимет); разные
    job'ы считаются раздельно."""
    client, book, user = ws
    assert admit(user, "member", registry=book, redis=client, job="job-1").action == "allow"
    assert admit(user, "member", registry=book, redis=client, job="job-1").action == "allow"
    assert int(client.get(conc_key(user))) == 1  # НЕ 2 (идемпотентность)
    assert admit(user, "member", registry=book, redis=client, job="job-2").action == "allow"
    assert int(client.get(conc_key(user))) == 2  # разные job — раздельно
    assert conc_release(user, "job-1", redis=client) is True
    assert int(client.get(conc_key(user))) == 1


def test_admit_reused_flag_marks_guard_branch_only(ws):
    """N1 (reopen Ф4.2e): 4-й элемент ADMIT — reused: '1' только в allow
    по SISMEMBER-гварду (резерв уже стоит), '0' — при свежем взятии и
    в deny/park; wiring-компенсация разрешена только при reused=False."""
    client, book, user = ws
    fresh = admit(user, "member", registry=book, redis=client, job="job-r")
    assert fresh.action == "allow" and fresh.reused is False
    guard = admit(user, "member", registry=book, redis=client, job="job-r")
    assert guard.action == "allow" and guard.reused is True  # гвард, без INCR
    assert int(client.get(conc_key(user))) == 1
    limit = book.quota_for("member").tokens_per_day
    charge_tokens(user, limit, redis=client)
    denied = admit(user, "member", registry=book, redis=client, job="job-r2")
    assert denied.action == "deny" and denied.reused is False
    # другие job'ы после deny тоже fresh (гвард строго per-job)
    assert admit(user, "member", registry=book, redis=client, job="job-r3").reused is False


def test_readmit_in_expired_unswept_window_does_not_double_count(ws):
    """Окно «lease истёк, свип ещё не прошёл»: резерв жив (маркер+счётчик),
    повторный admit того же job — refresh lease, счётчик НЕ дублируется
    (пробник критика iter2: member 1→2 — перманентная утечка)."""
    client, book, user = ws
    assert (
        admit(user, "member", registry=book, redis=client, job="job-w", lease_ttl_ms=150).action
        == "allow"
    )
    time_mod.sleep(0.25)  # lease истёк, свип НЕ зван
    assert conc_heartbeat(user, "job-w", redis=client) is False  # lease мёртв
    assert admit(user, "member", registry=book, redis=client, job="job-w").action == "allow"
    assert int(client.get(conc_key(user))) == 1  # было 2 (утечка iter2-пробника)
    assert conc_heartbeat(user, "job-w", redis=client) is True  # lease ожил


def test_guest_readmit_in_expired_unswept_window_not_self_denied(ws):
    """Гость (conc=1): свой протухший (несвипнутый) резерв НЕ блокирует
    re-admit того же job — было deny quota_conc_exceeded (self-deny,
    стоп до ручной правки)."""
    client, book, user = ws
    assert (
        admit(user, "guest", registry=book, redis=client, job="job-g", lease_ttl_ms=150).action
        == "allow"
    )
    time_mod.sleep(0.25)
    d = admit(user, "guest", registry=book, redis=client, job="job-g")
    assert d.action == "allow"  # было deny (пробник критика)
    assert int(client.get(conc_key(user))) == 1
