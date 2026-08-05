"""Unit-тесты MCPClient — httpx.MockTransport (без реального сервера)."""

from __future__ import annotations

import json

import httpx
import pytest

from kb_console.core.mcp_client import MCPClient

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def mock_transport():
    """Создаёт httpx.MockTransport с предустановленными ответами."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        method = body.get("method", "")
        rid = body.get("id", 1)

        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "mcp-knowledge-server", "version": "0.1.0"},
                    },
                },
            )
        elif method == "tools/list":
            tools = [{"name": f"tool_{i}", "description": f"Tool {i}"} for i in range(1, 17)]
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}},
            )
        elif method == "tools/call":
            arguments = body.get("params", {}) if isinstance(body.get("params", {}), dict) else {}
            param_name = arguments.get("name", "")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"ok": True, "tool": param_name, "args": arguments.get("arguments", {})},
                },
            )
        elif method == "health/check":
            return httpx.Response(
                200,
                json={"status": "healthy", "version": "0.1.0"},
            )
        else:
            return httpx.Response(404, json={"error": "Unknown method"})

    return httpx.MockTransport(handler)


@pytest.fixture
def error_transport():
    """Транспорт, возвращающий JSON-RPC ошибку."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        rid = body.get("id", 1)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32602, "message": "Invalid params: missing required field 'query'"},
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def http_error_transport():
    """Транспорт с HTTP 503."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"detail": "Service degraded: embedding backend unhealthy"},
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def connect_error_transport():
    """Транспорт, симулирующий ошибку соединения."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    return httpx.MockTransport(handler)


@pytest.fixture
def client(mock_transport):
    """MCPClient с mock-транспортом."""
    c = httpx.AsyncClient(transport=mock_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


@pytest.fixture
def error_client(error_transport):
    """MCPClient с error-транспортом."""
    c = httpx.AsyncClient(transport=error_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


@pytest.fixture
def http503_client(http_error_transport):
    """MCPClient с HTTP 503 транспортом."""
    c = httpx.AsyncClient(transport=http_error_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


@pytest.fixture
def conn_error_client(connect_error_transport):
    """MCPClient с ошибкой соединения."""
    c = httpx.AsyncClient(transport=connect_error_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


# ── Tests: initialize ───────────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_returns_protocol_version(client):
    """initialize должен вернуть protocolVersion."""
    result = await client.initialize()
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "mcp-knowledge-server"


# ── Tests: tools_list ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_list_returns_16_tools(client):
    """tools_list должен вернуть 16 инструментов."""
    tools = await client.tools_list()
    assert len(tools) == 16
    assert tools[0]["name"] == "tool_1"


# ── Tests: tools_call ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_call_passes_params(client):
    """tools_call должен передавать params и возвращать result."""
    result = await client.tools_call("search_knowledge", {"query": "test", "top_k": 5})
    assert result["ok"] is True
    assert result["tool"] == "search_knowledge"
    assert result["args"]["query"] == "test"


@pytest.mark.asyncio
async def test_tools_call_sends_api_key_header():
    """tools_call с api_key должен слать заголовок X-API-Key."""

    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        )

    transport = httpx.MockTransport(handler)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")
    client = MCPClient(base_url="http://test", api_key="secret-key", client=c)

    await client.tools_call("test_tool", {})

    assert "x-api-key" in {k.lower() for k in captured_headers}
    assert captured_headers.get("x-api-key", captured_headers.get("X-API-Key", "")) == "secret-key"


# ── Tests: error handling ───────────────────────────────────


@pytest.mark.asyncio
async def test_jsonrpc_error_returns_message(error_client):
    """JSON-RPC error должен возвращать сообщение из ответа."""
    with pytest.raises(RuntimeError, match="Invalid params"):
        await error_client.tools_call("test", {})


@pytest.mark.asyncio
async def test_http_503_returns_readable_message(http503_client):
    """HTTP 503 должен содержать понятное сообщение о деградации."""
    with pytest.raises(RuntimeError, match="Сервер временно недоступен"):
        await http503_client.initialize()


@pytest.mark.asyncio
async def test_connect_error_returns_russian_message(conn_error_client):
    """ConnectError должен возвращать сообщение на русском."""
    with pytest.raises(RuntimeError, match="Сервер недоступен"):
        await conn_error_client.initialize()
