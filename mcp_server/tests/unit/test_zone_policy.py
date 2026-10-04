"""P2-1 (bibliography B1): зонная политика «private = admin-only для чтения».

Единая точка истины — `auth_zone.ADMIN_LEVELS` = {"write"} (admin-эквивалент,
см. auth.check_tool_permission: write → всё; legacy master-ключ — write-уровень).

RED на старом коде: read/import/editor получали обе зоны (или private по явному
`zone=private`). После фикса — public-only для ВСЕХ уровней ниже admin.
Мутационный детектор: возврат любого уровня в ADMIN_LEVELS (или в
availability._FULL_ZONE_LEVELS через auth_zones) роняет тест.
"""

from __future__ import annotations

from mcp_server.tools.auth_zone import (
    ADMIN_LEVELS,
    is_admin,
    zone_from_auth,
    zones_from_auth,
)
from mcp_server.tools.availability import auth_zones


def test_admin_levels_only_write():
    """Множество admin-уровней = {write}; read/editor/import/subscriber исключены."""
    assert ADMIN_LEVELS == frozenset({"write"})
    assert "read" not in ADMIN_LEVELS
    assert "editor" not in ADMIN_LEVELS
    assert "import" not in ADMIN_LEVELS
    assert "subscriber" not in ADMIN_LEVELS


def test_zone_from_auth_non_admin_public_only():
    """Все уровни ниже admin → public, даже при явном zone=private/both."""
    for level in ("subscriber", "read", "import", "editor", "none", ""):
        assert zone_from_auth({"zone": "private", "_auth": {"level": level}}) == "public", level
        assert zone_from_auth({"zone": "both", "_auth": {"level": level}}) == "public", level
        assert zone_from_auth({"_auth": {"level": level}}) == "public", level


def test_zone_from_auth_admin_full_access():
    """admin (write) → явная зона или default (both)."""
    assert zone_from_auth({"zone": "private", "_auth": {"level": "write"}}) == "private"
    assert zone_from_auth({"zone": "public", "_auth": {"level": "write"}}) == "public"
    assert zone_from_auth({"_auth": {"level": "write"}}) == "both"


def test_zones_from_auth_non_admin_single_public():
    assert zones_from_auth({"_auth": {"level": "read"}}) == ["public"]
    assert zones_from_auth({"_auth": {"level": "editor"}}) == ["public"]
    assert zones_from_auth({"zone": "private", "_auth": {"level": "editor"}}) == ["public"]
    assert zones_from_auth({"_auth": {"level": "write"}}) == ["public", "private"]


def test_is_admin():
    assert is_admin({"_auth": {"level": "write"}}) is True
    assert is_admin({"_auth": {"level": "read"}}) is False
    assert is_admin({"_auth": {"level": "editor"}}) is False
    assert is_admin({"_auth": {"level": "import"}}) is False
    # Внутренний вызов (нет `_auth`) = system → полный доступ (server-side джобы);
    # HTTP всегда инжектит `_auth` (в т.ч. level "none"), поэтому политика активна.
    assert is_admin({}) is True
    assert is_admin({"_auth": {"level": "none"}}) is False


def test_auth_zones_non_admin_public_only():
    """availability.auth_zones: ниже admin → только public (мутационный детектор)."""
    for level in ("subscriber", "read", "import", "editor", "none", ""):
        assert auth_zones({"level": level}) == {"public"}, level
    assert auth_zones({"level": "write"}) == {"public", "private"}
    assert auth_zones({}) == {"public"}
