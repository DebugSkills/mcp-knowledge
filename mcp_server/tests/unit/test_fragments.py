"""Unit tests for fragment operations (Фаза 13.23).

Tests: add_fragment (validations, sequence, ID formula, sanitization),
update_fragment (title rewrite, version conflict, empty-content, parent guard),
delete_fragment (parent guard, success), find_fragment (validation, results).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.storage.schema import COLLECTION_PRIVATE
from mcp_server.tools.fragments import (
    _rewrite_heading,
    _sanitize_fragment_title,
    add_fragment,
    delete_fragment,
    find_fragment,
    update_fragment,
)

pytestmark = pytest.mark.asyncio


# ── Title sanitization helpers ─────────────────────────────

class TestSanitizeTitle:
    def test_strips_newlines(self):
        assert _sanitize_fragment_title("hello\nworld") == "hello world"

    def test_strips_carriage_return(self):
        # \r\n → пробел, двойной пробел от обоих символов
        assert _sanitize_fragment_title("hello\r\nworld") in ("hello world", "hello  world")

    def test_strips_leading_hashes(self):
        assert _sanitize_fragment_title("## My Title") == "My Title"
        assert _sanitize_fragment_title("#Title") == "Title"

    def test_truncates_to_120_chars(self):
        long_title = "x" * 200
        assert len(_sanitize_fragment_title(long_title)) == 120

    def test_empty_after_sanitize(self):
        assert _sanitize_fragment_title("   ") == ""


class TestRewriteHeading:
    def test_replaces_existing_heading(self):
        result = _rewrite_heading("# Old Title\n\ncontent here", "New Title")
        assert result == "# New Title\n\ncontent here"

    def test_prepends_when_no_heading(self):
        result = _rewrite_heading("just some text", "New Title")
        assert result == "# New Title\n\njust some text"

    def test_replaces_only_first_heading(self):
        result = _rewrite_heading("# First\nbody\n# Second", "New Title")
        assert result.startswith("# New Title")
        assert "# Second" in result


# ── add_fragment tests ─────────────────────────────────────

class TestAddFragment:
    async def test_missing_collection_id(self, app_state):
        result = await add_fragment({"title": "T", "content": "C"}, app_state)
        assert "error" in result
        assert "collection_id" in result["error"].lower()

    async def test_missing_title(self, app_state):
        result = await add_fragment(
            {"collection_id": "cid", "content": "C"}, app_state,
        )
        assert "error" in result
        assert "title" in result["error"].lower()

    async def test_missing_content(self, app_state):
        result = await add_fragment(
            {"collection_id": "cid", "title": "T"}, app_state,
        )
        assert "error" in result
        assert "content" in result["error"].lower()

    async def test_empty_content_nh_iter3_1(self, app_state):
        result = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "T", "content": "   "},
            app_state,
        )
        assert "error" in result
        assert "empty" in result["error"].lower()

    async def test_collection_not_found(self, app_state):
        app_state.store.read = AsyncMock(return_value=None)
        result = await add_fragment(
            {"collection_id": "nonexistent", "title": "T", "content": "C"},
            app_state,
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_not_a_collection(self, app_state):
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        standalone = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="not-a-book",
                domain="eng", subject="test",
                content_type="pdf",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="Some content",
        )
        app_state.store.read = AsyncMock(return_value=standalone)
        result = await add_fragment(
            {"collection_id": "not-a-book", "title": "T", "content": "C"},
            app_state,
        )
        assert "error" in result
        assert "not a book" in result["error"].lower()

    async def test_deprecated_collection_nh_iter2_1(self, app_state):
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        deprecated = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="old-book",
                domain="eng", subject="test",
                content_type="collection",
                status="deprecated",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Old Book\n\n...",
        )
        app_state.store.read = AsyncMock(return_value=deprecated)
        result = await add_fragment(
            {"collection_id": "old-book", "title": "T", "content": "C"},
            app_state,
        )
        assert "error" in result
        assert "deprecated" in result["error"].lower()

    async def test_stale_payload_deprecated_does_not_override_ssot(self, app_state):
        """Ф3-fix2a (P2-2): SSOT-winner — payload лишь производная от SSOT.

        Легаси payload-only deprecate (до Ф3) / частичный сбой payload-записи
        НЕ блокируют add_fragment: канонический статус живёт в SSOT
        frontmatter (двойная запись SSOT-first из _lifecycle_transition),
        reconcile дозалечивает payload ИЗ SSOT, не наоборот.
        Прежний payload-wins тест (NH-iter2-1) инвертирован осознанно.
        """
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        published_root = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="payload-dep-book",
                domain="eng", subject="test",
                content_type="collection",
                status="published",  # SSOT — источник правды
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Book\n\n...",
        )
        app_state.store.read = AsyncMock(return_value=published_root)
        # Qdrant payload говорит deprecated (stale) — НЕ источник правды
        dep_point = MagicMock()
        dep_point.payload = {"knowledge_id": "payload-dep-book", "status": "deprecated"}
        app_state.qdrant.scroll = MagicMock(return_value=([dep_point], None))
        app_state.store.write_entry = AsyncMock()
        app_state.store.flush = AsyncMock()
        app_state.pipeline.enqueue = AsyncMock(return_value=type("R", (), {"indexed": True})())

        result = await add_fragment(
            {"collection_id": "payload-dep-book", "title": "T", "content": "C"},
            app_state,
        )
        assert "error" not in result, result

    async def test_ssot_deprecated_wins_over_stale_payload_published(self, app_state):
        """Ф3-fix2a (P2-2): расхождение SSOT=deprecated vs payload=published
        (частичный сбой payload-этапа после успешной SSOT-записи в
        _lifecycle_transition) → guard БЛОКИРУЕТ: SSOT winner.

        Прежний payload-wins тест (payload published «воскрешал» SSOT
        deprecated) инвертирован осознанно: производная (payload) не может
        отменять источник правды (frontmatter). Мутация «вернуть
        payload-wins» роняет этот тест.
        """
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        deprecated_root = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="restored-book",
                domain="eng", subject="test",
                content_type="collection",
                status="deprecated",  # SSOT-запись состоялась
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Book\n\n...",
        )
        app_state.store.read = AsyncMock(return_value=deprecated_root)
        # Stale payload уверяет «published» (payload-запись не дошла) —
        # не может воскресить deprecated-книгу.
        ok_point = MagicMock()
        ok_point.payload = {"knowledge_id": "restored-book", "status": "published"}
        app_state.qdrant.scroll = MagicMock(return_value=([ok_point], None))

        result = await add_fragment(
            {"collection_id": "restored-book", "title": "T", "content": "C"},
            app_state,
        )
        assert "error" in result
        assert "deprecated" in result["error"].lower()

    async def test_ssot_write_precedes_payload_and_survives_payload_failure(
        self, app_state,
    ):
        """Ф3-fix2a (P2-2): SSOT-first порядок — store.write_entry (SSOT
        frontmatter) строго ДО pipeline.enqueue (Qdrant payload); падение
        payload-этапа НЕ роняет SSOT-исход (winner): flush коммитится,
        ответ без error, indexed=False."""
        order: list[str] = []
        app_state.store.write_entry = AsyncMock(
            side_effect=lambda entry: order.append("ssot_write"),
        )
        app_state.store.flush = AsyncMock(
            side_effect=lambda *a, **k: order.append("git_commit"),
        )
        app_state.pipeline.enqueue = AsyncMock(
            side_effect=RuntimeError("payload index down"),
        )

        result = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "Order", "content": "body"},
            app_state,
        )

        assert "error" not in result, result
        assert result["indexed"] is False
        app_state.pipeline.enqueue.assert_awaited_once()
        # SSOT записан и закоммичен; payload-сбой не прервал SSOT-исход
        assert order == ["ssot_write", "git_commit"]

    async def test_title_sanitization_applied(self, app_state):
        """Title with \n and leading # is sanitized in the generated body."""
        app_state.store.write_entry = AsyncMock()
        app_state.store.flush = AsyncMock()

        result = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "## Hello\nWorld", "content": "content here"},
            app_state,
        )
        # Should succeed (no error)
        assert "error" not in result
        # The body written to store should have sanitized title
        write_call = app_state.store.write_entry.call_args[0][0]
        assert write_call.content.startswith("# Hello World")
        # The knowledge_id uses slugified title: "hello-world"
        assert "hello-world" in write_call.frontmatter.knowledge_id

    async def test_sequence_is_max_plus_one(self, app_state):
        """Add to a book with existing sections, verify seq = max+1."""
        # Mock _build_toc to return sections with seq 1,2,3
        with patch("mcp_server.tools.fragments._build_toc") as mock_toc:
            mock_toc.return_value = [
                {"knowledge_id": "a", "title": "A", "sequence_number": 1},
                {"knowledge_id": "b", "title": "B", "sequence_number": 2},
                {"knowledge_id": "c", "title": "C", "sequence_number": 3},
            ]
            app_state.store.write_entry = AsyncMock()
            app_state.store.flush = AsyncMock()

            result = await add_fragment(
                {"collection_id": "eng-testing-book-collection",
                 "title": "D", "content": "new section"},
                app_state,
            )
            assert result.get("sequence_number") == 4

    async def test_id_formula_idempotent(self, app_state):
        """Same title+content → same knowledge_id (sha256 body[:200])."""
        app_state.store.write_entry = AsyncMock()
        app_state.store.flush = AsyncMock()

        result1 = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "Test", "content": "same body"},
            app_state,
        )
        result2 = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "Test", "content": "same body"},
            app_state,
        )
        assert result1["fragment_id"] == result2["fragment_id"]

    async def test_successful_add_increments_data_version(self, app_state):
        """Successful add_fragment increments data_version."""
        app_state.store.write_entry = AsyncMock()
        app_state.store.flush = AsyncMock()
        v_before = app_state.data_version

        result = await add_fragment(
            {"collection_id": "eng-testing-book-collection",
             "title": "New", "content": "content"},
            app_state,
        )
        assert "error" not in result
        assert app_state.data_version == v_before + 1


# ── update_fragment tests ──────────────────────────────────

class TestUpdateFragment:
    async def test_missing_fragment_id(self, app_state):
        result = await update_fragment({}, app_state)
        assert "error" in result
        assert "fragment_id" in result["error"].lower()

    async def test_not_a_section_no_parent(self, app_state):
        """Update on root/standalone (no parent_knowledge_id) → error."""
        result = await update_fragment(
            {"fragment_id": "ru-test-entry"}, app_state,
        )
        assert "error" in result
        assert "not a book section" in result["error"].lower()

    async def test_empty_content_nh_iter3_1(self, app_state):
        """Empty content update → error."""
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        section_entry = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="test-section",
                domain="eng", subject="test",
                content_type="book",
                parent_knowledge_id="parent-book",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Title\n\nold content",
        )
        app_state.store.read = AsyncMock(return_value=section_entry)

        result = await update_fragment(
            {"fragment_id": "test-section", "content": "   "}, app_state,
        )
        assert "error" in result
        assert "empty" in result["error"].lower()

    async def test_title_rewrite(self, app_state):
        """Title param → first heading rewritten."""
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        section_entry = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="test-section",
                domain="eng", subject="test",
                content_type="book",
                parent_knowledge_id="parent-book",
                version=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Old Title\n\nold content",
        )
        app_state.store.read = AsyncMock(return_value=section_entry)
        app_state.store.update = AsyncMock(return_value=section_entry)

        await update_fragment(
            {"fragment_id": "test-section", "title": "New Title"}, app_state,
        )
        update_call = app_state.store.update.call_args
        new_content = update_call[1].get("content")
        assert new_content is not None
        assert "# New Title" in new_content

    async def test_version_conflict(self, app_state):
        """VersionConflictError → conflict=True in response."""
        from datetime import datetime, timezone

        from mcp_server.models import (
            KnowledgeEntry,
            KnowledgeFrontmatter,
            VersionConflictError,
        )

        section_entry = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="test-section",
                domain="eng", subject="test",
                content_type="book",
                parent_knowledge_id="parent-book",
                version=2,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Title\n\ncontent",
        )
        app_state.store.read = AsyncMock(return_value=section_entry)
        app_state.store.update = AsyncMock(
            side_effect=VersionConflictError("test-section", 1, 2),
        )

        result = await update_fragment(
            {"fragment_id": "test-section", "content": "new", "version": 1},
            app_state,
        )
        assert result.get("conflict") is True
        assert result.get("expected_version") == 1
        assert result.get("current_version") == 2


# ── delete_fragment tests ──────────────────────────────────

class TestDeleteFragment:
    async def test_missing_fragment_id(self, app_state):
        result = await delete_fragment({}, app_state)
        assert "error" in result

    async def test_not_a_section_no_parent(self, app_state):
        """Delete root/standalone → error (P1-5 guard)."""
        result = await delete_fragment(
            {"fragment_id": "ru-test-entry"}, app_state,
        )
        assert "error" in result
        assert "not a book section" in result["error"].lower()

    async def test_successful_delete_increments_data_version(self, app_state):
        """Delete a section → data_version++, qdrant.delete called, no cascade."""
        from datetime import datetime, timezone

        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        section_entry = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="test-section",
                domain="eng", subject="test",
                content_type="book",
                parent_knowledge_id="parent-book",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            content="# Title\n\ncontent",
        )
        app_state.store.read = AsyncMock(return_value=section_entry)
        app_state.store.delete = AsyncMock(return_value=True)
        v_before = app_state.data_version

        result = await delete_fragment(
            {"fragment_id": "test-section"}, app_state,
        )
        assert result.get("deleted") is True
        assert app_state.data_version == v_before + 1
        # Qdrant delete called (without cascade)
        app_state.qdrant.delete_by_knowledge_id.assert_called_once_with("test-section", collection_name=COLLECTION_PRIVATE)


# ── find_fragment tests ────────────────────────────────────

class TestFindFragment:
    async def test_missing_collection_id(self, app_state):
        result = await find_fragment({"query": "test"}, app_state)
        assert "error" in result

    async def test_missing_query(self, app_state):
        result = await find_fragment({"collection_id": "cid"}, app_state)
        assert "error" in result

    async def test_collection_not_found(self, app_state):
        result = await find_fragment(
            {"collection_id": "nonexistent", "query": "test"}, app_state,
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_returns_fragments(self, app_state):
        """find_fragment delegates to search_knowledge and reformats results."""
        with patch("mcp_server.tools.fragments.search_knowledge") as mock_search:
            mock_search.return_value = {
                "results": [
                    {"knowledge_id": "sec-1", "title": "T1",
                     "score": 0.9, "content": "long content..."},
                    {"knowledge_id": "sec-2", "title": "T2",
                     "score": 0.8, "content": "another section..."},
                ],
            }
            result = await find_fragment(
                {"collection_id": "eng-testing-book-collection",
                 "query": "test", "limit": 5},
                app_state,
            )
            assert "error" not in result
            assert result["total"] == 2
            assert len(result["fragments"]) == 2
            assert result["fragments"][0]["fragment_id"] == "sec-1"
            assert result["fragments"][0]["score"] == 0.9
            assert "snippet" in result["fragments"][0]
