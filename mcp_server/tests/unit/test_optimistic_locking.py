"""F2: Unit tests for Optimistic Locking (expected_version → VersionConflictError).

Covers:
- update_entry(expected_version=N) success when versions match
- update_entry(expected_version=N) conflict when versions don't match
- update_entry without expected_version → backward-compatible (last-write-wins)
- VersionConflictError attributes (knowledge_id, expected, actual)
- mcp_handler conflict detection (MCP_CONFLICT = -32005)
- P1-4: atomic check under _git_lock (TOCTOU prevention, verified via mock)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.tools.crud import update_entry
from mcp_server.models import VersionConflictError
from mcp_server.mcp_handler import (
    MCP_CONFLICT,
    _handle_tools_call,
    _jsonrpc_error,
)

pytestmark = pytest.mark.asyncio


# ── update_entry: optimistic locking ───────────────────────

async def test_update_entry_success_no_version(app_state):
    """F2: update without expected_version → backward-compatible (last-write-wins)."""
    result = await update_entry(
        {"knowledge_id": "ru-test-entry", "content": "# Updated\nContent."},
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "ru-test-entry"
    assert result["version"] == 1


async def test_update_entry_success_matching_version(app_state):
    """F2: update with expected_version=1 when current version=1 → success."""
    result = await update_entry(
        {"knowledge_id": "ru-test-entry", "content": "# Updated", "version": 1},
        app_state,
    )
    assert "error" not in result
    assert result["knowledge_id"] == "ru-test-entry"


async def test_update_entry_conflict_wrong_version(app_state):
    """F2: expected_version=2 when current version=1 → conflict."""
    result = await update_entry(
        {"knowledge_id": "ru-test-entry", "content": "# Updated", "version": 2},
        app_state,
    )
    assert result["conflict"] is True
    assert result["expected_version"] == 2
    assert result["current_version"] == 1
    assert "Version conflict" in result["message"]
    assert result["knowledge_id"] == "ru-test-entry"


async def test_update_entry_conflict_no_error_field(app_state):
    """F2: conflict response uses 'message' field (not legacy 'error')."""
    result = await update_entry(
        {"knowledge_id": "ru-test-entry", "content": "# Updated", "version": 99},
        app_state,
    )
    assert result["conflict"] is True
    assert "message" in result  # план: поле 'message'
    assert "current_version" in result  # план: поле 'current_version'
    assert "actual_version" not in result  # НЕ legacy 'actual_version'


# ── VersionConflictError class ─────────────────────────────

class TestVersionConflictError:
    """F2: Verify VersionConflictError exception attributes."""

    def test_attributes(self):
        """VersionConflictError stores knowledge_id, expected, actual."""
        exc = VersionConflictError("test-id", expected=3, actual=4)
        assert exc.knowledge_id == "test-id"
        assert exc.expected == 3
        assert exc.actual == 4

    def test_message_format(self):
        """Error message contains version info."""
        exc = VersionConflictError("ru-test", expected=2, actual=5)
        msg = str(exc)
        assert "ru-test" in msg
        assert "v2" in msg
        assert "v5" in msg


# ── MCP handler: conflict → JSON-RPC error ────────────────

class TestMcpHandlerConflict:
    """F2: mcp_handler._handle_tools_call detects conflict → MCP_CONFLICT (-32005)."""

    async def test_tools_call_detects_conflict(self):
        """When tool handler returns {'conflict': True, ...}, handler returns JSON-RPC error."""
        req = MagicMock()
        req.state.auth = MagicMock(authenticated=True, key_level="write", key_hash="abc")
        req.app.state = MagicMock()

        # Mock tool handler that returns a conflict result
        async def conflict_handler(params, app_state):
            return {
                "message": "Version conflict for 'test': expected v2, actual v3",
                "knowledge_id": "test",
                "expected_version": 2,
                "current_version": 3,
                "conflict": True,
            }

        # Patch TOOL_HANDLERS
        with patch("mcp_server.mcp_handler.TOOL_HANDLERS", {"test_tool": conflict_handler}), \
             patch("mcp_server.mcp_handler.check_tool_permission", MagicMock()):

            result = await _handle_tools_call(
                {"name": "test_tool", "arguments": {}},
                request_id=42,
                request=req,
            )

        assert "error" in result
        assert result["error"]["code"] == MCP_CONFLICT
        assert "Version conflict" in result["error"]["message"]
        assert result["error"]["data"]["expected_version"] == 2
        assert result["error"]["data"]["current_version"] == 3

    async def test_tools_call_normal_result_not_conflict(self):
        """Normal result (no conflict flag) is returned as success."""
        req = MagicMock()
        req.state.auth = MagicMock(authenticated=True, key_level="write", key_hash="abc")
        req.app.state = MagicMock()

        async def normal_handler(params, app_state):
            return {"status": "ok", "version": 5}

        with patch("mcp_server.mcp_handler.TOOL_HANDLERS", {"test_tool": normal_handler}), \
             patch("mcp_server.mcp_handler.check_tool_permission", MagicMock()):

            result = await _handle_tools_call(
                {"name": "test_tool", "arguments": {}},
                request_id=1,
                request=req,
            )

        assert "result" in result
        content_text = result["result"]["content"][0]["text"]
        parsed = json.loads(content_text)
        assert parsed["status"] == "ok"
        assert parsed["version"] == 5

    async def test_conflict_result_has_correct_err_code(self):
        """MCP_CONFLICT = -32005 as per JSON-RPC error code registry."""
        assert MCP_CONFLICT == -32005

    async def test_conflict_error_without_expected_data(self):
        """Conflict error without expected/current version still returns -32005."""
        req = MagicMock()
        req.state.auth = MagicMock(authenticated=True, key_level="write", key_hash="abc")
        req.app.state = MagicMock()

        async def minimal_conflict_handler(params, app_state):
            return {"conflict": True, "message": "Generic conflict"}

        with patch("mcp_server.mcp_handler.TOOL_HANDLERS", {"test_tool": minimal_conflict_handler}), \
             patch("mcp_server.mcp_handler.check_tool_permission", MagicMock()):

            result = await _handle_tools_call(
                {"name": "test_tool", "arguments": {}},
                request_id=99,
                request=req,
            )

        assert result["error"]["code"] == MCP_CONFLICT
        assert result["error"]["data"]["expected_version"] is None
        assert result["error"]["data"]["current_version"] is None


# ── TOCTOU (P1-4) prevention: mock store.update atomicity ──

class TestTocTouPrevention:
    """F2 P1-4: Verify that version check is atomic (happens inside store.update, not in crud.py)."""

    async def test_crud_passes_expected_version_to_store(self):
        """F2 P1-4: crud.py update_entry passes expected_version directly to store.update(), not reading first."""
        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
        from datetime import datetime, timezone
        from mcp_server.tools.crud import update_entry

        mock_store = MagicMock()
        mock_store.read = AsyncMock()
        mock_store.update = AsyncMock()

        # Return a valid entry so update_entry doesn't error on entry.frontmatter.domain
        fm = KnowledgeFrontmatter(
            knowledge_id="test-id", domain="eng", subject="test",
            version=3, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        sample = KnowledgeEntry(frontmatter=fm, content="test")
        mock_store.update.return_value = sample

        state = MagicMock()
        state.store = mock_store
        state.pipeline = MagicMock()
        state.pipeline.enqueue = AsyncMock()
        state.knowledge_index = MagicMock()
        state.knowledge_index.update_section = MagicMock()
        state.qdrant = MagicMock()

        await update_entry(
            {"knowledge_id": "test-id", "content": "x", "version": 3},
            state,
        )

        # store.read should NOT be called (crud.py doesn't read before update)
        mock_store.read.assert_not_called()
        # store.update IS called with expected_version
        mock_store.update.assert_called_once()
        call_kwargs = mock_store.update.call_args.kwargs
        assert "expected_version" in call_kwargs
        assert call_kwargs["expected_version"] == 3


# ── Helpers ────────────────────────────────────────────────

from unittest.mock import patch  # noqa: F401 — used in TestMcpHandlerConflict
