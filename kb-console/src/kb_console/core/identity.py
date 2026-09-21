"""Identity-хелперы kb-console (Ф2.4/Ф3, kb-console-roles B2).

Пер-юзер идентичность в NiceGUI page-context: повторная верификация Basic
из headers запроса через UserStore-TTL-кэш (кэш горячий — middleware уже
верифицировал тот же заголовок, CPU-работы нет; R5 закрыт: Client.request
доступен в nicegui 3.15.0/3.17.1).

Семантика ролей для UI-гейтов и MCP-ключей:
- legacy (пустой users-стор): effective_role='admin', ключ=MCP_API_KEY —
  поведение 002 бит-ин-бит (ничего не скрыто);
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
    """Identity из nicegui page-context (Basic → UserStore, кэш горячий).

    Вне page-context/запроса (unit-тесты, CLI) → None. Ошибки подавляем:
    identity-хелпер НИКОГДА не ломает рендер страницы.
    """
    users = _users_store()
    if users is None or not users.has_users():
        return None
    try:
        from nicegui import context

        headers = dict(context.client.request.headers)
    except (ImportError, AttributeError, RuntimeError, ValueError, KeyError):
        # KeyError: вне HTTP-запроса (screen-test/фоновые задачи) request
        # требует NICEGUI_SCREEN_TEST_PORT — identity недоступна, это норма.
        return None
    return identity_from_headers(headers, users)


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
