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


def _auth_level(params: dict) -> str:
    """Уровень ключа из служебного _auth (dict или AuthInfo)."""
    auth = params.get("_auth")
    if isinstance(auth, dict):
        return auth.get("level") or ""
    return getattr(auth, "key_level", "") or ""


def is_subscriber(params: dict) -> bool:
    """Subscriber-ключ (W3): принудительно контур A (public)."""
    return _auth_level(params) == "subscriber"


def zone_from_auth(params: dict, default: str = "both") -> str:
    """Резолв целевой зоны read-тула.

    Приоритет:
    1. subscriber → "public" (явный параметр zone игнорируется);
    2. явный zone ∈ (public, private) → он;
    3. zone ∈ ("both", "auto") → default (команда, auto-merge);
    4. иначе → ValueError (fail loud, зона не размывается).

    Returns:
        "public" | "private" | "both".
    """
    if is_subscriber(params):
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
