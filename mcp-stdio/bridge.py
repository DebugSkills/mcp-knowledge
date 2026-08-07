#!/usr/bin/env python3
# ruff: noqa: EXE001
"""mcp-stdio/bridge.py — тонкий stdio↔HTTP мост для MCP Knowledge Server.

Назначение:
    Прозрачно транслирует JSON-RPC 2.0 запросы из stdin (newline-delimited JSON)
    в HTTP POST на /mcp эндпоинт mcp-knowledge сервера, и возвращает ответы
    в stdout (тоже newline-delimited JSON). Это позволяет стандартным MCP-клиентам
    (Kilo Code, Claude Desktop, Cline) подключаться к серверу, который реализует
    JSON-RPC поверх HTTP (не stdio/SSE).

Протокол:
    Каждая строка stdin = один JSON-RPC запрос.
    Каждый ответ = одна JSON-строка в stdout + '\\n'.
    Никакого другого вывода в stdout (логи/ошибки — в stderr).

Переменные окружения:
    MCP_SERVER_URL — URL сервера (по умолчанию http://localhost:8000)
    MCP_API_KEY    — API-ключ для заголовка X-API-Key (по умолчанию пусто — без auth)

Зависимости:
    Только stdlib (Python 3.11+). Не требует httpx, requests, или любых внешних пакетов.

Примеры:
    # Ручная проверка (список инструментов):
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp-stdio/bridge.py

    # Проверка здоровья сервера:
    python3 mcp-stdio/bridge.py --health

    # Конфиг Kilo Code (.kilo/kilo.jsonc):
    # "mcp": {
    #   "mcp-knowledge": {
    #     "type": "local",
    #     "command": ["python3", "mcp-stdio/bridge.py"],
    #     "environment": {
    #       "MCP_SERVER_URL": "http://localhost:8000",
    #       "MCP_API_KEY": "sk-..."
    #     },
    #     "timeout": 60000,
    #     "enabled": true
    #   }
    # }
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── Константы ──────────────────────────────────────────────

_DEFAULT_SERVER_URL = "http://localhost:8000"
_DEFAULT_TIMEOUT = 60  # секунд (импорт книг может идти долго)

# JSON-RPC 2.0 error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
# MCP-specific server errors
_SERVER_UNAVAILABLE = -32000


# ── HTTP-клиент (stdlib) ────────────────────────────────────


def _post_json(
    url: str,
    payload: bytes,
    api_key: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """POST JSON на сервер, вернуть распарсенный ответ.

    Args:
        url: Полный URL (напр. http://localhost:8000/mcp).
        payload: JSON-тело запроса в байтах.
        api_key: Значение для заголовка X-API-Key (пустая строка — не слать).
        timeout: Таймаут в секундах.

    Returns:
        Распарсенный JSON-ответ от сервера (dict).

    Raises:
        urllib.error.URLError: при проблемах сети.
        urllib.error.HTTPError: при HTTP-ошибках (4xx, 5xx).
        json.JSONDecodeError: если сервер вернул не-JSON.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw)


# ── Обработка одного JSON-RPC запроса ──────────────────────


def _process_line(
    line: str,
    server_url: str,
    api_key: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Обработать одну строку JSON-RPC запроса.

    Выполняет:
        1. Парсинг JSON из строки.
        2. Валидация базовой структуры JSON-RPC (jsonrpc, method, id).
        3. POST на ${server_url}/mcp.
        4. Возврат ответа как JSON-строки.

    Ошибки парсинга → JSON-RPC error -32700.
    Ошибки сети/HTTP → JSON-RPC error -32000.
    Ответ сервера → пробрасывается как есть.

    Args:
        line: Одна строка JSON-RPC запроса.
        server_url: Базовый URL MCP-сервера.
        api_key: API-ключ (пустая строка = без auth).
        timeout: Таймаут HTTP-запроса в секундах.

    Returns:
        JSON-строка с ответом (всегда валидный JSON-RPC 2.0).
    """
    # 1. Парсинг JSON
    try:
        request_body = json.loads(line)
    except json.JSONDecodeError as e:
        print(f"[bridge] Parse error: {e}", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": _PARSE_ERROR, "message": f"Parse error: {e}"},
            "id": None,
        })

    if not isinstance(request_body, dict):
        print("[bridge] Invalid request: not a JSON object", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": _INVALID_REQUEST, "message": "Request must be a JSON object"},
            "id": None,
        })

    # 2. Базовая валидация JSON-RPC
    if request_body.get("jsonrpc") != "2.0":
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": _INVALID_REQUEST, "message": "jsonrpc must be '2.0'"},
            "id": request_body.get("id"),
        })

    if "method" not in request_body:
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": _INVALID_REQUEST, "message": "Missing 'method'"},
            "id": request_body.get("id"),
        })

    request_id = request_body.get("id")

    # 3. POST на сервер
    mcp_url = f"{server_url.rstrip('/')}/mcp"
    payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")

    try:
        server_response = _post_json(mcp_url, payload, api_key, timeout)
    except urllib.error.HTTPError as e:
        print(f"[bridge] HTTP error {e.code}: {e.reason}", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": _SERVER_UNAVAILABLE,
                "message": f"HTTP {e.code}: {e.reason}",
            },
            "id": request_id,
        })
    except urllib.error.URLError as e:
        print(f"[bridge] Сервер недоступен: {e.reason}", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": _SERVER_UNAVAILABLE,
                "message": f"Сервер недоступен: {e.reason}",
            },
            "id": request_id,
        })
    except (TimeoutError, OSError) as e:
        print(f"[bridge] Таймаут/ошибка сети: {e}", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": _SERVER_UNAVAILABLE,
                "message": f"Сервер недоступен: {e}",
            },
            "id": request_id,
        })
    except json.JSONDecodeError as e:
        print(f"[bridge] Некорректный JSON от сервера: {e}", file=sys.stderr)
        return json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": _SERVER_UNAVAILABLE,
                "message": f"Некорректный JSON-ответ от сервера: {e}",
            },
            "id": request_id,
        })

    # 4. Возврат ответа (пробрасываем как есть)
    return json.dumps(server_response, ensure_ascii=False)


# ── Health check ────────────────────────────────────────────


def _check_health(server_url: str, timeout: int = 5) -> str:
    """GET /health — проверить статус сервера.

    Args:
        server_url: Базовый URL сервера.
        timeout: Таймаут запроса.

    Returns:
        Строка с результатом проверки для вывода в stdout.
    """
    health_url = f"{server_url.rstrip('/')}/health"
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        status = data.get("status", "unknown")
        qdrant = data.get("qdrant", "unknown")
        return f"OK: status={status} qdrant={qdrant}"
    except urllib.error.URLError as e:
        return f"ERROR: сервер недоступен — {e.reason}"
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} — {e.reason}"
    except (ValueError, TypeError, OSError) as e:
        return f"ERROR: {e}"


# ── Main loop ───────────────────────────────────────────────


def main() -> None:
    """Главный цикл: читать JSON-RPC из stdin → отправлять на сервер → писать ответ в stdout.

    Завершается с exit 0 при EOF на stdin.
    Поддерживает флаг --health для однократной проверки.
    """
    # Проверка аргументов
    if "--health" in sys.argv:
        server_url = os.environ.get("MCP_SERVER_URL", _DEFAULT_SERVER_URL)
        result = _check_health(server_url)
        print(result, file=sys.stdout)
        sys.stdout.flush()
        return

    server_url = os.environ.get("MCP_SERVER_URL", _DEFAULT_SERVER_URL)
    api_key = os.environ.get("MCP_API_KEY", "")
    timeout = _DEFAULT_TIMEOUT

    print(f"[bridge] MCP stdio bridge started, server={server_url}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue  # пропускаем пустые строки

        response = _process_line(line, server_url, api_key, timeout)
        print(response, file=sys.stdout)
        sys.stdout.flush()

    # EOF — чистый выход
    print("[bridge] EOF, exiting", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
