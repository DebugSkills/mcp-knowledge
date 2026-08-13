"""B1: Мульти-ключевая аутентификация (MCP_READ_KEYS / MCP_WRITE_KEYS).

Constant-time сравнение через hmac.compare_digest (P1-5).
Read-ключ → только read-tools. Write-ключ → все tools.
Graceful rotation: добавить ключ → перезапуск → убрать старый.
Маскирование ключей в логах (только первые 4 символа + хеш).

Фаза 3 E2 (v1.1): Rate limiting integration — batch-aware, per-key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import typing
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .config import settings

logger = logging.getLogger("mcp_knowledge.auth")

# ── Read-only tools (доступны с MCP_READ_KEYS) ──────────
READ_TOOLS: set[str] = {
    "search_knowledge",
    "search_by_tags",
    "get_entry",
    "get_knowledge_map",
    "list_domains",
    "list_subjects",
    "list_projects",
    "list_collections",  # read-only: список книг/коллекций (Фаза 13.10)
    "analyze_content",  # read-only: LLM/TF-IDF анализ без записи в хранилище (Фаза 13.8)
    "review_queue",  # read-only: топ устаревших секций (Фаза 13.14)
    "review_queue_books",  # read-only: агрегат книг (Фаза 13.14)
    "list_quality_issues",  # read-only: список quality issues (Фаза 13.14)
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
    "find_fragment",  # Фаза 13.23: поиск секций в книге (read-only)
}

# ── Write tools (только MCP_WRITE_KEYS) ──────────────────
WRITE_TOOLS: set[str] = {
    "write_knowledge",
    "update_entry",
    "delete_entry",
    "reindex",
    "resolve_quality_issue",  # Фаза 13.14: мутирует Qdrant payload (deprecate/restore/merge)
    "bulk_resolve_issues",  # P0: пакетный resolve/ignore issues (мутирует issues.jsonl)
    "bulk_deprecate_duplicates",  # Фаза 1 dedup: пакетный deprecate дублей (мутирует payload + issues + audit)
    "run_quality_scan",  # Фаза 13.14: скан + запись issues в БД
    "add_fragment",  # Фаза 13.23: создание секции книги
    "update_fragment",  # Фаза 13.23: обновление секции книги
    "delete_fragment",  # Фаза 13.23: удаление секции книги
}

# ── Import tools (MCP_IMPORT_KEYS: read + import_content, без delete/reindex) ──
IMPORT_TOOLS: set[str] = {
    "import_content",
    "cancel_import",
    "extract_pdf_text",  # PDF→текст для авто-классификации (оперирует загруженным PDF)
}

# ── Methods, не требующие аутентификации ─────────────────
UNAUTHENTICATED_METHODS: set[str] = {
    "initialize",
    "ping",
    "notifications/initialized",  # Фаза 13.21: MCP notification (JSON-RPC 2.0 §4.1)
}


@dataclass
class AuthInfo:
    """Результат аутентификации, сохраняется в request.state."""
    authenticated: bool = False
    key_level: str = "none"  # "read" | "import" | "write" | "none"
    key_hash: str = ""  # sha256 первых 8 символов для аудита


def mask_key(key: str) -> str:
    """Маскирование ключа для логов: первые 4 символа + sha256 хеш."""
    if len(key) < 8:
        return "[too-short]"
    prefix = key[:4]
    key_hash = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"{prefix}...{key_hash}"


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time сравнение строк через hmac.compare_digest."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def authenticate_key(provided_key: str) -> AuthInfo:
    """Проверить API-ключ constant-time против WRITE/IMPORT/READ списков.

    Возвращает AuthInfo с уровнем доступа.
    Первый совпавший ключ определяет уровень (приоритет: write > import > read).
    """
    # Проверяем write-ключи (более высокий приоритет)
    for stored_key in settings.MCP_WRITE_KEYS:
        if stored_key and _constant_time_compare(provided_key, stored_key):
            logger.debug(
                "Auth SUCCESS: write-key matched (masked=%s)",
                mask_key(provided_key),
            )
            return AuthInfo(
                authenticated=True,
                key_level="write",
                key_hash=hashlib.sha256(provided_key.encode()).hexdigest()[:16],
            )

    # Проверяем import-ключи (read + import_content)
    for stored_key in settings.MCP_IMPORT_KEYS:
        if stored_key and _constant_time_compare(provided_key, stored_key):
            logger.debug(
                "Auth SUCCESS: import-key matched (masked=%s)",
                mask_key(provided_key),
            )
            return AuthInfo(
                authenticated=True,
                key_level="import",
                key_hash=hashlib.sha256(provided_key.encode()).hexdigest()[:16],
            )

    # Проверяем read-ключи
    for stored_key in settings.MCP_READ_KEYS:
        if stored_key and _constant_time_compare(provided_key, stored_key):
            logger.debug(
                "Auth SUCCESS: read-key matched (masked=%s)",
                mask_key(provided_key),
            )
            return AuthInfo(
                authenticated=True,
                key_level="read",
                key_hash=hashlib.sha256(provided_key.encode()).hexdigest()[:16],
            )

    logger.warning(
        "Auth FAILED: invalid key (masked=%s)",
        mask_key(provided_key),
    )
    return AuthInfo(authenticated=False, key_level="none")


def check_tool_permission(auth_info: AuthInfo, tool_name: str) -> None:
    """Проверить, разрешён ли доступ к tool с данным уровнем ключа.

    Raises:
        HTTPException(403) если доступ запрещён.
    """
    if tool_name in UNAUTHENTICATED_METHODS:
        return

    if not auth_info.authenticated:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")

    # Write-ключ → доступ ко всему
    if auth_info.key_level == "write":
        return

    # Import-ключ → read-tools + import_content
    if auth_info.key_level == "import":
        if tool_name in READ_TOOLS or tool_name in IMPORT_TOOLS:
            return
        logger.warning(
            "Auth FORBIDDEN: import-key attempted non-import tool '%s' (key_hash=%s)",
            tool_name,
            auth_info.key_hash,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Import key cannot access tool '{tool_name}'. "
            f"Use a write key for write operations.",
        )

    # Read-ключ → только read-tools
    if tool_name in READ_TOOLS:
        return

    # Read-ключ пытается выполнить write-операцию
    logger.warning(
        "Auth FORBIDDEN: read-key attempted write tool '%s' (key_hash=%s)",
        tool_name,
        auth_info.key_hash,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Read-only key cannot access tool '{tool_name}'. "
        f"Use a write key for write operations.",
    )


class AuthMiddleware:
    """Pure ASGI middleware: проверка X-API-Key header.

    НЕ наследует BaseHTTPMiddleware — реализует __call__ напрямую,
    устраняя root cause Content-Length mismatch при конкурентной нагрузке
    (BaseHTTPMiddleware → anyio memory-object-stream → body-chunk overflow).

    Пропускает без проверки: /health, /metrics, /docs, /openapi.json.
    Для /mcp: извлекает ключ, проводит аутентификацию,
    сохраняет AuthInfo в scope["state"]["auth"].

    POST /mcp: читает body через обёрнутую receive (кеширует body-chunks),
    проверяет rate-limit, затем переигрывает кешированные чанки вниз.
    """

    SKIP_PATHS: typing.ClassVar[set[str]] = {
        "/health",
        "/health/",
        "/health/live",
        "/health/live/",
        "/metrics",
        "/metrics/",
        "/docs",
        "/openapi.json",
    }

    def __init__(self, app, fastapi_app=None):
        """Сохранить ссылки: внутреннее ASGI-приложение + FastAPI (для app.state).

        app: внутреннее ASGI-приложение (Router, переданное Starlette.add_middleware).
        fastapi_app: FastAPI-приложение (нужен для доступа к app.state.rate_limiter_*).
            В тестах через ASGITransport scope["app"] не устанавливается,
            поэтому fastapi_app передаётся явно.
        """
        self.app = app
        self._fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        """Pure ASGI entry point.

        Обрабатывает HTTP-запросы: аутентификация X-API-Key,
        rate-limit для POST /mcp (с body-reading приёмом).
        Не-HTTP запросы (websocket) пропускает прозрачно.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        normalized = path.rstrip("/")

        # Инициализируем state (FastAPI ожидает dict-like scope["state"])
        scope.setdefault("state", {})

        # Пропускаем health/metrics/docs без аутентификации + rate limit
        if normalized in self.SKIP_PATHS or path in self.SKIP_PATHS:
            scope["state"]["auth"] = AuthInfo()
            await self.app(scope, receive, send)
            return

        # Извлекаем X-API-Key из scope headers
        headers = _parse_scope_headers(scope)
        api_key = headers.get("x-api-key", "")

        if not api_key:
            # Для GET-запросов не на /mcp — пропускаем (браузеры)
            if method == "GET" and path != "/mcp":
                scope["state"]["auth"] = AuthInfo()
                await self.app(scope, receive, send)
                return

            logger.warning("Auth: no X-API-Key header for %s %s", method, path)
            scope["state"]["auth"] = AuthInfo()
            await self.app(scope, receive, send)
            return

        auth_info = authenticate_key(api_key)
        scope["state"]["auth"] = auth_info

        # ── E2: Rate limiting (batch-aware, per-key) для POST /mcp ──
        if method == "POST" and path == "/mcp":
            # Читаем все body-чанки (кешируем для переигрывания вниз)
            body_chunks: list[bytes] = []
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.request":
                    body_chunks.append(message.get("body", b""))
                    more_body = message.get("more_body", False)

            raw_body = b"".join(body_chunks)

            # Проверяем rate limit
            # Приоритет: self._fastapi_app (явно передан) → scope["app"] (uvicorn)
            # → self.app (fallback — Router без .state, rate-limiter обойдётся)
            root_app = self._fastapi_app or scope.get("app") or self.app
            rate_limit_response = await _check_rate_limit_bytes(
                raw_body, auth_info, root_app
            )
            if rate_limit_response is not None:
                await _send_json_response(send, rate_limit_response, 429)
                return

            # Переигрываем body вниз через обёрнутую receive
            chunk_index = 0

            async def wrapped_receive():
                nonlocal chunk_index
                if chunk_index < len(body_chunks):
                    chunk = body_chunks[chunk_index]
                    is_last = (chunk_index == len(body_chunks) - 1)
                    chunk_index += 1
                    return {
                        "type": "http.request",
                        "body": chunk,
                        "more_body": not is_last,
                    }
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, wrapped_receive, send)
        else:
            await self.app(scope, receive, send)


def _parse_scope_headers(scope: dict) -> dict[str, str]:
    """Извлечь HTTP-заголовки из ASGI scope в case-insensitive словарь.

    scope["headers"] — список кортежей (b"header-name", b"value") в latin-1.
    Возвращает словарь с ключами в нижнем регистре.
    """
    result: dict[str, str] = {}
    for key_bytes, value_bytes in scope.get("headers", []):
        key = key_bytes.decode("latin-1").lower()
        value = value_bytes.decode("latin-1")
        result[key] = value
    return result


async def _check_rate_limit_bytes(
    raw_body: bytes,
    auth_info: AuthInfo,
    app,
) -> dict | None:
    """Проверить rate limit для POST /mcp по сырым байтам body.

    Batch-aware: парсит body, считает число JSON-RPC методов,
    тратит N токенов (не 1 на HTTP request).

    Returns:
        Словарь JSON-RPC error для 429 ответа, или None если лимит не превышен.
    """
    app_state = getattr(app, "state", None)
    if app_state is None:
        return None

    rate_limiter = getattr(app_state, "rate_limiter", None)
    if rate_limiter is None:
        return None  # rate limiter не включён

    # Считаем число методов в запросе (batch-aware P1-5)
    method_count = 1
    try:
        body = json.loads(raw_body)
        if isinstance(body, list):
            method_count = len(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        method_count = 1

    # Выбор rate limiter'а по уровню ключа
    key_hash = auth_info.key_hash or "anonymous"
    if auth_info.key_level == "read":
        limiter = getattr(app_state, "rate_limiter_read", rate_limiter)
    elif auth_info.key_level == "write":
        limiter = getattr(app_state, "rate_limiter_write", rate_limiter)
    else:
        limiter = rate_limiter

    if not await limiter.check(key_hash, count=method_count):
        from .metrics import rate_limit_rejected
        rate_limit_rejected.labels(key_level=auth_info.key_level).inc()

        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32003,
                "message": (
                    f"Rate limit exceeded: {method_count} method(s) requested, "
                    f"try again in a few seconds."
                ),
            },
            "id": None,
        }

    return None


async def _send_json_response(send, content: dict, status_code: int = 200) -> None:
    """Отправить JSON-ответ через ASGI send.

    Используется для rate-limit 429 ответов, которые должны быть отправлены
    до передачи управления внутреннему приложению.
    """
    body = json.dumps(content).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    })


def get_auth(request: Request) -> AuthInfo:
    """Dependency: получить AuthInfo из request.state (или scope["state"])."""
    auth = getattr(request.state, "auth", None)
    if auth is None and hasattr(request, "scope"):
        auth = request.scope.get("state", {}).get("auth")
    return auth if auth is not None else AuthInfo()
