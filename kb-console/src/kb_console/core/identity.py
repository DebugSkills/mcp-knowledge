"""Identity-хелперы kb-console (Ф2.4/Ф3, kb-console-roles B2; 035 — v2).

Per-юзер идентичность в NiceGUI page-context. Порядок источников (035 P1-1):
1. cookie-сессия (identity_from_session): middleware уже ревалидировал её
   против UserStore на этом же запросе (роль из стора актуальна);
2. fallback — повторная верификация Basic из headers (identity_from_headers,
   TTL-кэш горячий) — «Basic бит-в-бит»: curl/браузер с кэшированными
   Basic-кредами получают полноценную роль, а не fail-closed;
3. ни то ни другое → fail-closed ('contributor' при непустом сторе).

Семантика ролей для UI-гейтов и MCP-ключей:
- legacy (пустой users-стор): эффективная роль 'admin', ключ=MCP_API_KEY —
  поведение 002 бит-ин-бит (current_identity → None, ранний возврат);
- identity есть: роль из users.jsonl, ключ по api_key_for_role (Ф3.1);
- непустой стор БЕЗ identity (не должно случаться — middleware режет):
  fail-closed → 'contributor'.
"""

from __future__ import annotations

from typing import Any

from ..auth import parse_basic_credentials
from ..config import api_key_for_role

ROLE_LEVEL: dict[str, int] = {"admin": 3, "editor": 2, "contributor": 1}
"""Порядок привилегий для require_min-гейтов (admin > editor > contributor)."""


def identity_from_headers(headers: dict[str, str], users: Any) -> dict[str, Any] | None:
    """Верифицировать Basic из headers через UserStore (TTL-кэш).

    None — нет заголовка / неверные креды. Синхронный pbkdf2 при кэш-миссе:
    в page-context вызывать под общим правилом P2-4 (executor), но на
    практике кэш всегда горячий — middleware верифицировал тот же запрос.
    """
    auth = headers.get("authorization", "")
    creds = parse_basic_credentials(auth)
    if creds is None:
        return None
    record = users.verify(*creds)
    if record is None:
        return None
    return {"id": record.id, "username": record.username, "role": record.role}


def identity_from_session(session: Any) -> dict[str, Any] | None:
    """Identity из cookie-сессии (payload, записанный /api/login; 035).

    Ревалидация против стора — зона gate-middleware (каждый запрос); здесь
    формальная проверка payload. Legacy-payload → None (legacy-режим не
    имеет per-user identity — эффективная роль admin через effective_role,
    бит-в-бит с 002).
    """
    if not isinstance(session, dict):
        return None
    ident = session.get("identity")
    if not isinstance(ident, dict) or ident.get("legacy"):
        return None
    username = ident.get("username")
    role = ident.get("role")
    if not username or not role:
        return None
    return {"id": str(ident.get("user_id") or ""), "username": username, "role": role}


def identity_from_request(request: Any, users: Any) -> dict[str, Any] | None:
    """Порядок P1-1: session → Basic-fallback (None при отсутствии обоих)."""
    try:
        session = request.session
    except (AssertionError, AttributeError, KeyError):
        session = None
    if session is not None:
        from_session = identity_from_session(session)
        if from_session is not None:
            return from_session
    try:
        headers = dict(request.headers)
    except (AttributeError, KeyError, TypeError):
        return None
    return identity_from_headers(headers, users)


def effective_role(identity: dict[str, Any] | None, *, has_users: bool) -> str:
    """Роль для UI-гейтов: legacy → admin (бит-ин-бит 002), fail-closed → contributor."""
    if identity is not None:
        return identity["role"]
    return "admin" if not has_users else "contributor"


def api_key_for_request(
    identity: dict[str, Any] | None,
    *,
    base: str,
    has_users: bool,
    admin: str = "",
    editor: str = "",
    contributor: str = "",
) -> str:
    """MCP-ключ текущего запроса: legacy → base, identity → по роли (Ф3.1)."""
    if identity is not None:
        return api_key_for_role(
            identity["role"], base=base, admin=admin, editor=editor,
            contributor=contributor,
        )
    return base


# ── Page-context helpers (Ф3.2) ──────────────────────────────


def _users_store() -> Any:
    """USERS_STORE из core.runtime (app.py кладёт при старте; без side-effects)."""
    from . import runtime

    return runtime.USERS_STORE


def current_identity() -> dict[str, Any] | None:
    """Identity из nicegui page-context (035: session → Basic → None).

    Legacy-режим (пустой стор) — ранний возврат None, бит-в-бит с 002:
    эффективная роль admin через effective_role(has_users=False).
    Вне page-context/запроса (unit-тесты, CLI) → None. Ошибки подавляем:
    identity-хелпер НИКОГДА не ломает рендер страницы.
    """
    users = _users_store()
    if users is None or not users.has_users():
        return None
    try:
        from nicegui import context

        request = context.client.request
    except (ImportError, AttributeError, RuntimeError, ValueError, KeyError):
        # KeyError: вне HTTP-запроса (screen-test/фоновые задачи) request
        # требует NICEGUI_SCREEN_TEST_PORT — identity недоступна, это норма.
        return None
    return identity_from_request(request, users)


def current_role() -> str:
    """Роль текущего запроса для UI-гейтов (legacy → admin, fail-closed → contributor)."""
    return effective_role(current_identity(), has_users=_has_users())


def _has_users() -> bool:
    store = _users_store()
    return store.has_users() if store is not None else False


def current_actor() -> str:
    """Username для audit-actor (кто делает мутацию); legacy → 'admin'."""
    identity = current_identity()
    return identity["username"] if identity else "admin"


def is_admin() -> bool:
    return current_role() == "admin"


def can_replace() -> bool:
    """Replace-импорт: admin/editor (P1-1 серверный гейт Ф1 — UI лишь скрывает)."""
    return ROLE_LEVEL.get(current_role(), 0) >= ROLE_LEVEL["editor"]
