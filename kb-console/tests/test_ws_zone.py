"""Ф2 (ai-workspace) шаг #7: ws-зонный резолвер роль→зона (D7, I5/I6).

Инварианты:
- SSOT-согласованность с политикой консоли (test_role_zone_matrix):
  «private → только admin» — editor/contributor/None/unknown → public;
- fail-closed: отсутствие/неизвестность роли НЕ открывает private (D7);
- zone_for_identity переиспользует effective_role (legacy=admin→private,
  fail-closed→contributor→public) — новая роль-логика не вводится (I5).

Мутационный детектор M1 (демо): fail-open дефолт (unknown→private)
обязан ловиться инвариантом — снятие fail-closed краснит тест.
"""

from __future__ import annotations

import pytest

from kb_console.core.users import ROLES
from kb_console.core.ws_zone import (
    WS_ZONE_BY_ROLE,
    zone_for_identity,
    zone_for_role,
    zones_for_role,
)

ROLES_ZONES = [
    ("admin", "private"),
    ("editor", "public"),
    ("contributor", "public"),
]


class TestZoneByRole:
    @pytest.mark.parametrize(("role", "zone"), ROLES_ZONES)
    def test_known_roles(self, role, zone):
        assert zone_for_role(role) == zone

    @pytest.mark.parametrize(
        "role", [None, "", "hacker", "ADMIN", "super-admin", "admin ", "root", 1]
    )
    def test_unknown_or_missing_role_fails_closed_to_public(self, role):
        """None/неизвестная/битая роль → public: private ниже admin недоступен (D7)."""
        assert zone_for_role(role) == "public"


class TestZonesForRole:
    def test_admin_sees_both_zones(self):
        assert zones_for_role("admin") == ("private", "public")

    @pytest.mark.parametrize("role", ["editor", "contributor", None, "ghost"])
    def test_below_admin_public_only(self, role):
        zones = zones_for_role(role)
        assert zones == ("public",)
        assert "private" not in zones


class TestConsistencyWithConsoleMatrix:
    def test_every_console_role_has_zone_mapping(self):
        """WS_ZONE_BY_ROLE покрывает роли консоли (users.ROLES) и только их."""
        assert set(WS_ZONE_BY_ROLE) == set(ROLES)

    def test_private_is_admin_only(self):
        """SSOT-согласованность с test_role_zone_matrix: private → только admin."""
        private_roles = [r for r, z in WS_ZONE_BY_ROLE.items() if z == "private"]
        assert private_roles == ["admin"]

    @pytest.mark.parametrize(("role", "zone"), ROLES_ZONES)
    def test_zone_for_role_matches_ssot_dict(self, role, zone):
        """zone_for_role — честная проекция SSOT-словаря для известных ролей."""
        assert zone_for_role(role) == WS_ZONE_BY_ROLE[role] == zone


class TestZoneForIdentity:
    @pytest.mark.parametrize(("role", "zone"), ROLES_ZONES)
    def test_identity_role(self, role, zone):
        ident = {"id": "usr_x", "username": "u", "role": role}
        assert zone_for_identity(ident, has_users=True) == zone

    def test_legacy_no_users_is_admin_private(self):
        """legacy (пустой стор, identity=None) → admin → private (бит-ин-бит 002/I5)."""
        assert zone_for_identity(None, has_users=False) == "private"

    def test_fail_closed_no_identity_with_users_is_public(self):
        """Непустой стор без identity → contributor → public (fail-closed)."""
        assert zone_for_identity(None, has_users=True) == "public"

    def test_delegates_to_effective_role(self, monkeypatch):
        """zone_for_identity НЕ вводит свою роль-логику: делегирует effective_role."""
        from kb_console.core import ws_zone

        seen = {}

        def fake_effective_role(identity, *, has_users):
            seen["args"] = (identity, has_users)
            return "editor"

        monkeypatch.setattr(ws_zone, "effective_role", fake_effective_role)
        assert ws_zone.zone_for_identity({"role": "admin"}, has_users=True) == "public"
        assert seen["args"] == ({"role": "admin"}, True)


class TestMutationDetectors:
    def test_m1_fail_open_default_detected(self):
        """M1 (демо): мутант без fail-closed (дефолт 'private') нарушает инвариант —
        ассерт инварианта обязан падать на мутанте; живой резолвер держит."""

        def mutant(role):
            return WS_ZONE_BY_ROLE.get(role or "", "private")  # fail-open дефолт

        with pytest.raises(AssertionError):
            assert mutant("ghost-role") == "public"  # инвариант ловит мутанта
        assert zone_for_role("ghost-role") == "public"  # живой код — держит

    def test_m2_drop_unknown_still_public(self, monkeypatch):
        """M2 (демо+откат): даже «подброшенная» приватная роль в SSOT-словаре
        не открывает private через зоны ниже admin (зоны = производная роли)."""
        from kb_console.core import ws_zone

        monkeypatch.setattr(
            ws_zone, "WS_ZONE_BY_ROLE", {**WS_ZONE_BY_ROLE, "ghost": "private"}
        )
        assert ws_zone.zone_for_role("ghost") == "private"  # сам маппинг сработал
        assert ws_zone.zones_for_role("editor") == ("public",)  # editor не расширился
        assert ws_zone.zones_for_role("contributor") == ("public",)
