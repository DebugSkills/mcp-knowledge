"""Integration tests: HTTP endpoints (13.21) — POST /upload, GET /imports, cancel.

Tests the FastAPI endpoints for PDF upload queue using httpx ASGITransport.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from mcp_server.config import settings


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
    """Write key из актуального settings (e2e-фикстура может мутировать его)."""
    keys = settings.MCP_WRITE_KEYS or ["test-write-key"]
    return keys[0]


@pytest.fixture
def sample_text_pdf_path(tmp_path):
    """Create a minimal text PDF for upload test."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    path = tmp_path / "upload_test.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, "Test content for upload endpoint.")
    c.save()
    return str(path)


# ═══════════════════════════════════════════════════════════════
# POST /upload
# ═══════════════════════════════════════════════════════════════


class TestUploadEndpoint:
    """POST /upload — multipart PDF upload."""

    @pytest.mark.asyncio
    async def test_upload_success(self, sample_text_pdf_path):
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with open(sample_text_pdf_path, "rb") as f:  # noqa: ASYNC230
                files = {"file": ("test.pdf", f, "application/pdf")}
                response = await client.post(
                    "/upload", files=files, headers={"X-API-Key": write_key}
                )

        assert response.status_code == 200
        data = response.json()
        assert "pdf_path" in data
        assert "content_hash" in data

    @pytest.mark.asyncio
    async def test_upload_no_auth_returns_401(self):
        app = _make_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/upload",
                files={"file": ("test.pdf", b"fake", "application/pdf")},
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_upload_no_file_returns_415(self):
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/upload", headers={"X-API-Key": write_key})
            assert response.status_code == 415  # not multipart


# ═══════════════════════════════════════════════════════════════
# GET /imports + GET /imports/active + POST cancel
# ═══════════════════════════════════════════════════════════════


class TestImportsEndpoints:
    """GET /imports, GET /imports/active, POST /imports/{id}/cancel."""

    @pytest.mark.asyncio
    async def test_list_imports_empty(self):
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/imports", headers={"X-API-Key": write_key})
            assert response.status_code == 200
            assert isinstance(response.json(), list)

    @pytest.mark.asyncio
    async def test_active_no_running(self):
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/imports/active", headers={"X-API-Key": write_key})
            assert response.status_code == 200
            data = response.json()
            assert data.get("active") is False

    @pytest.mark.asyncio
    async def test_cancel_no_active(self):
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/nonexistent/cancel", headers={"X-API-Key": write_key}
            )
            assert response.status_code == 404
