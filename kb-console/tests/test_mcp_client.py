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
            tools = [{"name": f"tool_{i}", "description": f"Tool {i}"} for i in range(1, 20)]
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}},
            )
        elif method == "tools/call":
            arguments = body.get("params", {}) if isinstance(body.get("params", {}), dict) else {}
            param_name = arguments.get("name", "")
            # Реалистичный MCP content envelope (как в mcp_handler.py:206)
            inner_result = {"ok": True, "tool": param_name, "args": arguments.get("arguments", {})}
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(inner_result)}],
                    },
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
async def test_tools_list_returns_17_tools(client):
    """tools_list должен вернуть 17 инструментов (после добавления analyze_content)."""
    tools = await client.tools_list()
    assert len(tools) == 19
    assert tools[0]["name"] == "tool_1"


# ── Tests: tools_call ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_call_passes_params(client):
    """tools_call должен передавать params и возвращать unwrapped result."""
    result = await client.tools_call("search_knowledge", {"query": "test", "top_k": 5})
    # После unwrap в _unwrap_result() — плоский dict, не envelope
    assert result["ok"] is True
    assert result["tool"] == "search_knowledge"
    assert result["args"]["query"] == "test"


@pytest.mark.asyncio
async def test_tools_call_unwraps_envelope(client):
    """tools_call должен анрапнуть MCP content envelope и вернуть реальные данные."""
    # Mock handler возвращает конверт {"content": [{"type": "text", "text": json.dumps({...})}]}
    # После unwrap → плоский dict БЕЗ envelope-обёртки
    result = await client.tools_call("import_content", {"content": "test", "domain": "d", "subject": "s"})
    assert isinstance(result, dict)
    # Проверяем, что НЕ конверт (нет ключа "content" с list[dict])
    if "content" in result:
        content_val = result["content"]
        assert not (isinstance(content_val, list) and len(content_val) > 0
                     and isinstance(content_val[0], dict) and content_val[0].get("type") == "text"), \
            "Result should NOT contain MCP envelope after unwrap"
    # Результат — плоский dict (mock возвращает ok/tool/args)
    assert result.get("ok") is True or result.get("tool") is not None


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


# ── Tests: get_progress (Фаза 13.9) ───────────────────────────


@pytest.fixture
def progress_transport():
    """Транспорт с поддержкой GET /imports/{id}/progress."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/imports/" in str(request.url) and "/progress" in str(request.url):
            import_id = str(request.url).split("/imports/")[1].split("/progress")[0]
            if import_id == "unknown":
                return httpx.Response(404, json={"detail": "unknown import_id"})
            return httpx.Response(
                200,
                json={
                    "import_id": import_id,
                    "status": "running",
                    "phase": "writing",
                    "imported": 52,
                    "total": 100,
                    "failed": 1,
                    "messages": [
                        {"t": "22:34:05", "level": "info", "text": "import_content: 50/100 sections written, git commit"},
                    ],
                    "started_at": "2026-08-06T22:30:00+00:00",
                    "updated_at": "2026-08-06T22:34:05+00:00",
                },
            )
        # Fallback to mock transport behavior for /mcp
        body = json.loads(request.content) if request.content else {}
        method = body.get("method", "")
        rid = body.get("id", 1)
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    return httpx.MockTransport(handler)


@pytest.fixture
def progress_client(progress_transport):
    """MCPClient с progress-транспортом."""
    c = httpx.AsyncClient(transport=progress_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


@pytest.mark.asyncio
async def test_get_progress_parses_json(progress_client):
    """get_progress возвращает распарсенный JSON-снапшот."""
    snap = await progress_client.get_progress("test-id")
    assert snap is not None
    assert snap["import_id"] == "test-id"
    assert snap["status"] == "running"
    assert snap["imported"] == 52
    assert snap["total"] == 100
    assert snap["failed"] == 1
    assert len(snap["messages"]) == 1


@pytest.mark.asyncio
async def test_get_progress_returns_none_on_404(progress_client):
    """get_progress возвращает None при 404 (неизвестный import_id)."""
    snap = await progress_client.get_progress("unknown")
    assert snap is None


@pytest.mark.asyncio
async def test_get_progress_sends_api_key(progress_transport):
    """get_progress с api_key шлёт заголовок X-API-Key."""

    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"import_id": "x", "status": "done", "imported": 1, "total": 1, "failed": 0, "messages": [], "started_at": "", "updated_at": ""})

    transport = httpx.MockTransport(handler)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")
    client = MCPClient(base_url="http://test", api_key="key-123", client=c)
    await client.get_progress("x")
    assert "x-api-key" in {k.lower() for k in captured_headers}


# ── Tests: helper methods (Variant A: Surface & Enrich) ───────


@pytest.fixture
def enriched_transport():
    """Транспорт с реалистичными ответами для get_entry, search_knowledge, list_collections."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        method = body.get("method", "")
        rid = body.get("id", 1)

        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})

            if tool_name == "get_entry":
                kid = args.get("knowledge_id", "")
                inner = {
                    "knowledge_id": kid,
                    "domain": "engineering",
                    "subject": "testing",
                    "title": "Test Book",
                    "content_type": "collection",
                    "parent_knowledge_id": None,
                    "sequence_number": None,
                    "children": [
                        {"knowledge_id": "eng-test-ch01", "title": "Chapter 1", "sequence_number": 1},
                    ],
                    "content": "# Test Book\n\nContent.",
                }
            elif tool_name == "search_knowledge":
                inner = {
                    "query": args.get("query", ""),
                    "results": [
                        {
                            "knowledge_id": "eng-test-ch01",
                            "chunk_id": "chunk-1",
                            "content": "Test content",
                            "score": 0.95,
                            "section_header": "# Chapter 1",
                            "domain": "engineering",
                            "subject": "testing",
                            "tags": ["test"],
                            "title": "Chapter 1",
                            "parent_knowledge_id": "eng-test-collection",
                            "content_type": "book",
                        },
                    ],
                    "total": 1,
                }
            elif tool_name == "list_collections":
                inner = {
                    "results": [
                        {
                            "collection_id": "eng-test-book-collection",
                            "title": "Test Book",
                            "domain": "engineering",
                            "subject": "testing",
                            "project": "test-project",
                            "tags": ["test"],
                            "section_count": 2,
                            "updated_at": "2026-01-01T00:00:00+00:00",
                        },
                    ],
                    "next_cursor": None,
                    "total": 1,
                }
            else:
                inner = {"ok": True}

            wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    return httpx.MockTransport(handler)


@pytest.fixture
def enriched_client(enriched_transport):
    """MCPClient с enriched-транспортом (реалистичные ответы)."""
    c = httpx.AsyncClient(transport=enriched_transport, base_url="http://test")
    return MCPClient(base_url="http://test", client=c)


@pytest.mark.asyncio
async def test_get_entry_helper_unwraps(enriched_client):
    """get_entry helper возвращает распарсенный dict."""
    result = await enriched_client.get_entry("eng-test-book-collection")
    assert isinstance(result, dict)
    assert result["knowledge_id"] == "eng-test-book-collection"
    assert result["title"] == "Test Book"
    assert result["content_type"] == "collection"
    assert len(result["children"]) == 1


@pytest.mark.asyncio
async def test_search_knowledge_helper_unwraps(enriched_client):
    """search_knowledge helper возвращает results list."""
    result = await enriched_client.search_knowledge("test query", top_k=3)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["title"] == "Chapter 1"
    assert result[0]["parent_knowledge_id"] == "eng-test-collection"


@pytest.mark.asyncio
async def test_list_collections_helper_unwraps(enriched_client):
    """list_collections helper возвращает results list."""
    result = await enriched_client.list_collections(domain="engineering")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["collection_id"] == "eng-test-book-collection"
    assert result[0]["section_count"] == 2
