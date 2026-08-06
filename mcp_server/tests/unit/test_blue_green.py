"""F1: Unit tests for Blue-green reindex (Qdrant Collection Aliases).

Covers:
- reindex(blue_green=True) → zero-downtime path via pipeline.reindex_blue_green()
- reindex(blue_green=False) → legacy delete-all path via pipeline.reindex_all()
- blue_green result structure (active, target, alias_swapped, reindex_result)
- Default blue_green=True (backward-compatible)
- Schema constants (COLLECTION_ALIAS, V1, V2, build_collection_params)
- P1-3: collection_name parameter via mock verifications
"""

from __future__ import annotations

import pytest
from mcp_server.storage.schema import (
    COLLECTION_ALIAS,
    COLLECTION_V1,
    COLLECTION_V2,
    VECTOR_SIZE,
    build_collection_params,
)
from mcp_server.tools.admin import reindex

pytestmark = pytest.mark.asyncio


# ── Blue-green reindex via admin tool ──────────────────────

async def test_reindex_blue_green_happy_path(app_state):
    """F1: reindex(blue_green=True) uses pipeline.reindex_blue_green()."""
    result = await reindex({"blue_green": True}, app_state)
    assert "error" not in result
    assert result["blue_green"] is True
    assert result["total_docs"] == 1
    assert result["total_chunks"] == 3
    assert result["failed"] == 0
    assert result["collection_active"] == "knowledge_v1"
    assert result["collection_target"] == "knowledge_v2"
    assert result["alias_swapped"] is True
    assert result["index_total_entries"] == 3


async def test_reindex_blue_green_default(app_state):
    """F1: reindex() without blue_green param defaults to True."""
    result = await reindex({}, app_state)
    assert result["blue_green"] is True
    assert result["collection_active"] == "knowledge_v1"
    assert result["collection_target"] == "knowledge_v2"


async def test_reindex_legacy_delete_all(app_state):
    """F1: reindex(blue_green=False) falls back to legacy reindex_all()."""
    result = await reindex({"blue_green": False}, app_state)
    assert result["blue_green"] is False
    assert result["total_docs"] == 1
    assert result["total_chunks"] == 3
    # Legacy path doesn't include collection metadata
    assert "collection_active" not in result


async def test_reindex_blue_green_with_domain(app_state):
    """F1: reindex with domain filter + blue-green."""
    result = await reindex({"blue_green": True, "domain": "engineering"}, app_state)
    assert result["blue_green"] is True
    assert result["domain"] == "engineering"


async def test_reindex_blue_green_has_index_info(app_state):
    """F1: blue-green reindex includes index rebuilding info."""
    result = await reindex({"blue_green": True}, app_state)
    assert result["index_sections"] >= 0
    assert result["index_total_entries"] == 3


# ── Schema constants ───────────────────────────────────────

class TestSchemaConstants:
    """F1: Verify schema naming convention for blue-green aliases."""

    def test_alias_name_matches_legacy(self):
        """COLLECTION_ALIAS = 'knowledge' — прозрачен для кода Ф1/Ф2."""
        assert COLLECTION_ALIAS == "knowledge"

    def test_v1_v2_names_are_distinct(self):
        """V1 and V2 names are distinct and follow convention."""
        assert COLLECTION_V1 == "knowledge_v1"
        assert COLLECTION_V2 == "knowledge_v2"
        assert COLLECTION_V1 != COLLECTION_V2

    def test_build_collection_params_default_uses_alias(self):
        """build_collection_params() without name uses COLLECTION_ALIAS."""
        params = build_collection_params()
        assert params["collection_name"] == COLLECTION_ALIAS
        assert params["vectors_config"].size == VECTOR_SIZE

    def test_build_collection_params_with_name(self):
        """build_collection_params(name='knowledge_v2') sets custom name."""
        params = build_collection_params("knowledge_v2")
        assert params["collection_name"] == "knowledge_v2"

    def test_build_collection_params_hnsw_config(self):
        """build_collection_params includes HNSW config."""
        params = build_collection_params()
        assert params["hnsw_config"] is not None
        assert params["hnsw_config"].m == 16

    def test_collection_alias_constant(self):
        """COLLECTION_ALIAS is used as default for all operations."""
        assert isinstance(COLLECTION_ALIAS, str)
        assert len(COLLECTION_ALIAS) > 0
