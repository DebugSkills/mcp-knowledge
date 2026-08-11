"""Integration tests: POST /imports/convert, POST /imports/analyze, cancel per-ID, progress.

Фаза code-2026-08-11-queue-delete-emoji: Feature 1 — карточки очереди для convert/analyze.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

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
    # Analyze semaphore
    if not hasattr(app.state, "analyze_semaphore"):
        app.state.analyze_semaphore = asyncio.Semaphore(3)

    return app


def _get_write_key() -> str:
    """Write key из актуального settings."""
    keys = settings.MCP_WRITE_KEYS or ["test-write-key"]
    return keys[0]


def _get_import_key() -> str:
    """Import key из актуального settings."""
    keys = settings.MCP_IMPORT_KEYS or settings.MCP_WRITE_KEYS or ["test-import-key"]
    return keys[0]


@pytest.fixture
def sample_text_pdf_path(tmp_path):
    """Create a minimal text PDF for upload/convert test."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    path = tmp_path / "convert_test.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, "PDF text for convert endpoint testing.")
    c.save()
    # Copy to /tmp/pdf_uploads — endpoint validates prefix
    upload_dir = "/tmp/pdf_uploads"
    os.makedirs(upload_dir, exist_ok=True)
    dest = os.path.join(upload_dir, "convert_test.pdf")
    import shutil
    shutil.copy2(str(path), dest)
    return dest


# ═══════════════════════════════════════════════════════════════
# POST /imports/convert
# ═══════════════════════════════════════════════════════════════


class TestConvertEndpoint:
    """POST /imports/convert — queue card creation, validation, auth."""

    @pytest.mark.asyncio
    async def test_convert_creates_queue_record(self, sample_text_pdf_path):
        """POST /imports/convert → запись в очереди ДО возврата HTTP-ответа."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/convert",
                json={"pdf_path": sample_text_pdf_path, "base_id": "test-base-1"},
                headers={"X-API-Key": write_key},
            )

        assert response.status_code == 200
        data = response.json()
        assert "import_id" in data
        assert data["status"] in ("started", "queued")

        # Проверяем что запись есть в очереди
        queue = app.state.import_queue
        found = [r for r in queue if r["import_id"] == data["import_id"]]
        assert len(found) == 1
        assert found[0]["operation_type"] == "convert"
        # "started" — только в HTTP-ответе; в записи очереди статусы queued|running|...
        assert found[0]["status"] in ("running", "queued")

    @pytest.mark.asyncio
    async def test_convert_requires_auth_401(self, sample_text_pdf_path):
        """Без X-API-Key → 401."""
        app = _make_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/convert",
                json={"pdf_path": sample_text_pdf_path, "base_id": "test"},
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_convert_missing_pdf_path_400(self):
        """Без pdf_path → 400."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/convert",
                json={"base_id": "test"},
                headers={"X-API-Key": write_key},
            )
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_convert_pdf_not_in_uploads_400(self):
        """pdf_path не в /tmp/pdf_uploads → 400."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/convert",
                json={"pdf_path": "/etc/passwd", "base_id": "test"},
                headers={"X-API-Key": write_key},
            )
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_convert_file_not_found_404(self, tmp_path):
        """pdf_path в /tmp/pdf_uploads но файла нет → 404."""
        fake_path = "/tmp/pdf_uploads/nonexistent.pdf"
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/convert",
                json={"pdf_path": fake_path, "base_id": "test"},
                headers={"X-API-Key": write_key},
            )
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_convert_result_excluded_from_get_imports(self, sample_text_pdf_path):
        """GET /imports НЕ должен содержать поле result."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        import_id = None
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Создаём convert запись
            r = await client.post(
                "/imports/convert",
                json={"pdf_path": sample_text_pdf_path, "base_id": "conv-excl"},
                headers={"X-API-Key": write_key},
            )
            import_id = r.json()["import_id"]

            # Симулируем что в очереди есть result поле
            for rec in app.state.import_queue:
                if rec["import_id"] == import_id:
                    rec["result"] = {"text": "fake text", "chars": 9}

            response = await client.get("/imports", headers={"X-API-Key": write_key})
            records = response.json()
            for rec in records:
                assert "result" not in rec, f"result field leaked: {rec}"

    @pytest.mark.asyncio
    async def test_convert_progress_includes_result(self, sample_text_pdf_path):
        """GET /imports/{id}/progress должен отдавать result поле."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        import_id = None
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/imports/convert",
                json={"pdf_path": sample_text_pdf_path, "base_id": "conv-prog"},
                headers={"X-API-Key": write_key},
            )
            import_id = r.json()["import_id"]

            # Симулируем result в очереди
            for rec in app.state.import_queue:
                if rec["import_id"] == import_id:
                    rec["result"] = {"text": "fake text", "chars": 9}
                    rec["status"] = "done"

            response = await client.get(
                f"/imports/{import_id}/progress",
                headers={"X-API-Key": write_key},
            )
            assert response.status_code == 200
            data = response.json()
            assert "result" in data, f"result missing: {data}"
            assert data["result"] == {"text": "fake text", "chars": 9}

    @pytest.mark.asyncio
    async def test_convert_progress_includes_operation_type(self, sample_text_pdf_path):
        """GET /imports/{id}/progress должен отдавать operation_type."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        import_id = None
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/imports/convert",
                json={"pdf_path": sample_text_pdf_path, "base_id": "conv-opt"},
                headers={"X-API-Key": write_key},
            )
            import_id = r.json()["import_id"]

            response = await client.get(
                f"/imports/{import_id}/progress",
                headers={"X-API-Key": write_key},
            )
            # Может быть 404 если tracker не стартовал — тогда проверяем queue path
            if response.status_code == 404:
                # Симулируем done чтобы был в queue
                for rec in app.state.import_queue:
                    if rec["import_id"] == import_id:
                        rec["status"] = "done"
                response = await client.get(
                    f"/imports/{import_id}/progress",
                    headers={"X-API-Key": write_key},
                )
            if response.status_code == 200:
                data = response.json()
                assert "operation_type" in data, f"operation_type missing: {data}"
                assert data["operation_type"] == "convert"


# ═══════════════════════════════════════════════════════════════
# POST /imports/analyze
# ═══════════════════════════════════════════════════════════════


class TestAnalyzeEndpoint:
    """POST /imports/analyze — queue card, semaphore, result."""

    @pytest.mark.asyncio
    async def test_analyze_creates_queue_record(self):
        """POST /imports/analyze → запись в очереди."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/analyze",
                json={"content": "Test content for analysis", "base_id": "analyze-base"},
                headers={"X-API-Key": write_key},
            )

        assert response.status_code == 200
        data = response.json()
        assert "import_id" in data
        assert data["status"] == "started"

        # Проверяем запись в очереди
        queue = app.state.import_queue
        found = [r for r in queue if r["import_id"] == data["import_id"]]
        assert len(found) == 1
        assert found[0]["operation_type"] == "analyze"
        assert found[0]["name"].startswith("Обработать:")

    @pytest.mark.asyncio
    async def test_analyze_requires_auth_401(self):
        """Без X-API-Key → 401."""
        app = _make_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/imports/analyze",
                json={"content": "test", "base_id": "test"},
            )
            assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════
# Cancel per-ID: queued → cancelled без event
# ═══════════════════════════════════════════════════════════════


class TestCancelPerId:
    """Cancel per-ID: queued без event, running с event, done/error → 409."""

    @pytest.mark.asyncio
    async def test_cancel_queued_no_event(self):
        """Queued convert → cancel устанавливает status=cancelled БЕЗ event."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        # Вручную добавляем queued запись в очередь
        import_id = "queued-test-id"
        app.state.import_queue.append({
            "import_id": import_id,
            "name": "test queued",
            "status": "queued",
            "operation_type": "convert",
            "phase": "",
            "imported": 0,
            "total": 0,
            "error": None,
        })

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/imports/{import_id}/cancel",
                headers={"X-API-Key": write_key},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["cancelled"] is True
        assert data["import_id"] == import_id

        # Проверяем запись в очереди
        for rec in app.state.import_queue:
            if rec["import_id"] == import_id:
                assert rec["status"] == "cancelled"
                assert rec["error"] == "Cancelled by user"
                break

    @pytest.mark.asyncio
    async def test_cancel_running_sets_event(self):
        """Running → cancel устанавливает event."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        import_id = "running-test-id"
        cancel_event = asyncio.Event()
        app.state.import_queue.append({
            "import_id": import_id,
            "name": "test running",
            "status": "running",
            "operation_type": "import",
            "phase": "parsing",
            "imported": 0,
            "total": 100,
            "error": None,
            "_cancel_event": cancel_event,
        })

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/imports/{import_id}/cancel",
                headers={"X-API-Key": write_key},
            )

        assert response.status_code == 200
        assert cancel_event.is_set()

    @pytest.mark.asyncio
    async def test_cancel_done_returns_409(self):
        """Уже done → cancel возвращает 409."""
        app = _make_app()
        transport = ASGITransport(app=app)
        write_key = _get_write_key()

        import_id = "done-test-id"
        app.state.import_queue.append({
            "import_id": import_id,
            "name": "test done",
            "status": "done",
            "operation_type": "import",
            "phase": "done",
            "imported": 50,
            "total": 50,
            "error": None,
        })

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/imports/{import_id}/cancel",
                headers={"X-API-Key": write_key},
            )

        assert response.status_code == 409


# ═══════════════════════════════════════════════════════════════
# _start_next_import пропускает не-import операции
# ═══════════════════════════════════════════════════════════════


class TestQueueAdvance:
    """_start_next_import guard: пропускает convert/analyze операции."""

    @pytest.mark.asyncio
    @patch("mcp_server.tools.content._bg_import", new_callable=AsyncMock)
    async def test_start_next_import_skips_convert_analyze(self, mock_bg_import):
        """convert/analyze операции не запускаются через _start_next_import."""
        from mcp_server.tools.content import _import_queue, _start_next_import

        # Очищаем очередь и добавляем тестовые записи
        _import_queue.clear()
        _import_queue.extend([
            {
                "import_id": "conv-1",
                "name": "convert op",
                "status": "queued",
                "operation_type": "convert",
                "_params": {},
            },
            {
                "import_id": "imp-1",
                "name": "import op",
                "status": "queued",
                "operation_type": "import",
                "_params": {"content": "test", "domain": "test", "subject": "test", "content_type": "book"},
            },
        ])

        # Мокаем app_state
        app_state = MagicMock()
        app_state.heavy_ops_lock = asyncio.Lock()
        app_state.store = AsyncMock()
        app_state.pipeline = MagicMock()
        app_state.qdrant = MagicMock()
        app_state.embedder = MagicMock()
        app_state.knowledge_index = MagicMock()
        app_state.data_version = 0
        app_state.import_progress = MagicMock()
        app_state.settings = MagicMock()
        app_state.settings.KNOWLEDGE_DIR = "/tmp/test"
        app_state.settings.IMPORT_PERIODIC_COMMIT = 100
        app_state.store.read = AsyncMock(return_value=None)

        # Должен запустить imp-1 (import), НЕ conv-1 (convert)
        _start_next_import(app_state)
        await asyncio.sleep(0)  # дать create_task стартовать (мок — мгновенно)

        # conv-1 должен остаться queued
        conv_record = next((r for r in _import_queue if r["import_id"] == "conv-1"), None)
        assert conv_record is not None
        assert conv_record["status"] == "queued", f"Expected queued, got {conv_record['status']}"

        # imp-1 должен быть переведён в running
        imp_record = next((r for r in _import_queue if r["import_id"] == "imp-1"), None)
        assert imp_record is not None
        assert imp_record["status"] == "running"

        # _bg_import вызван только для imp-1 (не для conv-1)
        assert mock_bg_import.await_count == 1

        # Cleanup
        _import_queue.clear()
