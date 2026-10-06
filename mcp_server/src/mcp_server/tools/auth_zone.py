"""W3 + Ф2.0: резолв зоны read-тулов из служебного ключа params["_auth"].

mcp_handler._handle_tools_call прокидывает AuthInfo в args тула под ключом
`_auth` (W3.9, внутренний служебный ключ — НЕ в JSON Schema).

Зонная политика (Ф2.0, C1′ — zone-scope токенов):
- write/system → обе зоны (не сужается), прежняя семантика явного `zone`;
- subscriber → public; legacy/env/без флага zone_explicit → public (fail-closed);
- zone_explicit=True → скоуп ключа из AuthInfo.zone (public|private|both);
- решётка: запрошенная зона ∩ скоуп ключа; вне скоупа → ZoneAccessError
  (403-семантика; mcp_handler конвертирует в MCP_AUTH_FAILED, не -32603);
- R2: запрос "both" от singleton-скоупа → ∩ = скоуп, НЕ отказ
  (resources.py:84 хардкодит zone="both").

Запись политикой НЕ затрагивается (import/write/editor пишут private через
auth-free пути; зонный гейт записи — ZONE-MATRIX-WRITE, отдельная задача).
"""

from __future__ import annotations

VALID_ZONES: tuple[str, str] = ("public", "private")

# P2-1 (bibliography): уровни ключей, которым доступны ОБЕ зоны при ЧТЕНИИ.
# admin-эквивалент = "write" (auth.check_tool_permission: write -> всё; legacy
# master-ключ MCP_API_KEY/MCP_WRITE_KEYS — write-уровень). Все прочие уровни
# (subscriber/read/import/editor/none) → public-only, КРОМЕ явного zone-scope
# (zone_explicit=True, Ф2.0). Запись политикой НЕ затрагивается.
ADMIN_LEVELS: frozenset[str] = frozenset({"write"})

# Системный (внутренний) вызов: `_auth` вообще отсутствует — так тулы вызывают
# server-side джобы (reconcile, GC, source-ref runtime, CLI, тесты). HTTP-путь
# ВСЕГДА инжектит `_auth` (mcp_handler.py, даже для неаутентифицированного
# ключа → level "none") → внешний вызов политикой ограничен. Отсутствие `_auth`
# трактуется как system (полный доступ), чтобы внутренние синхронизации не
# теряли private-контент.
SYSTEM_LEVEL: str = "system"


class ZoneAccessError(PermissionError):
    """Ф2.0: запрошенная зона вне скоупа ключа — auth-отказ (403-семантика).

    mcp_handler конвертирует в MCP_AUTH_FAILED (-32002) БЕЗ ERROR-traceback
    (не -32603 и не молчаливое понижение до public).
    """


def _auth_level(params: dict) -> str:
    """Уровень ключа из служебного _auth (dict или AuthInfo).

    Отсутствие `_auth` → SYSTEM_LEVEL (внутренний вызов, полный доступ).
    HTTP-путь всегда инжектит `_auth` (в т.ч. level "none") → политика активна.
    """
    auth = params.get("_auth")
    if auth is None:
        return SYSTEM_LEVEL
    if isinstance(auth, dict):
        return auth.get("level") or ""
    return getattr(auth, "key_level", "") or ""


def _auth_level_of(auth) -> str:
    """Уровень из auth-носителя (AuthInfo | dict | None). None → "" (внешний)."""
    if auth is None:
        return ""
    if isinstance(auth, dict):
        return auth.get("level") or auth.get("key_level") or ""
    return getattr(auth, "key_level", "") or ""


def _auth_zone_flag(auth) -> bool:
    """Флаг zone_explicit носителя. Строго `is True` — MagicMock auto-attr
    (truthy Mock) НЕ проходит гейт; truthy-мусор тоже (fail-closed)."""
    if auth is None:
        return False
    if isinstance(auth, dict):
        return auth.get("zone_explicit") is True
    return getattr(auth, "zone_explicit", False) is True


def _auth_zone_value(auth) -> str:
    """Зона носителя (используется ТОЛЬКО при zone_explicit=True)."""
    if auth is None:
        return "public"
    if isinstance(auth, dict):
        return auth.get("zone") or "public"
    return getattr(auth, "zone", "public") or "public"


def zones_for_auth(auth) -> set[str]:
    """Допустимые зоны ЧТЕНИЯ для auth-носителя (AuthInfo | dict | None).

    Единая точка зонной политики (Ф2.0): availability.auth_zones делегирует
    сюда (parity by construction, R1 — дубль политики ликвидирован).

    - write/system → обе (не сужается);
    - subscriber → public;
    - zone_explicit=False (legacy/env/старые JSONL-строки) → {public};
    - zone_explicit=True → скоуп из zone: public→{public}, private→{private},
      both→обе; мусорная зона → {public} (fail-closed);
    - auth=None → {public} (внешний вызов без ключа — fail-closed).
    """
    level = _auth_level_of(auth)
    if level in ADMIN_LEVELS or level == SYSTEM_LEVEL:
        return {"public", "private"}
    if level == "subscriber":
        return {"public"}
    if not _auth_zone_flag(auth):
        return {"public"}
    zone = _auth_zone_value(auth)
    if zone == "private":
        return {"private"}
    if zone == "both":
        return {"public", "private"}
    return {"public"}  # "public" и мусор → fail-closed


def auth_zone_scope(params: dict) -> set[str]:
    """Скоуп зон в контексте params тула.

    Отсутствие `_auth` = внутренний system-вызов → обе зоны (см. _auth_level);
    прочее — zones_for_auth по носителю.
    """
    auth = params.get("_auth")
    if auth is None:
        return {"public", "private"}
    return zones_for_auth(auth)


def _full_access(level: str) -> bool:
    """Полный зонный доступ: admin-уровень или внутренний system-вызов."""
    return level in ADMIN_LEVELS or level == SYSTEM_LEVEL


def is_subscriber(params: dict) -> bool:
    """Subscriber-ключ (W3): принудительно контур A (public)."""
    return _auth_level(params) == "subscriber"


def is_admin(params: dict) -> bool:
    """admin-эквивалент (write/master-ключ) → полный доступ к обеим зонам."""
    return _full_access(_auth_level(params))


def zone_from_auth(params: dict, default: str = "both") -> str:
    """Резолв целевой зоны read-тула (Ф2.0 C1′: zone-scope ключа).

    Полный доступ (write/system) → прежняя семантика:
    1. явный zone ∈ (public, private) → он;
    2. zone ∈ ("both", "auto") или None → default (команда, auto-merge);
    3. иначе → ValueError (fail loud, зона не размывается).

    Прочие уровни → решётка «скоуп ключа ∩ запрошенная зона»:
    - requested ∈ (public, private): в скоупе → он; вне → ZoneAccessError
      (auth-отказ, 403-семантика — НЕ молчаливое public);
    - requested ∈ ("both", "auto", None, ""): singleton-скоуп → он
      (R2: ∩, resources.py:84 hardcode "both" → public), both-скоуп → default;
    - мусорный requested → ValueError (invalid params).

    Returns:
        "public" | "private" | "both".
    """
    if _full_access(_auth_level(params)):
        zone = params.get("zone")
        if zone in VALID_ZONES:
            return zone
        if zone in ("both", "auto"):
            return default
        if zone in (None, ""):
            return default
        raise ValueError(f"unknown zone: {zone}")

    scope = zones_for_auth(params.get("_auth"))
    requested = params.get("zone")
    if requested in VALID_ZONES:
        if requested in scope:
            return requested
        raise ZoneAccessError(
            f"key zone scope {sorted(scope)} does not include requested "
            f"zone '{requested}'"
        )
    if requested in ("both", "auto", None, ""):
        if scope == {"private"}:
            return "private"
        if scope == {"public", "private"}:
            return default
        return "public"
    raise ValueError(f"unknown zone: {requested}")


def zones_from_auth(params: dict, default: str = "both") -> list[str]:
    """Список коллекционных зон для запроса (public первым — приоритет при merge)."""
    zone = zone_from_auth(params, default)
    if zone in VALID_ZONES:
        return [zone]
    return [VALID_ZONES[0], VALID_ZONES[1]]  # both → public + private
