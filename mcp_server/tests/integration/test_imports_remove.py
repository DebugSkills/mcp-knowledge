"""Integration tests: POST /imports/{id}/remove + POST /imports/remove-finished.

Tests the new queue-cleanup HTTP endpoints using httpx ASGITransport.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient


def _make_app():
    """Create FastAPI app with all required state initialized."""
    from mcp_server.main import app

    if not hasattr(app.state, "heavy_ops_lock"):
        app.state.heavy_ops_lock = asyncio.Lock()
        app.state.scan_lock = app.state.heavy_ops_lock
    if not hasattr(app.state, "import_task"):
        app.state.import_task = None
        app.state.import_cancel_event = None
        app.state.import_queue = []
    if not hasattr(app.state, "import_progress"):
        from mcp_server.progress import ImportProgressTracker

        app.state.import_progress = ImportProgressTracker()

    return app


def _get_write_key() -> str:
    """Write key из актуального settings."""
    from mcp_server.config import settings

    keys = settings.MCP_WRITE_KEYS or ["test-write-key"]
    return keys[0]


def _get_read_key() -> str:
    """Read key из актуального settings."""
    from mcp_server.config import settings

    keys = settings.MCP_READ_KEYS or ["test-read-key"]
    return keys[0]


def _seed_queue(app, records: list[dict]) -> None:
    """Наполнить import_queue тестовыми записями."""
    app.state.import_queue = records


# ═══════════════════════════════════════════════════════════════
# POST /imports/{import_id}/remove
# ═══════════════════════════════════════════════════════════════


class TestRemoveImport:
    """POST /imports/{import_id}/remove."""

    @pytest.mark.asyncio
    async def test_remove_done_removes_from_queue(self):
        """Удаление done-записи: GET /imports больше не содержит."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "done-1", "name": "test.pdf", "status": "done"},
            {"import_id": "queued-1", "name": "other.pdf", "status": "queued"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Удаляем done-1
            resp = await client.post(
                "/imports/done-1/remove", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["removed"] is True
            assert data["import_id"] == "done-1"

            # Проверяем, что done-1 исчезла из очереди
            resp2 = await client.get("/imports", headers={"X-API-Key": write_key})
            assert resp2.status_code == 200
            queue = resp2.json()
            assert len(queue) == 1
            assert queue[0]["import_id"] == "queued-1"

    @pytest.mark.asyncio
    async def test_remove_running_returns_409(self):
        """Удаление running-записи должно вернуть 409."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "run-1", "name": "test.pdf", "status": "running"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/run-1/remove", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 409
            assert "must be cancelled first" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_404(self):
        """Удаление несуществующей записи → 404."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "done-1", "name": "test.pdf", "status": "done"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/nonexistent/remove", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_queued_succeeds(self):
        """Удаление queued-записи (без _params) — OK."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "q-1", "name": "test.pdf", "status": "queued"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/q-1/remove", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 200
            assert resp.json()["removed"] is True

            resp2 = await client.get("/imports", headers={"X-API-Key": write_key})
            assert resp2.json() == []


# ═══════════════════════════════════════════════════════════════
# POST /imports/remove-finished
# ═══════════════════════════════════════════════════════════════


class TestRemoveFinished:
    """POST /imports/remove-finished."""

    @pytest.mark.asyncio
    async def test_remove_finished_clears_done_error_cancelled(self):
        """Удаляет все done/error/cancelled, running остаётся."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "done-1", "name": "a.pdf", "status": "done"},
            {"import_id": "err-1", "name": "b.pdf", "status": "error", "error": "fail"},
            {"import_id": "cancel-1", "name": "c.pdf", "status": "cancelled"},
            {"import_id": "run-1", "name": "d.pdf", "status": "running"},
            {"import_id": "queued-1", "name": "e.pdf", "status": "queued"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/remove-finished", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["removed"] == 3  # done + error + cancelled

            # Проверяем: остались только running и queued
            resp2 = await client.get("/imports", headers={"X-API-Key": write_key})
            queue = resp2.json()
            remaining_ids = {r["import_id"] for r in queue}
            assert remaining_ids == {"run-1", "queued-1"}

    @pytest.mark.asyncio
    async def test_remove_finished_noop_when_empty(self):
        """remove-finished на пустой очереди → removed=0."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/remove-finished", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 200
            assert resp.json()["removed"] == 0

    @pytest.mark.asyncio
    async def test_remove_finished_only_running_returns_0(self):
        """Если в очереди только running → removed=0."""
        app = _make_app()
        write_key = _get_write_key()
        _seed_queue(app, [
            {"import_id": "run-1", "name": "a.pdf", "status": "running"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/remove-finished", headers={"X-API-Key": write_key}
            )
            assert resp.status_code == 200
            assert resp.json()["removed"] == 0

            # running остался
            resp2 = await client.get("/imports", headers={"X-API-Key": write_key})
            assert len(resp2.json()) == 1
            assert resp2.json()[0]["import_id"] == "run-1"


# ═══════════════════════════════════════════════════════════════
# Auth tests
# ═══════════════════════════════════════════════════════════════


class TestRemoveAuth:
    """Auth проверки для remove-endpoints."""

    @pytest.mark.asyncio
    async def test_remove_no_auth_returns_401(self):
        """Без ключа → 401."""
        app = _make_app()
        _seed_queue(app, [
            {"import_id": "done-1", "name": "test.pdf", "status": "done"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/imports/done-1/remove")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_remove_read_key_returns_403(self):
        """Read-ключ → 403."""
        app = _make_app()
        read_key = _get_read_key()
        _seed_queue(app, [
            {"import_id": "done-1", "name": "test.pdf", "status": "done"},
        ])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/done-1/remove", headers={"X-API-Key": read_key}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_remove_finished_no_auth_returns_401(self):
        """Без ключа → 401."""
        app = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/imports/remove-finished")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_remove_finished_read_key_returns_403(self):
        """Read-ключ → 403."""
        app = _make_app()
        read_key = _get_read_key()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/imports/remove-finished", headers={"X-API-Key": read_key}
            )
            assert resp.status_code == 403
