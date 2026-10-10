"""Конфигурация тестов ai_workspace (Ф3.1; Ф1-acceptance — fail-loud).

- Регистрирует маркер ``integration`` (требует живой ws-redis).
- ``WS_REDIS_URL`` НЕ задан → integration-тесты авто-skip (unit-only запуск).
- ``WS_REDIS_URL`` задан, но PING не прошёл → жёсткий провал прогона
  (``pytest.exit`` в ``pytest_configure``): заданный контур не может быть
  молча пропущен, иначе «127 skipped» = ложный зелёный (skip-гонка
  ``ws-up-test`` → ``ws-test-integration``; ``make ws-up-test`` поднимает
  ws-redis на 127.0.0.1:6390 — test-only).
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
    # Ф1-acceptance: контур задан, но мёртв → fail-loud через hook (НЕ skip).
    if _FATAL_PROBE_MSG is not None:
        pytest.exit(_FATAL_PROBE_MSG, returncode=1)


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
    except Exception as exc:  # noqa: BLE001 — классифицируется ниже
        return False, f"недоступен по {url} ({type(exc).__name__})"
    return True, ""


REDIS_REACHABLE, SKIP_REASON = _probe_redis()

# Ф1-acceptance: проба выполняется при импорте conftest (requires_redis нужен
# тест-модулям уже на collection — до pytest_configure), поэтому жёсткий выход
# откладывается в hook pytest_configure, а не бросается «грязным» исключением.
_FATAL_PROBE_MSG: str | None = None
if not REDIS_REACHABLE and os.environ.get("WS_REDIS_URL"):
    _FATAL_PROBE_MSG = (
        f"WS_REDIS_URL задан, но ws-redis недоступен — {SKIP_REASON} — "
        "integration-прогон НЕ может быть пропущен (сначала `make ws-up-test`)."
    )

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
