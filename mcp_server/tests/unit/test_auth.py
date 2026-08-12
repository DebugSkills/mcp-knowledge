"""Unit tests for MCP Auth — multi-key authentication, constant-time comparison.

Covers:
- authenticate_key: write/read/none levels
- check_tool_permission: read/write scope enforcement
- mask_key: key masking for logs
- _constant_time_compare: hmac.compare_digest usage
- AuthMiddleware: skip paths, missing/valid keys
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from mcp_server.auth import (
    AuthInfo,
    AuthMiddleware,
    _constant_time_compare,
    authenticate_key,
    check_tool_permission,
    get_auth,
    mask_key,
)

# ── monkeypatched settings ────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch: pytest.MonkeyPatch):
    """Inject test keys into settings."""
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_READ_KEYS",
        ["read-key-12345678", "read-key-secondary"],
    )
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_WRITE_KEYS",
        ["write-key-abcdefgh"],
    )
    monkeypatch.setattr(
        "mcp_server.auth.settings.MCP_IMPORT_KEYS",
        ["import-key-12345678"],
    )


# ── mask_key ──────────────────────────────────────────────────


class TestMaskKey:
    def test_normal_key(self):
        result = mask_key("my-secret-api-key-42")
        assert result.startswith("my-s")
        assert "..." in result

    def test_too_short_key(self):
        assert mask_key("short") == "[too-short]"

    def test_exact_boundary(self):
        result = mask_key("12345678")
        assert result != "[too-short]"


# ── _constant_time_compare ────────────────────────────────────


class TestConstantTimeCompare:
    def test_match(self):
        assert _constant_time_compare("hello", "hello") is True

    def test_mismatch(self):
        assert _constant_time_compare("hello", "world") is False


# ── authenticate_key ───────────────────────────────────────────


class TestAuthenticateKey:
    def test_write_key_match(self):
        result = authenticate_key("write-key-abcdefgh")
        assert result.authenticated is True
        assert result.key_level == "write"
        assert len(result.key_hash) == 16

    def test_read_key_match(self):
        result = authenticate_key("read-key-12345678")
        assert result.authenticated is True
        assert result.key_level == "read"

    def test_secondary_read_key(self):
        result = authenticate_key("read-key-secondary")
        assert result.authenticated is True
        assert result.key_level == "read"

    def test_import_key_match(self):
        result = authenticate_key("import-key-12345678")
        assert result.authenticated is True
        assert result.key_level == "import"

    def test_unknown_key(self):
        result = authenticate_key("totally-invalid-key")
        assert result.authenticated is False
        assert result.key_level == "none"
        assert result.key_hash == ""

    def test_empty_key(self):
        result = authenticate_key("")
        assert result.authenticated is False


# ── check_tool_permission ──────────────────────────────────────


class TestCheckToolPermission:
    def test_write_key_grants_all(self):
        auth = AuthInfo(authenticated=True, key_level="write")
        check_tool_permission(auth, "write_knowledge")
        check_tool_permission(auth, "search_knowledge")
        check_tool_permission(auth, "reindex")

    def test_read_key_grants_read_tools(self):
        auth = AuthInfo(authenticated=True, key_level="read")
        check_tool_permission(auth, "search_knowledge")
        check_tool_permission(auth, "get_knowledge_map")
        check_tool_permission(auth, "list_domains")

    def test_read_key_blocks_write_tools(self):
        auth = AuthInfo(authenticated=True, key_level="read")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "write_knowledge")
        assert exc.value.status_code == 403

    def test_read_key_blocks_reindex(self):
        auth = AuthInfo(authenticated=True, key_level="read")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "reindex")
        assert exc.value.status_code == 403

    def test_import_key_grants_read_and_import_tools(self):
        auth = AuthInfo(authenticated=True, key_level="import")
        check_tool_permission(auth, "search_knowledge")
        check_tool_permission(auth, "list_domains")
        check_tool_permission(auth, "analyze_content")
        check_tool_permission(auth, "import_content")

    def test_import_key_blocks_delete_and_write(self):
        auth = AuthInfo(authenticated=True, key_level="import")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "delete_entry")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "write_knowledge")
        assert exc.value.status_code == 403

    def test_unauthenticated_raises_401(self):
        auth = AuthInfo(authenticated=False, key_level="none")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "search_knowledge")
        assert exc.value.status_code == 401

    def test_initialize_bypasses_auth(self):
        auth = AuthInfo(authenticated=False, key_level="none")
        check_tool_permission(auth, "initialize")
        check_tool_permission(auth, "ping")


# ── Helpers for ASGI scope mocking ────────────────────────────

def _asgi_scope(
    path: str = "/mcp",
    method: str = "POST",
    headers: dict | None = None,
) -> dict:
    """Построить ASGI scope словарь (как приходит от uvicorn)."""
    raw_headers: list[tuple[bytes, bytes]] = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": raw_headers,
        "query_string": b"",
    }


def _asgi_receive(body_bytes: bytes = b'{"jsonrpc":"2.0","method":"tools/list","id":1}') -> callable:
    """Создать ASGI receive функцию с заданным body."""
    _chunks: list[bytes] = [body_bytes]
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _asgi_send_collector() -> tuple[callable, list[dict]]:
    """Создать ASGI send функцию, собирающую все сообщения."""
    messages: list[dict] = []

    async def send(message: dict):
        messages.append(message)

    return send, messages


# ── AuthMiddleware (pure ASGI) ──────────────────────────────────


class TestAuthMiddleware:
    @pytest.fixture
    def middleware(self):
        return AuthMiddleware(app=MagicMock())

    async def test_skip_health(self, middleware: AuthMiddleware):
        for path in ["/health", "/metrics", "/docs", "/openapi.json"]:
            scope = _asgi_scope(path=path, method="GET")
            receive = _asgi_receive()
            send, _messages = _asgi_send_collector()
            # Устанавливаем мок-обработчик на ASGI app,
            # который возвращает 200 через send
            async def mock_app(s, r, snd):
                await snd({"type": "http.response.start", "status": 200, "headers": []})
                await snd({"type": "http.response.body", "body": b"ok"})

            middleware.app = mock_app
            await middleware(scope, receive, send)
            # Проверяем: scope["state"]["auth"] установлен как AuthInfo()
            assert scope["state"]["auth"].authenticated is False
            assert scope["state"]["auth"].key_level == "none"

    async def test_skip_health_trailing_slash(self, middleware: AuthMiddleware):
        scope = _asgi_scope(path="/health/", method="GET")
        receive = _asgi_receive()
        send, _messages = _asgi_send_collector()

        async def mock_app(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

        middleware.app = mock_app
        await middleware(scope, receive, send)
        assert scope["state"]["auth"].authenticated is False

    async def test_get_non_mcp_bypasses_auth(self, middleware: AuthMiddleware):
        scope = _asgi_scope(path="/some-page", method="GET")
        receive = _asgi_receive()
        send, _messages = _asgi_send_collector()

        async def mock_app(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

        middleware.app = mock_app
        await middleware(scope, receive, send)
        # GET без ключа → unauthenticated
        assert scope["state"]["auth"].authenticated is False

    async def test_post_mcp_without_key_sets_unauthenticated(self, middleware: AuthMiddleware):
        scope = _asgi_scope(path="/mcp", method="POST")
        receive = _asgi_receive()
        send, _messages = _asgi_send_collector()

        async def mock_app(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

        middleware.app = mock_app
        await middleware(scope, receive, send)
        auth = scope.get("state", {}).get("auth")
        assert auth is not None
        assert auth.authenticated is False

    async def test_post_mcp_with_valid_key(self, middleware: AuthMiddleware):
        scope = _asgi_scope(
            path="/mcp", method="POST",
            headers={"X-API-Key": "write-key-abcdefgh"},
        )
        receive = _asgi_receive()
        send, _messages = _asgi_send_collector()

        async def mock_app(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

        middleware.app = mock_app
        # Мок rate limiter (не включён — rate_limiter=None)
        # При отсутствии rate_limiter запрос проходит
        await middleware(scope, receive, send)
        auth = scope.get("state", {}).get("auth")
        assert auth.authenticated is True
        assert auth.key_level == "write"

    async def test_post_mcp_with_invalid_key(self, middleware: AuthMiddleware):
        scope = _asgi_scope(
            path="/mcp", method="POST",
            headers={"X-API-Key": "not-a-real-key"},
        )
        receive = _asgi_receive()
        send, _messages = _asgi_send_collector()

        async def mock_app(s, r, snd):
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": b"ok"})

        middleware.app = mock_app
        await middleware(scope, receive, send)
        auth = scope.get("state", {}).get("auth")
        assert auth.authenticated is False


# ── get_auth ───────────────────────────────────────────────────


class TestGetAuth:
    def test_returns_auth_from_state(self):
        req = MagicMock(spec=Request)
        req.state.auth = AuthInfo(authenticated=True, key_level="write", key_hash="abc123")
        result = get_auth(req)
        assert result.authenticated is True
        assert result.key_level == "write"

    def test_returns_default_for_missing_state(self):
        req = MagicMock(spec=Request)
        del req.state.auth
        result = get_auth(req)
        assert isinstance(result, AuthInfo)
        assert result.authenticated is False
