"""Тесты core/storage_secret.py (035, план §3б / P2-5).

Лестница: env CONSOLE_STORAGE_SECRET → файл <dir(CONSOLE_USERS_FILE)>/storage_secret
(chmod 0600, атомарная запись tmp+os.replace) → ephemeral + WARNING.
Секрет НЕ логируется.
"""

from __future__ import annotations

import logging
import os
import stat

from kb_console.core.storage_secret import resolve_storage_secret


class TestEnvPriority:
    def test_env_wins_over_file(self, tmp_path):
        path = tmp_path / "storage_secret"
        path.write_text("file-secret", encoding="utf-8")
        secret = resolve_storage_secret("env-secret", base_path=str(tmp_path / "users.jsonl"))
        assert secret == "env-secret"
        # файл не перезаписан env-значением
        assert path.read_text(encoding="utf-8") == "file-secret"


class TestFilePersist:
    def test_first_start_generates_and_persists(self, tmp_path):
        secret = resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        assert secret and len(secret) >= 32
        stored = (tmp_path / "storage_secret").read_text(encoding="utf-8").strip()
        assert stored == secret

    def test_reread_stable_across_calls(self, tmp_path):
        first = resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        second = resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        assert first == second

    def test_file_permissions_0600(self, tmp_path):
        resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        mode = stat.S_IMODE((tmp_path / "storage_secret").stat().st_mode)
        assert mode == 0o600

    def test_no_tmp_leftover(self, tmp_path):
        resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "storage_secret"]
        assert leftovers == []

    def test_generated_is_urlsafe_token(self, tmp_path):
        secret = resolve_storage_secret("", base_path=str(tmp_path / "users.jsonl"))
        # token_urlsafe-алфавит: A-Z a-z 0-9 - _
        assert all(c.isalnum() or c in "-_" for c in secret)


class TestEphemeralFallback:
    def test_missing_dir_ephemeral_with_warning(self, tmp_path, caplog, monkeypatch):
        """Родительской директории нет (RO/volume не смонтирован) → WARNING +
        ephemeral-секрет (деградация, не крах); новых директорий не создаём."""
        base = str(tmp_path / "no" / "such" / "dir" / "users.jsonl")
        with caplog.at_level(logging.WARNING, logger="kb_console.storage_secret"):
            secret = resolve_storage_secret("", base_path=base)
        assert secret and len(secret) >= 32
        assert not (tmp_path / "no").exists(), "mkdir не делаем: volume обязан существовать"
        assert any("storage_secret" in r.getMessage() for r in caplog.records)

    def test_ephemeral_unique_per_call(self, tmp_path):
        base = str(tmp_path / "ro" / "users.jsonl")
        a = resolve_storage_secret("", base_path=base)
        b = resolve_storage_secret("", base_path=base)
        assert a != b

    def test_readonly_dir_ephemeral(self, tmp_path, monkeypatch):
        """Директория есть, но запись падает (RO fs) → ephemeral + WARNING."""
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            base = str(ro / "users.jsonl")
            secret = resolve_storage_secret("", base_path=base)
            assert secret and len(secret) >= 32
            assert not (ro / "storage_secret").exists()
        finally:
            os.chmod(ro, 0o700)

    def test_secret_not_logged(self, tmp_path, caplog):
        """P2-5: значение секрета не попадает в логи (даже в WARNING)."""
        base = str(tmp_path / "no" / "dir" / "users.jsonl")
        with caplog.at_level(logging.WARNING, logger="kb_console.storage_secret"):
            secret = resolve_storage_secret("", base_path=base)
        for record in caplog.records:
            assert secret not in record.getMessage()
