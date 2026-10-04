"""P2-1 (bibliography B2): закрытие read-поверхностей «private = admin-only».

Поверхности:
- get_knowledge_map: не-admin → _public_knowledge_map (private-kid отсутствуют);
  admin → полная карта (knowledge_index.get_map).
- find_fragment: не-admin по private-коллекции → no-oracle («Collection not found»,
  без признака существования); admin → обычная делегация в search_knowledge.
- kb:// resources: зона по политике (не-admin → public-only, admin → обе зоны).
- quality-семейство ЧТЕНИЕ (list_quality_issues/review_queue/review_queue_books/
  review_duplicate_pairs/list_audit_log): admin-only на уровне auth (403 ниже write).

Мутационные детекторы: возврат is_subscriber-гейта или жёсткого ZONE_PRIVATE
(или перенос quality-чтения обратно в READ_TOOLS) роняет соответствующий тест.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from mcp_server.auth import AuthInfo, check_tool_permission
from mcp_server.tools.fragments import find_fragment
from mcp_server.tools.read import get_knowledge_map

pytestmark = pytest.mark.asyncio

QUALITY_READS = [
    "review_queue",
    "review_queue_books",
    "list_quality_issues",
    "review_duplicate_pairs",
    "list_audit_log",
]


# ── get_knowledge_map ────────────────────────────────────────


class TestKnowledgeMapZone:
    async def test_non_admin_uses_public_map(self, app_state):
        """Все уровни ниже admin → _public_knowledge_map (private-kid не отдаются)."""
        for level in ("subscriber", "read", "import", "editor", ""):
            with patch(
                "mcp_server.tools.read._public_knowledge_map",
                new=AsyncMock(return_value={"total_entries": 0, "sections": []}),
            ) as pub:
                result = await get_knowledge_map({"_auth": {"level": level}}, app_state)
                pub.assert_awaited_once()
                assert "error" not in result

    async def test_admin_uses_full_map(self, app_state):
        """admin (write) → полная карта из knowledge_index (не public-фильтр)."""
        app_state.knowledge_index.get_map = MagicMock(
            return_value={"total_entries": 1, "sections": [{"section": "x", "files": 1, "path": "x"}]}
        )
        with patch("mcp_server.tools.read._public_knowledge_map", new=AsyncMock()) as pub:
            result = await get_knowledge_map({"_auth": {"level": "write"}}, app_state)
            pub.assert_not_awaited()
            app_state.knowledge_index.get_map.assert_called_once_with(None)
            assert result["total_entries"] == 1


# ── find_fragment ────────────────────────────────────────────


class TestFindFragmentZone:
    async def test_non_admin_private_collection_no_oracle(self, app_state):
        """Не-admin на private-коллекции → «Collection not found» (без существования)."""
        with patch(
            "mcp_server.tools.fragments._collection_in_public",
            new=AsyncMock(return_value=False),
        ):
            result = await find_fragment(
                {"collection_id": "eng-testing-book-collection", "query": "x",
                 "_auth": {"level": "read"}},
                app_state,
            )
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_admin_private_collection_allowed(self, app_state):
        """admin → делегация в search_knowledge без public-оракула."""
        with patch(
            "mcp_server.tools.fragments._collection_in_public",
            new=AsyncMock(return_value=False),
        ) as pub:
            with patch(
                "mcp_server.tools.fragments.search_knowledge",
                new=AsyncMock(return_value={"results": []}),
            ) as search:
                result = await find_fragment(
                    {"collection_id": "eng-testing-book-collection", "query": "x",
                     "_auth": {"level": "write"}},
                    app_state,
                )
        pub.assert_not_awaited()
        search.assert_awaited_once()
        assert "error" not in result


# ── kb:// resources ──────────────────────────────────────────


class TestKbResourceZone:
    async def test_non_admin_public_only(self, app_state):
        from mcp_server.resources import get_kb_resource

        with patch(
            "mcp_server.resources._collect_unique_values",
            new=AsyncMock(return_value=set()),
        ) as col:
            await get_kb_resource("kb://", app_state, auth=AuthInfo(authenticated=True, key_level="read"))
            assert col.await_args.kwargs["zones"] == ["public"]

    async def test_admin_both_zones(self, app_state):
        from mcp_server.resources import get_kb_resource

        with patch(
            "mcp_server.resources._collect_unique_values",
            new=AsyncMock(return_value=set()),
        ) as col:
            await get_kb_resource("kb://", app_state, auth=AuthInfo(authenticated=True, key_level="write"))
            assert col.await_args.kwargs["zones"] == ["public", "private"]


# ── quality-семейство ЧТЕНИЕ → admin-only ────────────────────


class TestQualityReadsAdminOnly:
    def test_write_allowed(self):
        for tool in QUALITY_READS:
            check_tool_permission(AuthInfo(authenticated=True, key_level="write"), tool)

    @pytest.mark.parametrize("level", ["editor", "read", "import", "subscriber"])
    @pytest.mark.parametrize("tool", QUALITY_READS)
    def test_non_write_forbidden(self, level, tool):
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(AuthInfo(authenticated=True, key_level=level), tool)
        assert exc.value.status_code == 403

    def test_mutations_stay_write_and_editor(self):
        """Мутации run_quality_scan/resolve_quality_issue НЕ сужаются."""
        check_tool_permission(AuthInfo(authenticated=True, key_level="write"), "run_quality_scan")
        check_tool_permission(AuthInfo(authenticated=True, key_level="write"), "resolve_quality_issue")
        check_tool_permission(AuthInfo(authenticated=True, key_level="editor"), "run_quality_scan")
        check_tool_permission(AuthInfo(authenticated=True, key_level="editor"), "resolve_quality_issue")
