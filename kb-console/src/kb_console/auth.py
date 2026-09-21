"""HTTP Basic auth для kb-console — pure-ASGI middleware + interlock.

code-2026-09-20-002, вариант A2d (спецификация .boardData.md §7 «kb-console-auth»).

Почему pure-ASGI, а НЕ BaseHTTPMiddleware:
  - NiceGUI-транспорт = Socket.IO на mount /_nicegui_ws/ (engine.io HTTP-polling
    + WS-upgrade); BaseHTTPMiddleware обрабатывает только http-scope —
    websocket проходит ТРАНЗИТОМ → любой BaseHTTP-auth обходится через WS;
  - паттерн уже в репо: mcp_server/src/mcp_server/auth.py:332-456 (там же
    причина pure-ASGI — Content-Length mismatch BaseHTTPMiddleware под нагрузкой).
    Отличия от mcp_server: та middleware пропускает WS транзитом и проверяет
    X-API-Key; эта — наоборот ОБРАБАТЫВАЕТ websocket (Basic-креды браузера
    ходят и в WS-upgrade, и в polling), а транзитом пускает только lifespan.

Один механизм закрывает HTTP + WS + socket.io-polling: браузер кеширует
Basic-креды и прикладывает их ко ВСЕМ запросам. Известный нюанс: Safari
не прикладывает cached Basic к WS-upgrade → engine.io деградирует на
HTTP-polling, который тоже за auth — дыры нет (задокументировано в README).

kb-console-roles Ф2 (B2), middleware v2 — per-user Basic поверх 002:
  - users-стор НЕпуст → per-user verify (username+password, pbkdf2;
    CPU-bound → ТОЛЬКО через run_in_executor, P2-4) + identity в
    scope["state"]["user"] (для role-гейтов Ф3);
  - users-стор пуст → legacy-режим 002 бит-в-бит (username игнорируется);
  - стор НЕпуст + CONSOLE_PASSWORD задан → старый пароль отклоняется
    ПОЛНОСТЬЮ + warning в interlock (P2-5b: скрытый неаудитируемый
    админ-вход запрещён).

Skip-путей НЕТ by design: статика /_nicegui/* обязана быть за auth.
Сессий/cookie/storage_secret НЕТ — stateless. Инвариант: Basic без TLS =
креды base64 → сетевой доступ только ssh -L или TLS-фасад (доки, не код).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("kb_console.auth")

_VALID_AUTH_MODES = ("auto", "off", "required")
"""Допустимые значения CONSOLE_AUTH (для ValueError-сообщения)."""

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
"""Адреса, публикация на которых безопасна без пароля (loopback)."""

_REALM_CHALLENGE = b'Basic realm="kb-console"'
"""Значение заголовка WWW-Authenticate при 401 (HTTP Basic челлендж)."""

_AUTH_FAILURE_DELAY = 0.5
"""Фиксированная задержка (сек) на неверный пароль — brute-force замедление."""

_WS_POLICY_CODE = 1008
"""Code закрытия websocket-соединения при отказе (policy violation)."""

# ASGI-типы (scope/receive/send — Any: ASGI-контракт без protocol-классов).
_Scope = dict[str, Any]
_Receive = Callable[[], Awaitable[dict[str, Any]]]
_Send = Callable[[dict[str, Any]], Awaitable[None]]


def resolve_auth_mode(
    password: str,
    auth_mode: str,
    host: str,
    users_present: bool = False,
) -> str:
    """Interlock-матрица режима auth: чистая функция (тестируемая напрямую).

    Аргументы — значения env (CONSOLE_PASSWORD, CONSOLE_AUTH, CONSOLE_HOST)
    + users_present: непуст ли users-стор (Ф2, per-user режим).

    Возвращает "on" (проверять креды) или "off" (транзит).

    Матрица (спецификация §7:4383 + delta P2-5b):
      - невалидный auth_mode → ValueError со списком допустимых;
      - off → "off"; при заданном пароле — warning «пароль игнорируется»;
      - users_present → "on" (auto и required; пароль не нужен);
        + заданный CONSOLE_PASSWORD игнорируется ПОЛНОСТЬЮ с warning
        (P2-5b: два независимых источника входа = скрытый неаудитируемый
        админ — запрещено);
      - required + пустой стор + пусто → RuntimeError (fail-fast, прод);
      - required + пароль → "on";
      - auto + пароль → "on";
      - auto + пусто + loopback → "off" (тихо — поведение 001 сохранено);
      - auto + пусто + bind≠loopback → "off" + warning (bridge-паттерн
        `0.0.0.0` + `-p 127.0.0.1:...` не ломается, но оператор предупреждён).

    Пароль в логи НЕ пишется.
    """
    if auth_mode not in _VALID_AUTH_MODES:
        raise ValueError(
            f"CONSOLE_AUTH must be one of: {', '.join(_VALID_AUTH_MODES)}; got {auth_mode!r}"
        )

    if auth_mode == "off":
        if password:
            logger.warning(
                "CONSOLE_AUTH=off: CONSOLE_PASSWORD задан, но игнорируется (auth выключен)"
            )
        return "off"

    # kb-console-roles Ф2: непустой users-стор → per-user auth, пароль не нужен
    if users_present:
        if password:
            logger.warning(
                "CONSOLE_PASSWORD игнорируется: активен users-стор "
                "(per-user auth; скрытый неаудитируемый админ-вход запрещён)"
            )
        return "on"

    if auth_mode == "required" and not password:
        raise RuntimeError(
            "CONSOLE_AUTH=required, но CONSOLE_PASSWORD пуст — задайте пароль, "
            "создайте учётки (CONSOLE_ADMIN_USER/CONSOLE_ADMIN_PASSWORD) "
            "или ослабьте режим (auto/off)"
        )

    if not password:
        if host not in _LOOPBACK_HOSTS:
            logger.warning(
                "kb-console слушает %s без пароля (CONSOLE_PASSWORD пуст): "
                "доступ без аутентификации. Задайте CONSOLE_PASSWORD "
                "или CONSOLE_AUTH=off для явного отключения предупреждения.",
                host,
            )
        return "off"

    return "on"


def parse_basic_credentials(authorization: str) -> tuple[str, str] | None:
    """Разобрать Authorization: Basic → (username, password) | None.

    Ф2 v2: username больше НЕ игнорируется при per-user auth; здесь только
    парсинг (схема/ base64/ наличие ':'), семантика — в verify-путях.
    """
    if not authorization.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def verify_basic_auth(authorization: str, password: str) -> bool:
    """Проверить заголовок Authorization: Basic против пароля.

    username игнорируется (один оператор), сравнение constant-time
    (hmac.compare_digest, подход mcp_server/auth.py:124-126).
    Пароль пустой включён в сравнение — вызывается только при auth ON.
    """
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _username, _sep, provided_password = decoded.partition(":")
    return hmac.compare_digest(provided_password.encode("utf-8"), password.encode("utf-8"))


def _parse_scope_headers(scope: _Scope) -> dict[str, str]:
    """Извлечь HTTP-заголовки из ASGI scope в case-insensitive словарь.

    Копия mcp_server/auth.py:459-470 (kb-console самодостаточен и не
    импортирует mcp_server — DRY-комментарий вместо зависимости).
    """
    result: dict[str, str] = {}
    for key_bytes, value_bytes in scope.get("headers", []):
        key = key_bytes.decode("latin-1").lower()
        value = value_bytes.decode("latin-1")
        result[key] = value
    return result


class ConsoleAuthMiddleware:
    """Pure-ASGI HTTP Basic auth: http + websocket + транзит lifespan.

    Ветвление по scope["type"] (guard первым, до парсинга заголовков):
      - http: без кредов → 401 + WWW-Authenticate; с кредами → app вниз;
      - websocket: без кредов → websocket.close ДО accept (app вниз НЕ
        вызывается); с кредами → app вниз;
      - lifespan и любые прочие → транзит (uvicorn шлёт lifespan-scope на
        старте — наивный else-reject сломал бы запуск; паттерн
        mcp_server/auth.py:376-378).

    Режим mode="off" — полный транзит (нулевой оверхед).

    Ф2 v2: users (UserStore | None) — непустой стор включает per-user
    verify (pbkdf2 через run_in_executor — P2-4: CPU-bound pbkdf2 НЕ
    блокирует event loop NiceGUI; прецедент Фазы 13.15); identity кладётся
    в scope["state"]["user"] (dict: id/username/role) для role-гейтов Ф3.
    Пустой стор / users=None → legacy-режим 002 бит-в-бит.

    Неверный пароль: logger.warning (без пароля в сообщении) +
    фиксированная задержка failure_delay (anti-brute-force).
    """

    def __init__(
        self,
        app: Any,
        password: str = "",
        mode: str = "on",
        failure_delay: float = _AUTH_FAILURE_DELAY,
        users: Any = None,  # UserStore | None (Any — без циклического импорта)
    ) -> None:
        self.app = app
        self._password = password
        self._enabled = mode == "on"
        self._failure_delay = failure_delay
        self._users = users

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        scope_type = scope["type"]

        # Guard: не-http и не-websocket (lifespan и прочие) — транзит.
        if scope_type not in ("http", "websocket") or not self._enabled:
            await self.app(scope, receive, send)
            return

        headers = _parse_scope_headers(scope)
        authorization = headers.get("authorization", "")

        # Ф2 v2: непустой users-стор → per-user ветка (legacy-пароль отклонён)
        if self._users is not None and self._users.has_users():
            if await self._authenticate_user(scope, authorization):
                await self.app(scope, receive, send)
                return
            await self._reject(scope, scope_type, authorization, send)
            return

        if verify_basic_auth(authorization, self._password):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, scope_type, authorization, send)

    async def _authenticate_user(self, scope: _Scope, authorization: str) -> bool:
        """Per-user verify: pbkdf2 в executor + identity в scope-state.

        True → запрос пропущен; False → reject (без деталей, существует ли
        юзер — не раскрываем).
        """
        creds = parse_basic_credentials(authorization)
        if creds is None:
            return False
        username, password = creds
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            None, self._users.verify, username, password
        )
        if record is None:
            return False
        scope.setdefault("state", {})["user"] = {
            "id": record.id,
            "username": record.username,
            "role": record.role,
        }
        return True

    async def _reject(
        self,
        scope: _Scope,
        scope_type: str,
        authorization: str,
        send: _Send,
    ) -> None:
        """Общий отказ: warning + задержка при неверных кредах, 401/close."""
        if authorization:
            # Неверные креды — brute-force-сигнатура: warning + задержка.
            logger.warning(
                "Auth failed (401): неверные учётные данные, %s %s",
                scope_type,
                scope.get("path", ""),
            )
            await asyncio.sleep(self._failure_delay)
        # Отсутствующий заголовок — первичный челлендж браузера: тихий 401.

        if scope_type == "websocket":
            await send(
                {"type": "websocket.close", "code": _WS_POLICY_CODE, "reason": "unauthorized"}
            )
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", _REALM_CHALLENGE),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"401 Unauthorized: kb-console\n"})
