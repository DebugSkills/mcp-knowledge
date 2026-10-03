"""TTL-кэш Source-frontmatter (bibliography Ф4b2, план §3.4:189).

Read-time citation-enrichment читает SSOT-frontmatter Source-записей;
кэш закрывает повторные чтения внутри батча и между запросами:

- ключ — `source_id` (строка); значение — dict frontmatter (snapshot);
- TTL `SOURCE_CACHE_TTL_SEC` (паттерн `TOKEN_INDEX_TTL_SEC`, config.py:108) —
  defence-in-depth: первичная свежесть обеспечивается write-хуками
  `tools/source_ref_runtime` (refresh/index_add/index_remove → invalidate);
- размер ограничен `SOURCE_CACHE_MAX_ENTRIES` (LRU-вытеснение старейшего);
- потокобезопасен: `threading.Lock` (кодовая база async single-threaded,
  lock — дешёвая страховка при executor-доступе).

Только FOUND-значения кэшируются: miss (записи нет) НЕ кэшируется —
отсутствующий Source перечитывается (дешево, редкий случай), и появление
записи не требует инвалидации негативной записи.

CSL в Qdrant payload НЕ пишется (§3.4:191): модуль ничего не индексирует.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

from ..config import settings

logger = logging.getLogger("mcp_knowledge.content.source_cache")

#: Монотонные часы (модульная ссылка — патчится в тестах TTL).
_monotonic: Callable[[], float] = time.monotonic


class SourceFrontmatterCache:
    """Bounded LRU + TTL кэш source_id → frontmatter-dict."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 256,
        clock: Callable[[], float] = _monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def get(self, source_id: str) -> dict | None:
        """Snapshot frontmatter, если он в кэше и TTL не истёк; иначе None."""
        with self._lock:
            item = self._entries.get(source_id)
            if item is None:
                return None
            stored_at, fm = item
            if (self._clock() - stored_at) >= self._ttl:
                del self._entries[source_id]
                return None
            self._entries.move_to_end(source_id)  # LRU: свежее использование
            return fm

    def put(self, source_id: str, fm: dict) -> None:
        """Положить snapshot (dict копируется — иммутабельность значения)."""
        if not source_id or not isinstance(fm, dict):
            return
        with self._lock:
            self._entries[source_id] = (self._clock(), dict(fm))
            self._entries.move_to_end(source_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)  # вытеснить старейший

    def invalidate(self, source_id: str | None = None) -> None:
        """Снять один ключ; без аргумента — очистить целиком (write-хук)."""
        with self._lock:
            if source_id is None:
                self._entries.clear()
            else:
                self._entries.pop(source_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ── Глобальный экземпляр (паттерн _TOC_CACHE read.py:30) ────────────────

_CACHE: SourceFrontmatterCache | None = None


def get_source_cache() -> SourceFrontmatterCache:
    """Ленивый синглтон с настройками из config (TTL/max)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = SourceFrontmatterCache(
            ttl_seconds=float(settings.SOURCE_CACHE_TTL_SEC),
            max_entries=settings.SOURCE_CACHE_MAX_ENTRIES,
            clock=lambda: _monotonic(),  # позднее связывание: патчится в тестах
        )
    return _CACHE


def reset_source_cache() -> None:
    """Сброс синглтона (изоляция тестов / полный рескан)."""
    global _CACHE
    _CACHE = None


def invalidate_source_cache(source_id: str | None = None) -> None:
    """Инвалидация глобального кэша (вызывается write-хуками runtime)."""
    if _CACHE is not None:
        _CACHE.invalidate(source_id)
