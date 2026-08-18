"""W1.10: монозональность книг — зона секций относительно зоны книги.

Книга — единая зона; private доминирует в ОБЕ стороны:
- private-книга + секция zone=public → private + issue zone_violation (critical)
- public-книга + секция zone=private → private + issue (warn) + маркер book_partial_public
- без zone → наследование зоны книги (без issue)
- update_fragment с конфликтующей zone → принуждение + issue
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from mcp_server.content.preprocessor import Section
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.quality.issues import get_issues_store_path, set_store_dir
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.content import _batch_write_sections, import_content
from mcp_server.tools.fragments import add_fragment, update_fragment

pytestmark = pytest.mark.asyncio

# Контент книги для import_content-тестов (>50 символов, 2 структурные секции).
_BOOK_CONTENT = (
    "# Replacement Book\n\n"
    "## Chapter 1\n\n"
    "This is the first chapter with enough body text to be valid content.\n\n"
    "## Chapter 2\n\n"
    "This is the second chapter with its own distinct body text."
)


# ── Fixtures ────────────────────────────────────────────────

@pytest.fixture
def issue_store(tmp_path):
    """Изолированное хранилище issues + сброс глобального оверрайда."""
    d = tmp_path / "quality"
    set_store_dir(str(d))
    yield d
    set_store_dir(None)


@pytest.fixture
def real_store(tmp_path) -> MarkdownStore:
    return MarkdownStore(knowledge_root=tmp_path / "knowledge")


# ── Helpers ─────────────────────────────────────────────────

async def _make_book(store: MarkdownStore, knowledge_id: str, zone: str) -> None:
    fm = KnowledgeFrontmatter(
        knowledge_id=knowledge_id,
        domain="engineering",
        subject="testing",
        content_type="collection",
        zone=zone,
        tags=["test"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content="# Book\n\nBody."))


def _zone_issues() -> list[dict]:
    path = get_issues_store_path()
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("type") == "zone_violation"]


# ── add_fragment монозональность ───────────────────────────

class TestAddFragmentMonoZone:
    async def test_private_book_public_section_forced_down(
        self, app_state, real_store, issue_store,
    ):
        """own=public при parent=private → private + issue critical."""
        await _make_book(real_store, "eng-testing-private-book", "private")
        app_state.store = real_store

        result = await add_fragment(
            {"collection_id": "eng-testing-private-book",
             "title": "Public Section", "content": "body", "zone": "public"},
            app_state,
        )
        assert "error" not in result
        section = await real_store.read(result["fragment_id"])
        assert section.frontmatter.zone == "private"
        assert result.get("zone") == "private"
        assert result.get("zone_forced") is True

        issues = _zone_issues()
        assert len(issues) == 1
        assert issues[0]["knowledge_id"] == result["fragment_id"]
        assert issues[0]["severity"] == "critical"
        assert issues[0]["metadata"]["zone_forced"] is True

    async def test_public_book_private_section_partial_marker(
        self, app_state, real_store, issue_store,
    ):
        """own=private при parent=public → private + issue warn + book_partial_public."""
        await _make_book(real_store, "eng-testing-public-book", "public")
        app_state.store = real_store

        result = await add_fragment(
            {"collection_id": "eng-testing-public-book",
             "title": "Private Section", "content": "body", "zone": "private"},
            app_state,
        )
        assert "error" not in result
        section = await real_store.read(result["fragment_id"])
        assert section.frontmatter.zone == "private"
        assert result.get("book_partial_public") is True
        assert result.get("zone") == "private"

        issues = _zone_issues()
        assert len(issues) == 1
        assert issues[0]["severity"] == "warn"
        assert issues[0]["metadata"]["book_partial_public"] is True

    async def test_no_zone_inherits_book_zone_no_issue(
        self, app_state, real_store, issue_store,
    ):
        """Без zone → наследование зоны книги, без issues."""
        await _make_book(real_store, "eng-testing-public-book-2", "public")
        app_state.store = real_store

        result = await add_fragment(
            {"collection_id": "eng-testing-public-book-2",
             "title": "Inherited", "content": "body"},
            app_state,
        )
        assert "error" not in result
        section = await real_store.read(result["fragment_id"])
        assert section.frontmatter.zone == "public"
        assert result.get("zone") == "public"
        assert _zone_issues() == []

    async def test_public_section_in_public_book_no_issue(
        self, app_state, real_store, issue_store,
    ):
        """own=public при parent=public → без конфликта."""
        await _make_book(real_store, "eng-testing-public-book-3", "public")
        app_state.store = real_store

        result = await add_fragment(
            {"collection_id": "eng-testing-public-book-3",
             "title": "Public Section", "content": "body", "zone": "public"},
            app_state,
        )
        assert "error" not in result
        section = await real_store.read(result["fragment_id"])
        assert section.frontmatter.zone == "public"
        assert result.get("zone_forced") is not True
        assert result.get("book_partial_public") is not True
        assert _zone_issues() == []

    async def test_unknown_zone_error(self, app_state, real_store, issue_store):
        await _make_book(real_store, "eng-testing-private-book-4", "private")
        app_state.store = real_store

        result = await add_fragment(
            {"collection_id": "eng-testing-private-book-4",
             "title": "T", "content": "body", "zone": "internal"},
            app_state,
        )
        assert "error" in result
        assert "zone" in result["error"].lower()


# ── update_fragment монозональность ────────────────────────

class TestUpdateFragmentMonoZone:
    async def test_update_zone_forced_down(
        self, app_state, real_store, issue_store,
    ):
        """private-книга, update zone=public → принуждение private + issue critical."""
        await _make_book(real_store, "eng-testing-private-book-5", "private")
        section_fm = KnowledgeFrontmatter(
            knowledge_id="eng-testing-sec-u1",
            domain="engineering",
            subject="testing",
            content_type="book",
            parent_knowledge_id="eng-testing-private-book-5",
            sequence_number=1,
            zone="private",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await real_store.write_entry(
            KnowledgeEntry(frontmatter=section_fm, content="# Sec\n\nbody"),
        )
        app_state.store = real_store

        result = await update_fragment(
            {"fragment_id": "eng-testing-sec-u1", "zone": "public"}, app_state,
        )
        assert "error" not in result
        section = await real_store.read("eng-testing-sec-u1")
        assert section.frontmatter.zone == "private"
        assert result.get("zone") == "private"
        assert result.get("zone_forced") is True

        issues = _zone_issues()
        assert len(issues) == 1
        assert issues[0]["knowledge_id"] == "eng-testing-sec-u1"
        assert issues[0]["severity"] == "critical"


# ── P1-1 (гейт W1): import_content replace-наследование зоны ──

class TestImportReplaceZoneInheritance:
    async def test_replace_inherits_zone_of_replaced_book(
        self, app_state, real_store,
    ):
        """replace_collection_id без явной zone → наследует зону заменяемой книги (§9.8)."""
        await _make_book(real_store, "eng-testing-replaced-public", "public")
        app_state.store = real_store

        result = await import_content(
            {
                "content": _BOOK_CONTENT,
                "domain": "engineering",
                "subject": "testing",
                "title": "Replacement Book",
                "replace_collection_id": "eng-testing-replaced-public",
                "quality_checks": False,
            },
            app_state,
        )
        assert "error" not in result
        root = await real_store.read(result["collection_id"])
        assert root is not None
        assert root.frontmatter.zone == "public"
        for child in root.frontmatter.children:
            section = await real_store.read(child["knowledge_id"])
            assert section.frontmatter.zone == "public"

    async def test_replace_explicit_zone_wins(
        self, app_state, real_store,
    ):
        """Явная zone при replace_collection_id побеждает наследование."""
        await _make_book(real_store, "eng-testing-replaced-public-2", "public")
        app_state.store = real_store

        result = await import_content(
            {
                "content": _BOOK_CONTENT,
                "domain": "engineering",
                "subject": "testing",
                "title": "Replacement Book Two",
                "replace_collection_id": "eng-testing-replaced-public-2",
                "zone": "private",
                "quality_checks": False,
            },
            app_state,
        )
        assert "error" not in result
        root = await real_store.read(result["collection_id"])
        assert root is not None
        assert root.frontmatter.zone == "private"
        for child in root.frontmatter.children:
            section = await real_store.read(child["knowledge_id"])
            assert section.frontmatter.zone == "private"


# ── P1-1 (гейт W1): PDF-путь _batch_write_sections зона ───────

class TestBatchWriteSectionsZone:
    async def test_pdf_batch_write_public_zone_in_ssot(
        self, app_state, real_store,
    ):
        """_batch_write_sections с zone=public → root+секции public в SSOT."""
        app_state.store = real_store

        sections = [
            Section(
                title="Chapter 1",
                body="# Chapter 1\n\nBody one.",
                sequence_number=1,
                tags=["test"],
                meta={
                    "knowledge_id": "eng-testing-pdf-s1",
                    "domain": "engineering",
                    "subject": "testing",
                    "project": None,
                    "content_type": "book",
                    "cross_subjects": [],
                },
            ),
        ]
        result = await _batch_write_sections(
            sections,
            {
                "domain": "engineering",
                "subject": "testing",
                "title": "PDF Book",
                "zone": "public",
            },
            app_state,
            import_id="import-zone-pdf",
            cancel_event=asyncio.Event(),
        )
        assert "error" not in result

        section_entry = await real_store.read("eng-testing-pdf-s1")
        assert section_entry is not None
        assert section_entry.frontmatter.zone == "public"

        root_id = section_entry.frontmatter.parent_knowledge_id
        root = await real_store.read(root_id)
        assert root is not None
        assert root.frontmatter.zone == "public"
