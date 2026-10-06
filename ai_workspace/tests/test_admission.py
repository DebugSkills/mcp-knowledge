"""Integration-тесты scheduler/admission (Ф4.2) — живой ws-redis.

Ключевые доказательства (контракт Ф4.2):
- deny-коды D3/D6 (токены дня / личный параллелизм) и park D5 (ext-бюджет
  исчерпан — НЕ deny);
- атомарность conc-резерва: N параллельных admit guest (conc=1) → ровно
  1 allow — проверка и INCR в одной Lua;
- TTL дневного ключа до локальной полуночи (D7), повторный charge не
  продлевает жизнь ключа за полночь;
- фолбэк неизвестной роли на квоту defaults.role (Ф4.1).

Реестр — боевой (read-only, не мутируется); redis — изолированные
пользователи test-f42-* (паттерн test_slots_lua).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ai_workspace.registry import Registry
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler.admission import (
    BUDGET_GLOBAL_KEY,
    AdmissionDenied,
    Decision,
    admit,
    charge_tokens,
    conc_exit,
    conc_key,
    seconds_to_local_midnight,
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
    keys += list(client.scan_iter(match=f"ws:budget:{user}"))
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
    limit = book.budgets["ext"].limit
    client.set(BUDGET_GLOBAL_KEY, limit)  # ровно лимит → исчерпан (>=)
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
    # ниже лимита ext пропускает
    client.set(BUDGET_GLOBAL_KEY, limit - 1)
    assert (
        admit(user, "member", registry=book, redis=client, shelf="ext").action
        == "allow"
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
