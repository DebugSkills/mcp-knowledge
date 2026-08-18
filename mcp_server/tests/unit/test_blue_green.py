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
    COLLECTION_NAME,
    COLLECTION_PRIVATE,
    COLLECTION_PUBLIC,
    COLLECTION_V1,
    COLLECTION_V2,
    LEGACY_ALIAS,
    PRIVATE_V1,
    PRIVATE_V2,
    PUBLIC_V1,
    PUBLIC_V2,
    ZONE_PRIVATE,
    ZONE_PUBLIC,
    blue_green_names_for_zone,
    build_collection_params,
    build_payload_point,
    collection_for_zone,
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

    def test_build_collection_params_requires_name(self):
        """W2: build_collection_params() без имени → ValueError (fail loud)."""
        with pytest.raises(ValueError):
            build_collection_params()

    def test_build_collection_params_with_name(self):
        """build_collection_params(name='knowledge_v2') sets custom name."""
        params = build_collection_params("knowledge_v2")
        assert params["collection_name"] == "knowledge_v2"

    def test_build_collection_params_hnsw_config(self):
        """build_collection_params includes HNSW config."""
        params = build_collection_params(COLLECTION_V2)
        assert params["hnsw_config"] is not None
        assert params["hnsw_config"].m == 16

    def test_collection_alias_constant(self):
        """COLLECTION_ALIAS is used as default for all operations."""
        assert isinstance(COLLECTION_ALIAS, str)
        assert len(COLLECTION_ALIAS) > 0


# ── W2: Зональные константы и маппинги ─────────────────────

class TestZoneConstants:
    """W2: Двухконтурная модель — зональные константы и маппинги."""

    def test_zone_constants_exist(self):
        """Все зональные константы определены с ожидаемыми значениями."""
        assert COLLECTION_PUBLIC == "knowledge_public"
        assert COLLECTION_PRIVATE == "knowledge_private"
        assert PUBLIC_V1 == "knowledge_public_v1"
        assert PUBLIC_V2 == "knowledge_public_v2"
        assert PRIVATE_V1 == "knowledge_private_v1"
        assert PRIVATE_V2 == "knowledge_private_v2"
        assert LEGACY_ALIAS == "knowledge"
        assert ZONE_PUBLIC == "public"
        assert ZONE_PRIVATE == "private"

    def test_zone_constants_unique(self):
        """Все имена коллекций уникальны."""
        all_names = [
            COLLECTION_PUBLIC, COLLECTION_PRIVATE,
            PUBLIC_V1, PUBLIC_V2, PRIVATE_V1, PRIVATE_V2, LEGACY_ALIAS,
        ]
        assert len(set(all_names)) == len(all_names)

    def test_w1_shims_remain(self):
        """Шимы W1 сохранены: COLLECTION_ALIAS/V1/V2/NAME работают."""
        assert COLLECTION_ALIAS == LEGACY_ALIAS
        assert COLLECTION_V1 == "knowledge_v1"
        assert COLLECTION_V2 == "knowledge_v2"
        assert COLLECTION_NAME == COLLECTION_PRIVATE  # W2: временный алиас

    def test_collection_for_zone(self):
        assert collection_for_zone("public") == COLLECTION_PUBLIC
        assert collection_for_zone("private") == COLLECTION_PRIVATE

    def test_collection_for_zone_unknown_fails_loud(self):
        with pytest.raises(ValueError):
            collection_for_zone("x")

    def test_blue_green_names_for_zone_public(self):
        assert blue_green_names_for_zone("public") == (
            PUBLIC_V1, PUBLIC_V2, COLLECTION_PUBLIC,
        )

    def test_blue_green_names_for_zone_private(self):
        assert blue_green_names_for_zone("private") == (
            PRIVATE_V1, PRIVATE_V2, COLLECTION_PRIVATE,
        )

    def test_blue_green_names_for_zone_unknown_fails_loud(self):
        with pytest.raises(ValueError):
            blue_green_names_for_zone("unknown")


# ── W2: zone в payload-point ────────────────────────────────

def _make_point(**overrides):
    kwargs = {
        "point_id": "p1",
        "vector": [0.1, 0.2],
        "knowledge_id": "k1",
        "chunk_id": "c1",
        "content": "hello",
        "domain": "d",
        "subject": "s",
        "project": None,
        "tags": [],
        "cross_subjects": [],
        "section_header": "",
        "chunk_index": 0,
        "updated_at": "2026-01-01T00:00:00",
    }
    kwargs.update(overrides)
    return build_payload_point(**kwargs)


class TestBuildPayloadPointZone:
    """W2: build_payload_point пишет zone в payload."""

    def test_zone_explicit_public(self):
        point = _make_point(zone="public")
        assert point.payload["zone"] == "public"

    def test_zone_default_private(self):
        point = _make_point()
        assert point.payload["zone"] == "private"

    def test_zone_does_not_overwrite_existing_keys(self):
        point = _make_point(zone="public")
        assert point.payload["knowledge_id"] == "k1"
        assert point.payload["chunk_id"] == "c1"
        assert point.payload["domain"] == "d"
        assert point.payload["subject"] == "s"
