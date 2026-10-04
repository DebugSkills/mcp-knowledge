"""W3: резолв зоны read-тулов из служебного ключа params["_auth"].

mcp_handler._handle_tools_call прокидывает AuthInfo в args тула под ключом
`_auth` (W3.9, внутренний служебный ключ — НЕ в JSON Schema). Единый принцип
§2.4: каждый read-tool получает зону из auth-контекста (subscriber → forced
public) и никогда не касается чужой зоны.

Команда (read/import/write): явный параметр `zone` тула (public|private)
или default ("both" → обе зоны + merge на стороне тула).
"""

from __future__ import annotations

VALID_ZONES: tuple[str, str] = ("public", "private")

# P2-1 (bibliography): уровни ключей, которым доступны ОБЕ зоны при ЧТЕНИИ.
# admin-эквивалент = "write" (auth.check_tool_permission: write -> всё; legacy
# master-ключ MCP_API_KEY/MCP_WRITE_KEYS — write-уровень). Все прочие уровни
# (subscriber/read/import/editor/none) → public-only при чтении (private =
# admin-only). Запись политикой НЕ затрагивается (import/write/editor пишут
# private через auth-free пути).
ADMIN_LEVELS: frozenset[str] = frozenset({"write"})


def _auth_level(params: dict) -> str:
    """Уровень ключа из служебного _auth (dict или AuthInfo)."""
    auth = params.get("_auth")
    if isinstance(auth, dict):
        return auth.get("level") or ""
    return getattr(auth, "key_level", "") or ""


def is_subscriber(params: dict) -> bool:
    """Subscriber-ключ (W3): принудительно контур A (public)."""
    return _auth_level(params) == "subscriber"


def is_admin(params: dict) -> bool:
    """admin-эквивалент (write/master-ключ) → полный доступ к обеим зонам."""
    return _auth_level(params) in ADMIN_LEVELS


def zone_from_auth(params: dict, default: str = "both") -> str:
    """Резолв целевой зоны read-тула (P2-1: private = admin-only).

    Приоритет:
    1. уровень НЕ в ADMIN_LEVELS (subscriber/read/import/editor/none) →
       "public" безусловно — явный параметр `zone` НЕ переопределяет политику;
    2. admin (write) + явный zone ∈ (public, private) → он;
    3. admin + zone ∈ ("both", "auto") → default (команда, auto-merge);
    4. admin + иначе → ValueError (fail loud, зона не размывается).

    Returns:
        "public" | "private" | "both".
    """
    if _auth_level(params) not in ADMIN_LEVELS:
        return "public"
    zone = params.get("zone")
    if zone in VALID_ZONES:
        return zone
    if zone in ("both", "auto"):
        return default
    if zone in (None, ""):
        return default
    raise ValueError(f"unknown zone: {zone}")


def zones_from_auth(params: dict, default: str = "both") -> list[str]:
    """Список коллекционных зон для запроса (public первым — приоритет при merge)."""
    zone = zone_from_auth(params, default)
    if zone in VALID_ZONES:
        return [zone]
    return [VALID_ZONES[0], VALID_ZONES[1]]  # both → public + private
