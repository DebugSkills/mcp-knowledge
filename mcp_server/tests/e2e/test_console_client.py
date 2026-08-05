"""E2E тест S20: kb-console MCPClient против реального MCP сервера.

Импортирует MCPClient из kb-console, подключается через e2e_http_app
(httpx.AsyncClient + ASGITransport in-process), проверяет:
- initialize OK
- tools_list → 16 инструментов
- health → healthy (qdrant/embedding ok)
- tools_call search_knowledge → работает
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Добавляем kb-console src в путь (пакет вне mcp_server)
_KB_CONSOLE_SRC = str(Path(__file__).resolve().parents[3] / "kb-console" / "src")
if _KB_CONSOLE_SRC not in sys.path:
    sys.path.insert(0, _KB_CONSOLE_SRC)

# В контейнере mcp-server пакета kb-console НЕТ (отдельный образ kb-console:prod) —
# там S20 пропускается, а связку консоль↔сервер проверяет smoke-проба :8085 (verify).
# Локально / в dev (kb-console установлен) — тест работает полностью.
pytest.importorskip("kb_console")

# Ключ из фикстуры e2e (conftest.py: MCP_READ_KEYS='["e2e-read-key"]')
E2E_READ_KEY = "e2e-read-key"


@pytest.mark.e2e
class TestMCPClientE2E:
    """E2E S20: kb-console MCPClient против in-process MCP сервера."""

    async def test_initialize_ok(self, e2e_http_app):
        """initialize должен вернуть protocolVersion (без auth)."""
        from kb_console.core.mcp_client import MCPClient

        client = MCPClient(base_url="http://test", client=e2e_http_app)
        try:
            result = await client.initialize()
            assert "protocolVersion" in result, f"Expected protocolVersion, got {result}"
            assert "serverInfo" in result
        finally:
            await client.close()

    async def test_tools_list_16_tools(self, e2e_http_app):
        """tools_list должен вернуть 16 инструментов (с auth)."""
        from kb_console.core.mcp_client import MCPClient

        client = MCPClient(
            base_url="http://test",
            api_key=E2E_READ_KEY,
            client=e2e_http_app,
        )
        try:
            tools = await client.tools_list()
            assert len(tools) == 16, f"Expected 16 tools, got {len(tools)}"
            tool_names = {t["name"] for t in tools}
            assert "search_knowledge" in tool_names
            assert "import_content" in tool_names
            assert "write_knowledge" in tool_names
            assert "reindex" in tool_names
        finally:
            await client.close()

    async def test_tools_call_search_knowledge(self, e2e_http_app):
        """tools_call search_knowledge должен отработать (с auth)."""
        from kb_console.core.mcp_client import MCPClient

        client = MCPClient(
            base_url="http://test",
            api_key=E2E_READ_KEY,
            client=e2e_http_app,
        )
        try:
            await client.initialize()
            result = await client.tools_call(
                "search_knowledge",
                {"query": "test query", "top_k": 3},
            )
            # Может вернуть [] если коллекция пуста
            assert isinstance(result, (list, dict)), f"Unexpected result type: {type(result)}"
        finally:
            await client.close()

    async def test_health_healthy(self, e2e_http_app):
        """/health должен вернуть healthy с рабочими qdrant и embedding."""
        import httpx

        base = "http://test"
        # health эндпоинты без auth (SKIP_PATHS в AuthMiddleware)
        async with httpx.AsyncClient(
            transport=e2e_http_app._transport,
            base_url=base,
            timeout=httpx.Timeout(10.0),
        ) as health_client:
            r = await health_client.get(f"{base}/health")
            assert r.status_code in (200, 503)
            data = r.json()
            assert "status" in data
            checks = data.get("checks", {})

            # Qdrant должен быть ok (реальный localhost:6333)
            qdrant_check = checks.get("qdrant", {})
            assert qdrant_check.get("ok") is True, f"Qdrant check failed: {qdrant_check}"

            # Embedding должен быть ok (реальный Ollama)
            embed_check = checks.get("embedding", {})
            assert embed_check.get("ok") is True, f"Embedding check failed: {embed_check}"
