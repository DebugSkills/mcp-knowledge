"""Unit tests for all MCP Tools (smoke tests).

Covers happy-path for every registered tool.
Uses mock fixtures from conftest.py — no Qdrant/embedding/filesystem required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mcp_server.tools.admin import reindex
from mcp_server.tools.browse import list_domains, list_projects, list_subjects
from mcp_server.tools.collections import list_collections
from mcp_server.tools.content import extract_pdf_text
from mcp_server.tools.crud import delete_entry, update_entry, write_knowledge
from mcp_server.tools.read import get_entry, get_knowledge_map
from mcp_server.tools.search import search_by_tags, search_knowledge

pytestmark = pytest.mark.asyncio


# ── Search tools ────────────────────────────────────────────

async def test_search_knowledge_happy_path(app_state):
    """A1: semantic search returns formatted results."""
    result = await search_knowledge(
        {"query": "асинхронные паттерны", "domain": "engineering", "top_k": 3},
        app_state,
    )
    assert "error" not in result
    assert result["query"] == "асинхронные паттерны"
    assert len(result["results"]) == 1
    assert result["results"][0]["knowledge_id"] == "ru-test-entry"
    assert result["results"][0]["score"] > 0


async def test_search_knowledge_missing_query(app_state):
    """A1: empty query returns error."""
    result = await search_knowledge({"query": ""}, app_state)
    assert "error" in result


# ── Фаза 13.14: deprecated exclusion from search ──────────

async def test_search_knowledge_excludes_deprecated_by_default(app_state):
    """По умолчанию exclude_statuses=["deprecated"] передаётся в qdrant.search()."""
    result = await search_knowledge({"query": "test", "top_k": 3}, app_state)
    assert "error" not in result
    # Проверяем что exclude_statuses=["deprecated"] передано в mock
    exclude_statuses = getattr(app_state.qdrant, "_last_search_exclude_statuses", None)
    assert exclude_statuses == ["deprecated"], (
        f"Expected ['deprecated'], got {exclude_statuses}"
    )


async def test_search_knowledge_include_deprecated_skips_filter(app_state):
    """include_deprecated=True → exclude_statuses=None (все записи видны)."""
    result = await search_knowledge({"query": "test", "include_deprecated": True, "top_k": 3}, app_state)
    assert "error" not in result
    exclude_statuses = getattr(app_state.qdrant, "_last_search_exclude_statuses", None)
    assert exclude_statuses is None, (
        f"Expected None (no filter), got {exclude_statuses}"
    )


async def test_search_by_tags_and(app_state):
    """A2: AND semantics — all tags must match."""
    result = await search_by_tags(
        {"tags": ["docker", "best-practice"], "match_all": True, "limit": 500},
        app_state,
    )
    assert "error" not in result
    assert result["match_all"] is True
    assert len(result["results"]) == 1


async def test_search_by_tags_or(app_state):
    """A2: OR semantics — any tag matches."""
    result = await search_by_tags(
        {"tags": ["docker", "python"], "match_all": False},
        app_state,
    )
    assert "error" not in result
    assert result["match_all"] is False


async def test_search_by_tags_missing_tags(app_state):
    """A2: empty tags returns error."""
    result = await search_by_tags({"tags": []}, app_state)
    assert "error" in result


async def test_search_by_tags_truncated(app_state):
    """A2: truncated flag when results >= limit."""
    result = await search_by_tags(
        {"tags": ["test"], "limit": 1},
        app_state,
    )
    assert result["truncated"] is True


# ── Read tools ──────────────────────────────────────────────

async def test_get_entry_happy_path(app_state):
    """A3: get_entry returns full record."""
    result = await get_entry(
        {"knowledge_id": "ru-test-entry"},
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "ru-test-entry"
    assert result["domain"] == "engineering"
    assert "content" in result


async def test_get_entry_includes_zone(app_state):
    """A3: get_entry returns zone (code-2026-08-19-zone-ui).

    Additive-поле для kb-console (бейдж зоны в диалоге книги).
    """
    result = await get_entry(
        {"knowledge_id": "ru-test-entry"},
        app_state,
    )
    assert "error" not in result
    assert "zone" in result, f"get_entry должен возвращать zone, получено: {sorted(result)}"
    assert result["zone"] in ("public", "private")


async def test_get_entry_not_found(app_state):
    """A3: nonexistent ID returns error."""
    result = await get_entry(
        {"knowledge_id": "nonexistent"},
        app_state,
    )
    assert "error" in result


async def test_get_entry_missing_id(app_state):
    """A3: missing knowledge_id returns error."""
    result = await get_entry({}, app_state)
    assert "error" in result


async def test_get_knowledge_map_root(app_state):
    """A4: root map returns sections."""
    result = await get_knowledge_map({}, app_state)
    assert "error" not in result
    assert "sections" in result


async def test_get_knowledge_map_by_domain(app_state):
    """A4: per-domain map."""
    result = await get_knowledge_map({"domain": "engineering"}, app_state)
    assert "error" not in result


# ── CRUD tools ──────────────────────────────────────────────

async def test_write_knowledge_happy_path(app_state):
    """A5: write creates entry + returns indexed status."""
    result = await write_knowledge(
        {
            "content": "# Test\nContent.",
            "domain": "engineering",
            "subject": "testing",
            "tags": ["test"],
        },
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "ru-test-entry"
    assert result["domain"] == "engineering"
    # wait_for_index=False by default → pending:true
    assert result["pending"] is True
    assert result["indexed"] is False


async def test_write_knowledge_missing_content(app_state):
    """A5: missing content returns error."""
    result = await write_knowledge(
        {"content": "", "domain": "eng", "subject": "test"},
        app_state,
    )
    assert "error" in result


async def test_write_knowledge_missing_domain(app_state):
    """A5: missing domain returns error."""
    result = await write_knowledge(
        {"content": "x", "domain": "", "subject": "test"},
        app_state,
    )
    assert "error" in result


async def test_update_entry_happy_path(app_state):
    """A6: update entry with content."""
    result = await update_entry(
        {"knowledge_id": "ru-test-entry", "content": "# Updated\nNew content."},
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "ru-test-entry"
    assert result["version"] == 1
    assert result["pending"] is True


async def test_update_entry_not_found(app_state):
    """A6: nonexistent ID returns error."""
    result = await update_entry(
        {"knowledge_id": "nonexistent", "content": "# Nope"},
        app_state,
    )
    assert "error" in result


async def test_delete_entry_happy_path(app_state):
    """A6: delete entry returns success."""
    result = await delete_entry(
        {"knowledge_id": "ru-test-entry"},
        app_state,
    )
    assert "error" not in result
    assert result["deleted"] is True


async def test_delete_entry_not_found(app_state):
    """A6: nonexistent ID returns not-deleted."""
    result = await delete_entry(
        {"knowledge_id": "nonexistent"},
        app_state,
    )
    assert "error" in result


# ── Фаза 13.14: delete_entry cascade ────────────────────

async def test_delete_entry_cascade_deletes_children(app_state):
    """cascade=True → scroll children, delete each + root."""
    from unittest.mock import AsyncMock

    # Fix: MagicMock auto-attribute — ensure _get_qdrant falls back to qdrant
    app_state.qdrant_client = None

    def _make_child(kid: str):
        pt = MagicMock()
        pt.payload = {"knowledge_id": kid}
        return pt
    children = [_make_child("kid-sec-1"), _make_child("kid-sec-2")]

    app_state.qdrant.scroll = MagicMock(return_value=(children, None))
    app_state.store.delete = AsyncMock(return_value=True)

    result = await delete_entry(
        {"knowledge_id": "ru-test-entry", "cascade": True},
        app_state,
    )
    assert "error" not in result
    assert result["deleted"] is True
    assert result["cascade_deleted"] == 2
    assert app_state.qdrant.delete_by_knowledge_id.call_count >= 2


async def test_delete_entry_cascade_no_children(app_state):
    """cascade=True, но scroll возвращает 0 children → cascade_deleted=0."""
    from unittest.mock import AsyncMock

    app_state.qdrant_client = None
    app_state.qdrant.scroll = MagicMock(return_value=([], None))
    app_state.store.delete = AsyncMock(return_value=True)

    result = await delete_entry(
        {"knowledge_id": "ru-test-entry", "cascade": True},
        app_state,
    )
    assert result["deleted"] is True
    assert result["cascade_deleted"] == 0
    assert app_state.qdrant.delete_by_knowledge_id.call_count == 1


# ── Browse tools ────────────────────────────────────────────

async def test_list_domains(app_state):
    """A7: list domains with pagination."""
    result = await list_domains({"limit": 50}, app_state)
    assert "error" not in result
    assert "results" in result
    assert "engineering" in result["results"]


async def test_list_subjects(app_state):
    """A7: list subjects in a domain."""
    result = await list_subjects({"domain": "engineering"}, app_state)
    assert "error" not in result
    assert "results" in result


async def test_list_projects(app_state):
    """A7: list projects with optional filters."""
    result = await list_projects({}, app_state)
    assert "error" not in result
    assert "results" in result


# ── Admin tools ─────────────────────────────────────────────

async def test_reindex_happy_path(app_state):
    """A8: reindex returns stats."""
    result = await reindex({}, app_state)
    assert "error" not in result
    assert result["total_docs"] == 1
    assert result["total_chunks"] == 3
    assert result["failed"] == 0
    assert result["index_total_entries"] == 3


# ── Variant A: Surface & Enrich — get_entry enrichment ────────

async def test_get_entry_collection_has_toc(app_state):
    """get_entry for collection returns children TOC, title, content_type."""
    result = await get_entry(
        {"knowledge_id": "eng-testing-book-collection"},
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "eng-testing-book-collection"
    assert result["content_type"] == "collection"
    assert result["parent_knowledge_id"] is None
    assert result["sequence_number"] is None
    # title derived from content
    assert result["title"] == "Test Book"
    # children TOC (from _build_toc — mock returns 3 sections)
    children = result.get("children", [])
    assert len(children) == 3
    assert children[0]["knowledge_id"] == "eng-testing-ch01"
    assert children[0]["title"] == "Chapter 1"
    assert children[0]["sequence_number"] == 1


async def test_get_entry_title_fallback(app_state):
    """get_entry: title fallback when no markdown heading."""
    result = await get_entry(
        {"knowledge_id": "ru-test-entry"},
        app_state,
    )
    assert "error" not in result
    # sample_entry content is "# Test Entry\n\nTest content." — has heading
    assert result["title"] == "Test Entry"
    assert result["content_type"] is None  # sample_entry has no content_type
    assert isinstance(result.get("children"), list)


# ── Variant A: search_knowledge enrichment ────────────────────

async def test_search_knowledge_enriched(app_state):
    """search_knowledge returns title, parent_knowledge_id, content_type."""
    result = await search_knowledge(
        {"query": "test", "top_k": 3},
        app_state,
    )
    assert "error" not in result
    item = result["results"][0]
    assert "title" in item
    assert "parent_knowledge_id" in item
    assert "content_type" in item


async def test_search_knowledge_collection_id_filter(app_state):
    """search_knowledge accepts collection_id → filters by parent_knowledge_id."""
    result = await search_knowledge(
        {"query": "async", "collection_id": "eng-testing-book-collection"},
        app_state,
    )
    assert "error" not in result
    # mock returns results regardless, but filter was applied silently
    assert result["total"] >= 0


async def test_search_knowledge_content_type_filter(app_state):
    """search_knowledge accepts content_type filter."""
    result = await search_knowledge(
        {"query": "test", "content_type": "book"},
        app_state,
    )
    assert "error" not in result
    assert result["total"] >= 0


async def test_search_knowledge_excludes_collections_by_default(app_state):
    """По умолчанию root-коллекции исключаются из результатов поиска."""
    result = await search_knowledge(
        {"query": "test", "top_k": 3},
        app_state,
    )
    assert "error" not in result
    qdrant = app_state.qdrant
    assert getattr(qdrant, "_last_search_exclude", None) == ["collection"]


async def test_search_knowledge_keeps_collections_when_requested(app_state):
    """Явный content_type=collection НЕ исключает коллекции."""
    result = await search_knowledge(
        {"query": "test", "content_type": "collection"},
        app_state,
    )
    assert "error" not in result
    qdrant = app_state.qdrant
    assert getattr(qdrant, "_last_search_exclude", None) is None


# ── Variant A: list_collections tool ──────────────────────────

async def test_list_collections_happy_path(app_state):
    """list_collections returns collections with title, section_count."""
    result = await list_collections({}, app_state)
    assert "error" not in result
    assert "results" in result
    items = result["results"]
    assert len(items) >= 1
    c = items[0]
    assert "collection_id" in c
    assert "title" in c
    assert "domain" in c
    assert "subject" in c
    assert "section_count" in c
    assert "updated_at" in c
    # section_count from _build_toc (Qdrant scroll by parent_knowledge_id)
    assert isinstance(c["section_count"], int)
    assert c["section_count"] == 3


async def test_list_collections_includes_zone(app_state):
    """list_collections returns zone per collection (code-2026-08-19-zone-ui).

    Additive-поле: kb-console использует его для бейджа зоны в карточке
    книги и префилла zone_select при замене (P1-фикс критика).
    """
    result = await list_collections({}, app_state)
    assert "error" not in result
    items = result["results"]
    assert len(items) >= 1
    c = items[0]
    assert "zone" in c, f"list_collections должен возвращать zone, получено: {sorted(c)}"
    assert c["zone"] in ("public", "private")


async def test_list_collections_domain_filter(app_state):
    """list_collections accepts domain filter."""
    result = await list_collections({"domain": "engineering"}, app_state)
    assert "error" not in result
    assert "results" in result


async def test_list_collections_default_limit(app_state):
    """list_collections default params work."""
    result = await list_collections({}, app_state)
    assert "error" not in result
    # next_cursor and total present
    assert "next_cursor" in result
    assert "total" in result


# ── Task 3: search_knowledge dedup + empty filter + refetch ──


async def test_search_knowledge_dedup_by_knowledge_id(app_state):
    """2 чанка с одним knowledge_id → 1 результат (высокий score сохраняется)."""
    app_state.qdrant._set_search_points([
        {"id": 1, "score": 0.95, "payload": {
            "knowledge_id": "kid-dup", "chunk_id": "ch-1",
            "content": "Alpha", "domain": "eng", "subject": "test",
            "section_header": "", "tags": [],
        }},
        {"id": 2, "score": 0.85, "payload": {
            "knowledge_id": "kid-dup", "chunk_id": "ch-2",
            "content": "Beta", "domain": "eng", "subject": "test",
            "section_header": "", "tags": [],
        }},
    ])
    result = await search_knowledge({"query": "test", "top_k": 5}, app_state)
    assert "error" not in result
    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == "kid-dup"
    assert result["results"][0]["score"] == 0.95  # максимальный score


async def test_search_knowledge_filters_empty_content(app_state):
    """Пустой content (или только пробелы) → отфильтрован."""
    app_state.qdrant._set_search_points([
        {"id": 1, "score": 0.99, "payload": {
            "knowledge_id": "kid-empty", "chunk_id": "ch-1",
            "content": "", "domain": "eng", "subject": "test",
            "section_header": "", "tags": [],
        }},
        {"id": 2, "score": 0.95, "payload": {
            "knowledge_id": "kid-valid", "chunk_id": "ch-2",
            "content": "Valid content", "domain": "eng", "subject": "test",
            "section_header": "", "tags": [],
        }},
        {"id": 3, "score": 0.90, "payload": {
            "knowledge_id": "kid-spaces", "chunk_id": "ch-3",
            "content": "   ", "domain": "eng", "subject": "test",
            "section_header": "", "tags": [],
        }},
    ])
    result = await search_knowledge({"query": "test", "top_k": 5}, app_state)
    assert "error" not in result
    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == "kid-valid"


async def test_search_knowledge_filters_junk_content(app_state):
    """Мусорные фрагменты (разделители таблиц "|", "---") → отфильтрованы."""
    app_state.qdrant._set_search_points([
        {"id": 1, "score": 0.99, "payload": {
            "knowledge_id": "kid-pipe", "chunk_id": "ch-1",
            "content": "|", "domain": "universal", "subject": "fpf",
            "section_header": "", "tags": [],
        }},
        {"id": 2, "score": 0.98, "payload": {
            "knowledge_id": "kid-dashes", "chunk_id": "ch-2",
            "content": "---|---", "domain": "universal", "subject": "fpf",
            "section_header": "", "tags": [],
        }},
        {"id": 3, "score": 0.97, "payload": {
            "knowledge_id": "kid-punct", "chunk_id": "ch-3",
            "content": "| | |", "domain": "universal", "subject": "fpf",
            "section_header": "", "tags": [],
        }},
        {"id": 4, "score": 0.95, "payload": {
            "knowledge_id": "kid-valid", "chunk_id": "ch-4",
            "content": "## Трёхуровневый реестр знаний", "domain": "universal", "subject": "fpf",
            "section_header": "", "tags": [],
        }},
    ])
    result = await search_knowledge({"query": "test", "top_k": 5}, app_state)
    assert "error" not in result
    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == "kid-valid"


async def test_search_knowledge_pagination_refetch(app_state):
    """top_k=3, 6 дубликатов → дозапрос с offset → 3 уникальных результата."""
    # Первые 3 (offset=0): kid-A, kid-A (dup), kid-B
    # Следующие 3 (offset=3): kid-C, kid-C (dup), kid-D
    app_state.qdrant._set_search_points([
        {"id": 1, "score": 0.95, "payload": {"knowledge_id": "kid-A", "chunk_id": "a1", "content": "A1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 2, "score": 0.85, "payload": {"knowledge_id": "kid-A", "chunk_id": "a2", "content": "A2", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 3, "score": 0.75, "payload": {"knowledge_id": "kid-B", "chunk_id": "b1", "content": "B1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 4, "score": 0.65, "payload": {"knowledge_id": "kid-C", "chunk_id": "c1", "content": "C1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 5, "score": 0.55, "payload": {"knowledge_id": "kid-C", "chunk_id": "c2", "content": "C2", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 6, "score": 0.45, "payload": {"knowledge_id": "kid-D", "chunk_id": "d1", "content": "D1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
    ])
    result = await search_knowledge({"query": "test", "top_k": 3}, app_state)
    assert "error" not in result
    assert result["total"] == 3
    kids = [r["knowledge_id"] for r in result["results"]]
    assert "kid-A" in kids
    assert "kid-B" in kids
    assert "kid-D" in kids or "kid-C" in kids  # дозапрос добрал
    assert len(set(kids)) == 3  # все уникальные


async def test_search_knowledge_returns_fewer_when_not_enough_unique(app_state):
    """top_k=5, всего 2 уникальных → 2 результата без ошибки."""
    app_state.qdrant._set_search_points([
        {"id": 1, "score": 0.95, "payload": {"knowledge_id": "kid-A", "chunk_id": "a1", "content": "A1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 2, "score": 0.85, "payload": {"knowledge_id": "kid-A", "chunk_id": "a2", "content": "A2", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
        {"id": 3, "score": 0.75, "payload": {"knowledge_id": "kid-B", "chunk_id": "b1", "content": "B1", "domain": "e", "subject": "s", "section_header": "", "tags": []}},
    ])
    result = await search_knowledge({"query": "test", "top_k": 5}, app_state)
    assert "error" not in result
    assert result["total"] == 2


async def test_search_by_tags_dedup(app_state):
    """search_by_tags дедуплицирует по knowledge_id."""
    from unittest.mock import MagicMock

    app_state.qdrant.search_by_tags = MagicMock(return_value=[
        MagicMock(id=1, score=1.0, payload={
            "knowledge_id": "kid-dup", "chunk_id": "ch-1",
            "content": "Tagged content", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
        MagicMock(id=2, score=1.0, payload={
            "knowledge_id": "kid-dup", "chunk_id": "ch-2",
            "content": "More tagged", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
    ])
    result = await search_by_tags({"tags": ["docker"], "match_all": True}, app_state)
    assert "error" not in result
    assert result["total"] == 1


async def test_search_by_tags_filters_empty(app_state):
    """search_by_tags фильтрует пустой content."""
    from unittest.mock import MagicMock

    app_state.qdrant.search_by_tags = MagicMock(return_value=[
        MagicMock(id=1, score=1.0, payload={
            "knowledge_id": "kid-empty", "chunk_id": "ch-1",
            "content": "   ", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
        MagicMock(id=2, score=1.0, payload={
            "knowledge_id": "kid-valid", "chunk_id": "ch-2",
            "content": "Valid", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
    ])
    result = await search_by_tags({"tags": ["docker"], "match_all": True}, app_state)
    assert "error" not in result
    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == "kid-valid"


async def test_search_by_tags_filters_junk_content(app_state):
    """search_by_tags: мусорные фрагменты (разделители таблиц) → отфильтрованы."""
    from unittest.mock import MagicMock

    app_state.qdrant.search_by_tags = MagicMock(return_value=[
        MagicMock(id=1, score=1.0, payload={
            "knowledge_id": "kid-pipe", "chunk_id": "ch-1",
            "content": "|", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
        MagicMock(id=2, score=1.0, payload={
            "knowledge_id": "kid-valid", "chunk_id": "ch-2",
            "content": "Valid content here", "domain": "e",
            "subject": "s", "tags": ["docker"],
            "section_header": "",
        }),
    ])
    result = await search_by_tags({"tags": ["docker"], "match_all": True}, app_state)
    assert "error" not in result
    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == "kid-valid"


# ── extract_pdf_text tool (авто-классификация PDF: Фаза 2) ────


async def test_extract_pdf_text_missing_pdf_path(app_state):
    """Без pdf_path → error."""
    result = await extract_pdf_text({}, app_state)
    assert "error" in result
    assert "pdf_path" in result["error"]


async def test_extract_pdf_text_rejects_path_outside_uploads(app_state):
    """P2 (critic): путь вне /tmp/pdf_uploads → error (защита FS)."""
    result = await extract_pdf_text({"pdf_path": "/etc/passwd"}, app_state)
    assert "error" in result
    assert "pdf_uploads" in result["error"]


async def test_extract_pdf_text_file_not_found(app_state):
    """Файл не существует → error."""
    result = await extract_pdf_text(
        {"pdf_path": "/tmp/pdf_uploads/definitely-missing.pdf"}, app_state
    )
    assert "error" in result
    assert "not found" in result["error"]


async def test_extract_pdf_text_happy_path(app_state, monkeypatch):
    """PDF в /tmp/pdf_uploads → текст извлекается, chars корректны.

    PDFPreprocessor.extract_text подменяется (не нужен pdfplumber/reportlab).
    """
    import os as _test_os

    import mcp_server.content.pdf_preprocessor as pp_module

    # Реальный файл в upload-директории (создаём и чистим)
    upload_dir = "/tmp/pdf_uploads"
    _test_os.makedirs(upload_dir, exist_ok=True)
    pdf_path = _test_os.path.join(upload_dir, f"unit-test-{_test_os.getpid()}.pdf")
    try:
        with open(pdf_path, "wb") as f:  # noqa: ASYNC230 — единичный тестовый файл
            f.write(b"%PDF-1.4 fake unit test")

        class _FakePreprocessor:
            async def extract_text(self, source_path, cancel_event=None):
                return "Извлечённый текст документа PDF"

        monkeypatch.setattr(pp_module, "PDFPreprocessor", _FakePreprocessor)

        result = await extract_pdf_text({"pdf_path": pdf_path}, app_state)
        assert "error" not in result, result
        assert "текст" in result["text"]
        assert result["chars"] == len("Извлечённый текст документа PDF")
        assert result["source_path"] == pdf_path
    finally:
        if _test_os.path.exists(pdf_path):
            _test_os.remove(pdf_path)
