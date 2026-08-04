"""B2: MCP JSON-RPC 2.0 handler — POST /mcp endpoint.

MCP Protocol Spec (P1-1):
- Protocol version: 2024-11-05
- initialize → handshake с capabilities
- tools/list → все 9 tools с JSON Schema
- tools/call → валидация params → вызов handler
- Error codes: −32700, −32600, −32601, −32602, −32000..−32099
- Request body ≤ 1 MB
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import check_tool_permission, get_auth
from .prompts import PROMPTS, get_prompt
from .resources import RESOURCES, get_kb_resource
from .tools import TOOL_HANDLERS, TOOLS

logger = logging.getLogger("mcp_knowledge.mcp")

# ── MCP Protocol constants ─────────────────────────────────

SERVER_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mcp-knowledge-server"
SERVER_VERSION = "0.1.0"
MAX_REQUEST_SIZE = 1_048_576  # 1 MB

# ── JSON-RPC 2.0 Error codes ──────────────────────────────

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# MCP-specific server errors (−32000..−32099)
MCP_TOOL_NOT_FOUND = -32001
MCP_AUTH_FAILED = -32002
MCP_RATE_LIMITED = -32003
MCP_REQUEST_TOO_LARGE = -32004
MCP_CONFLICT = -32005  # F2: optimistic locking version conflict (HTTP 409)

# ── Helpers ────────────────────────────────────────────────


def _jsonrpc_error(code: int, message: str, id: Any = None, data: Any = None) -> dict:
    """Сформировать JSON-RPC 2.0 error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    resp: dict[str, Any] = {"jsonrpc": "2.0", "error": error, "id": id}
    return resp


def _jsonrpc_result(result: Any, id: Any) -> dict:
    """Сформировать JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "result": result, "id": id}


def _validate_jsonrpc(request_body: dict) -> dict | None:
    """Проверить обязательные поля JSON-RPC 2.0: jsonrpc, method, id.

    Returns error dict если валидация не пройдена, None если OK.
    """
    if request_body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(
            JSONRPC_INVALID_REQUEST,
            "Invalid Request: jsonrpc must be '2.0'",
            request_body.get("id"),
        )
    if "method" not in request_body:
        return _jsonrpc_error(
            JSONRPC_INVALID_REQUEST,
            "Invalid Request: missing 'method'",
            request_body.get("id"),
        )
    # id: string, number, null (notification) — допустимы
    # отсутствие id — ошибка (кроме batch где id обязателен по спеке)
    if "id" not in request_body:
        return _jsonrpc_error(
            JSONRPC_INVALID_REQUEST,
            "Invalid Request: missing 'id'",
            None,
        )
    return None


# ── Method handlers ────────────────────────────────────────


async def _handle_initialize(params: dict, request_id: Any, _request: Request) -> dict:
    """MCP initialize: handshake с protocol version и capabilities."""
    client_version = params.get("protocolVersion", "unknown")
    client_info = params.get("clientInfo", {})
    logger.info(
        "MCP initialize: client=%s v%s (capabilities=%s)",
        client_info.get("name", "unknown"),
        client_version,
        list(params.get("capabilities", {}).keys()) if isinstance(params.get("capabilities"), dict) else "none",
    )
    return _jsonrpc_result(
        {
            "protocolVersion": SERVER_PROTOCOL_VERSION,
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
        },
        request_id,
    )


async def _handle_tools_list(_params: dict, request_id: Any, _request: Request) -> dict:
    """tools/list: возврат всех 9 tools с JSON Schema."""
    return _jsonrpc_result({"tools": TOOLS}, request_id)


async def _handle_tools_call(params: dict, request_id: Any, request: Request) -> dict:
    """tools/call: валидация params → вызов handler с проверкой прав."""
    tool_name = params.get("name", "")
    tool_args = params.get("arguments", {})

    if not tool_name:
        return _jsonrpc_error(
            JSONRPC_INVALID_PARAMS,
            "Missing tool name",
            request_id,
        )

    # Проверка прав доступа
    auth_info = get_auth(request)
    try:
        check_tool_permission(auth_info, tool_name)
    except Exception as e:
        status_code = getattr(e, "status_code", 500)
        if status_code == 401:
            return _jsonrpc_error(
                MCP_AUTH_FAILED,
                f"Authentication failed: {e.detail}",
                request_id,
            )
        elif status_code == 403:
            return _jsonrpc_error(
                MCP_AUTH_FAILED,
                f"Forbidden: {e.detail}",
                request_id,
            )
        raise

    # Поиск handler'а
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return _jsonrpc_error(
            MCP_TOOL_NOT_FOUND,
            f"Tool not found: '{tool_name}'. Available: {sorted(TOOL_HANDLERS.keys())}",
            request_id,
        )

    # Вызов handler'а
    try:
        start = time.monotonic()
        app_state = request.app.state
        result = await handler(tool_args, app_state)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Tool '%s' completed in %.1f ms (key_hash=%s)",
            tool_name,
            elapsed_ms,
            auth_info.key_hash or "none",
        )

        # F2: Optimistic locking conflict detection
        if isinstance(result, dict) and result.get("conflict"):
            return _jsonrpc_error(
                MCP_CONFLICT,
                result.get("message", "Version conflict"),
                request_id,
                data={
                    "expected_version": result.get("expected_version"),
                    "current_version": result.get("current_version"),
                },
            )

        return _jsonrpc_result({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}, request_id)
    except Exception as exc:
        logger.exception("Tool '%s' failed", tool_name)
        return _jsonrpc_error(
            JSONRPC_INTERNAL_ERROR,
            f"Tool execution failed: {exc}",
            request_id,
        )


async def _handle_resources_list(_params: dict, request_id: Any, _request: Request) -> dict:
    """resources/list: список всех ресурсов kb://."""
    return _jsonrpc_result({"resources": RESOURCES}, request_id)


async def _handle_resources_read(params: dict, request_id: Any, request: Request) -> dict:
    """resources/read: чтение kb:// ресурса (kb://, kb://{domain}, kb://{domain}/{subject})."""
    uri = params.get("uri", "")
    if not uri:
        return _jsonrpc_error(
            JSONRPC_INVALID_PARAMS,
            "Missing 'uri' parameter for resources/read",
            request_id,
        )
    try:
        app_state = request.app.state
        contents = await get_kb_resource(uri, app_state)
        return _jsonrpc_result({"contents": [{"uri": uri, "text": json.dumps(contents, ensure_ascii=False), "mimeType": "application/json"}]}, request_id)
    except Exception as exc:
        logger.exception("resources/read failed for uri=%s", uri)
        return _jsonrpc_error(
            JSONRPC_INTERNAL_ERROR,
            f"Failed to read resource: {exc}",
            request_id,
        )


async def _handle_prompts_list(_params: dict, request_id: Any, _request: Request) -> dict:
    """prompts/list: список всех доступных промптов."""
    return _jsonrpc_result({"prompts": PROMPTS}, request_id)


async def _handle_prompts_get(params: dict, request_id: Any, _request: Request) -> dict:
    """prompts/get: получение содержимого промпта по имени."""
    name = params.get("name", "")
    if not name:
        return _jsonrpc_error(
            JSONRPC_INVALID_PARAMS,
            "Missing 'name' parameter for prompts/get",
            request_id,
        )
    prompt_data = get_prompt(name)
    if prompt_data is None:
        return _jsonrpc_error(
            JSONRPC_INVALID_PARAMS,
            f"Prompt not found: '{name}'. Available: {[p['name'] for p in PROMPTS]}",
            request_id,
        )
    return _jsonrpc_result(prompt_data, request_id)


# ── Method dispatch table ──────────────────────────────────

METHOD_DISPATCH: dict[str, Any] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "resources/list": _handle_resources_list,
    "resources/read": _handle_resources_read,
    "prompts/list": _handle_prompts_list,
    "prompts/get": _handle_prompts_get,
}

# ── Main handler for POST /mcp ─────────────────────────────


async def handle_mcp_request(request: Request) -> JSONResponse:
    """Обработчик POST /mcp — точка входа JSON-RPC 2.0.

    Поддерживает:
    - Одиночные запросы: {"jsonrpc":"2.0","method":"...","id":...}
    - Batch-запросы: [{"jsonrpc":"2.0",...}, ...]
    """
    # Проверка размера тела
    content_length = request.headers.get("content-length", "0")
    try:
        if int(content_length) > MAX_REQUEST_SIZE:
            logger.warning("Request too large: %s bytes (max %d)", content_length, MAX_REQUEST_SIZE)
            return JSONResponse(
                content=_jsonrpc_error(MCP_REQUEST_TOO_LARGE, f"Request body exceeds {MAX_REQUEST_SIZE} bytes"),
                status_code=413,
            )
    except ValueError:
        pass

    # Чтение тела
    try:
        raw_body = await request.body()
        if len(raw_body) > MAX_REQUEST_SIZE:
            return JSONResponse(
                content=_jsonrpc_error(MCP_REQUEST_TOO_LARGE, f"Request body exceeds {MAX_REQUEST_SIZE} bytes"),
                status_code=413,
            )
        body = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse error: %s", e)
        return JSONResponse(
            content=_jsonrpc_error(JSONRPC_PARSE_ERROR, f"Parse error: {e}"),
            status_code=400,
        )

    # Batch-запросы: массив → обработать каждый
    if isinstance(body, list):
        if not body:
            return JSONResponse(
                content=_jsonrpc_error(JSONRPC_INVALID_REQUEST, "Empty batch"),
                status_code=400,
            )
        responses = []
        for item in body:
            if not isinstance(item, dict):
                responses.append(_jsonrpc_error(JSONRPC_INVALID_REQUEST, "Batch item must be an object"))
                continue
            resp = await _dispatch_single(item, request)
            # Notifications (id=null) не возвращают ответ
            if resp is not None:
                responses.append(resp)
        return JSONResponse(content=responses)

    # Одиночный запрос
    if not isinstance(body, dict):
        return JSONResponse(
            content=_jsonrpc_error(JSONRPC_INVALID_REQUEST, "Request must be an object or array"),
            status_code=400,
        )

    resp = await _dispatch_single(body, request)
    if resp is None:
        # Notification — no response
        return JSONResponse(content="", status_code=204)
    return JSONResponse(content=resp)


async def _dispatch_single(body: dict, request: Request) -> dict | None:
    """Обработать один JSON-RPC запрос.

    Returns:
        Response dict, или None для notification (id=null).
    """
    # Валидация JSON-RPC структуры
    validation_error = _validate_jsonrpc(body)
    if validation_error:
        return validation_error

    method = body["method"]
    params = body.get("params", {})
    request_id = body.get("id")

    # Поиск handler'а в dispatch table
    handler = METHOD_DISPATCH.get(method)
    if handler is None:
        logger.warning("Method not found: '%s'", method)
        return _jsonrpc_error(
            JSONRPC_METHOD_NOT_FOUND,
            f"Method not found: '{method}'. Available: {sorted(METHOD_DISPATCH.keys())}",
            request_id,
        )

    # Вызов handler'а
    try:
        return await handler(params, request_id, request)
    except Exception as exc:
        logger.exception("Unhandled error in method '%s'", method)
        return _jsonrpc_error(
            JSONRPC_INTERNAL_ERROR,
            f"Internal error: {exc}",
            request_id,
        )
