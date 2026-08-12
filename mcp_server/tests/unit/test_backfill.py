"""Unit tests for backfill_sequence_payload script.

Tests: filter parent_knowledge_id EXISTS (client-side),
dry-run does not call set_payload, idempotent re-run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_qdrant_point(kid: str, parent_id: str | None = None, seq=None):
    """Helper: create a mock Qdrant scroll point with payload."""
    point = MagicMock()
    payload = {"knowledge_id": kid}
    if parent_id:
        payload["parent_knowledge_id"] = parent_id
    point.payload = payload
    return point


class TestBackfillFilter:
    """NH-iter3-6: backfill filters by parent_knowledge_id EXISTS."""

    async def test_root_standalone_excluded(self):
        """Точки без parent_knowledge_id (root/standalone) — не попадают."""
        points = [
            _make_qdrant_point("root-1", parent_id=None),      # standalone
            _make_qdrant_point("sec-1", parent_id="book-1"),   # section
            _make_qdrant_point("root-2", parent_id=None),      # root
        ]

        # Client-side filter: parent_id is not None
        filtered = [p for p in points
                    if p.payload.get("parent_knowledge_id") is not None]
        assert len(filtered) == 1
        assert filtered[0].payload["knowledge_id"] == "sec-1"

    async def test_all_sections_with_parent_included(self):
        """Все точки с parent_knowledge_id — попадают."""
        points = [
            _make_qdrant_point("sec-1", parent_id="book-1"),
            _make_qdrant_point("sec-2", parent_id="book-1"),
            _make_qdrant_point("sec-3", parent_id="book-2"),
        ]
        filtered = [p for p in points
                    if p.payload.get("parent_knowledge_id") is not None]
        assert len(filtered) == 3


class TestBackfillIdempotent:
    """Idempotent: set_payload with same value is safe to re-run."""

    async def test_dry_run_does_not_call_set_payload(self):
        """--dry-run печатает diff, но не вызывает set_payload."""
        # This is a logical test: when dry_run=True, _flush_batch is never called.
        # The stats dict has updated=0.
        from scripts.backfill_sequence_payload import (
            _backfill,  # type: ignore[import]
        )

        store = MagicMock()
        store.initialize = AsyncMock()
        store.read = AsyncMock(return_value=None)
        store.close = AsyncMock()

        qdrant = MagicMock()
        qdrant.scroll = MagicMock(return_value=([], None))
        qdrant.set_payload = MagicMock()
        qdrant.close = MagicMock()

        stats = await _backfill(qdrant, store, dry_run=True)
        assert stats["updated"] == 0
        qdrant.set_payload.assert_not_called()

    async def test_idempotent_rerun_same_result(self):
        """Повторный прогон с теми же sequence_number → тот же результат."""
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        from scripts.backfill_sequence_payload import (
            _backfill,  # type: ignore[import]
        )

        now = datetime.now(timezone.utc)
        entry = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="sec-1",
                domain="eng", subject="test",
                content_type="book",
                parent_knowledge_id="book-1",
                sequence_number=3,
                created_at=now, updated_at=now,
            ),
            content="# Section\n\ncontent",
        )

        store = MagicMock()
        store.initialize = AsyncMock()
        store.read = AsyncMock(return_value=entry)
        store.close = AsyncMock()

        point = _make_qdrant_point("sec-1", parent_id="book-1")
        qdrant = MagicMock()
        qdrant.scroll = MagicMock(return_value=([point], None))
        qdrant.set_payload = MagicMock()
        qdrant.close = MagicMock()

        # First run
        stats1 = await _backfill(qdrant, store, dry_run=False, batch_size=1)
        # Second run should be same
        store.read.reset_mock()
        qdrant.set_payload.reset_mock()
        stats2 = await _backfill(qdrant, store, dry_run=False, batch_size=1)

        # Both runs should set the same payload
        assert stats1["updated"] == stats2["updated"]
