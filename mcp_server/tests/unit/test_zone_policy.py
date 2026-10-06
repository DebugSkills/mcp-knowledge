"""Зонная политика read-тулов: P2-1 (private=admin-only) + Ф2.0 (C1′ zone-scope).

Единая точка истины — auth_zone.zones_for_auth (Ф2.0): write/system → обе;
subscriber → public; zone_explicit=False (legacy/env) → public (fail-closed);
zone_explicit=True → скоуп из носителя. Решётка: запрошенная зона ∩ скоуп;
вне скоупа → ZoneAccessError (403-семантика, НЕ молчаливое public).

Мутационные детекторы:
- возврат любого уровня в ADMIN_LEVELS роняет test_admin_levels_only_write;
- honour-ние зоны БЕЗ флага (убрать `is True`-гейт) роняет legacy-кейсы;
- возврат к молчаливому public вне скоупа роняет fail-loud-кейсы.
"""

from __future__ import annotations

import pytest
from mcp_server.auth import AuthInfo
from mcp_server.tools.auth_zone import (
    ADMIN_LEVELS,
    ZoneAccessError,
    auth_zone_scope,
    is_admin,
    zone_from_auth,
    zones_for_auth,
    zones_from_auth,
)

# ── Реальные носители (RealType-контракт, не MagicMock) ───────


def _auth(level: str, zone: str = "both", explicit: bool = False, **kw) -> AuthInfo:
    return AuthInfo(
        authenticated=True, key_level=level, zone=zone,
        zone_explicit=explicit, **kw,
    )


# ── ADMIN_LEVELS ──────────────────────────────────────────────


def test_admin_levels_only_write():
    """Множество admin-уровней = {write}; read/editor/import/subscriber исключены."""
    assert ADMIN_LEVELS == frozenset({"write"})
    assert "read" not in ADMIN_LEVELS
    assert "editor" not in ADMIN_LEVELS
    assert "import" not in ADMIN_LEVELS
    assert "subscriber" not in ADMIN_LEVELS


# ── Матрица zone_from_auth (Ф2.0 C1′) ─────────────────────────


def test_explicit_read_private_scope_resolves_private():
    """read+private explicit → private (дефект C1′ закрыт: зона honour-ится)."""
    assert zone_from_auth({"_auth": _auth("read", "private", explicit=True)}) == "private"
    assert zones_from_auth({"_auth": _auth("read", "private", explicit=True)}) == ["private"]


def test_explicit_read_both_scope_resolves_both():
    """read+both explicit (сервисный ключ верстака) → обе зоны."""
    assert zone_from_auth({"_auth": _auth("read", "both", explicit=True)}) == "both"
    assert zones_from_auth({"_auth": _auth("read", "both", explicit=True)}) == ["public", "private"]


def test_explicit_public_scope_resolves_public():
    assert zone_from_auth({"_auth": _auth("read", "public", explicit=True)}) == "public"


def test_out_of_scope_request_fail_loud():
    """Вне скоупа (public-scope + запрос private) → auth-отказ, НЕ молчание.

    Мутационный детектор: возврат «public» вместо raise роняет тест.
    """
    # legacy-ключ (без флага, скоуп public) явно просит private
    with pytest.raises(ZoneAccessError):
        zone_from_auth({"zone": "private", "_auth": {"level": "read"}})
    # явный public-scope ключ просит private
    with pytest.raises(ZoneAccessError):
        zone_from_auth({"zone": "private", "_auth": _auth("read", "public", explicit=True)})
    # private-scope ключ просит public (∩ пуст)
    with pytest.raises(ZoneAccessError):
        zone_from_auth({"zone": "public", "_auth": _auth("read", "private", explicit=True)})
    # subscriber — та же решётка (явный private вне public-скоупа)
    with pytest.raises(ZoneAccessError):
        zone_from_auth({"zone": "private", "_auth": {"level": "subscriber"}})


def test_zone_access_error_is_permission_error():
    """403-семантика: ZoneAccessError ⊂ PermissionError (НЕ ValueError/-32603)."""
    assert issubclass(ZoneAccessError, PermissionError)
    assert not issubclass(ZoneAccessError, ValueError)


def test_write_full_access():
    """write → все зоны (не сужается), прежняя семантика явного zone."""
    assert zone_from_auth({"zone": "private", "_auth": {"level": "write"}}) == "private"
    assert zone_from_auth({"zone": "public", "_auth": {"level": "write"}}) == "public"
    assert zone_from_auth({"_auth": {"level": "write"}}) == "both"
    # write-носитель с любым zone/флагом → полный доступ
    assert zones_from_auth({"_auth": _auth("write", "public", explicit=True)}) == ["public", "private"]


def test_subscriber_public():
    """subscriber → public (без явного запроса зоны)."""
    assert zone_from_auth({"_auth": {"level": "subscriber"}}) == "public"
    assert zone_from_auth({"zone": "both", "_auth": {"level": "subscriber"}}) == "public"


def test_missing_auth_is_system_full_access():
    """`_auth` нет → внутренний system-вызов → обе зоны."""
    assert zone_from_auth({}) == "both"
    assert zones_from_auth({}) == ["public", "private"]


def test_legacy_levels_without_flag_public():
    """Legacy read/import/editor/none без флага → public (fail-closed C1′).

    Мутационный детектор: honour-ние зоны без zone_explicit роняет тест.
    """
    for level in ("read", "import", "editor", "none", ""):
        auth = _auth(level, zone="both", explicit=False)
        assert zone_from_auth({"_auth": auth}) == "public", level
        assert zones_from_auth({"_auth": auth}) == ["public"], level


def test_dict_auth_without_zone_public():
    """dict-_auth без zone/флага → fail-closed public."""
    assert zone_from_auth({"_auth": {"level": "read"}}) == "public"
    assert zone_from_auth({"zone": "both", "_auth": {"level": "editor"}}) == "public"


def test_request_both_from_singleton_scope_is_intersection():
    """R2: запрос zone="both" от singleton-скоупа → ∩ = скоуп, НЕ отказ.

    resources.py:84 хардкодит zone="both" — public-ключ получает public.
    """
    assert zone_from_auth(
        {"zone": "both", "_auth": _auth("read", "public", explicit=True)}
    ) == "public"
    assert zone_from_auth(
        {"zone": "both", "_auth": _auth("read", "private", explicit=True)}
    ) == "private"
    assert zone_from_auth({"zone": "both", "_auth": {"level": "read"}}) == "public"
    assert zone_from_auth({"zone": "auto", "_auth": _auth("read", "private", explicit=True)}) == "private"


def test_request_zone_from_both_scope():
    """read+both explicit + явный запрос public/private → он (∩)."""
    assert zone_from_auth(
        {"zone": "private", "_auth": _auth("read", "both", explicit=True)}
    ) == "private"
    assert zone_from_auth(
        {"zone": "public", "_auth": _auth("read", "both", explicit=True)}
    ) == "public"


def test_unknown_zone_value_raises_value_error():
    """Мусорное значение zone → ValueError (invalid params), не auth-отказ."""
    with pytest.raises(ValueError):
        zone_from_auth({"zone": "garbage", "_auth": {"level": "write"}})
    with pytest.raises(ValueError):
        zone_from_auth({"zone": "garbage", "_auth": {"level": "read"}})


# ── zones_for_auth: единая точка политики ─────────────────────


def test_zones_for_auth_matrix():
    assert zones_for_auth(None) == {"public"}
    assert zones_for_auth({}) == {"public"}
    assert zones_for_auth({"level": "write"}) == {"public", "private"}
    assert zones_for_auth({"level": "subscriber"}) == {"public"}
    assert zones_for_auth({"level": "read"}) == {"public"}
    assert zones_for_auth({"level": "read", "zone": "both", "zone_explicit": True}) == {"public", "private"}
    assert zones_for_auth({"level": "read", "zone": "private", "zone_explicit": True}) == {"private"}
    assert zones_for_auth({"level": "read", "zone": "both", "zone_explicit": False}) == {"public"}
    # зона без флага НЕ honour-ится (детектор снятия `is True`-гейта)
    assert zones_for_auth({"level": "read", "zone": "private"}) == {"public"}
    # мусорная зона при явном флаге → fail-closed public
    assert zones_for_auth({"level": "read", "zone": "garbage", "zone_explicit": True}) == {"public"}
    assert zones_for_auth(_auth("read", "both", explicit=True)) == {"public", "private"}


def test_zones_for_auth_mock_robust():
    """MagicMock auto-attr zone_explicit (truthy Mock) НЕ проходит гейт `is True`."""
    from unittest.mock import MagicMock

    mock_auth = MagicMock(key_level="read", zone="both")
    assert zones_for_auth(mock_auth) == {"public"}


def test_auth_zone_scope_params_context():
    """auth_zone_scope: params без _auth → system (обе); с _auth → скоуп ключа."""
    assert auth_zone_scope({}) == {"public", "private"}
    assert auth_zone_scope({"_auth": _auth("read", "private", explicit=True)}) == {"private"}
    assert auth_zone_scope({"_auth": {"level": "read"}}) == {"public"}
    assert auth_zone_scope({"_auth": {"level": "write"}}) == {"public", "private"}


# ── is_admin (без изменений) ──────────────────────────────────


def test_is_admin():
    assert is_admin({"_auth": {"level": "write"}}) is True
    assert is_admin({"_auth": {"level": "read"}}) is False
    assert is_admin({"_auth": {"level": "editor"}}) is False
    assert is_admin({"_auth": {"level": "import"}}) is False
    # Внутренний вызов (нет `_auth`) = system → полный доступ (server-side джобы);
    # HTTP всегда инжектит `_auth` (в т.ч. level "none"), поэтому политика активна.
    assert is_admin({}) is True
    assert is_admin({"_auth": {"level": "none"}}) is False
