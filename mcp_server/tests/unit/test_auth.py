"""Unit tests for MCP Auth — multi-key authentication, constant-time comparison.

Covers:
- authenticate_key: write/read/none levels
- check_tool_permission: read/write scope enforcement
- mask_key: key masking for logs
- _constant_time_compare: hmac.compare_digest usage
- AuthMiddleware: skip paths, missing/valid keys
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.responses import Response

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

    def test_unauthenticated_raises_401(self):
        auth = AuthInfo(authenticated=False, key_level="none")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(auth, "search_knowledge")
        assert exc.value.status_code == 401

    def test_initialize_bypasses_auth(self):
        auth = AuthInfo(authenticated=False, key_level="none")
        check_tool_permission(auth, "initialize")
        check_tool_permission(auth, "ping")


# ── Helpers for Request mocking ────────────────────────────────

def _mock_request(
    path: str = "/mcp",
    method: str = "POST",
    headers: dict | None = None,
    body_bytes: bytes = b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
) -> MagicMock:
    """Build a properly mocked FastAPI Request with state."""
    req = MagicMock(spec=Request)
    req.url.path = path
    req.method = method
    req.state.auth = AuthInfo()
    # Headers: convert dict to case-insensitive lookup
    req.headers = MagicMock()
    req.headers.get = lambda key, default="": (headers or {}).get(key, default)
    # Body: async callable returning bytes (required by _check_rate_limit E2)
    req.body = AsyncMock(return_value=body_bytes)
    # app.state — rate limiter mock (optional)
    req.app = MagicMock()
    req.app.state.rate_limiter = None  # отключён по умолчанию
    req.app.state.rate_limiter_read = None
    req.app.state.rate_limiter_write = None
    return req


# ── AuthMiddleware ──────────────────────────────────────────────


class TestAuthMiddleware:
    @pytest.fixture
    def middleware(self):
        return AuthMiddleware(app=MagicMock())

    async def test_skip_health(self, middleware: AuthMiddleware):
        for path in ["/health", "/metrics", "/docs", "/openapi.json"]:
            req = _mock_request(path=path, method="GET")
            async def call_next(r): return Response(content="ok")
            resp = await middleware.dispatch(req, call_next)
            assert resp.status_code == 200

    async def test_skip_health_trailing_slash(self, middleware: AuthMiddleware):
        req = _mock_request(path="/health/", method="GET")
        async def call_next(r): return Response(content="ok")
        resp = await middleware.dispatch(req, call_next)
        assert resp.status_code == 200

    async def test_get_non_mcp_bypasses_auth(self, middleware: AuthMiddleware):
        req = _mock_request(path="/some-page", method="GET")
        async def call_next(r): return Response(content="ok")
        resp = await middleware.dispatch(req, call_next)
        assert resp.status_code == 200

    async def test_post_mcp_without_key_sets_unauthenticated(self, middleware: AuthMiddleware):
        req = _mock_request(path="/mcp", method="POST", headers={})
        async def call_next(r): return Response(content="ok")
        resp = await middleware.dispatch(req, call_next)
        assert resp.status_code == 200
        auth = get_auth(req)
        assert auth.authenticated is False

    async def test_post_mcp_with_valid_key(self, middleware: AuthMiddleware):
        req = _mock_request(
            path="/mcp", method="POST",
            headers={"X-API-Key": "write-key-abcdefgh"},
        )
        async def call_next(r): return Response(content="ok")
        resp = await middleware.dispatch(req, call_next)
        assert resp.status_code == 200
        auth = get_auth(req)
        assert auth.authenticated is True
        assert auth.key_level == "write"

    async def test_post_mcp_with_invalid_key(self, middleware: AuthMiddleware):
        req = _mock_request(
            path="/mcp", method="POST",
            headers={"X-API-Key": "not-a-real-key"},
        )
        async def call_next(r): return Response(content="ok")
        await middleware.dispatch(req, call_next)
        auth = get_auth(req)
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
