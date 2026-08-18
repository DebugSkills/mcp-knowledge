"""Unit tests for W1: two-zone access model — frontmatter/zone plumbing.

Фаза W1 плана двухконтурной модели доступа (public/private зоны):
- KnowledgeFrontmatter.zone: default private, Literal + validator (W1.1)
- WriteRequest.zone: default private (W1.2)
- KnowledgeEntry.zone property (W1.3)
- Legacy parse: YAML без zone → private
- store.write сериализует zone в YAML frontmatter (W1.8)
- write_knowledge пробрасывает zone → WriteRequest (W1.4)
- resolve_zone / is_partial_public семантика (W1.7)
"""

from __future__ import annotations

import pytest
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter, WriteRequest
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.zone_utils import is_partial_public, resolve_zone
from pydantic import ValidationError

# ── W1.1: KnowledgeFrontmatter.zone ─────────────────────────

class TestZoneField:
    def test_default_private(self):
        fm = KnowledgeFrontmatter(
            knowledge_id="ru-test-zone", domain="eng", subject="test",
        )
        assert fm.zone == "private"

    def test_explicit_public(self):
        fm = KnowledgeFrontmatter(
            knowledge_id="ru-test-zone", domain="eng", subject="test", zone="public",
        )
        assert fm.zone == "public"

    def test_unknown_zone_rejected(self):
        with pytest.raises(ValidationError):
            KnowledgeFrontmatter(
                knowledge_id="ru-test-zone", domain="eng", subject="test",
                zone="internal",
            )


# ── W1.3: KnowledgeEntry.zone property ──────────────────────

class TestEntryZoneProperty:
    def test_entry_zone_property(self):
        fm = KnowledgeFrontmatter(
            knowledge_id="ru-test-zone", domain="eng", subject="test", zone="public",
        )
        entry = KnowledgeEntry(frontmatter=fm, content="body")
        assert entry.zone == "public"

    def test_entry_zone_property_default(self):
        fm = KnowledgeFrontmatter(
            knowledge_id="ru-test-zone", domain="eng", subject="test",
        )
        entry = KnowledgeEntry(frontmatter=fm, content="body")
        assert entry.zone == "private"


# ── W1.2: WriteRequest.zone ─────────────────────────────────

class TestWriteRequestZone:
    def test_default_private(self):
        req = WriteRequest(content="x", domain="eng", subject="test")
        assert req.zone == "private"

    def test_explicit(self):
        req = WriteRequest(content="x", domain="eng", subject="test", zone="public")
        assert req.zone == "public"


# ── Legacy parse: файл без zone → private ───────────────────

class TestLegacyParse:
    def test_legacy_yaml_without_zone_is_private(self):
        text = (
            "---\n"
            "knowledge_id: ru-legacy-entry\n"
            "domain: engineering\n"
            "subject: testing\n"
            "version: 1\n"
            "created_at: '2026-01-01T00:00:00+00:00'\n"
            "updated_at: '2026-01-01T00:00:00+00:00'\n"
            "---\n"
            "Legacy content.\n"
        )
        entry = MarkdownStore._parse_text(text)
        assert entry.frontmatter.zone == "private"

    def test_legacy_unknown_zone_value_rejected(self):
        text = (
            "---\n"
            "knowledge_id: ru-legacy-entry\n"
            "domain: engineering\n"
            "subject: testing\n"
            "zone: internal\n"
            "version: 1\n"
            "created_at: '2026-01-01T00:00:00+00:00'\n"
            "updated_at: '2026-01-01T00:00:00+00:00'\n"
            "---\n"
            "Legacy content.\n"
        )
        with pytest.raises(ValidationError):
            MarkdownStore._parse_text(text)


# ── W1.8: store.write сериализует zone ──────────────────────

class TestStoreWriteZone:
    @pytest.mark.asyncio
    async def test_write_serializes_explicit_zone(self, tmp_path):
        store = MarkdownStore(knowledge_root=tmp_path / "knowledge")
        req = WriteRequest(
            content="# T\n\nbody", domain="eng", subject="test", zone="public",
        )
        entry = await store.write(req)
        path = tmp_path / "knowledge" / "eng" / "test" / f"{entry.frontmatter.knowledge_id}.md"
        raw = path.read_text(encoding="utf-8")
        assert "zone: public" in raw

    @pytest.mark.asyncio
    async def test_write_default_zone_private_in_yaml(self, tmp_path):
        store = MarkdownStore(knowledge_root=tmp_path / "knowledge")
        req = WriteRequest(content="# T\n\nbody", domain="eng", subject="test")
        entry = await store.write(req)
        path = tmp_path / "knowledge" / "eng" / "test" / f"{entry.frontmatter.knowledge_id}.md"
        raw = path.read_text(encoding="utf-8")
        assert "zone: private" in raw


# ── W1.4: write_knowledge пробрасывает zone ─────────────────

class TestWriteKnowledgeZonePassthrough:
    @pytest.mark.asyncio
    async def test_write_knowledge_passes_zone(self, app_state, tmp_path):
        from mcp_server.tools.crud import write_knowledge

        store = MarkdownStore(knowledge_root=tmp_path / "knowledge")
        app_state.store = store

        result = await write_knowledge(
            {"content": "# Z\n\nbody", "domain": "eng", "subject": "test",
             "zone": "public"},
            app_state,
        )
        assert "error" not in result
        entry = await store.read(result["knowledge_id"])
        assert entry.frontmatter.zone == "public"

    @pytest.mark.asyncio
    async def test_write_knowledge_unknown_zone_error(self, app_state, tmp_path):
        from mcp_server.tools.crud import write_knowledge

        store = MarkdownStore(knowledge_root=tmp_path / "knowledge")
        app_state.store = store

        result = await write_knowledge(
            {"content": "# Z\n\nbody", "domain": "eng", "subject": "test",
             "zone": "internal"},
            app_state,
        )
        assert "error" in result
        assert "zone" in result["error"].lower()


# ── W1.7: resolve_zone семантика ────────────────────────────

class TestResolveZone:
    def test_no_parent_returns_own(self):
        assert resolve_zone("public", None) == ("public", False)
        assert resolve_zone("private", None) == ("private", False)

    def test_none_zone_defaults_private(self):
        assert resolve_zone(None, None) == ("private", False)

    def test_public_under_private_forced(self):
        assert resolve_zone("public", "private") == ("private", True)

    def test_private_under_public_not_forced(self):
        assert resolve_zone("private", "public") == ("private", False)

    def test_same_zone_no_force(self):
        assert resolve_zone("public", "public") == ("public", False)
        assert resolve_zone("private", "private") == ("private", False)

    def test_unknown_zone_raises(self):
        with pytest.raises(ValueError):
            resolve_zone("internal", None)


class TestPartialPublic:
    def test_private_under_public_is_partial(self):
        assert is_partial_public("private", "public") is True

    def test_other_cases_not_partial(self):
        assert is_partial_public("public", "private") is False
        assert is_partial_public("public", "public") is False
        assert is_partial_public("public", None) is False
        assert is_partial_public("private", None) is False
