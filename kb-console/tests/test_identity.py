"""Тесты identity-хелперов (Ф2.4/Ф3, kb-console-roles B2).

get_current_identity — повторная верификация Basic из page-context через
UserStore-TTL-кэш (R5 закрыт: Client.request доступен в nicegui 3.15.0).
effective_role: legacy (пустой стор) → admin (бит-ин-бит 002); непустой
стор без identity → contributor (fail-closed).
"""

from __future__ import annotations

import base64

from kb_console.core.identity import (
    ROLE_LEVEL,
    api_key_for_request,
    effective_role,
    identity_from_headers,
)
from kb_console.core.users import UserStore


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class TestIdentityFromHeaders:
    def test_valid_credentials(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        identity = identity_from_headers({"authorization": _basic("alice", "pw")}, store)
        assert identity is not None
        assert identity["username"] == "alice"
        assert identity["role"] == "editor"

    def test_wrong_password_no_identity(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        assert identity_from_headers({"authorization": _basic("alice", "bad")}, store) is None

    def test_missing_header(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        assert identity_from_headers({}, store) is None


class TestEffectiveRole:
    def test_legacy_empty_store_is_admin(self, tmp_path):
        """Пустой стор = legacy-режим 002: UI ничего не скрывает."""
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        assert effective_role(None, has_users=store.has_users()) == "admin"

    def test_identity_role_wins(self, tmp_path):
        assert effective_role({"role": "editor"}, has_users=True) == "editor"

    def test_no_identity_with_users_fail_closed(self):
        """Непустой стор без identity → contributor (минимальные права)."""
        assert effective_role(None, has_users=True) == "contributor"


class TestRoleLevel:
    def test_ordering(self):
        assert ROLE_LEVEL["admin"] > ROLE_LEVEL["editor"] > ROLE_LEVEL["contributor"]


class TestApiKeyForRequest:
    def test_legacy_uses_base_key(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        assert api_key_for_request(None, base="k-base", has_users=store.has_users()) == "k-base"

    def test_identity_role_key(self, tmp_path):
        key = api_key_for_request(
            {"role": "editor"}, base="k", has_users=True,
            admin="", editor="k-ed", contributor="",
        )
        assert key == "k-ed"

    def test_identity_role_fallback_to_base(self):
        assert api_key_for_request({"role": "admin"}, base="k", has_users=True) == "k"
