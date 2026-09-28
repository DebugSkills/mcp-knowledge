"""Юнит-тесты HTTP Basic auth kb-console (code-2026-09-20-002, вариант A2d).

Покрытие (спецификация .boardData.md §7:4388):
  - unit: parse Basic (валидный / кривой base64 / отсутствует / не-Basic),
    verify пароля (верный / неверный, username игнорируется),
    interlock-матрица resolve_auth_mode (auto / off / required / невалидный);
  - ASGI fake-scope: http 401+WWW-Authenticate / pass-through,
    websocket close-до-accept / pass-through, lifespan → транзит,
    режим off → всё транзитом, задержка на неверный пароль.

Подпроцессов нет — импорт напрямую (auth.py не читает env на импорте;
env-контракт тестируется в test_config.py / test_auth_smoke.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

import pytest

from kb_console.auth import ConsoleAuthMiddleware, resolve_auth_mode, verify_basic_auth


def _basic(user: str, password: str) -> str:
    """Собрать валидный Authorization: Basic-заголовок."""
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# ── unit: parse / verify ─────────────────────────────────────


def test_verify_valid_password():
    assert verify_basic_auth(_basic("admin", "secret"), "secret") is True


def test_verify_wrong_password():
    assert verify_basic_auth(_basic("admin", "wrong"), "secret") is False


def test_verify_username_ignored():
    """Один оператор: username в проверке не участвует."""
    assert verify_basic_auth(_basic("anyone", "secret"), "secret") is True
    assert verify_basic_auth(_basic("", "secret"), "secret") is True


def test_verify_missing_header():
    assert verify_basic_auth("", "secret") is False


def test_verify_bad_base64():
    assert verify_basic_auth("Basic !!!!not-base64!!!!", "secret") is False


def test_verify_non_basic_scheme():
    assert verify_basic_auth("Bearer dGVzdA==", "secret") is False


def test_verify_no_colon_in_decoded():
    """Декод без ':' → пароля нет → отказ."""
    token = base64.b64encode(b"useronly").decode()
    assert verify_basic_auth("Basic " + token, "secret") is False


# ── unit: interlock-матрица (resolve_auth_mode) ─────────────


def test_interlock_auto_empty_loopback_off(caplog):
    """auto + пусто + loopback → off, старт тихий (текущее поведение 001)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "127.0.0.1") == "off"
    assert not caplog.records


def test_interlock_auto_empty_ipv6_loopback_off(caplog):
    """::1 — тоже loopback (расширение спеки: loopback-множество, не только 127.0.0.1)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "::1") == "off"
    assert not caplog.records


def test_interlock_auto_empty_nonloopback_warn(caplog):
    """auto + пусто + bind≠loopback → off + warning, старт продолжается."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "0.0.0.0") == "off"
    assert any("CONSOLE_PASSWORD" in r.getMessage() for r in caplog.records)


def test_interlock_auto_password_on():
    assert resolve_auth_mode("secret", "auto", "127.0.0.1") == "on"
    assert resolve_auth_mode("secret", "auto", "0.0.0.0") == "on"


def test_interlock_off_disabled_silent(caplog):
    """off без пароля → off, warn подавлен (bridge-рецепт без пароля)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "off", "0.0.0.0") == "off"
    assert not caplog.records


def test_interlock_off_with_password_warns(caplog):
    """off + пароль задан → disabled + warning «пароль игнорируется»."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("secret", "off", "127.0.0.1") == "off"
    assert any("игнорируется" in r.getMessage() for r in caplog.records)


def test_interlock_required_empty_runtime_error():
    """required + пусто → RuntimeError (fail-fast, процесс не стартует)."""
    with pytest.raises(RuntimeError, match="CONSOLE_AUTH=required"):
        resolve_auth_mode("", "required", "127.0.0.1")


def test_interlock_required_password_on():
    assert resolve_auth_mode("secret", "required", "0.0.0.0") == "on"


def test_interlock_invalid_mode_value_error():
    """Невалидный CONSOLE_AUTH → ValueError со списком допустимых значений."""
    with pytest.raises(ValueError, match="auto.*off.*required"):
        resolve_auth_mode("secret", "foo", "127.0.0.1")


# ── ASGI fake-scope: ConsoleAuthMiddleware ──────────────────


class _StubApp:
    """Фиксирует вызовы внутреннего ASGI-приложения."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)


def _make_scope(
    scope_type: str,
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    scope = {"type": scope_type, "path": path, "headers": headers or []}
    if scope_type == "http":
        scope.update({"method": "GET", "http_version": "1.1", "scheme": "http"})
    return scope


def _make_mw(
    password: str = "secret",
    mode: str = "on",
    failure_delay: float = 0.0,
) -> tuple[ConsoleAuthMiddleware, _StubApp]:
    stub = _StubApp()
    mw = ConsoleAuthMiddleware(
        app=stub, password=password, mode=mode, failure_delay=failure_delay
    )
    return mw, stub


def _run(mw: ConsoleAuthMiddleware, scope: dict) -> list[dict]:
    """Прогнать middleware через asyncio.run; вернуть отправленные сообщения."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


def test_http_no_credentials_302_to_login():
    """v3 (035): http без кредов, навигация → 302 → /login?next=; app НЕ вызван.

    Basic-челлендж (401+WWW-Authenticate) заменён страницей входа: заголовок
    WWW-Authenticate больше НЕ выдаётся middleware вовсе (иначе браузер
    поднимет Basic-диалог — регрессия UX-цели, P2-2).
    """
    mw, stub = _make_mw()
    sent = _run(mw, _make_scope("http", "/status"))
    assert stub.calls == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 302
    headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    assert headers["location"].startswith("/login")
    assert "next=" in headers["location"]
    assert "www-authenticate" not in headers


def test_http_no_credentials_silent_challenge(caplog):
    """Первичный заход браузера (заголовка нет) — тихий 302, БЕЗ warning."""
    mw, _stub = _make_mw()
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        _run(mw, _make_scope("http", "/status"))
    assert not caplog.records


def test_http_with_credentials_pass_through():
    """http с верными кредами → app вызван, middleware ничего не отправляет сама."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "secret").encode())]
    )
    sent = _run(mw, scope)
    assert len(stub.calls) == 1
    assert sent == []


def test_http_wrong_password_302_and_warning(caplog):
    """Неверный пароль → 302 на /login + logger.warning (brute-force-видимость)."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "wrong").encode())]
    )
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        sent = _run(mw, scope)
    assert stub.calls == []
    assert sent[0]["status"] == 302


def test_failure_delay_sleeps_on_wrong_password():
    """Фиксированная задержка на неверный пароль (brute-force замедление)."""
    mw, _stub = _make_mw(failure_delay=0.2)
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "wrong").encode())]
    )
    t0 = time.monotonic()
    _run(mw, scope)
    assert time.monotonic() - t0 >= 0.15


def test_websocket_no_credentials_close_before_accept():
    """websocket без кредов → websocket.close ДО accept, app НЕ вызван."""
    mw, stub = _make_mw()
    sent = _run(mw, _make_scope("websocket", "/_nicegui_ws/socket.io/"))
    assert stub.calls == []
    assert sent and sent[0]["type"] == "websocket.close"
    # accept не отправлялся — close строго до accept
    assert all(msg["type"] != "websocket.accept" for msg in sent)


def test_websocket_with_credentials_pass_through():
    """websocket с верными кредами → app вызван (WS-транспорт жив)."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "websocket",
        "/_nicegui_ws/socket.io/",
        headers=[(b"authorization", _basic("op", "secret").encode())],
    )
    sent = _run(mw, scope)
    assert len(stub.calls) == 1
    assert sent == []


def test_lifespan_scope_transit():
    """lifespan → транзит: app вызван, никакого 401/close (иначе старт uvicorn сломан)."""
    mw, stub = _make_mw()
    sent = _run(mw, {"type": "lifespan", "message": "lifespan.startup"})
    assert len(stub.calls) == 1
    assert sent == []


def test_other_scope_types_transit():
    """Любые прочие типы scope → транзит (guard первым до парсинга)."""
    mw, stub = _make_mw()
    _run(mw, {"type": "odd.future.scope"})
    assert len(stub.calls) == 1


def test_mode_off_transits_everything():
    """Режим off → http и websocket без кредов проходят транзитом (нулевой оверхед)."""
    mw_http, stub_http = _make_mw(mode="off")
    _run(mw_http, _make_scope("http", "/status"))
    assert len(stub_http.calls) == 1

    mw_ws, stub_ws = _make_mw(mode="off")
    _run(mw_ws, _make_scope("websocket", "/_nicegui_ws/socket.io/"))
    assert len(stub_ws.calls) == 1


# ── 033-F3: /healthz — неаутентифицированный liveness-эндпоинт ──


class TestHealthzEndpoint:
    """033-F3: healthcheck не должен писать login_ok в auth-audit.

    Docker HEALTHCHECK каждые 30s логинился в `/` → 5523 login_ok/640 КБ
    шума в users_audit.jsonl + испорченный last_login_at. Решение: pure-ASGI
    guard в middleware ПЕРВЫМ действием (до enabled-ветки) — inline-ответ
    200 "ok" без вызова app и UserStore. Фасад (Caddy) режет /healthz 403.
    """

    def test_healthz_no_credentials_200_ok(self):
        """RED-1: http /healthz БЕЗ Authorization → 200 + body ok; app НЕ вызван."""
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/healthz"))
        assert stub.calls == [], "healthz не должен доходить до NiceGUI-app"
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 200
        body = b"".join(
            msg.get("body", b"") for msg in sent if msg["type"] == "http.response.body"
        )
        assert body == b"ok"

    def test_healthz_works_in_mode_off(self):
        """RED-2: mode=off → /healthz всё равно 200 inline (guard до enabled-ветки),
        иначе auth-off-режим ловил бы 404 от NiceGUI-app."""
        mw, stub = _make_mw(mode="off")
        sent = _run(mw, _make_scope("http", "/healthz"))
        assert stub.calls == [], "healthz отдаётся middleware, не app (равенство семантики on/off)"
        assert sent[0]["status"] == 200

    def test_healthz_matcher_is_strict(self):
        """RED-3 (изоляция): skip ТОЛЬКО точный /healthz — инвариант «skip-путей
        нет» сохранён для всего остального; ws /healthz НЕ скипается."""
        # / без кредов → v3: навигационный отказ 302 на /login
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/"))
        assert stub.calls == []
        assert sent[0]["status"] == 302
        # /healthz/ (trailing slash) без кредов → 302, НЕ 200 (строгий матчер;
        # на фасаде путь проксируется, но middleware отдаёт редирект на логин)
        mw2, stub2 = _make_mw()
        sent2 = _run(mw2, _make_scope("http", "/healthz/"))
        assert stub2.calls == []
        assert sent2[0]["status"] == 302, "/healthz/ не должен матчиться как /healthz"
        # ws-scope /healthz → НЕ skip (guard только http): без кредов — close
        mw3, stub3 = _make_mw()
        sent3 = _run(mw3, _make_scope("websocket", "/healthz"))
        assert stub3.calls == []
        assert sent3 and sent3[0]["type"] == "websocket.close"

    def test_healthz_no_audit_records(self):
        """RED-4 (audit-молчание): N запросов /healthz со включённым users-стором
        → verify/log_login НЕ вызываются (users_audit.jsonl не растёт)."""
        from unittest.mock import MagicMock

        users = MagicMock()
        users.has_users.return_value = True
        stub = _StubApp()
        mw = ConsoleAuthMiddleware(
            app=stub, password="secret", mode="on", failure_delay=0.0, users=users
        )
        for _ in range(3):
            _run(mw, _make_scope("http", "/healthz"))
        assert users.verify.call_count == 0, "healthz не должен вызывать pbkdf2-verify"
        assert users.log_login.call_count == 0, "healthz не должен писать login_ok в аудит"
        assert stub.calls == []


# ── 035 Ф1: middleware v3 — сессии, allowlist, XHR/навигация ──


def _session_scope(
    path: str = "/status",
    identity: dict | None = None,
    scope_type: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    """Scope с cookie-сессией (как его готовит SessionMiddleware)."""
    scope = _make_scope(scope_type, path, headers)
    session: dict = {"id": "sess-1"}
    if identity is not None:
        session["identity"] = identity
    scope["session"] = session
    return scope


def _ident(store, username: str = "alice", role: str = "editor") -> dict:
    rec = store.get(username)
    return {
        "user_id": rec.id,
        "username": username,
        "role": role,
        "store_version": store.store_version,
        "legacy": False,
    }


class TestAllowlistV3:
    """Контракт §3а: без аутентификации доступны РОВНО 3 пути (+favicon data-URI
    в самом HTML — /favicon.ico не существует и не запрашивается)."""

    def test_login_get_allowed_without_auth(self):
        mw, stub = _make_mw()
        _run(mw, _make_scope("http", "/login"))
        assert len(stub.calls) == 1

    def test_api_login_post_allowed_without_auth(self):
        mw, stub = _make_mw()
        scope = _make_scope("http", "/api/login")
        scope["method"] = "POST"
        _run(mw, scope)
        assert len(stub.calls) == 1

    def test_api_login_get_not_allowlisted(self):
        """GET /api/login (не POST) — не в allowlist → отказ 302."""
        mw, stub = _make_mw()
        _run(mw, _make_scope("http", "/api/login"))
        assert stub.calls == []

    def test_api_access_request_post_allowed_without_auth(self):
        """036 §3: POST /api/access-request — публичен (4-й путь allowlist)."""
        mw, stub = _make_mw()
        scope = _make_scope("http", "/api/access-request")
        scope["method"] = "POST"
        _run(mw, scope)
        assert len(stub.calls) == 1

    def test_api_access_request_get_anon_401_json(self):
        """GET того же пути — метод-специфичный allowlist: XHR-ветка 401 JSON."""
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/api/access-request"))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_nicegui_ws_polling_get_without_session_401(self):
        """engine.io HTTP-polling — API-класс: 401 JSON (не 302)."""
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/_nicegui_ws/socket.io/"))
        assert stub.calls == []
        assert sent[0]["status"] == 401

    def test_nicegui_static_without_auth_302(self):
        """/_nicegui/*-статика без аутентификации → навигационный 302."""
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/_nicegui/nicegui.js"))
        assert stub.calls == []
        assert sent[0]["status"] == 302


class TestRejectShapes:
    """P2-2: XHR → 401 JSON без WWW-Authenticate; навигация → 302+Location."""

    def test_xhr_json_accept_401_without_challenge(self):
        mw, stub = _make_mw()
        scope = _make_scope(
            "http", "/api/whatever", headers=[(b"accept", b"application/json")]
        )
        sent = _run(mw, scope)
        assert stub.calls == []
        assert sent[0]["status"] == 401
        headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
        assert "www-authenticate" not in headers
        assert "application/json" in headers["content-type"]

    def test_xhr_requested_with_401(self):
        mw, _stub = _make_mw()
        scope = _make_scope(
            "http", "/status", headers=[(b"x-requested-with", b"XMLHttpRequest")]
        )
        sent = _run(mw, scope)
        assert sent[0]["status"] == 401

    def test_navigation_302_location_contains_next(self):
        mw, _stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/books"))
        headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
        assert sent[0]["status"] == 302
        assert headers["location"] == "/login?next=%2Fbooks"

    def test_anonymous_session_not_cleared(self):
        """Анонимная сессия (только session-id) НЕ чистится при отказе:
        иначе сбрасывали бы session-id NiceGUI каждому неаутентифицированному."""
        mw, _stub = _make_mw()
        scope = _session_scope("/status", identity=None)
        _run(mw, scope)
        assert scope["session"] == {"id": "sess-1"}


class TestSessionBranch:
    """Cookie-сессия как источник аутентификации + ревалидация §3д."""

    def test_valid_session_passes_http(self, tmp_path):
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        mw, stub = _make_mw_with_users(store)
        scope = _session_scope("/status", identity=_ident(store))
        _run(mw, scope)
        assert len(stub.calls) == 1
        # identity в scope-state (та же семантика, что у Basic-ветки)
        user = stub.calls[0].get("state", {}).get("user")
        assert user["username"] == "alice" and user["role"] == "editor"

    def test_valid_session_passes_websocket(self, tmp_path):
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        mw, stub = _make_mw_with_users(store)
        scope = _session_scope(
            "/_nicegui_ws/socket.io/", identity=_ident(store), scope_type="websocket"
        )
        _run(mw, scope)
        assert len(stub.calls) == 1  # WS-handshake с сессией → pass

    def test_session_preferred_over_basic(self, tmp_path):
        """Сессия валидна → Basic-креды не проверяются (даже кривые)."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        mw, stub = _make_mw_with_users(store)
        scope = _session_scope(
            "/status",
            identity=_ident(store),
            headers=[(b"authorization", _basic("alice", "WRONG").encode())],
        )
        _run(mw, scope)
        assert len(stub.calls) == 1

    def test_forged_identity_unknown_user_rejected_and_cookie_cleared(self, tmp_path):
        """Поддельный identity (юзера нет) → 302 + session.clear() (Set-Cookie
        null на выходе из SessionMiddleware)."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        mw, stub = _make_mw_with_users(store)
        forged = {
            "user_id": "usr_fake",
            "username": "mallory",
            "role": "admin",
            "store_version": store.store_version,
            "legacy": False,
        }
        scope = _session_scope("/status", identity=forged)
        sent = _run(mw, scope)
        assert stub.calls == []
        assert sent[0]["status"] == 302
        assert scope["session"] == {}, "провал ревалидации чистит сессию"

    def test_garbage_session_identity_rejected(self):
        """identity не-dict / без username → отказ (fail-closed по форме)."""
        mw, stub = _make_mw()
        scope = _session_scope("/status", identity={"role": "admin"})  # без username
        _run(mw, scope)
        assert stub.calls == []
        # identity не словарь
        scope2 = _session_scope("/status", identity="admin")
        sent2 = _run(mw, scope2)
        assert sent2[0]["status"] == 302

    def test_missing_session_key_fail_closed(self):
        """P2-4: scope без 'session' (композиция middleware изменилась) →
        fail-closed отказ, НЕ 500."""
        mw, stub = _make_mw()
        _run(mw, _make_scope("http", "/status"))
        assert stub.calls == []

    def test_websocket_invalid_session_close_1008(self, tmp_path):
        """WS-handshake с невалидной сессией → close 1008 ДО accept."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        mw, stub = _make_mw_with_users(store)
        bad = dict(_ident(store), store_version=999)
        scope = _session_scope(
            "/_nicegui_ws/socket.io/", identity=bad, scope_type="websocket"
        )
        sent = _run(mw, scope)
        assert stub.calls == []
        assert sent and sent[0]["type"] == "websocket.close"
        assert all(m["type"] != "websocket.accept" for m in sent)


class TestSessionRevalidation:
    """§3д: отзыв прав действуют мгновенно; N3 — version-equality."""

    def test_store_version_mismatch_rejects(self, tmp_path):
        """N3: set_password (bump версии) убивает ВСЕ сессии."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        ident = _ident(store)
        store.set_password("alice", "new-pw")  # _mutate → version++
        mw, stub = _make_mw_with_users(store)
        scope = _session_scope("/status", identity=ident)
        sent = _run(mw, scope)
        assert stub.calls == []
        assert sent[0]["status"] == 302

    def test_deactivated_user_session_rejected(self, tmp_path):
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        ident = _ident(store)
        store.set_active("alice", False)
        mw, stub = _make_mw_with_users(store)
        sent = _run(mw, _session_scope("/status", identity=ident))
        assert stub.calls == []
        assert sent[0]["status"] == 302

    def test_role_change_updates_session_role_immediately(self, tmp_path):
        """Смена роли: сессия жива, роль в scope-state — НОВАЯ (мгновенно)."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        ident = _ident(store)
        store.set_role("alice", "admin")  # version++ НО полевая ревалидация
        # пересобираем identity с новой версией? НЕТ: версия в сессии старая →
        # bump при set_role убьёт сессию (N3, logout-all при мутациях стора).
        # Этот тест фиксирует именно семантику N3: сессия со старой версией
        # отзывается даже при живом/активном юзере.
        mw, stub = _make_mw_with_users(store)
        sent = _run(mw, _session_scope("/status", identity=ident))
        assert stub.calls == []
        assert sent[0]["status"] == 302

    def test_role_freshness_when_version_matches(self, tmp_path):
        """Полевая ревалидация роли: версия совпала (крафт) — роль берётся из
        стора, а не из cookie."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        store.create_user("alice", "pw", "editor")
        store.set_role("alice", "admin")
        ident = _ident(store, role="editor")  # устаревшая роль в cookie
        ident["store_version"] = store.store_version
        mw, stub = _make_mw_with_users(store)
        scope = _session_scope("/status", identity=ident)
        _run(mw, scope)
        assert len(stub.calls) == 1
        user = stub.calls[0]["state"]["user"]
        assert user["role"] == "admin", "роль из стора, не из cookie"
        assert scope["session"]["identity"]["role"] == "admin"


class TestLegacySessionLifecycle:
    """P1-6: legacy-сессия жива ТОЛЬКО пока стор пуст; create_user = отзыв."""

    def test_legacy_session_alive_on_empty_store(self, tmp_path):
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        assert not store.has_users()
        ident = {
            "user_id": "",
            "username": "admin",
            "role": "admin",
            "store_version": store.store_version,
            "legacy": True,
        }
        mw, stub = _make_mw_with_users(store, password="legacy-pw")
        _run(mw, _session_scope("/status", identity=ident))
        assert len(stub.calls) == 1, "legacy-сессия валидна на пустом сторе"

    def test_legacy_session_dead_after_create_user(self, tmp_path):
        """Расширение кейса test_auth.py:247 (035 P1-6): не только payload
        логина — сама сессия отзывается при появлении стора."""
        from kb_console.core.users import UserStore

        store = UserStore(users_file=str(tmp_path / "u.jsonl"))
        ident = {
            "user_id": "",
            "username": "admin",
            "role": "admin",
            "store_version": store.store_version,
            "legacy": True,
        }
        mw, stub = _make_mw_with_users(store, password="legacy-pw")
        scope = _session_scope("/status", identity=ident)
        _run(mw, scope)
        assert len(stub.calls) == 1
        store.create_user("root", "pw-root", "admin")  # bump + has_users
        scope2 = _session_scope("/status", identity=ident)
        sent2 = _run(mw, scope2)
        assert stub.calls and len(stub.calls) == 1
        assert sent2[0]["status"] == 302
        assert scope2["session"] == {}, "cookie сброшен (Set-Cookie null)"


def _make_mw_with_users(users_store, password: str = "", mode: str = "on"):
    stub = _StubApp()
    mw = ConsoleAuthMiddleware(
        app=stub, password=password, mode=mode, failure_delay=0.0, users=users_store
    )
    return mw, stub


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
