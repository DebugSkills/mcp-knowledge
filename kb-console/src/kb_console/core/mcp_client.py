"""MCPClient — JSON-RPC 2.0 клиент для MCP Knowledge Server.

Общается с сервером через POST /mcp (классический JSON-RPC, не SSE).
Поддерживает инжектируемый httpx.AsyncClient для E2E-тестов (ASGITransport).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Self

import httpx

logger = logging.getLogger("kb_console.mcp_client")


class MCPClient:
    """Асинхронный JSON-RPC 2.0 клиент для MCP Knowledge Server.

    Args:
        base_url: URL сервера (default: http://localhost:8000).
        api_key: Ключ для заголовка X-API-Key (если пустой — заголовок не слать).
        timeout: Таймаут HTTP-запросов в секундах.
        client: Инжектируемый httpx.AsyncClient (для тестов через ASGITransport).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str = "",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._own_client = False

        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
            self._own_client = True

        self._request_id = 0

    async def close(self) -> None:
        """Закрыть HTTP-клиент (только если создан внутри MCPClient)."""
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Low-level JSON-RPC call ─────────────────────────────

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Выполнить JSON-RPC 2.0 вызов.

        Returns:
            result из ответа.

        Raises:
            RuntimeError: при ошибках соединения, HTTP, или JSON-RPC error.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }

        url = f"{self.base_url}/mcp"

        try:
            response = await self._client.post(url, json=payload, headers=self._headers())
        except httpx.ConnectError as e:
            msg = f"Сервер недоступен: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except httpx.TimeoutException as e:
            msg = f"Таймаут запроса к серверу: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        # HTTP-level errors
        if response.status_code == 401:
            raise RuntimeError("Ошибка аутентификации: неверный API-ключ (401)")
        if response.status_code == 403:
            raise RuntimeError("Доступ запрещён: недостаточно прав (403)")
        if response.status_code == 429:
            raise RuntimeError("Слишком много запросов: превышен rate limit (429)")
        if response.status_code == 503:
            raise RuntimeError("Сервер временно недоступен: degraded (503)")

        if response.status_code != 200:
            raise RuntimeError(
                f"Ошибка HTTP {response.status_code}: {response.text[:200]}"
            )

        # JSON-RPC level
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON response: %s", response.text[:200])
            raise RuntimeError(f"Некорректный JSON-ответ от сервера: {e}") from e

        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            code = err.get("code", -1)
            logger.error("JSON-RPC error code=%s: %s", code, msg)
            raise RuntimeError(f"Ошибка JSON-RPC ({code}): {msg}")

        return data.get("result", {})

    def _unwrap_result(self, result: Any) -> Any:
        """Развернуть MCP content envelope если есть.

        mcp_handler.py:206 оборачивает результат tools/call в:
        {"content": [{"type": "text", "text": json.dumps(result)}]}

        Этот метод извлекает настоящий result, или возвращает как есть
        (backward-compat для initialize/tools_list, где конверта нет).
        """
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if not isinstance(content, list) or len(content) == 0:
            return result
        first = content[0]
        if not isinstance(first, dict) or first.get("type") != "text":
            return result
        text = first.get("text", "")
        if not isinstance(text, str):
            return result
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return result

    # ── High-level MCP methods ─────────────────────────────

    async def initialize(self) -> dict[str, Any]:
        """Выполнить initialize handshake.

        Returns:
            Результат инициализации (protocolVersion, serverInfo, ...).
        """
        return await self._call("initialize")

    async def tools_list(self) -> list[dict[str, Any]]:
        """Получить список всех MCP-инструментов.

        Returns:
            Список инструментов (name, description, inputSchema).
        """
        result = await self._call("tools/list")
        return result.get("tools", [])

    async def tools_call(
        self, name: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Вызвать MCP-инструмент.

        Args:
            name: Имя инструмента (search_knowledge, import_content, ...).
            params: Параметры вызова.

        Returns:
            Результат выполнения инструмента (автоматически разворачивает
            MCP content envelope).
        """
        raw = await self._call("tools/call", {"name": name, "arguments": params or {}})
        return self._unwrap_result(raw)

    async def resources_list(self) -> list[dict[str, Any]]:
        """Получить список MCP-ресурсов."""
        result = await self._call("resources/list")
        return result.get("resources", [])

    async def prompts_list(self) -> list[dict[str, Any]]:
        """Получить список MCP-промптов."""
        result = await self._call("prompts/list")
        return result.get("prompts", [])
