"""kb-console-roles Ф2.3-2.4 (B2): middleware v2 — per-user Basic поверх 002.

Спецификация .boardData.md §7 (delta P2-5b):
- стор непуст → per-user verify (username+password, pbkdf2 в executor);
- стор пуст → legacy-режим 002 бит-в-бит (username игнорируется, пароль);
- стор НЕпуст + CONSOLE_PASSWORD задан → старый пароль отклоняется ПОЛНОСТЬЮ
  (вход по нему → 401) + warning «CONSOLE_PASSWORD игнорируется» (interlock);
- interlock v2: required + непустой стор → on (без пароля); required + пустой
  стор + пустой пароль → RuntimeError (как в 002);
- identity → scope["state"]["user"] (для Ф3 гейтов);
- WS-scope: per-user отказ → close-до-accept; lifespan → транзит.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import pytest

from kb_console.auth import (
    ConsoleAuthMiddleware,
    parse_basic_credentials,
    resolve_auth_mode,
)
from kb_console.core.users import UserStore


def _basic(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _hdr(authorization: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", authorization.encode())]


class _StubApp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)


def _make_scope(scope_type: str, path: str = "/", headers=None) -> dict:
    scope = {"type": scope_type, "path": path, "headers": headers or []}
    if scope_type == "http":
        scope.update({"method": "GET", "http_version": "1.1", "scheme": "http"})
    return scope


def _run(mw, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


@pytest.fixture
def users(tmp_path) -> UserStore:
    store = UserStore(users_file=str(tmp_path / "users.jsonl"), cache_ttl_sec=60.0)
    store.create_user("alice", "pw-alice", "editor")
    store.create_user("root", "pw-root", "admin")
    return store


def _mw(users_store, password: str = "", mode: str = "on"):
    stub = _StubApp()
    mw = ConsoleAuthMiddleware(
        app=stub, password=password, mode=mode, users=users_store, failure_delay=0.0
    )
    return mw, stub


# ── parse_basic_credentials (unit) ──────────────────────────


class TestParseBasicCredentials:
    def test_valid(self):
        assert parse_basic_credentials(_basic("alice", "pw")) == ("alice", "pw")

    def test_empty_username_kept(self):
        assert parse_basic_credentials(_basic("", "pw")) == ("", "pw")

    def test_missing_header(self):
        assert parse_basic_credentials("") is None

    def test_bad_base64(self):
        assert parse_basic_credentials("Basic !!!!") is None

    def test_non_basic_scheme(self):
        assert parse_basic_credentials("Bearer abc") is None

    def test_no_colon(self):
        token = base64.b64encode(b"nocolon").decode()
        assert parse_basic_credentials("Basic " + token) is None


# ── interlock v2 (resolve_auth_mode + users_present) ────────


class TestInterlockV2:
    def test_users_present_auto_on_without_password(self):
        assert resolve_auth_mode("", "auto", "127.0.0.1", users_present=True) == "on"

    def test_users_present_required_on_without_password(self):
        """required + непустой стор → on (юзеры есть — пароль не нужен)."""
        assert resolve_auth_mode("", "required", "0.0.0.0", users_present=True) == "on"

    def test_users_present_password_ignored_with_warning(self, caplog):
        """P2-5b: непустой стор + CONSOLE_PASSWORD → warning, режим всё равно on."""
        with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
            mode = resolve_auth_mode("legacy-pw", "auto", "127.0.0.1", users_present=True)
        assert mode == "on"
        assert any("CONSOLE_PASSWORD" in r.getMessage() for r in caplog.records)

    def test_empty_store_required_without_password_raises(self):
        with pytest.raises(RuntimeError):
            resolve_auth_mode("", "required", "0.0.0.0", users_present=False)

    def test_empty_store_legacy_matrix_unchanged(self):
        """Пустой стор: матрица 002 бит-в-бит (default users_present=False)."""
        assert resolve_auth_mode("", "auto", "127.0.0.1") == "off"
        assert resolve_auth_mode("pw", "auto", "127.0.0.1") == "on"
        assert resolve_auth_mode("", "off", "0.0.0.0") == "off"

    def test_invalid_mode_value_error(self):
        with pytest.raises(ValueError):
            resolve_auth_mode("", "sometimes", "127.0.0.1", users_present=True)


# ── middleware: per-user ветка (стор непуст) ────────────────


class TestMiddlewarePerUser:
    def test_correct_credentials_pass(self, users):
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("alice", "pw-alice"))))
        assert len(stub.calls) == 1
        assert sent == []
        # identity в scope-state (для Ф3)
        user = stub.calls[0].get("state", {}).get("user")
        assert user is not None and user["username"] == "alice" and user["role"] == "editor"

    def test_wrong_password_401(self, users):
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("alice", "wrong"))))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_unknown_user_401(self, users):
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("ghost", "pw"))))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_legacy_password_rejected_when_users_present(self, users):
        """P2-5b: вход по CONSOLE_PASSWORD при непустом сторе → 401."""
        mw, stub = _mw(users, password="legacy-pw")
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("admin", "legacy-pw"))))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_no_credentials_401_challenge(self, users):
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("http", "/status"))
        assert stub.calls == []
        headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
        assert headers["www-authenticate"] == 'Basic realm="kb-console"'

    def test_websocket_reject_close_before_accept(self, users):
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("websocket", "/_nicegui_ws/", headers=_hdr(_basic("alice", "bad"))))
        assert stub.calls == []
        assert sent == [{"type": "websocket.close", "code": 1008, "reason": "unauthorized"}]

    def test_websocket_correct_pass(self, users):
        mw, stub = _mw(users)
        _run(mw, _make_scope("websocket", "/_nicegui_ws/", headers=_hdr(_basic("root", "pw-root"))))
        assert len(stub.calls) == 1

    def test_lifespan_transparent(self, users):
        mw, stub = _mw(users)
        _run(mw, {"type": "lifespan"})
        assert len(stub.calls) == 1

    def test_mode_off_transparent(self, users):
        mw, stub = _mw(users, mode="off")
        _run(mw, _make_scope("http", "/status"))
        assert len(stub.calls) == 1

    def test_inactive_user_401(self, users):
        users.set_active("alice", False)
        mw, stub = _mw(users)
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("alice", "pw-alice"))))
        assert stub.calls == []
        assert sent[0]["status"] == 401


# ── middleware: пустой стор → legacy 002 бит-в-бит ──────────


class TestMiddlewareLegacyFallback:
    def test_empty_store_legacy_password_works(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        mw, stub = _mw(store, password="legacy-pw")
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("anyone", "legacy-pw"))))
        assert len(stub.calls) == 1
        assert sent == []

    def test_empty_store_username_ignored(self, tmp_path):
        """002: username игнорируется в legacy-режиме."""
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        mw, stub = _mw(store, password="legacy-pw")
        _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("zzz", "legacy-pw"))))
        assert len(stub.calls) == 1

    def test_empty_store_wrong_password_401(self, tmp_path):
        store = UserStore(users_file=str(tmp_path / "users.jsonl"))
        mw, stub = _mw(store, password="legacy-pw")
        sent = _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("op", "wrong"))))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_users_none_fully_legacy(self):
        """users=None (старый конструктор 002) — поведение не меняется."""
        stub = _StubApp()
        mw = ConsoleAuthMiddleware(app=stub, password="legacy-pw", mode="on", failure_delay=0.0)
        _run(mw, _make_scope("http", "/status", headers=_hdr(_basic("op", "legacy-pw"))))
        assert len(stub.calls) == 1
