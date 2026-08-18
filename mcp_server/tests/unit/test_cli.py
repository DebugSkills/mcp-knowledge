"""Тесты CLI токенов (W4.5, план two-zone-access §2.3).

Покрытие: token create (формат mcp_<l><z>_ + plaintext один раз, hash-only
в сторе), принудительная зона subscriber, 🔴-предупреждение write/import,
list --json, revoke, rotate. Вывод в тестах plain (stdout не tty) — ANSI
проверяется smoke-прогоном.
"""

from __future__ import annotations

import json
import re

import pytest

from mcp_server.cli import cmd_token
from mcp_server.token_store import TokenStore

TOKEN_RE = re.compile(r"^mcp_[sriw][abx]_[A-Za-z0-9]{32}$")


def _run(argv: list[str], capsys) -> str:
    cmd_token(argv)
    return capsys.readouterr().out


def _tokens_dir(tmp_path) -> str:
    return str(tmp_path / "tokens")


def _extract_key(out: str) -> str:
    for line in out.splitlines():
        if "🔑" in line:
            return line.split()[-1]
    raise AssertionError(f"plaintext-ключ не найден в выводе:\n{out}")


def _create(tmp_path, capsys, level="subscriber", zone="public", note="тест"):
    out = _run(
        ["create", "--level", level, "--zone", zone, "--note", note,
         "--tokens-dir", _tokens_dir(tmp_path)],
        capsys,
    )
    return out


class TestTokenCreate:
    def test_create_prints_key_once_and_stores_hash_only(self, tmp_path, capsys):
        out = _create(tmp_path, capsys)
        key = _extract_key(out)
        assert TOKEN_RE.match(key), f"неверный формат ключа: {key}"
        assert out.count(key) == 1  # plaintext печатается ОДИН раз
        raw = (tmp_path / "tokens" / "tokens.jsonl").read_text(encoding="utf-8")
        assert key not in raw  # в сторе только sha256
        assert "✅" in out and "подписчик" in out and "только public" in out

    def test_create_subscriber_forces_public_zone(self, tmp_path, capsys):
        out = _create(tmp_path, capsys, zone="private")
        key = _extract_key(out)
        assert key.startswith("mcp_sa_")
        store = TokenStore(tokens_dir=_tokens_dir(tmp_path))
        assert store.list()[0].zone == "public"

    def test_create_write_shows_team_only_warning(self, tmp_path, capsys):
        out = _create(tmp_path, capsys, level="write", zone="both")
        assert "mcp_wx_" in out
        assert "ВНИМАНИЕ" in out and "ТОЛЬКО команде" in out


class TestTokenList:
    def test_list_json_machine_readable(self, tmp_path, capsys):
        _create(tmp_path, capsys)
        _create(tmp_path, capsys, level="read", zone="both")
        out = _run(["list", "--json", "--tokens-dir", _tokens_dir(tmp_path)], capsys)
        data = json.loads(out)
        assert len(data) == 2
        assert {rec["level"] for rec in data} == {"subscriber", "read"}
        assert all(rec["status"] == "active" for rec in data)

    def test_list_table_shows_rows(self, tmp_path, capsys):
        _create(tmp_path, capsys, note="Boosty Иван")
        out = _run(["list", "--tokens-dir", _tokens_dir(tmp_path)], capsys)
        assert "Уровень" in out and "Статус" in out and "subscriber" in out
        assert "Boosty Иван" in out


class TestTokenRevoke:
    def test_revoke_sets_inactive(self, tmp_path, capsys):
        _create(tmp_path, capsys)
        store = TokenStore(tokens_dir=_tokens_dir(tmp_path))
        token_id = store.list()[0].id
        out = _run(["revoke", token_id, "--tokens-dir", _tokens_dir(tmp_path)], capsys)
        assert "отозван" in out
        # свежий инстанс: TTL-кэш старого не видит CLI-мутацию
        fresh = TokenStore(tokens_dir=_tokens_dir(tmp_path))
        assert fresh.get(token_id).active is False

    def test_revoke_unknown_exits_1(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            _run(["revoke", "tok_missing", "--tokens-dir", _tokens_dir(tmp_path)], capsys)
        assert exc_info.value.code == 1


class TestTokenRotate:
    def test_rotate_revokes_old_and_creates_same_params(self, tmp_path, capsys):
        _create(tmp_path, capsys, level="import", zone="private", note="Boosty Иван")
        store = TokenStore(tokens_dir=_tokens_dir(tmp_path))
        old = store.list()[0]
        out = _run(["rotate", old.id, "--tokens-dir", _tokens_dir(tmp_path)], capsys)
        # свежий инстанс: TTL-кэш старого не видит CLI-мутацию
        fresh = TokenStore(tokens_dir=_tokens_dir(tmp_path))
        records = fresh.list()
        assert len(records) == 2
        assert fresh.get(old.id).active is False
        new = next(rec for rec in records if rec.id != old.id)
        assert (new.level, new.zone, new.note) == ("import", "private", "Boosty Иван")
        assert "mcp_ib_" in out
        assert out.count(_extract_key(out)) == 1  # новый plaintext один раз
