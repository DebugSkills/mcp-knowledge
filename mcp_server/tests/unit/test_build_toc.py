"""Unit tests for _build_toc (on-the-fly TOC from Qdrant scroll).

Tests: dedupe by knowledge_id, sort by sequence, fallback sort by title,
cache (data_version + TTL), MAX_TOC_SECTIONS guard.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from mcp_server.storage.schema import ZONE_PRIVATE
from mcp_server.tools.read import (
    _TOC_CACHE,
    _TOC_TTL,
    MAX_TOC_SECTIONS,
    _build_toc,
    _toc_cache_key,
)

pytestmark = pytest.mark.asyncio


def _make_point(kid: str, chunk_idx: int, seq=None, content="", updated_at=""):
    """Helper: create a mock Qdrant scroll point."""
    point = MagicMock()
    point.payload = {
        "knowledge_id": kid,
        "chunk_index": chunk_idx,
        "sequence_number": seq,
        "content": content,
        "updated_at": updated_at,
    }
    return point


class TestBuildToc:
    """Core _build_toc behavior tests."""

    def setup_method(self):
        """Clear TOC cache between tests."""
        _TOC_CACHE.clear()

    async def test_dedupe_multi_chunk_keeps_title_from_chunk_0(self, app_state):
        """Секция с 3 чанками (chunk_index 0,1,2) → 1 запись, title из chunk_index==0."""
        points = [
            _make_point("sec-1", 0, 1, "# Main Title\n..."),
            _make_point("sec-1", 1, 1, "body paragraph"),
            _make_point("sec-1", 2, 1, "more body"),
        ]
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        toc = await _build_toc("book-id", app_state)
        assert len(toc) == 1
        assert toc[0]["title"] == "Main Title"
        assert toc[0]["sequence_number"] == 1

    async def test_dedupe_picks_min_chunk_index(self, app_state):
        """При разных порядках чанков — title всегда из chunk_index==0."""
        points = [
            _make_point("sec-1", 2, 1, "# Wrong Title\n..."),  # more chunks first
            _make_point("sec-1", 0, 1, "# Correct Title\n..."),
            _make_point("sec-1", 1, 1, "body"),
        ]
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        toc = await _build_toc("book-id", app_state)
        assert len(toc) == 1
        assert toc[0]["title"] == "Correct Title"

    async def test_sort_by_sequence_number_ascending(self, app_state):
        """Секции с seq=5,2,3 → порядок 2,3,5."""
        points = [
            _make_point("sec-1", 0, 5, "# Z\n..."),
            _make_point("sec-2", 0, 2, "# A\n..."),
            _make_point("sec-3", 0, 3, "# M\n..."),
        ]
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        toc = await _build_toc("book-id", app_state)
        seqs = [s["sequence_number"] for s in toc]
        assert seqs == [2, 3, 5]

    async def test_fallback_sort_by_title_when_sequence_missing(self, app_state):
        """Секции без sequence_number → сортировка по title + warning."""
        points = [
            _make_point("sec-1", 0, None, "# C\n..."),
            _make_point("sec-2", 0, None, "# A\n..."),
            _make_point("sec-3", 0, 1, "# B\n..."),
        ]
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        toc = await _build_toc("book-id", app_state)
        # Сначала seq=1 (B), потом остальные по title (A, C) — None идут в конец
        titles = [s["title"] for s in toc]
        assert titles[0] == "B"   # has sequence_number=1
        assert titles[1] == "A"   # no sequence, fallback title
        assert titles[2] == "C"   # no sequence, fallback title

    async def test_cache_hit_no_scroll_on_same_data_version(self, app_state):
        """data_version не изменился → повторный вызов НЕ делает scroll."""
        points = [_make_point("sec-1", 0, 1, "# A\n...")]
        mock_scroll = MagicMock(return_value=(points, None))
        app_state.qdrant.scroll = mock_scroll

        # Clear cache
        _TOC_CACHE.clear()

        toc1 = await _build_toc("book-id", app_state)
        assert len(toc1) == 1
        scroll_calls = mock_scroll.call_count

        # Second call — should hit cache
        toc2 = await _build_toc("book-id", app_state)
        assert len(toc2) == 1
        assert mock_scroll.call_count == scroll_calls  # no new scroll

    async def test_cache_invalidated_on_data_version_change(self, app_state):
        """data_version изменился → кэш инвалидирован → повторный scroll."""
        points = [_make_point("sec-1", 0, 1, "# A\n...")]
        mock_scroll = MagicMock(return_value=(points, None))
        app_state.qdrant.scroll = mock_scroll

        _TOC_CACHE.clear()

        await _build_toc("book-id", app_state)
        scroll_calls_before = mock_scroll.call_count

        # Change data_version
        app_state.data_version += 1
        toc = await _build_toc("book-id", app_state)
        assert len(toc) == 1
        assert mock_scroll.call_count == scroll_calls_before + 1  # new scroll

    async def test_cache_ttl_expiry_forces_rescroll(self, app_state):
        """TTL истёк → повторный scroll даже при неизменном data_version."""
        points = [_make_point("sec-1", 0, 1, "# A\n...")]
        mock_scroll = MagicMock(return_value=(points, None))
        app_state.qdrant.scroll = mock_scroll

        _TOC_CACHE.clear()

        await _build_toc("book-id", app_state)
        scroll_before = mock_scroll.call_count

        # Force TTL expiry by manipulating the cache entry
        cached = _TOC_CACHE.get(_toc_cache_key(ZONE_PRIVATE, "book-id"))
        if cached:
            # Set timestamp to old enough that TTL expires
            _TOC_CACHE[_toc_cache_key(ZONE_PRIVATE, "book-id")] = (cached[0], time.monotonic() - _TOC_TTL - 1, cached[2])

        await _build_toc("book-id", app_state)
        assert mock_scroll.call_count == scroll_before + 1

    async def test_max_toc_sections_guard_truncates(self, app_state):
        """>MAX_TOC_SECTIONS точек → truncate + warning (тест на guard)."""
        # Create more points than MAX_TOC_SECTIONS
        points = [
            _make_point(f"sec-{i}", 0, i, f"# {i}\n...") for i in range(100)
        ]
        # Mock scroll to return all in one batch
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        # Temporarily lower the guard for testing
        original_guard = MAX_TOC_SECTIONS
        try:
            import mcp_server.tools.read as read_mod
            read_mod.MAX_TOC_SECTIONS = 50

            with patch("mcp_server.tools.read.logger") as mock_logger:
                toc = await _build_toc("book-id", app_state)
                # Should truncate at ~50 (the guard limit)
                assert len(toc) <= 50
                # Warning should be logged
                mock_logger.warning.assert_called()
        finally:
            read_mod.MAX_TOC_SECTIONS = original_guard

    async def test_sections_without_content_generate_fallback_title(self, app_state):
        """Секция без content получает fallback title = knowledge_id."""
        points = [
            _make_point("sec-abc", 0, 1, "", updated_at="2026-01-01T00:00:00Z"),
        ]
        app_state.qdrant.scroll = MagicMock(return_value=(points, None))

        toc = await _build_toc("book-id", app_state)
        assert len(toc) == 1
        assert toc[0]["title"] == "sec-abc"  # fallback to knowledge_id
        assert toc[0]["updated_at"] == "2026-01-01T00:00:00Z"
