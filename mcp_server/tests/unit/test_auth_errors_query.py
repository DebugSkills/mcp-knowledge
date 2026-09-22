"""Auth-матрица errors_query (006 §4): admin-only, 8/8.

write ✅; editor/read/import/subscriber → 403; без ключа → 401;
EDITOR_TOOLS = WRITE − {admin×5}; инвариант вхождений во все множества;
subscriber не видит тул в tools/list (mcp_handler.py:141-143).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from mcp_server.auth import (
    EDITOR_TOOLS,
    IMPORT_TOOLS,
    READ_TOOLS,
    SUBSCRIBER_TOOLS,
    WRITE_TOOLS,
    AuthInfo,
    check_tool_permission,
)
from mcp_server.tools import TOOLS


def _auth(level: str) -> AuthInfo:
    return AuthInfo(authenticated=True, key_level=level)


class TestErrorsQueryAuthMatrix:
    def test_write_key_allowed(self):
        check_tool_permission(_auth("write"), "errors_query")  # не raisen

    @pytest.mark.parametrize("level", ["editor", "read", "import", "subscriber"])
    def test_non_write_forbidden_403(self, level):
        with pytest.raises(HTTPException) as ei:
            check_tool_permission(_auth(level), "errors_query")
        assert ei.value.status_code == 403

    def test_editor_gets_admin_hint(self):
        with pytest.raises(HTTPException) as ei:
            check_tool_permission(_auth("editor"), "errors_query")
        assert "write key" in ei.value.detail

    def test_no_key_401(self):
        with pytest.raises(HTTPException) as ei:
            check_tool_permission(AuthInfo(authenticated=False), "errors_query")
        assert ei.value.status_code == 401


class TestErrorsQuerySetInvariants:
    def test_editor_tools_is_write_minus_admin(self):
        """Регресс-защита утечки через вычитание (спека §4-2, факт №6)."""
        assert EDITOR_TOOLS == WRITE_TOOLS - {
            "reindex",
            "set_zone",
            "bulk_resolve_issues",
            "bulk_deprecate_duplicates",
            "errors_query",
        }

    def test_membership_invariants(self):
        assert "errors_query" in WRITE_TOOLS
        assert "errors_query" not in EDITOR_TOOLS
        assert "errors_query" not in READ_TOOLS
        assert "errors_query" not in IMPORT_TOOLS
        assert "errors_query" not in SUBSCRIBER_TOOLS

    def test_subscriber_does_not_see_tool_in_list(self):
        """tools/list для subscriber = TOOLS ∩ SUBSCRIBER_TOOLS (mcp_handler:141)."""
        subscriber_visible = {t["name"] for t in TOOLS} & SUBSCRIBER_TOOLS
        assert "errors_query" not in subscriber_visible

    def test_registered_in_tools_registry(self):
        assert any(t["name"] == "errors_query" for t in TOOLS)
