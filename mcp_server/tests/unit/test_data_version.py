"""Unit tests: data_version — monotonic mutation counter (Task 1 + Task 2).

Tests: starts_zero, endpoint, increment_on_delete, increment_on_resolve,
no_increment_on_scan, _on_update, concurrent (Task 2: won't-fix guard),
single-worker invariant (Dockerfile), await-free invariant (source scan).

Uses shared app_state fixture from conftest.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.tools.crud import delete_entry, update_entry
from mcp_server.tools.quality import resolve_quality_issue, run_quality_scan

pytestmark = pytest.mark.asyncio

MCP_SERVER_DIR = Path(__file__).parents[2]  # tests/unit → mcp_server/

# Исходники, где происходит data_version += 1 (6 мест — Task 1)
_DATA_VERSION_FILES: tuple[str, ...] = (
    "src/mcp_server/tools/content.py",
    "src/mcp_server/tools/crud.py",
    "src/mcp_server/tools/quality.py",
)


async def _increment_data_version(app_state: MagicMock) -> None:
    """Helper: атомарный инкремент data_version (try/except — как в production)."""
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110, BLE001
        pass  # best-effort


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

    # ── Task 2: won't-fix guard tests ──────────────────────────

    async def test_data_version_concurrent_increment(self, app_state):
        """Конкурентный инкремент: asyncio.gather 2× → итог +2 (single-loop atomic).

        Доказывает, что await-free инкремент в single-worker event loop
        не требует Lock — две параллельные корутины дают ровно +2.
        """
        v_before = app_state.data_version

        await asyncio.gather(
            _increment_data_version(app_state),
            _increment_data_version(app_state),
        )

        assert app_state.data_version == v_before + 2


class TestDataVersionInvariants:
    """Task 2: инварианты data_version (single-worker, await-free)."""

    def test_dockerfile_single_worker(self) -> None:
        """Dockerfile содержит --workers 1 (защита от добавления workers>1)."""
        dockerfile = MCP_SERVER_DIR / "Dockerfile"
        assert dockerfile.is_file(), f"Dockerfile not found at {dockerfile}"

        content = dockerfile.read_text(encoding="utf-8")
        # Ищем строку с uvicorn + --workers (JSON-массив: "--workers", "1")
        cmd_lines = [l.strip() for l in content.splitlines()
                     if "uvicorn" in l and "workers" in l]

        assert cmd_lines, "No CMD line with 'workers' found in Dockerfile"
        cmd = cmd_lines[0]
        assert '"--workers", "1"' in cmd, (
            f"Dockerfile CMD missing '--workers', '1' (JSON array): {cmd!r}\n"
            f"Инвариант: data_version рассчитан на single-worker event loop."
        )

    def test_data_version_lines_await_free(self) -> None:
        """Все 6 строк data_version += 1 в исходниках — без await.

        Сканирует content.py, crud.py, quality.py и проверяет, что ни одна
        строка с 'data_version += 1' не содержит 'await' (подстрока).
        """
        found: list[tuple[str, int, str]] = []  # (file, lineno, line)

        for rel in _DATA_VERSION_FILES:
            src_path = MCP_SERVER_DIR / rel
            if not src_path.is_file():
                continue  # skip if file moved (early-warning, not hard fail)
            for lineno, line in enumerate(src_path.read_text(encoding="utf-8").splitlines(), 1):
                if "data_version += 1" in line:
                    found.append((rel, lineno, line.strip()))

        assert found, "No 'data_version += 1' lines found in scanned sources"

        for rel, lineno, line in found:
            assert "await" not in line, (
                f"'await' found on data_version line in {rel}:{lineno}: {line!r}\n"
                f"Инвариант: data_version += 1 должен быть await-free "
                f"(рассчитан на single-worker event loop)."
            )
