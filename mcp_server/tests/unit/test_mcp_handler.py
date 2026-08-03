"""Unit tests for MCP JSON-RPC 2.0 handler.

Covers:
- JSON-RPC envelope validation (jsonrpc, method, id)
- Dispatch table routing (+ unknown method)
- Error codes (−32700 parse, −32600 invalid, −32601 not found, −32602 params)
- Batch request handling
- Size limit enforcement
- initialize/tools_list handshake
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from mcp_server.mcp_handler import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    MCP_REQUEST_TOO_LARGE,
    MCP_TOOL_NOT_FOUND,
    MAX_REQUEST_SIZE,
    METHOD_DISPATCH,
    SERVER_PROTOCOL_VERSION,
    _dispatch_single,
    _handle_initialize,
    _handle_tools_call,
    _handle_tools_list,
    _jsonrpc_error,
    _jsonrpc_result,
    _validate_jsonrpc,
    handle_mcp_request,
)


# ── Helpers ────────────────────────────────────────────────────


def _make_mock_request(body_bytes: bytes, content_length: str = "0") -> MagicMock:
    """Build a mock Request for handle_mcp_request tests.
    
    handle_mcp_request uses: request.headers['content-length'], request.body().
    """
    if content_length == "0":
        content_length = str(len(body_bytes))
    
    req = MagicMock(spec=Request)
    req.headers = {"content-length": content_length}
    req.body = AsyncMock(return_value=body_bytes)
    req.state.auth = MagicMock(authenticated=True, key_level="write", key_hash="abc")
    return req


def _make_mock_request_for_dispatch() -> MagicMock:
    """Build a mock Request for _dispatch_single / handler tests."""
    req = MagicMock(spec=Request)
    req.state.auth = MagicMock(authenticated=True, key_level="write", key_hash="abc")
    req.app.state = MagicMock()
    return req


# ── Helpers: _jsonrpc_error / _jsonrpc_result ──────────────────


class TestJsonRpcEnvelope:
    def test_error_format(self):
        err = _jsonrpc_error(-32600, "Invalid Request", id=42, data={"detail": "x"})
        assert err["jsonrpc"] == "2.0"
        assert err["error"]["code"] == -32600
        assert err["error"]["message"] == "Invalid Request"
        assert err["error"]["data"] == {"detail": "x"}
        assert err["id"] == 42

    def test_result_format(self):
        res = _jsonrpc_result({"tools": []}, id=1)
        assert res["jsonrpc"] == "2.0"
        assert res["result"] == {"tools": []}
        assert res["id"] == 1


# ── _validate_jsonrpc ──────────────────────────────────────────


class TestValidateJsonRpc:
    def test_valid_request_passes(self):
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        assert _validate_jsonrpc(body) is None

    def test_missing_jsonrpc_key(self):
        body = {"method": "tools/list", "id": 1}
        err = _validate_jsonrpc(body)
        assert err is not None
        assert err["error"]["code"] == JSONRPC_INVALID_REQUEST

    def test_wrong_jsonrpc_version(self):
        body = {"jsonrpc": "1.0", "method": "tools/list", "id": 1}
        err = _validate_jsonrpc(body)
        assert err is not None
        assert "jsonrpc must be '2.0'" in err["error"]["message"]

    def test_missing_method(self):
        body = {"jsonrpc": "2.0", "id": 1}
        err = _validate_jsonrpc(body)
        assert err is not None

    def test_missing_id(self):
        body = {"jsonrpc": "2.0", "method": "tools/list"}
        err = _validate_jsonrpc(body)
        assert err is not None
        assert "missing 'id'" in err["error"]["message"]


# ── Handler tests ──────────────────────────────────────────────


class TestHandlers:
    """Tests for method handlers that don't need real app.state."""

    async def test_initialize_handshake(self):
        request = _make_mock_request_for_dispatch()
        result = await _handle_initialize(
            {"protocolVersion": "2024-11-05", "clientInfo": {"name": "test-client"}},
            request_id=1, _request=request,
        )
        assert result["jsonrpc"] == "2.0"
        assert result["result"]["protocolVersion"] == SERVER_PROTOCOL_VERSION
        assert result["result"]["serverInfo"]["name"] == "mcp-knowledge-server"
        assert "tools" in result["result"]["capabilities"]

    async def test_tools_list_returns_all_tools(self):
        result = await _handle_tools_list({}, request_id=1, _request=_make_mock_request_for_dispatch())
        assert "tools" in result["result"]
        tool_names = [t["name"] for t in result["result"]["tools"]]
        assert "search_knowledge" in tool_names
        assert "write_knowledge" in tool_names
        assert "list_subjects" in tool_names  # G1-fix
        assert "list_projects" in tool_names  # G1-fix


class TestToolsCall:
    async def test_missing_tool_name(self):
        req = _make_mock_request_for_dispatch()
        result = await _handle_tools_call(
            {"name": "", "arguments": {}}, request_id=1, request=req,
        )
        assert result["error"]["code"] == JSONRPC_INVALID_PARAMS

    async def test_unknown_tool(self):
        req = _make_mock_request_for_dispatch()
        result = await _handle_tools_call(
            {"name": "nonexistent_tool", "arguments": {}}, request_id=1, request=req,
        )
        assert result["error"]["code"] == MCP_TOOL_NOT_FOUND


# ── _dispatch_single ───────────────────────────────────────────


class TestDispatchSingle:
    async def test_unknown_method(self):
        body = {"jsonrpc": "2.0", "method": "unknown.method", "id": 1}
        result = await _dispatch_single(body, _make_mock_request_for_dispatch())
        assert result["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        assert "Available:" in result["error"]["message"]

    async def test_initialize_dispatches(self):
        body = {"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 2}
        result = await _dispatch_single(body, _make_mock_request_for_dispatch())
        assert result["result"]["protocolVersion"] == SERVER_PROTOCOL_VERSION

    async def test_invalid_body_fails_validation(self):
        body = {"method": "tools/list"}  # missing jsonrpc, id
        result = await _dispatch_single(body, _make_mock_request_for_dispatch())
        assert "error" in result


# ── handle_mcp_request ─────────────────────────────────────────


class TestHandleMcpRequest:
    async def test_valid_single_request(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1})
        req = _make_mock_request(body.encode())
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["jsonrpc"] == "2.0"
        assert data["result"]["protocolVersion"] == SERVER_PROTOCOL_VERSION

    async def test_batch_request(self):
        body = json.dumps([
            {"jsonrpc": "2.0", "method": "initialize", "id": 1},
            {"jsonrpc": "2.0", "method": "tools/list", "id": 2},
        ])
        req = _make_mock_request(body.encode())
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["result"]["protocolVersion"] == SERVER_PROTOCOL_VERSION
        assert "tools" in data[1]["result"]

    async def test_empty_batch(self):
        body = json.dumps([])
        req = _make_mock_request(body.encode())
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["error"]["code"] == JSONRPC_INVALID_REQUEST

    async def test_batch_item_not_dict(self):
        body = json.dumps(["not-a-dict", {"jsonrpc": "2.0", "method": "initialize", "id": 1}])
        req = _make_mock_request(body.encode())
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert isinstance(data, list)
        assert "error" in data[0]
        assert "result" in data[1]

    async def test_parse_error(self):
        req = _make_mock_request(b"not json at all")
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["error"]["code"] == JSONRPC_PARSE_ERROR

    async def test_size_limit_content_length(self):
        body = b"x" * 100
        req = _make_mock_request(body, content_length=str(MAX_REQUEST_SIZE + 1))
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["error"]["code"] == MCP_REQUEST_TOO_LARGE

    async def test_size_limit_body_length(self):
        body = b"x" * (MAX_REQUEST_SIZE + 100)
        req = _make_mock_request(body)
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["error"]["code"] == MCP_REQUEST_TOO_LARGE

    async def test_non_dict_non_list(self):
        req = _make_mock_request(json.dumps("just a string").encode())
        resp = await handle_mcp_request(req)
        data = json.loads(resp.body.decode())
        assert data["error"]["code"] == JSONRPC_INVALID_REQUEST


# ── Dispatch table completeness ────────────────────────────────


class TestDispatchTable:
    def test_all_required_methods(self):
        required = {
            "initialize", "tools/list", "tools/call",
            "resources/list", "resources/read",
            "prompts/list", "prompts/get",
        }
        registered = set(METHOD_DISPATCH.keys())
        assert required.issubset(registered)
