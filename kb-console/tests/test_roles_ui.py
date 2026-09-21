"""Тесты UI-гейтов Ф3 (kb-console-roles B2): ROUTES-матрица + self-lockout guard.

Матрица «роль × страница/действие» — докстринг-SSOT в pages/users_page.py;
здесь механическая проверка min_role в ROUTES и is_last_active_admin.
"""

from __future__ import annotations

from pathlib import Path

from kb_console.core.users import UserStore
from kb_console.pages import PAGES, ROUTES
from kb_console.pages.users_page import is_last_active_admin


class TestRoutesMatrix:
    def test_min_role_admin_pages(self):
        admin_pages = {p for p, _, _, mr in ROUTES if mr == "admin"}
        assert admin_pages == {"/tokens", "/users"}

    def test_common_pages_contributor(self):
        common = {p for p, _, _, mr in ROUTES if mr == "contributor"}
        assert common == {"/status", "/books", "/import", "/search", "/quality"}

    def test_all_roles_valid(self):
        from kb_console.core.identity import ROLE_LEVEL

        for _p, _l, _b, mr in ROUTES:
            assert mr in ROLE_LEVEL, f"неизвестная min_role: {mr}"

    def test_paths_unique(self):
        paths = [p for p, _, _, _ in ROUTES]
        assert len(paths) == len(set(paths))

    def test_pages_backcompat_2tuple(self):
        assert all(len(x) == 2 for x in PAGES)
        assert len(PAGES) == len(ROUTES)

    def test_users_route_registered(self):
        assert any(p == "/users" for p, _, _, _ in ROUTES)


class TestLastActiveAdminGuard:
    def test_deactivate_last_admin_blocked(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        assert is_last_active_admin(store, "root", new_active=False) is True

    def test_downgrade_last_admin_blocked(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        assert is_last_active_admin(store, "root", new_role="editor") is True

    def test_second_admin_allows(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        store.create_user("root2", "pw", "admin")
        assert is_last_active_admin(store, "root", new_active=False) is False

    def test_inactive_target_not_guarded(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        store.create_user("old", "pw", "admin")
        store.set_active("old", False, actor="root")
        assert is_last_active_admin(store, "old", new_active=False) is False

    def test_non_admin_target_not_guarded(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        store.create_user("ed", "pw", "editor")
        assert is_last_active_admin(store, "ed", new_active=False) is False

    def test_promote_to_admin_is_fine(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        store.create_user("ed", "pw", "editor")
        assert is_last_active_admin(store, "ed", new_role="admin") is False

    def test_unknown_user_not_guarded(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("root", "pw", "admin")
        assert is_last_active_admin(store, "ghost", new_active=False) is False


class TestLegacyRoleDefaults:
    def test_current_role_outside_request_is_admin(self):
        """Вне nicegui-запроса identity нет; пустой стор (tmp) → legacy admin.

        _users_store() подтягивает USERS_STORE из app — в unit-контексте без
        env CONSOLE_USERS_FILE стор указывает на /app/data/console (несуществует
        в тест-среде) → has_users()=False → legacy → 'admin' (бит-ин-бит 002).
        """
        import os

        os.environ.pop("CONSOLE_USERS_FILE", None)
        from kb_console.core.identity import current_role

        assert current_role() == "admin"
