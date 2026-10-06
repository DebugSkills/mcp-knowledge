"""Зонный резолвер ws-слоя верстака (arch-2026-10-05-ai-workspace, Ф2 шаг #7).

Контекст (план Фаза 2 «Auth/логин/роли», .boardData.md §8 «Ф2-спека» D/H):
MCP-ключ верстака — ЕДИНЫЙ сервисный ключ со scope read+zone=both (I6;
Ф2.0 cd9e6de — zone-scope токенов на стороне mcp_server, НЕ трогаем).
Зону выборки для пользователя определяет ws-слой — из роли учётной записи
консоли. Роль-логика НЕ дублируется: переиспользуется identity-стек
kb-console (``effective_role``), семантика legacy=admin бит-ин-бит (I5).

Политика зон (D7 «private admin-only v1»; SSOT-согласована с политикой
консоли в tests/test_role_zone_matrix.py — «private → только admin»)::

    admin       -> private  (зоны выборки: private + public)
    editor      -> public
    contributor -> public
    None/иная роль -> public  (fail-closed: ниже admin private НЕТ)
"""

from __future__ import annotations

from typing import Any

from .identity import effective_role

WS_ZONE_BY_ROLE: dict[str, str] = {
    "admin": "private",
    "editor": "public",
    "contributor": "public",
}
"""SSOT: базовая зона ws-выборки по роли (D7: private admin-only v1).

Согласована с матрицей консоли (tests/test_role_zone_matrix.py): private
доступен только admin; editor/contributor — public. Изменение маппинга
обязано синхронно правиться с матрицей (см. TestConsistencyWithConsoleMatrix).
"""

PUBLIC_ZONE = "public"
"""Fail-closed дефолт: неизвестная/отсутствующая роль -> public."""

PRIVATE_ZONE = "private"
"""Зона admin (D7 v1); в WS_ZONE_BY_ROLE встречается только у admin."""


def zone_for_role(role: str | None) -> str:
    """Базовая зона ws-выборки по роли пользователя.

    None/неизвестная роль -> ``"public"`` (fail-closed: непроверенная роль
    НИКОГДА не получает private — D7/I6).
    """
    if role in WS_ZONE_BY_ROLE:
        return WS_ZONE_BY_ROLE[role]
    return PUBLIC_ZONE


def zones_for_role(role: str | None) -> tuple[str, ...]:
    """Все зоны выборки роли: admin -> ``("private", "public")``, иначе ``("public",)``.

    Базовая зона роли — первой (у admin это private), затем добор public.
    Ключ верстака read+zone=both (I6): сервер сам фильтрует по zone-scope
    токена (Ф2.0), ws-слой лишь задаёт зону выборки для UX-слоя.
    """
    if zone_for_role(role) == PRIVATE_ZONE:
        return (PRIVATE_ZONE, PUBLIC_ZONE)
    return (PUBLIC_ZONE,)


def zone_for_identity(identity: dict[str, Any] | None, *, has_users: bool) -> str:
    """Зона ws-выборки по identity запроса — через ``effective_role`` (I5).

    Семантика legacy/fail-closed переиспользуется из identity.py бит-в-бит,
    новая роль-логика НЕ вводится:

    - identity есть -> зона роли identity (admin -> private, иначе public);
    - legacy (None + пустой users-стор) -> эффективная роль admin -> private;
    - fail-closed (None + непустой стор) -> contributor -> public.
    """
    return zone_for_role(effective_role(identity, has_users=has_users))
