"""Unit tests: data_version — monotonic mutation counter (Task 1).

Tests: starts_zero, endpoint, increment_on_delete, increment_on_resolve,
no_increment_on_scan, _on_update. Uses shared app_state fixture from conftest.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.tools.crud import delete_entry, update_entry
from mcp_server.tools.quality import resolve_quality_issue, run_quality_scan

pytestmark = pytest.mark.asyncio


class TestDataVersion:
    """Тесты data_version — монотонного счётчика мутаций."""

    def test_data_version_starts_zero(self, app_state):
        """data_version инициализируется в 0."""
        assert app_state.data_version == 0

    def test_data_version_endpoint_semantics(self, app_state):
        """Прямой доступ к data_version через app_state."""
        dv = getattr(app_state, "data_version", -1)
        assert dv == 0
        app_state.data_version += 1
        assert app_state.data_version == 1

    async def test_data_version_increment_on_delete(self, app_state):
        """delete_entry инкрементирует data_version."""
        app_state.store.delete = AsyncMock(return_value=True)
        v_before = app_state.data_version

        result = await delete_entry({"knowledge_id": "ru-test-entry"}, app_state)
        assert result["deleted"] is True
        assert app_state.data_version == v_before + 1

    async def test_data_version_increment_on_update(self, app_state):
        """update_entry инкрементирует data_version."""
        v_before = app_state.data_version

        result = await update_entry(
            {"knowledge_id": "ru-test-entry", "content": "# Updated\nNew."},
            app_state,
        )
        assert result["knowledge_id"] == "ru-test-entry"
        assert app_state.data_version == v_before + 1

    async def test_data_version_increment_on_resolve(self, app_state):
        """resolve_quality_issue (deprecate) инкрементирует data_version."""
        app_state.qdrant.set_payload = MagicMock()
        v_before = app_state.data_version

        result = await resolve_quality_issue(
            {"action": "deprecate", "knowledge_id": "ru-test-entry", "reason": "test"},
            app_state,
        )
        assert result["resolved"] is True
        assert app_state.data_version == v_before + 1

    async def test_data_version_no_increment_on_scan(self, app_state):
        """run_quality_scan НЕ инкрементирует data_version (progress-only)."""
        # Mock already_running scenario
        app_state.scan_lock.locked.return_value = True
        v_before = app_state.data_version

        result = await run_quality_scan({}, app_state)
        assert result["status"] == "already_running"
        assert app_state.data_version == v_before
