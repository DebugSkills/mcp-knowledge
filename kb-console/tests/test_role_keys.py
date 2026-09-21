"""Тесты маппинга роль→MCP-ключ (Ф3.1, kb-console-roles B2).

Спека §7 / план Ф3.1: MCP_API_KEY_ADMIN/EDITOR/CONTRIBUTOR c единым
fallback на MCP_API_KEY (legacy-инсталляции с одним ключом работают
бит-в-бит: все роли получают тот же ключ). Ключи не логируются.
"""

from __future__ import annotations

from kb_console.config import api_key_for_role


class TestApiKeyForRole:
    def test_fallback_single_key_all_roles(self):
        """Без per-role env ВСЕ роли получают MCP_API_KEY (legacy бит-в-бит)."""
        key = api_key_for_role(
            "admin", base="k-base", admin="", editor="", contributor=""
        )
        assert key == "k-base"
        assert (
            api_key_for_role("editor", base="k-base", admin="", editor="", contributor="")
            == "k-base"
        )
        assert (
            api_key_for_role("contributor", base="k-base", admin="", editor="", contributor="")
            == "k-base"
        )

    def test_admin_key_env(self):
        """MCP_API_KEY_ADMIN перекрывает base только для admin."""
        assert (
            api_key_for_role("admin", base="k", admin="k-adm", editor="", contributor="")
            == "k-adm"
        )
        assert (
            api_key_for_role("editor", base="k", admin="k-adm", editor="", contributor="")
            == "k"
        )

    def test_editor_key_env(self):
        assert (
            api_key_for_role("editor", base="k", admin="", editor="k-ed", contributor="")
            == "k-ed"
        )

    def test_contributor_key_env(self):
        assert (
            api_key_for_role(
                "contributor", base="k", admin="", editor="", contributor="k-contr"
            )
            == "k-contr"
        )

    def test_all_three_envs(self):
        assert (
            api_key_for_role("admin", base="k", admin="a1", editor="e1", contributor="c1")
            == "a1"
        )
        assert (
            api_key_for_role("editor", base="k", admin="a1", editor="e1", contributor="c1")
            == "e1"
        )
        assert (
            api_key_for_role("contributor", base="k", admin="a1", editor="e1", contributor="c1")
            == "c1"
        )

    def test_unknown_role_falls_back_to_base(self):
        """Неизвестная роль → базовый ключ (безопасный дефолт, не пустота)."""
        assert (
            api_key_for_role("hacker", base="k-base", admin="a", editor="e", contributor="c")
            == "k-base"
        )
