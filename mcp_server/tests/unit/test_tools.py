"""Unit tests for all 11 MCP Tools (smoke tests).

Covers happy-path for every registered tool.
Uses mock fixtures from conftest.py — no Qdrant/embedding/filesystem required.
"""

from __future__ import annotations

import pytest
from mcp_server.tools.admin import reindex
from mcp_server.tools.browse import list_domains, list_projects, list_subjects
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
