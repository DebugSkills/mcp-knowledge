"""Тонкая фабрика redis-клиента ws-контура (``ws:*``) для ai_workspace (Ф3.1).

DBD-Scan-решение (осознанное, НЕ молчаливый дубль): паттерн ПОВТОРЕН из
``kb-console/src/kb_console/core/redis_client.py`` (Ф2 #5b) — ленивый
``import redis``, env ``WS_REDIS_URL``, ``decode_responses=True``,
fail-closed ``RuntimeError`` без дефолтов, без логирования URL/паролей.
Причины не переиспользовать импортом:
- направление зависимости: kb-console (клиентское приложение) будет зависеть
  от ai_workspace (control-plane), а не наоборот;
- ai_workspace исполняется в server-side контуре и не должен тянуть пакет
  NiceGUI-приложения.
Консолидация возможна в Ф3.x (kb-console переключается на этот модуль) —
фиксация в ai_workspace/README.md.
"""

from __future__ import annotations

import os
from typing import Any

ENV_WS_REDIS_URL = "WS_REDIS_URL"

DEFAULT_SOCKET_CONNECT_TIMEOUT = 2.0
DEFAULT_SOCKET_TIMEOUT = 2.0
"""Таймауты ws-клиента (P1-4 критики Ф4): «чёрная дыра» (порт открыт,
ответа нет) не вешает вызов навсегда — соединение и каждая команда
обрываются отказом, который контуры ws (квоты — admission.py) переводят
в понятный fail-closed + ALARM, а не в зависание/трейс."""

_client: Any = None
"""Кэш singleton (лениво; ``reset_ws_redis()`` — тесты/смена конфига)."""


def make_ws_redis(
    url: str | None = None,
    *,
    socket_connect_timeout: float = DEFAULT_SOCKET_CONNECT_TIMEOUT,
    socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
) -> Any:
    """Новый redis-клиент ws-контура (``decode_responses=True``).

    ``url=None`` → env ``WS_REDIS_URL`` (compose: ``redis://ws-redis:6379/0``);
    отсутствует → ``RuntimeError`` (fail-closed). Подключение ленивое:
    реального I/O нет до первой команды. ``socket_connect_timeout`` /
    ``socket_timeout`` — явные таймауты (P1-4: деградация = быстрый отказ,
    не зависание; политика реакции — на контурах, напр. admission.py).
    """
    resolved = url or os.environ.get(ENV_WS_REDIS_URL)
    if not resolved:
        raise RuntimeError(
            "WS_REDIS_URL не задан: передайте url или установите env "
            f"{ENV_WS_REDIS_URL} (compose.workspace.yml: redis://ws-redis:6379/0)"
        )
    import redis  # лениво — модуль импортируется без установленного пакета

    return redis.Redis.from_url(
        resolved,
        decode_responses=True,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
    )


def get_ws_redis(url: str | None = None) -> Any:
    """Singleton-клиент ws-контура (лениво, один на процесс)."""
    global _client
    if _client is None:
        _client = make_ws_redis(url)
    return _client


def reset_ws_redis() -> None:
    """Сбросить singleton (юнит-тесты; смена конфига без рестарта)."""
    global _client
    _client = None
