"""kb-console-roles Ф1 (B2): уровень editor + IMPORT_TOOLS += add_fragment + У-3.

Матрица уровень×тул (зеркало test_auth.py):
- editor: READ + EDITOR_TOOLS ✅; reindex/set_zone/bulk_* → 403 (Q1/Q2/P2-2)
- import: add_fragment ✅ (Q3, P1-2); update_fragment/delete_fragment → 403
- регресс-граница: существующие уровни неизменны (write/import/read)
- У-3: ADMIN_LEVELS = {"write"} — import-ключ → 403 на /tokens (Q5)
- token_store: LEVEL_CODES["editor"] == "e" → префикс mcp_e<z>_ (Ф1.3)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
from mcp_server.token_store import LEVEL_CODES, TokenStore


# ── Состав множеств (спека B2 §4) ─────────────────────────────


class TestEditorToolsetDefinition:
    def test_editor_tools_is_write_minus_admin(self):
        """EDITOR_TOOLS = WRITE_TOOLS − {reindex, set_zone, bulk_×2} (P2-2/Q1/Q2)."""
        assert EDITOR_TOOLS == WRITE_TOOLS - {
            "reindex",
            "set_zone",
            "bulk_resolve_issues",
            "bulk_deprecate_duplicates",
        }

    def test_editor_tools_exact_content(self):
        assert EDITOR_TOOLS == {
            "write_knowledge",
            "update_entry",
            "delete_entry",
            "resolve_quality_issue",
            "run_quality_scan",  # P2-3: скан остаётся editor
            "add_fragment",
            "update_fragment",
            "delete_fragment",
        }

    def test_import_tools_includes_add_fragment(self):
        """P1-2 (Q3): «добавляющий» может добавлять секции в существующие книги."""
        assert "add_fragment" in IMPORT_TOOLS
        assert IMPORT_TOOLS == {
            "import_content",
            "cancel_import",
            "extract_pdf_text",
            "add_fragment",
        }


# ── check_tool_permission: editor ─────────────────────────────


class TestEditorPermissions:
    def test_editor_grants_read_tools(self):
        auth = AuthInfo(authenticated=True, key_level="editor")
        check_tool_permission(auth, "search_knowledge")
        check_tool_permission(auth, "get_knowledge_map")
        check_tool_permission(auth, "list_quality_issues")
        check_tool_permission(auth, "review_queue")

    def test_editor_grants_editor_tools(self):
        auth = AuthInfo(authenticated=True, key_level="editor")
        for tool in sorted(EDITOR_TOOLS):
            check_tool_permission(auth, tool)

    @pytest.mark.parametrize(
        "tool",
        ["reindex", "set_zone", "bulk_resolve_issues", "bulk_deprecate_duplicates"],
    )
    def test_editor_blocks_admin_tools(self, tool):
        auth = AuthInfo(authenticated=True, key_level="editor")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, tool)
        assert exc.value.status_code == 403

    def test_editor_error_message_names_level(self):
        auth = AuthInfo(authenticated=True, key_level="editor")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "reindex")
        assert "Editor" in exc.value.detail


# ── check_tool_permission: import += add_fragment (P1-2) ──────


class TestImportAddFragment:
    def test_import_grants_add_fragment(self):
        auth = AuthInfo(authenticated=True, key_level="import")
        check_tool_permission(auth, "add_fragment")

    def test_import_blocks_update_fragment(self):
        auth = AuthInfo(authenticated=True, key_level="import")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "update_fragment")
        assert exc.value.status_code == 403

    def test_import_blocks_delete_fragment(self):
        auth = AuthInfo(authenticated=True, key_level="import")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "delete_fragment")
        assert exc.value.status_code == 403

    def test_import_blocks_tokens_tools(self):
        """У-3-контекст: import не должен иметь админ-поверхности."""
        auth = AuthInfo(authenticated=True, key_level="import")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "write_knowledge")
        assert exc.value.status_code == 403


# ── Регресс-граница: существующие уровни неизменны ────────────


class TestExistingLevelsRegression:
    def test_write_grants_everything(self):
        auth = AuthInfo(authenticated=True, key_level="write")
        check_tool_permission(auth, "reindex")
        check_tool_permission(auth, "set_zone")
        check_tool_permission(auth, "bulk_resolve_issues")
        check_tool_permission(auth, "add_fragment")

    def test_read_unchanged(self):
        auth = AuthInfo(authenticated=True, key_level="read")
        check_tool_permission(auth, "analyze_content")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "write_knowledge")
        assert exc.value.status_code == 403

    def test_subscriber_unchanged(self):
        auth = AuthInfo(authenticated=True, key_level="subscriber")
        check_tool_permission(auth, "search_knowledge")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "list_quality_issues")
        assert exc.value.status_code == 403
        assert SUBSCRIBER_TOOLS  # множество на месте

    def test_read_tools_unchanged(self):
        assert "add_fragment" not in READ_TOOLS
        assert "reindex" in WRITE_TOOLS


# ── У-3: tokens_api ADMIN_LEVELS = {write} ────────────────────


class TestTokensApiAdminLevels:
    def test_admin_levels_is_write_only(self):
        from mcp_server.tokens_api import ADMIN_LEVELS

        assert ADMIN_LEVELS == {"write"}

    def test_import_key_forbidden_on_tokens_api(self):
        from mcp_server.tokens_api import _require_admin

        request = MagicMock()
        request.state = SimpleNamespace(auth=AuthInfo(authenticated=True, key_level="import"))
        with pytest.raises(HTTPException) as exc:
            _require_admin(request)
        assert exc.value.status_code == 403

    def test_write_key_allowed_on_tokens_api(self):
        from mcp_server.tokens_api import _require_admin

        request = MagicMock()
        request.state = SimpleNamespace(auth=AuthInfo(authenticated=True, key_level="write"))
        _require_admin(request)  # не падает

    def test_editor_key_forbidden_on_tokens_api(self):
        from mcp_server.tokens_api import _require_admin

        request = MagicMock()
        request.state = SimpleNamespace(auth=AuthInfo(authenticated=True, key_level="editor"))
        with pytest.raises(HTTPException) as exc:
            _require_admin(request)
        assert exc.value.status_code == 403


# ── token_store: editor create/префикс mcp_e*_ (Ф1.3) ─────────


class TestTokenStoreEditorLevel:
    def test_level_codes_include_editor(self):
        assert LEVEL_CODES["editor"] == "e"

    @pytest.mark.parametrize("zone,zone_code", [("public", "a"), ("private", "b"), ("both", "x")])
    def test_editor_token_prefix(self, tmp_path, zone, zone_code):
        store = TokenStore(tokens_dir=str(tmp_path))
        token_id, plaintext = store.create(level="editor", zone=zone, note="editor key")
        assert plaintext.startswith(f"mcp_e{zone_code}_")
        # round-trip через get_by_key
        found = store.get_by_key(plaintext)
        assert found is not None
        assert found.id == token_id
        assert found.level == "editor"
