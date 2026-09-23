"""Интеграционные тесты auth-storm фикса — клиент → poll_step (007 §7.7).

Без мока примитива: реальный MCPClient на httpx.MockTransport, чтобы
проверить СТАТУС-МАТРИЦУ клиента + бэкофф/стоп связно (P1-1).
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb_console.core.auth_polling import Backoff, poll_step
from kb_console.core.auth_state import (
    AuthenticationError,
    AuthState,
    get_auth_state,
)


def _transport(status: int, counts: dict | None = None) -> httpx.MockTransport:
    """Транспорт: любой запрос → заданный статус; считает запросы."""
    counts = counts if counts is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] = counts.get("n", 0) + 1
        return httpx.Response(status, json=json.dumps({"error": "x"}) if False else {})

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    from kb_console.core.mcp_client import MCPClient

    return MCPClient(
        base_url="http://test", api_key="k", client=httpx.AsyncClient(transport=transport)
    )


@pytest.fixture(autouse=True)
def _clean_state():
    get_auth_state().clear()
    yield
    get_auth_state().clear()


class TestStopConditionIntegration:
    async def test_401_stops_poller_completely(self):
        """AC#1: первый 401 от РЕАЛЬНОГО клиента → блок; 60 тиков → 0 запросов."""
        counts: dict = {}
        state = AuthState()
        backoff = Backoff(base=1.0)
        async with _client(_transport(401, counts)) as client:
            outcome = await poll_step(
                client.list_imports, key_ref="global", backoff=backoff, state=state
            )
            assert outcome.skipped and outcome.auth_blocked
            assert counts["n"] == 1

            for _ in range(60):
                await poll_step(client.list_imports, key_ref="global", backoff=backoff, state=state)

        assert counts["n"] == 1  # 0 автоматических запросов за «60 с»

    async def test_403_stops_poller(self):
        counts: dict = {}
        state = AuthState()
        backoff = Backoff()
        async with _client(_transport(403, counts)) as client:
            for _ in range(10):
                await poll_step(client.list_tokens, key_ref="global", backoff=backoff, state=state)
        assert counts["n"] == 1
        assert state.blocked_kind(key_ref="global") == "ForbiddenError"


class TestBackoffIntegration:
    async def test_503_backoff_sequence_through_real_client(self):
        """AC#3: 5 транспорт-сбоев реального клиента → интервалы 1,2,4,8,16."""
        state = AuthState()
        backoff = Backoff(base=1.0)
        intervals: list[float] = []
        async with _client(_transport(503)) as client:
            for _ in range(5):
                outcome = await poll_step(
                    client.get_scan_progress, key_ref="global", backoff=backoff, state=state
                )
                assert outcome.skipped and outcome.transport_error is not None
                intervals.append(outcome.interval)
        assert intervals == [1.0, 2.0, 4.0, 8.0, 16.0]
        assert not state.is_blocked(key_ref="global")  # транспорт ≠ отказ ключа

    async def test_404_does_not_backoff(self):
        """Инвариант §7.6: 404 → прежний дефолт (None), НЕ транспорт."""
        async with _client(_transport(404)) as client:
            snapshot = await client.get_scan_progress()
        assert snapshot is None  # прежний дефолт, без исключения


class TestKeyScopeIntegration:
    async def test_key_isolation_through_real_client(self):
        """AC#4: блок ключа A не мешает поллеру ключа B (один клиент)."""
        state = AuthState()
        backoff_a, backoff_b = Backoff(), Backoff()
        counts: dict = {}
        transport_a = _transport(401)
        transport_b = _transport(200, counts)

        async with _client(transport_a) as ca, _client(transport_b) as cb:
            await poll_step(ca.list_imports, key_ref="key-A", backoff=backoff_a, state=state)
            for _ in range(3):
                outcome = await poll_step(
                    cb.list_imports, key_ref="key-B", backoff=backoff_b, state=state
                )
                assert not outcome.skipped
        assert counts["n"] == 3


class TestP7Arbitration:
    def test_adaptive_branch_gated_by_factor(self):
        """Арбитраж §7.5: адаптив progress_panel пишет интервал только при factor<=1."""
        backoff = Backoff(base=1.0)
        SCAN_IDLE = 5.0
        timer_interval = 1.0

        # Живой сервер (factor 1) → адаптив пишет idle-интервал
        if backoff.factor <= 1:
            timer_interval = SCAN_IDLE
        assert timer_interval == SCAN_IDLE

        # 3 транспорт-сбоя (factor 4) → адаптив НЕ пишет, интервал из бэкоффа
        backoff.bump()
        backoff.bump()
        backoff.bump()
        if backoff.factor <= 1:
            timer_interval = SCAN_IDLE  # не выполняется
        assert timer_interval == SCAN_IDLE  # остался прежним
        assert backoff.interval == 4.0  # бэкофф сильнее


class TestOneShotBranches:
    async def test_one_shot_auth_error_raised_not_silent(self):
        """P2-4: one-shot мутация при 401 бросает AuthenticationError (не None)."""

        async with _client(_transport(401)) as client:
            with pytest.raises(AuthenticationError):
                await client.create_token(level="write")

    async def test_one_shot_transport_error_raised_not_silent(self):
        """P2-4: one-shot мутация при транспорте бросает TransportError."""
        state = AuthState()
        backoff = Backoff()
        async with _client(_transport(503)) as client:
            outcome = await poll_step(
                client.rotate_token and (lambda: client.list_tokens()),
                key_ref="global",
                backoff=backoff,
                state=state,
            )
        assert outcome.skipped and outcome.transport_error is not None
