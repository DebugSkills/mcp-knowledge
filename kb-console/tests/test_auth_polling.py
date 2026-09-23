"""Тесты auth_polling — стоп-условие, бэкофф, key-scope, арбитраж (007 §7.5-§7.7)."""

from __future__ import annotations

import pytest

from kb_console.core.auth_polling import Backoff, PollOutcome, poll_step
from kb_console.core.auth_state import (
    AuthenticationError,
    AuthState,
    ForbiddenError,
    TransportError,
)

# ── Backoff: последовательность 1→2→4→8→16…cap 60, reset (§7.6) ──


class TestBackoff:
    def test_initial_interval_is_base(self):
        """Живой ключ: примитив прозрачен, интервал штатный (R3)."""
        assert Backoff(base=1.0).interval == 1.0
        assert Backoff(base=2.0).interval == 2.0

    def test_backoff_sequence_1_2_4_8_16(self):
        """AC#3: после сбоев 1..5 interval = 1,2,4,8,16 (factor=16 на 5-м)."""
        backoff = Backoff(base=1.0, cap=60.0)
        observed = [backoff.bump() for _ in range(5)]
        assert observed == [1.0, 2.0, 4.0, 8.0, 16.0]
        assert backoff.factor == 16.0  # ⇒ 1/16 с⁻¹ = 3.75 req/мин ≤ 4

    def test_backoff_capped_at_60(self):
        backoff = Backoff(base=1.0, cap=60.0)
        for _ in range(20):
            interval = backoff.bump()
        assert interval == 60.0
        assert backoff.interval == 60.0

    def test_first_failure_keeps_interval(self):
        """R3: единичный флап не растягивает интервал (бит-в-бит прежнее)."""
        backoff = Backoff(base=1.0)
        backoff.bump()
        assert backoff.interval == 1.0
        assert backoff.factor == 1.0

    def test_reset_on_success(self):
        backoff = Backoff(base=1.0)
        for _ in range(6):
            backoff.bump()
        backoff.reset()
        assert backoff.interval == 1.0
        assert backoff.factor == 1.0

    def test_base_preserved_for_slow_pollers(self):
        """Интервалы поллеров НЕ меняются: бэкофф умножает штатный base."""
        backoff = Backoff(base=10.0, cap=60.0)
        assert backoff.bump() == 10.0
        assert backoff.bump() == 20.0
        assert backoff.bump() == 40.0
        assert backoff.bump() == 60.0  # cap

    def test_factor_gt1_only_after_repeated_failures(self):
        """P7-арбитраж: factor>1 только со 2-го сбоя подряд."""
        backoff = Backoff(base=1.0)
        backoff.bump()
        assert not backoff.factor > 1
        backoff.bump()
        assert backoff.factor > 1

    def test_invalid_base_rejected(self):
        with pytest.raises(ValueError):
            Backoff(base=0.0)


# ── Стоп-условие: 401 → 0 автоматических запросов (AC#1) ────────


class TestPollStepStop:
    async def test_first_401_blocks_and_zero_calls_after(self):
        """AC#1: первый тик 401 → блок; 60 последующих тиков → 0 вызовов."""
        state = AuthState()
        backoff = Backoff(base=1.0)
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            raise AuthenticationError("Ошибка аутентификации: неверный API-ключ (401)")

        outcome = await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
        assert outcome.skipped and outcome.auth_blocked
        assert calls["n"] == 1

        for _ in range(60):
            await poll_step(fetch, key_ref="global", backoff=backoff, state=state)

        assert calls["n"] == 1  # 0 автоматических запросов после блока

    async def test_403_blocks_too(self):
        state = AuthState()
        backoff = Backoff()

        async def fetch():
            raise ForbiddenError("Доступ запрещён: недостаточно прав (403)")

        outcome = await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
        assert outcome.skipped and outcome.auth_blocked
        assert state.blocked_kind(key_ref="global") == "ForbiddenError"

    async def test_on_auth_blocked_called_once(self):
        state = AuthState()
        backoff = Backoff()
        shown: list[str] = []

        async def fetch():
            raise AuthenticationError("401")

        for _ in range(3):
            await poll_step(
                fetch,
                key_ref="global",
                backoff=backoff,
                state=state,
                on_auth_blocked=lambda exc: shown.append(str(exc)),
            )
        # баннер показывается один раз — при первом отказе
        assert len(shown) == 1

    async def test_unblock_resumes(self):
        state = AuthState()
        backoff = Backoff()
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise AuthenticationError("401")
            return "ok"

        await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
        state.unblock(key_ref="global")  # ручное возобновление
        outcome = await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
        assert not outcome.skipped
        assert outcome.value == "ok"


# ── Бэкофф: TransportError → интервалы растут ───────────────────


class TestPollStepBackoff:
    async def test_five_transport_failures_intervals(self):
        """Интеграция AC#3: 5 транспорт-сбоев → 1,2,4,8,16."""
        state = AuthState()
        backoff = Backoff(base=1.0)
        intervals: list[float] = []

        async def fetch():
            raise TransportError("Сервер временно недоступен: degraded (503)")

        for _ in range(5):
            outcome = await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
            assert outcome.skipped
            assert not outcome.auth_blocked
            assert outcome.transport_error is not None
            intervals.append(outcome.interval)

        assert intervals == [1.0, 2.0, 4.0, 8.0, 16.0]

    async def test_success_resets_backoff(self):
        state = AuthState()
        backoff = Backoff(base=1.0)

        async def fail():
            raise TransportError("timeout")

        async def ok():
            return 42

        for _ in range(4):
            await poll_step(fail, key_ref="global", backoff=backoff, state=state)
        outcome = await poll_step(ok, key_ref="global", backoff=backoff, state=state)
        assert outcome.value == 42
        assert outcome.interval == 1.0
        assert backoff.factor == 1.0

    async def test_transport_does_not_block_auth_state(self):
        """Транспорт ≠ отказ ключа: AuthState чист, поллер жив."""
        state = AuthState()
        backoff = Backoff()

        async def fetch():
            raise TransportError("Сервер недоступен")

        await poll_step(fetch, key_ref="global", backoff=backoff, state=state)
        assert not state.is_blocked(key_ref="global")


# ── Key-scope (AC#4) ────────────────────────────────────────────


class TestPollStepKeyScope:
    async def test_blocked_key_a_poller_key_b_still_fetches(self):
        state = AuthState()
        backoff_a, backoff_b = Backoff(), Backoff()
        calls = {"n": 0}

        async def fetch_a():
            raise AuthenticationError("401")

        async def fetch_b():
            calls["n"] += 1
            return "b"

        await poll_step(fetch_a, key_ref="key-A", backoff=backoff_a, state=state)
        for _ in range(5):
            outcome = await poll_step(fetch_b, key_ref="key-B", backoff=backoff_b, state=state)
            assert not outcome.skipped
        assert calls["n"] == 5

    async def test_poll_outcome_dataclass_defaults(self):
        outcome = PollOutcome(skipped=True)
        assert outcome.value is None
        assert outcome.auth_blocked is False
        assert outcome.transport_error is None
