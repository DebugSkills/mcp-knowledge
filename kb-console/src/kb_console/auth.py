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

035, middleware v3 — cookie-сессия + allowlist + ревалидация (план §3):
  - без аутентификации доступны РОВНО 3 пути: /healthz (033-F3), /login
    (GET, статический HTML), /api/login (POST, rate-limit в login_page);
    /_nicegui/ и /_nicegui_ws/ — ВСЕГДА за аутентификацией (least
    privilege, P2-1; favicon = data-URI);
  - приоритет источников: cookie-сессия → Basic → отказ. Cookie-identity
    ревалидируется на КАЖДОМ запросе (HTTP и WS-handshake, §3д):
    * legacy-сессия валидна пока стор ПУСТ и store_version совпадает
      (P1-6: create_user → bump → отказ на первом же запросе);
    * обычная — UserStore.get(): active + роль из стора (мгновенный
      отзыв), плюс равенство session.store_version == store.store_version
      (N3: set_password/create_user/set_role/set_active = logout-all);
  - неаутентифицированный HTTP: навигация → 302 /login?next=<path>;
    XHR/API (пути /api/*, /_nicegui_ws/*, Accept: application/json или
    X-Requested-With) → 401 JSON БЕЗ WWW-Authenticate (P2-2 — иначе
    браузер поднимет Basic-диалог). Basic-челлендж не выдаётся вовсе:
    Basic-креды по-прежнему ПРИНИМАЮТСЯ (verify-deploy/curl back-compat);
  - провал ревалидации чистит сессию (scope['session'].clear() →
    Starlette шлёт Set-Cookie session=null на 302) — петли 302 нет;
  - websocket без аутентификации (в т.ч. сессия невалидна) → close 1008
    ДО accept (класс 002: WS/polling-транзит = обход auth);
  - scope['session'] читается только через .get() — отсутствие ключа =
    fail-closed отказ, не 500 (P2-4).

Сессии/stateless: Basic-ветка 002 сохранена бит-в-бит (legacy-пароль при
пустом сторе). Инвариант: Basic без TLS = креды base64 → сетевой доступ
только ssh -L или TLS-фасад (доки, не код).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("kb_console.auth")

_VALID_AUTH_MODES = ("auto", "off", "required")
"""Допустимые значения CONSOLE_AUTH (для ValueError-сообщения)."""

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
"""Адреса, публикация на которых безопасна без пароля (loopback)."""

_AUTH_FAILURE_DELAY = 0.5
"""Фиксированная задержка (сек) на неверный пароль — brute-force замедление."""

_WS_POLICY_CODE = 1008
"""Code закрытия websocket-соединения при отказе (policy violation)."""

_LOGIN_PATH = "/login"
"""Статическая страница входа (035): GET — в анонимном allowlist."""

_API_LOGIN_PATH = "/api/login"
"""REST-логин (035): POST — в анонимном allowlist (rate-limit в login_page)."""

_API_PREFIXES = ("/api/", "/_nicegui_ws/")
"""Пути, всегда классифицируемые как XHR/API (401 JSON, не 302)."""

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
    """Pure-ASGI gate v3: cookie-сессия + Basic + allowlist (http+ws+lifespan).

    Ветвление по scope["type"] (guard первым, до парсинга заголовков):
      - http: allowlist (3 пути) → сессия → Basic → 302/401;
      - websocket: сессия → Basic → websocket.close ДО accept;
      - lifespan и любые прочие → транзит.

    Порядок источников аутентификации: cookie-сессия (ревалидация §3д —
    legacy-ветка P1-6, поля active/role, version-equality N3) → per-user
    Basic (executor, identity в scope["state"]["user"]) → legacy-Basic
    (пароль CONSOLE_PASSWORD при пустом сторе) → отказ.

    Режим mode="off" — полный транзит (нулевой оверхед), кроме /healthz.

    Неверный пароль/невалидная сессия: logger.warning (без секретов) +
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

        # 033-F3: liveness-эндпоинт — ДО enabled-ветки (одинаковая семантика в
        # режимах on/off: auth-off не должен ловить 404 от NiceGUI-app) и до
        # парсинга заголовков. Строгое равенство: "/healthz/" идёт в отказ.
        if scope_type == "http" and scope.get("path") == "/healthz":
            await self._respond_healthz(send)
            return

        # Guard: не-http и не-websocket (lifespan и прочие) — транзит.
        if scope_type not in ("http", "websocket") or not self._enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        method = scope.get("method", "GET").upper()

        # 035 §3а-1: анонимный allowlist — РОВНО 3 пути (счётность).
        if scope_type == "http":
            if path == _LOGIN_PATH and method == "GET":
                await self.app(scope, receive, send)
                return
            if path == _API_LOGIN_PATH and method == "POST":
                await self.app(scope, receive, send)
                return

        headers = _parse_scope_headers(scope)
        authorization = headers.get("authorization", "")

        # 1) Cookie-сессия (035 §3д): ревалидация на каждом запросе.
        ok, had_identity = self._validate_session_identity(scope)
        if ok:
            await self.app(scope, receive, send)
            return

        # 2) Basic (back-compat: verify-deploy, curl, API-скрипты).
        if self._users is not None and self._users.has_users():
            if await self._authenticate_user(scope, authorization):
                await self.app(scope, receive, send)
                return
        elif verify_basic_auth(authorization, self._password):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, scope_type, authorization, send, had_identity)

    # ── session branch (035) ────────────────────────────────

    def _validate_session_identity(self, scope: _Scope) -> tuple[bool, bool]:
        """Ревалидация cookie-identity §3д. Возвращает (ok, had_identity).

        had_identity — была ли в сессии заявленная identity: нужно для
        очистки cookie при провале (анонимную сессию с session-id NiceGUI
        не трогаем). Синхронно: UserStore.get() — TTL-кэш под RLock, без
        await внутри (single-loop, WORKERS=1 — гонок нет).
        """
        session = scope.get("session")
        if not isinstance(session, dict):
            return False, False  # P2-4: fail-closed, не 500
        ident = session.get("identity")
        if not isinstance(ident, dict):
            return False, False
        had_identity = True

        if ident.get("legacy"):
            # P1-6: legacy-сессия валидна только пока стор ПУСТ (плюс
            # version-equality: create_user бампает версию).
            users = self._users
            store_empty = users is None or not users.has_users()
            version_ok = users is None or ident.get("store_version") == users.store_version
            if store_empty and version_ok:
                scope.setdefault("state", {})["user"] = {
                    "id": "",
                    "username": "admin",
                    "role": "admin",
                    "legacy": True,
                }
                return True, had_identity
            return False, had_identity

        username = ident.get("username")
        users = self._users
        if users is None or not username:
            return False, had_identity
        record = users.get(username)
        if record is None or not record.active:
            return False, had_identity
        # N3: любая мутация стора = logout-all сессий (версия глобальная).
        if ident.get("store_version") != users.store_version:
            return False, had_identity
        # Полевая ревалидация роли: смена роли действует мгновенно.
        if record.role != ident.get("role"):
            ident["role"] = record.role  # Session пометит modified → Set-Cookie
        scope.setdefault("state", {})["user"] = {
            "id": record.id,
            "username": record.username,
            "role": record.role,
        }
        return True, had_identity

    async def _respond_healthz(self, send: _Send) -> None:
        """Inline pure-ASGI 200 "ok" для /healthz (033-F3).

        Приложение (NiceGUI) и UserStore не вызываются — ноль side-effects
        для аудита и last_login_at. no-store: ответ неизменяем, кешировать
        нечего.
        """
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def _authenticate_user(self, scope: _Scope, authorization: str) -> bool:
        """Per-user verify: pbkdf2 в executor + identity в scope-state.

        True → запрос пропущен; False → reject (без деталей, существует ли
        юзер — не раскрываем). Логины фиксируются в users_audit.jsonl (Ф3.3).
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
            self._users.log_login(username, ok=False)
            return False
        self._users.log_login(username, ok=True)
        scope.setdefault("state", {})["user"] = {
            "id": record.id,
            "username": record.username,
            "role": record.role,
        }
        return True

    # ── reject (035: 302/401-JSON/close вместо Basic-челленджа) ──

    def _is_api_request(self, scope: _Scope, headers: dict[str, str]) -> bool:
        """XHR/API-класс: 401 JSON; всё прочее — навигация → 302.

        Классификация: путь под /api/ или /_nicegui_ws/ (engine.io-polling),
        Accept: application/json, заголовок X-Requested-With.
        """
        path = scope.get("path", "") or "/"
        if path.startswith(_API_PREFIXES):
            return True
        if "application/json" in headers.get("accept", ""):
            return True
        return bool(headers.get("x-requested-with"))

    async def _reject(
        self,
        scope: _Scope,
        scope_type: str,
        authorization: str,
        send: _Send,
        session_identity_failed: bool = False,
    ) -> None:
        """Отказ: warning + задержка при признаках подбора; 302/401/close.

        session_identity_failed — в сессии БЫЛА identity, не прошедшая
        ревалидацию → чистим cookie (scope['session'].clear(); Starlette
        на выходе шлёт Set-Cookie session=null) — петли 302 нет.
        """
        if authorization or session_identity_failed:
            logger.warning(
                "Auth failed: неверные учётные данные или просроченная сессия, %s %s",
                scope_type,
                scope.get("path", ""),
            )
            await asyncio.sleep(self._failure_delay)

        if session_identity_failed:
            session = scope.get("session")
            if isinstance(session, dict):
                session.clear()

        if scope_type == "websocket":
            await send(
                {"type": "websocket.close", "code": _WS_POLICY_CODE, "reason": "unauthorized"}
            )
            return

        headers = _parse_scope_headers(scope)
        if self._is_api_request(scope, headers):
            # P2-2: 401 JSON БЕЗ WWW-Authenticate (иначе браузер поднимет
            # Basic-диалог — регрессия UX-цели страницы входа).
            body = b'{"ok": false, "error": "unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        next_path = quote(scope.get("path", "/") or "/", safe="")
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"location", f"/login?next={next_path}".encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"302 -> /login\n"})
