"""Фабрика ws-redis-клиента (arch-2026-10-05-ai-workspace, Ф2 шаг #5b).

I11/I12: чат-сессии верстака живут в ОТДЕЛЬНОМ ws-redis (noeviction+AOF,
``compose.workspace.yml``). Клиент — для ``ConversationStore`` (шаг 5a,
duck-typed): ``decode_responses=True`` → store получает str (его ``_decode``
остаётся защитой от bytes-клиентов).

Контракт:
- ``import redis`` — только внутри ``make_ws_redis`` (лениво): модуль
  импортируется в юнит/CLI-контекстах без установленного пакета
  (прецедент: ``conversations.py`` без импорта redis).
- URL — только из аргумента или env ``WS_REDIS_URL``; отсутствует →
  ``RuntimeError`` с человекочитаемой причиной (fail-closed, без дефолтов
  мимо конфига).
- Server-side ONLY: модуль не импортируется клиентским кодом; наружу (UI)
  отдаётся только результат health-хелпера — dict без URL/паролей.
- Без логирования URL/паролей: в health — только класс исключения, не
  ``str(exc)`` (redis-ошибки несут host:port и query-строки с секретами).
"""

from __future__ import annotations

import os
from typing import Any

ENV_WS_REDIS_URL = "WS_REDIS_URL"

_client: Any = None
"""Кэш singleton (лениво; ``reset_ws_redis()`` — тесты/смена конфига)."""


def make_ws_redis(url: str | None = None) -> Any:
    """Новый redis-клиент ws-контура (``decode_responses=True``).

    ``url=None`` → env ``WS_REDIS_URL`` (compose: ``redis://ws-redis:6379/0``).
    Подключение ленивое: реального I/O нет до первой команды (redis-py).
    """
    resolved = url or os.environ.get(ENV_WS_REDIS_URL)
    if not resolved:
        raise RuntimeError(
            "WS_REDIS_URL не задан: передайте url или установите env "
            f"{ENV_WS_REDIS_URL} (compose.workspace.yml: redis://ws-redis:6379/0)"
        )
    import redis  # лениво — см. докстринг модуля

    return redis.Redis.from_url(resolved, decode_responses=True)


def get_ws_redis(url: str | None = None) -> Any:
    """Singleton-клиент ws-контура (лениво, один на процесс).

    ``url`` учитывается только при первом создании (до ``reset_ws_redis``).
    """
    global _client
    if _client is None:
        _client = make_ws_redis(url)
    return _client


def reset_ws_redis() -> None:
    """Сбросить singleton (юнит-тесты; смена конфига без рестарта)."""
    global _client
    _client = None


def ws_redis_health(client: Any) -> dict[str, Any]:
    """Health-проба (``PING``): ``{"ok": True}`` / ``{"ok": False, ...}``.

    Для UI/мониторинга: без URL/паролей — только класс исключения
    (``str`` redis-ошибок содержит host:port — не выносить наружу).
    """
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 — health-хелпер: любая ошибка = not ok
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True}
