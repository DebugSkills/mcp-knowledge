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
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

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
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
}

# ── Write tools (только MCP_WRITE_KEYS) ──────────────────
WRITE_TOOLS: set[str] = {
    "write_knowledge",
    "update_entry",
    "delete_entry",
    "reindex",
}

# ── Import tools (MCP_IMPORT_KEYS: read + import_content, без delete/reindex) ──
IMPORT_TOOLS: set[str] = {
    "import_content",
}

# ── Methods, не требующие аутентификации ─────────────────
UNAUTHENTICATED_METHODS: set[str] = {
    "initialize",
    "ping",
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


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware: проверка X-API-Key header.

    Пропускает без проверки: /health, /metrics, /docs, /openapi.json.
    Для /mcp: извлекает ключ, проводит аутентификацию,
    сохраняет AuthInfo в request.state.auth.
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

    async def dispatch(self, request: Request, call_next) -> Response:
        # Пропускаем health/metrics/docs без аутентификации + rate limit
        if request.url.path.rstrip("/") in self.SKIP_PATHS or request.url.path in self.SKIP_PATHS:
            request.state.auth = AuthInfo()
            return await call_next(request)

        # Извлекаем X-API-Key
        api_key = request.headers.get("X-API-Key", "")

        if not api_key:
            # Для GET-запросов не на /mcp — пропускаем (браузеры)
            if request.method == "GET" and request.url.path != "/mcp":
                request.state.auth = AuthInfo()
                return await call_next(request)

            logger.warning("Auth: no X-API-Key header for %s %s", request.method, request.url.path)
            request.state.auth = AuthInfo()
            return await call_next(request)

        auth_info = authenticate_key(api_key)
        request.state.auth = auth_info

        # ── E2: Rate limiting (batch-aware, per-key) ────────
        if request.method == "POST" and request.url.path == "/mcp":
            rate_limit_response = await self._check_rate_limit(request, auth_info)
            if rate_limit_response is not None:
                return rate_limit_response

        return await call_next(request)

    # ── E2: Rate limit check ────────────────────────────────

    async def _check_rate_limit(self, request: Request, auth_info: AuthInfo) -> JSONResponse | None:
        """Проверить rate limit для POST /mcp.

        Batch-aware: парсит body, считает число JSON-RPC методов,
        тратит N токенов (не 1 на HTTP request).

        Returns:
            JSONResponse с MCP_RATE_LIMITED если лимит превышен, иначе None.
        """
        rate_limiter = getattr(request.app.state, "rate_limiter", None)
        if rate_limiter is None:
            return None  # rate limiter не включён

        # Считаем число методов в запросе (batch-aware P1-5)
        method_count = 1  # по умолчанию одиночный запрос
        try:
            raw_body = await request.body()
            # Кэшируем body для повторного чтения handler'ом
            # (Starlette проверяет _body перед чтением потока)
            request._body = raw_body
            body = json.loads(raw_body)
            if isinstance(body, list):
                method_count = len(body)  # JSON-RPC batch → N методов
        except (json.JSONDecodeError, UnicodeDecodeError):
            method_count = 1  # невалидный JSON → 1 токен (парсинг упадёт позже)

        # Выбор rate limiter'а по уровню ключа
        key_hash = auth_info.key_hash or "anonymous"
        if auth_info.key_level == "read":
            limiter = getattr(request.app.state, "rate_limiter_read", rate_limiter)
        elif auth_info.key_level == "write":
            limiter = getattr(request.app.state, "rate_limiter_write", rate_limiter)
        else:
            # Неаутентифицированные запросы — используем дефолтный (строгий)
            limiter = rate_limiter

        if not await limiter.check(key_hash, count=method_count):
            # Фаза 12: инкремент rate_limit_rejected метрики
            from .metrics import rate_limit_rejected
            rate_limit_rejected.labels(key_level=auth_info.key_level).inc()

            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32003,  # MCP_RATE_LIMITED
                        "message": (
                            f"Rate limit exceeded: {method_count} method(s) requested, "
                            f"try again in a few seconds."
                        ),
                    },
                    "id": None,
                },
                status_code=429,
            )

        return None


def get_auth(request: Request) -> AuthInfo:
    """Dependency: получить AuthInfo из request.state."""
    return getattr(request.state, "auth", AuthInfo())
