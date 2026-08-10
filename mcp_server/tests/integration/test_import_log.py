"""P2 tests: GET /imports/{id}/log endpoint + ring buffer behavior.

Фаза code-2026-08-10-305.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport
from mcp_server.auth import AuthMiddleware, AuthInfo


# ═══════════════════════════════════════════════════════════════
# Auth setup — monkeypatch test keys
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _mock_auth_keys(monkeypatch):
    """Inject test API keys into settings."""
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_READ_KEYS", ["read-key-12345678"]
    )
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_WRITE_KEYS", ["write-key-abcdefgh"]
    )
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_IMPORT_KEYS", ["import-key-12345678"]
    )


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def log_app():
    """FastAPI app с AuthMiddleware + GET /imports/{id}/log + GET /imports."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, fastapi_app=app)

    # Pre-populated queue with log data
    app.state.import_queue = [
        {
            "import_id": "import-001",
            "name": "book_1.pdf",
            "status": "done",
            "phase": "done",
            "imported": 42,
            "total": 42,
            "error": None,
            "created_at": "2026-08-10T18:00:00Z",
            "finished_at": "2026-08-10T18:05:00Z",
            "log": [
                {"ts": "18:00:01", "level": "info", "text": "import started"},
                {"ts": "18:00:02", "level": "info", "text": "validate OK"},
                {"ts": "18:00:03", "level": "info", "text": "decompose: 42 sections"},
                {"ts": "18:00:04", "level": "info", "text": "indexing: 42 sections"},
                {"ts": "18:00:10", "level": "info", "text": "git commit: abc123 (42 sections)"},
                {"ts": "18:00:11", "level": "info", "text": "done: 42 sections in 10s"},
            ],
        },
        {
            "import_id": "import-002",
            "name": "book_2.pdf",
            "status": "running",
            "phase": "indexing",
            "imported": 10,
            "total": 50,
            "error": None,
            "log": [
                {"ts": "18:05:01", "level": "info", "text": "import started"},
                {"ts": "18:05:02", "level": "info", "text": "validate OK"},
            ],
        },
    ]

    @app.get("/imports/{import_id}/log")
    async def import_log(import_id: str, request: Request):
        """Mirror main.py implementation."""
        auth = getattr(request.state, "auth", None)
        if auth is None or not getattr(auth, "authenticated", False):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Authentication required")

        import_queue = getattr(request.app.state, "import_queue", None)
        if import_queue:
            for rec in import_queue:
                if rec.get("import_id") == import_id:
                    return {"import_id": import_id, "log": rec.get("log", [])}
        from fastapi import HTTPException
        raise HTTPException(404, f"Import {import_id} not found")

    @app.get("/imports")
    async def list_imports(request: Request):
        """Mirror main.py implementation — log excluded."""
        auth = getattr(request.state, "auth", None)
        if auth is None or not getattr(auth, "authenticated", False):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Authentication required")

        queue = getattr(request.app.state, "import_queue", [])
        sanitized = [
            {k: v for k, v in rec.items() if not k.startswith("_") and k != "log"}
            for rec in queue
        ]
        return sanitized

    return app


@pytest.fixture
def auth_headers():
    """Auth headers с валидным write-ключом."""
    return {"X-API-Key": "write-key-abcdefgh"}


# ═══════════════════════════════════════════════════════════════
# GET /imports/{id}/log — 200
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_import_log_200(log_app, auth_headers):
    """GET /imports/{id}/log → 200 + correct log entries."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports/import-001/log", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["import_id"] == "import-001"
        assert isinstance(data["log"], list)
        assert len(data["log"]) == 6
        # First entry
        assert data["log"][0]["level"] == "info"
        assert data["log"][0]["text"] == "import started"
        # Last entry
        assert data["log"][-1]["text"] == "done: 42 sections in 10s"


@pytest.mark.asyncio
async def test_get_import_log_running_has_log(log_app, auth_headers):
    """GET /imports/{id}/log → running карточка тоже имеет лог."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports/import-002/log", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["log"]) == 2


# ═══════════════════════════════════════════════════════════════
# GET /imports/{id}/log — 404
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_import_log_404_unknown_id(log_app, auth_headers):
    """GET /imports/{id}/log → 404 для неизвестного import_id."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports/nonexistent/log", headers=auth_headers)
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# GET /imports/{id}/log — 401 (no auth)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_import_log_401_no_auth(log_app):
    """GET /imports/{id}/log → 401 без API-ключа."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports/import-001/log")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════
# GET /imports — log NOT present (lean payload)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_imports_excludes_log_field(log_app, auth_headers):
    """GET /imports → ответ НЕ содержит поле 'log' (lean payload)."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        for rec in data:
            assert "log" not in rec, f"'log' field should NOT be in /imports response: {rec.keys()}"
            assert "import_id" in rec


# ═══════════════════════════════════════════════════════════════
# Ring buffer behavior (unit-level)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ring_buffer_truncation_marker():
    """_append_log должен добавлять маркер «log truncated» при переполнении."""
    from mcp_server.tools.content import _append_log, LOG_CAP

    rec: dict = {"log": []}
    # Заполняем буфер до предела
    for i in range(LOG_CAP + 10):
        _append_log(rec, "info", f"line {i:03d}")

    log = rec["log"]
    # Должен быть маркер truncation + LOG_CAP последних строк
    assert len(log) == LOG_CAP + 1, f"Expected {LOG_CAP + 1} entries (marker + {LOG_CAP} lines), got {len(log)}"
    marker = log[0]
    assert marker["level"] == "warning"
    assert "truncated" in marker["text"]
    assert f"{LOG_CAP} entries" in marker["text"]


@pytest.mark.asyncio
async def test_ring_buffer_marker_not_duplicated():
    """Маркер не должен дублироваться при повторном переполнении."""
    from mcp_server.tools.content import _append_log, LOG_CAP

    rec: dict = {"log": []}
    for i in range(LOG_CAP + 10):
        _append_log(rec, "info", f"line {i:03d}")

    # Добавляем ещё 50 строк — новый push должен удалить старые, но маркер один
    for i in range(50):
        _append_log(rec, "info", f"line extra {i:03d}")

    log = rec["log"]
    truncated_count = sum(1 for e in log if e.get("level") == "warning" and "truncated" in e.get("text", ""))
    assert truncated_count == 1, f"Truncation marker should appear exactly once, got {truncated_count}"


@pytest.mark.asyncio
async def test_append_log_text_truncated():
    """_append_log обрезает text до 300 символов."""
    from mcp_server.tools.content import _append_log

    rec: dict = {"log": []}
    long_text = "A" * 500
    _append_log(rec, "info", long_text)

    assert len(rec["log"]) == 1
    assert len(rec["log"][0]["text"]) <= 300


@pytest.mark.asyncio
async def test_append_log_timestamp_format():
    """_append_log добавляет timestamp в формате HH:MM:SS."""
    from mcp_server.tools.content import _append_log

    rec: dict = {"log": []}
    _append_log(rec, "info", "test message")

    ts = rec["log"][0]["ts"]
    # Проверяем формат HH:MM:SS
    parts = ts.split(":")
    assert len(parts) == 3, f"Expected HH:MM:SS format, got '{ts}'"
    assert 0 <= int(parts[0]) <= 23


# ═══════════════════════════════════════════════════════════════
# GET /imports — does NOT include log (defence against amplification)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_imports_with_log_field_not_leaked(log_app, auth_headers):
    """Даже если запись в очереди имеет 'log', GET /imports его не возвращает."""
    transport = ASGITransport(app=log_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/imports", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        for rec in data:
            # Проверяем, что log точно присутствует в исходных данных
            # (лежит в import_queue), но НЕ в ответе
            assert "log" not in rec, (
                f"CRITICAL: 'log' field leaked into GET /imports for {rec.get('import_id')}. "
                f"Keys present: {list(rec.keys())}"
            )
