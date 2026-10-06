"""Конфигурация тестов ai_workspace (Ф3.1).

- Регистрирует маркер ``integration`` (требует живой ws-redis).
- Авто-skip integration-тестов: ``WS_REDIS_URL`` не задан или PING не прошёл
  (``make ws-up-test`` поднимает ws-redis на 127.0.0.1:6390 — test-only).
"""

from __future__ import annotations

import os

import pytest

WS_TEST_ID_PREFIX = "test-"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: требует живой ws-redis (WS_REDIS_URL; make ws-up-test)",
    )


def _probe_redis() -> tuple[bool, str]:
    url = os.environ.get("WS_REDIS_URL")
    if not url:
        return False, "WS_REDIS_URL не задан (unit-only запуск)"
    try:
        import redis

        client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 — любая ошибка пробы = skip
        return False, f"ws-redis недоступен по WS_REDIS_URL ({type(exc).__name__})"
    return True, ""


REDIS_REACHABLE, SKIP_REASON = _probe_redis()

requires_redis = pytest.mark.skipif(not REDIS_REACHABLE, reason=SKIP_REASON)


@pytest.fixture()
def job_store():
    """JobStore на живом Redis + уборка ТОЛЬКО своих ключей (ws:job:test-*)."""
    from ai_workspace.orchestrator.job import JobStore
    from ai_workspace.redis_client import make_ws_redis

    store = JobStore(make_ws_redis())
    yield store
    keys = list(store.client.scan_iter(match=f"ws:job:{WS_TEST_ID_PREFIX}*"))
    if keys:
        store.client.delete(*keys)
