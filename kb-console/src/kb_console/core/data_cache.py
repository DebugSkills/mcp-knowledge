"""DataCache — module-level singleton for kb-console data caching (Task 1).

Hybrid invalidation: TTL + server-side data_version check.
- get(key, fetch_fn, ttl): возвращает кешированное значение если свежее
- check_version(client): сравнивает с сервером, при mismatch — invalidate_all()
- LRU eviction: OrderedDict, max_size=100

Thread-safety: NiceGUI async — обычный asyncio (без threading.Lock).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class DataCache:
    """TTL + LRU in-memory cache with server-side version-based invalidation."""

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._known_version: int | None = None

    async def get(self, key: str, fetch_fn: Callable[[], Any], ttl: float = 15) -> Any:
        """Вернуть кешированное значение или fetch + store.

        Args:
            key: ключ кеша.
            fetch_fn: async callable → значение (вызывается при cache miss).
            ttl: время жизни в секундах (default 15).

        Returns:
            Кешированное или свежее значение.
        """
        now = time.monotonic()

        if key in self._store:
            value, fetched_at = self._store[key]
            if now - fetched_at < ttl:
                # LRU: move to end (most recently used)
                self._store.move_to_end(key)
                return value
            # TTL истёк — удаляем просроченное
            del self._store[key]

        # Cache miss — fetch fresh
        value = await fetch_fn()
        self._store[key] = (value, now)
        self._store.move_to_end(key)

        # LRU eviction
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

        return value

    async def check_version(self, client) -> None:
        """Сравнить с серверной data_version, при mismatch — invalidate_all().

        Args:
            client: MCPClient с методом get_data_version().
        """
        try:
            server_version = await client.get_data_version()
        except Exception:  # noqa: BLE001
            return  # сервер недоступен — кеш остаётся

        if self._known_version is None:
            self._known_version = server_version
            return

        if server_version != self._known_version:
            self._known_version = server_version
            self._store.clear()

    def invalidate_all(self) -> None:
        """Полная очистка кеша."""
        self._store.clear()

    def invalidate(self, key: str) -> None:
        """Удалить конкретный ключ."""
        self._store.pop(key, None)

    @property
    def known_version(self) -> int | None:
        """Известная data_version (для тестов)."""
        return self._known_version

    @property
    def size(self) -> int:
        """Число записей в кеше (для тестов)."""
        return len(self._store)


# Module-level singleton
cache = DataCache(max_size=100)
