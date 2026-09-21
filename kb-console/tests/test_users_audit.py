"""Тесты users_audit.jsonl (Ф3.3, kb-console-roles B2).

Спека: users_audit.jsonl — отдельный от серверного audit.jsonl журнал
действий консоли: login_ok/login_fail, user_create/user_deactivate/
user_reset, role_change. Пишется UserStore при мутациях (actor) и
middleware при логинах.
"""

from __future__ import annotations

import json
from pathlib import Path

from kb_console.core.users import UserStore


class TestUsersAudit:
    def test_no_audit_file_until_first_event(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        assert not store.audit_path.exists()

    def test_create_user_writes_audit(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        lines = store.audit_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "user_create"
        assert rec["actor"] == "admin"
        assert rec["target"] == "alice"
        assert rec["details"]["role"] == "editor"
        assert rec["ts"]

    def test_default_actor_is_system(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor")  # bootstrap/system-путь
        rec = json.loads(store.audit_path.read_text(encoding="utf-8").splitlines()[0])
        assert rec["actor"] == "system"

    def test_set_password_writes_user_reset(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        store.set_password("alice", "pw2", actor="admin")
        events = [
            json.loads(x)["event"]
            for x in store.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert events == ["user_create", "user_reset"]

    def test_set_role_writes_role_change_with_old_new(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        store.set_role("alice", "admin", actor="admin")
        lines = store.audit_path.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        assert rec["event"] == "role_change"
        assert rec["details"] == {"old": "editor", "new": "admin"}

    def test_set_active_writes_user_deactivate_and_activate(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        store.set_active("alice", False, actor="admin")
        store.set_active("alice", True, actor="admin")
        events = [
            json.loads(x)["event"]
            for x in store.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert events == ["user_create", "user_deactivate", "user_activate"]

    def test_log_login_ok_and_fail(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        store.log_login("alice", ok=True)
        store.log_login("alice", ok=False)
        lines = store.audit_path.read_text(encoding="utf-8").splitlines()
        ok = json.loads(lines[-2])
        fail = json.loads(lines[-1])
        assert ok["event"] == "login_ok" and ok["target"] == "alice"
        assert fail["event"] == "login_fail" and fail["target"] == "alice"

    def test_audit_default_path_is_sibling(self, tmp_path: Path):
        store = UserStore(users_file=str(tmp_path / "sub" / "users.jsonl"))
        assert store.audit_path == tmp_path / "sub" / "users_audit.jsonl"

    def test_custom_audit_path(self, tmp_path: Path):
        store = UserStore(
            users_file=str(tmp_path / "users.jsonl"),
            audit_file=str(tmp_path / "audit.jsonl"),
        )
        store.create_user("alice", "pw", "editor", actor="admin")
        assert (tmp_path / "audit.jsonl").exists()

    def test_failed_mutation_does_not_write_audit(self, tmp_path: Path):
        """Дубликат username бросает ДО записи — аудит не polluted."""
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        store.create_user("alice", "pw", "editor", actor="admin")
        try:
            store.create_user("alice", "pw", "editor", actor="admin")
        except ValueError:
            pass
        lines = store.audit_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
