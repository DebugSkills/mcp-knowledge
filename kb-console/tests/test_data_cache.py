"""Unit tests: DataCache — TTL + LRU + version-based invalidation (Task 1)."""

from __future__ import annotations

import pytest
from kb_console.core.data_cache import DataCache, cache


class TestDataCache:
    """Тесты DataCache: TTL, LRU, version mismatch/same, invalidation."""

    async def test_data_cache_ttl_expiry(self):
        """Кеш протухает после TTL."""
        dc = DataCache(max_size=10)
        call_count = 0

        async def fetch():
            nonlocal call_count
            call_count += 1
            return f"value-{call_count}"

        v1 = await dc.get("k1", fetch, ttl=0)  # ttl=0 — мгновенное истечение
        assert v1 == "value-1"
        assert call_count == 1

        v2 = await dc.get("k1", fetch, ttl=0)  # должен вызвать fetch заново
        assert v2 == "value-2"
        assert call_count == 2

    async def test_data_cache_lru_eviction(self):
        """LRU: старые записи вытесняются при превышении max_size."""
        dc = DataCache(max_size=3)

        async def fetch(val: str):
            return val

        # Заполняем до лимита
        await dc.get("a", lambda: fetch("A"), ttl=999)
        await dc.get("b", lambda: fetch("B"), ttl=999)
        await dc.get("c", lambda: fetch("C"), ttl=999)
        assert dc.size == 3

        # Добавляем четвёртую — "a" (LRU) должна вытесниться
        await dc.get("d", lambda: fetch("D"), ttl=999)
        assert dc.size == 3

        # "a" должен перестать быть в кеше
        call_count = 0

        async def fetch_a():
            nonlocal call_count
            call_count += 1
            return "A2"

        v = await dc.get("a", fetch_a, ttl=999)
        assert v == "A2"
        assert call_count == 1  # был miss → fetch

    async def test_data_cache_invalidate_on_version_mismatch(self):
        """check_version при mismatch чистит весь кеш."""
        dc = DataCache(max_size=10)

        async def fetch():
            return "some-data"

        await dc.get("k1", fetch, ttl=999)
        assert dc.size == 1
        dc._known_version = 5  # известно версии 5

        # Мок client с версией 6 (mismatch)
        class MockClient:
            async def get_data_version(self):
                return 6

        await dc.check_version(MockClient())
        assert dc._known_version == 6
        assert dc.size == 0  # кеш очищен

    async def test_data_cache_no_invalidate_same_version(self):
        """Одинаковый version → кеш НЕ очищается."""
        dc = DataCache(max_size=10)

        async def fetch():
            return "data"

        await dc.get("k1", fetch, ttl=999)
        assert dc.size == 1
        dc._known_version = 3

        class MockClient:
            async def get_data_version(self):
                return 3

        await dc.check_version(MockClient())
        assert dc._known_version == 3
        assert dc.size == 1  # кеш сохранился

    def test_data_cache_invalidate_single_key(self):
        """invalidate удаляет конкретный ключ."""
        dc = DataCache()
        dc._store["k1"] = ("val1", 1.0)
        dc._store["k2"] = ("val2", 1.0)
        assert dc.size == 2

        dc.invalidate("k1")
        assert dc.size == 1
        assert "k1" not in dc._store
        assert "k2" in dc._store

    def test_data_cache_invalidate_all(self):
        """invalidate_all чистит весь кеш."""
        dc = DataCache()
        dc._store["k1"] = ("v1", 1.0)
        dc._store["k2"] = ("v2", 1.0)
        dc.invalidate_all()
        assert dc.size == 0

    def test_data_cache_singleton(self):
        """Module-level cache это синглтон DataCache."""
        assert isinstance(cache, DataCache)
        assert cache._max_size == 100
