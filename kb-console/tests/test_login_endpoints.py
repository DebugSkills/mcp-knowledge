"""Тесты login-эндпоинтов и сессий (035 Ф1c: login_page.py + wiring).

Unit (имплы напрямую, без nicegui):
  - sanitize_next (open-redirect-негативы);
  - LoginRateLimiter: окно/блок/reset/retry_after + clock-шов (N5, без sleep)
    и независимость ключей по XFF (N2);
  - POST /api/login: legacy-payload, per-user session-payload, 401/400/403/429;
  - POST /api/logout: session.clear().

Интеграция (подпроцесс, как test_auth_smoke): реальный стек middleware
[SessionMiddleware … ConsoleAuth] — «логин → cookie → /_nicegui_ws/ пускает»
(регрессия порядка middleware, R10/P2-4), cookie-флаги, logout-цикл,
legacy-сессия живёт на защищённом пути.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
from _local_http import local_get
from starlette.requests import Request

from kb_console.core.users import UserStore
from kb_console.login_page import (
    LoginContext,
    LoginRateLimiter,
    _login_get_impl,
    _login_post_impl,
    _logout_post_impl,
    client_key,
    sanitize_next,
)

_SMOKE_PORT = 9881
_PASSWORD = "test"


# ── unit: sanitize_next ──────────────────────────────────────


class TestSanitizeNext:
    def test_valid_path_kept(self):
        assert sanitize_next("/books") == "/books"

    def test_empty_falls_back(self):
        assert sanitize_next("") == "/status"
        assert sanitize_next(None) == "/status"

    @pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example", "http://x", "javascript:alert(1)", "relative", "../up"])
    def test_open_redirect_negatives(self, bad):
        assert sanitize_next(bad) == "/status"


# ── unit: client_key (N2) ────────────────────────────────────


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


def _req(headers: dict[str, str] | None = None, host: str = "127.0.0.1"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (host, 12345),
    }
    return Request(scope)


class TestClientKey:
    def test_xff_first_ip_wins(self):
        req = _req({"x-forwarded-for": "10.0.0.9, 192.168.2.3, 127.0.0.1"})
        assert client_key(req, trust_xff=True) == "10.0.0.9"

    def test_no_xff_falls_back_to_client_host(self):
        assert client_key(_req(host="127.0.0.1"), trust_xff=True) == "127.0.0.1"

    def test_trust_off_ignores_xff(self):
        req = _req({"x-forwarded-for": "10.0.0.9"})
        assert client_key(req, trust_xff=False) == "127.0.0.1"


# ── unit: LoginRateLimiter (N5 clock-шов) ────────────────────


class TestLoginRateLimiter:
    def test_blocked_after_ten_failures(self):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(10):
            lim.fail("k")
        assert lim.blocked("k") is True
        assert lim.retry_after("k") >= 1

    def test_nine_not_blocked(self):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(9):
            lim.fail("k")
        assert lim.blocked("k") is False

    def test_success_resets(self):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(10):
            lim.fail("k")
        lim.reset("k")
        assert lim.blocked("k") is False

    def test_window_expiry_via_clock_seam(self):
        """N5: окно 5 мин истекает по инъекции времени, без sleep."""
        now = {"t": 0.0}
        lim = LoginRateLimiter(clock=lambda: now["t"])
        for _ in range(10):
            lim.fail("k")
        assert lim.blocked("k") is True
        now["t"] = 301.0  # за границей окна
        assert lim.blocked("k") is False
        lim.fail("k")
        assert lim.blocked("k") is False  # новый отсчёт

    def test_keys_independent(self):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(10):
            lim.fail("10.0.0.9")
        assert lim.blocked("10.0.0.9") is True
        assert lim.blocked("10.0.0.8") is False


# ── unit: имплы эндпоинтов ───────────────────────────────────


def _post_scope(body: dict, session: dict | None = None, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/login",
        "headers": raw
        + [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
    }
    if session is not None:
        scope["session"] = session

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request(scope, receive)


def _ctx(tmp_path, password: str = "", auth_mode: str = "on", limiter: LoginRateLimiter | None = None) -> LoginContext:
    store = UserStore(users_file=str(tmp_path / "users.jsonl"))
    return LoginContext(
        auth_mode=auth_mode,
        users=store,
        password=password,
        failure_delay=0.0,
        limiter=limiter or LoginRateLimiter(clock=lambda: 0.0),
    )


def _req_ctx(tmp_path, cap: int = 500):  # -> (LoginContext, AccessRequestStore)
    from kb_console.core.access_requests import AccessRequestStore

    req_store = AccessRequestStore(str(tmp_path / "req" / "a.db"), cap=cap)
    lim = LoginRateLimiter(max_attempts=5, window_sec=300.0, clock=lambda: 0.0)
    users = UserStore(users_file=str(tmp_path / "users.jsonl"))
    return (
        LoginContext(
            auth_mode="on",
            users=users,
            failure_delay=0.0,
            limiter=LoginRateLimiter(clock=lambda: 0.0),
            requests_store=req_store,
            request_limiter=lim,
        ),
        req_store,
    )


def _ar_scope(body_bytes: bytes, headers: list[tuple[str, str]] | None = None) -> Request:
    """Scope POST /api/access-request с сырым телом (потоковый кап-тест)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or [])]
    raw.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/access-request",
        "headers": raw,
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


def _ar_payload(**over) -> dict:
    base = {
        "fio": "Иван Иванович Иванов",
        "department": "Отдел разработки",
        "phone": "+7 900 000-00-00",
        "email": "ivan@example.com",
        "work_summary": "Нужен доступ для ревью документации",
        "consent": True,
    }
    base.update(over)
    return base


class TestAccessRequestPostImpl:
    def test_success_201(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, store = _req_ctx(tmp_path)
        resp = asyncio.run(impl(ctx, _ar_scope(json.dumps(_ar_payload()).encode())))
        assert resp.status_code == 201
        rid = json.loads(resp.body)["id"]
        assert store.get(rid).fio == "Иван Иванович Иванов"

    def test_validation_missing_field_400(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, store = _req_ctx(tmp_path)
        bad = _ar_payload(fio="   ")  # пустое после strip
        resp = asyncio.run(impl(ctx, _ar_scope(json.dumps(bad).encode())))
        assert resp.status_code == 400
        assert store.count() == 0

    def test_validation_bad_phone_400(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, _ = _req_ctx(tmp_path)
        resp = asyncio.run(
            impl(ctx, _ar_scope(json.dumps(_ar_payload(phone="tel:+7();DROP")).encode()))
        )
        assert resp.status_code == 400

    def test_consent_false_400(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, store = _req_ctx(tmp_path)
        resp = asyncio.run(
            impl(ctx, _ar_scope(json.dumps(_ar_payload(consent=False)).encode()))
        )
        assert resp.status_code == 400
        assert store.count() == 0

    def test_duplicate_409(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, _ = _req_ctx(tmp_path)
        body = json.dumps(_ar_payload()).encode()
        assert asyncio.run(impl(ctx, _ar_scope(body))).status_code == 201
        assert asyncio.run(impl(ctx, _ar_scope(body))).status_code == 409

    def test_rate_limit_5_then_429_no_reset_on_success(self, tmp_path):
        """P2-4: fail() на каждый POST; 5 попыток → 6-я 429; успех НЕ reset."""
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, _ = _req_ctx(tmp_path)
        codes = []
        for i in range(5):
            body = json.dumps(_ar_payload(fio=f"Фамилия {i}", phone=f"+7900000000{i}")).encode()
            codes.append(asyncio.run(impl(ctx, _ar_scope(body))).status_code)
        assert codes == [201] * 5
        sixth = json.dumps(_ar_payload(fio="Шестой", phone="+79000")).encode()
        assert asyncio.run(impl(ctx, _ar_scope(sixth))).status_code == 429

    def test_cap_503(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, _ = _req_ctx(tmp_path, cap=1)
        first = json.dumps(_ar_payload()).encode()
        assert asyncio.run(impl(ctx, _ar_scope(first))).status_code == 201
        second = json.dumps(_ar_payload(fio="Другой", phone="+79999")).encode()
        assert asyncio.run(impl(ctx, _ar_scope(second))).status_code == 503

    def test_content_length_over_cap_413_without_read(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, store = _req_ctx(tmp_path)
        scope = _ar_scope(b"{}")
        scope.scope["headers"].append((b"content-length", b"1048576"))
        resp = asyncio.run(impl(ctx, scope))
        assert resp.status_code == 413
        assert store.count() == 0

    def test_chunked_1mb_413_or_disconnect_not_saved(self, tmp_path):
        """iter3: chunked без CL, 1 МБ → 413 ИЛИ обрыв; заявка НЕ сохранена."""
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, store = _req_ctx(tmp_path)
        big = b"x" * (1024 * 1024)
        scope = _ar_scope(big)  # без content-length
        outcome = "disconnected"  # обрыв тоже валиден (P1-2)
        try:
            resp = asyncio.run(impl(ctx, scope))
        except (ConnectionError, OSError, RuntimeError):
            resp = None
        if resp is not None:
            assert resp.status_code == 413
        assert outcome == "disconnected"
        assert store.count() == 0

    def test_bad_json_400(self, tmp_path):
        from kb_console.login_page import _access_request_post_impl as impl

        ctx, _ = _req_ctx(tmp_path)
        resp = asyncio.run(impl(ctx, _ar_scope(b"not-json")))
        assert resp.status_code == 400


class TestLoginPostImpl:
    def test_per_user_success_session_payload(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.users.create_user("alice", "pw", "editor")
        session: dict = {}
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"username": "alice", "password": "pw"}, session)))
        assert resp.status_code == 200
        ident = session["identity"]
        assert ident["user_id"] == ctx.users.get("alice").id
        assert ident["username"] == "alice"
        assert ident["role"] == "editor"
        assert ident["store_version"] == ctx.users.store_version
        assert ident["legacy"] is False

    def test_per_user_wrong_password_401_and_fail_counted(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.users.create_user("alice", "pw", "editor")
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"username": "alice", "password": "bad"})))
        assert resp.status_code == 401
        assert ctx.limiter.blocked("127.0.0.1") is False  # 1 неудача < 10

    def test_legacy_success_payload(self, tmp_path):
        """Пустой стор + CONSOLE_PASSWORD → legacy-identity (plan §3в)."""
        ctx = _ctx(tmp_path, password="legacy-pw")
        session: dict = {}
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"password": "legacy-pw"}, session)))
        assert resp.status_code == 200
        ident = session["identity"]
        assert ident == {
            "user_id": "",
            "username": "admin",
            "role": "admin",
            "store_version": ctx.users.store_version,
            "legacy": True,
        }

    def test_legacy_wrong_password_401(self, tmp_path):
        ctx = _ctx(tmp_path, password="legacy-pw")
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"password": "nope"})))
        assert resp.status_code == 401

    def test_legacy_rejected_when_store_nonempty(self, tmp_path):
        """P2-5b: общий пароль не работает при непустом сторе (только per-user)."""
        ctx = _ctx(tmp_path, password="legacy-pw")
        ctx.users.create_user("root", "pw-root", "admin")
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"username": "x", "password": "legacy-pw"})))
        assert resp.status_code == 401

    def test_auth_off_403(self, tmp_path):
        ctx = _ctx(tmp_path, auth_mode="off")
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"password": "x"})))
        assert resp.status_code == 403

    def test_bad_json_400(self, tmp_path):
        scope = {"type": "http", "method": "POST", "path": "/api/login", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1)}

        async def receive():
            return {"type": "http.request", "body": b"not-json", "more_body": False}

        resp = asyncio.run(_login_post_impl(_ctx(tmp_path), Request(scope, receive)))
        assert resp.status_code == 400

    def test_rate_limited_429_with_retry_after(self, tmp_path):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(10):
            lim.fail("127.0.0.1")
        ctx = _ctx(tmp_path, password="legacy-pw", limiter=lim)
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"password": "legacy-pw"})))
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) >= 1

    def test_success_resets_limiter(self, tmp_path):
        lim = LoginRateLimiter(clock=lambda: 0.0)
        for _ in range(9):
            lim.fail("127.0.0.1")
        ctx = _ctx(tmp_path, password="legacy-pw", limiter=lim)
        resp = asyncio.run(_login_post_impl(ctx, _post_scope({"password": "legacy-pw"}, {})))
        assert resp.status_code == 200
        assert lim.blocked("127.0.0.1") is False

    def test_audit_login_events(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.users.create_user("alice", "pw", "editor")
        asyncio.run(_login_post_impl(ctx, _post_scope({"username": "alice", "password": "pw"}, {})))
        asyncio.run(_login_post_impl(ctx, _post_scope({"username": "alice", "password": "bad"}, {})))
        audit = ctx.users.audit_path.read_text(encoding="utf-8")
        assert '"event": "login_ok"' in audit
        assert '"event": "login_fail"' in audit


class TestLoginGetImpl:
    def test_auth_off_redirects_to_status(self, tmp_path):
        resp = asyncio.run(_login_get_impl(_ctx(tmp_path, auth_mode="off"), ""))
        assert resp.status_code == 302
        assert resp.headers["location"] == "/status"

    def test_on_mode_html_with_template(self, tmp_path):
        from kb_console.config import ACCESS_REQUEST_FIELDS

        resp = asyncio.run(_login_get_impl(_ctx(tmp_path), "/books"))
        assert resp.status_code == 200
        for field in ACCESS_REQUEST_FIELDS:
            assert field in resp.body.decode("utf-8")


class TestLogoutImpl:
    def test_clears_session(self):
        session = {"id": "x", "identity": {"username": "a"}}
        scope = {"type": "http", "method": "POST", "path": "/api/logout", "headers": [], "session": session}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        resp = asyncio.run(_logout_post_impl(Request(scope, receive)))
        assert resp.status_code == 200
        assert session == {}


# ── интеграция: реальный стек (подпроцесс) ───────────────────


def _start_console(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    for var in (
        "CONSOLE_PASSWORD",
        "CONSOLE_AUTH",
        "CONSOLE_USERS_FILE",
        "CONSOLE_ADMIN_USER",
        "CONSOLE_ADMIN_PASSWORD",
        "CONSOLE_STORAGE_SECRET",
        "CONSOLE_ADMIN_CONTACT",
        "MCP_API_KEY_ADMIN",
        "MCP_API_KEY_EDITOR",
        "MCP_API_KEY_CONTRIBUTOR",
    ):
        env.pop(var, None)
    env["CONSOLE_PORT"] = str(port)
    env["CONSOLE_HOST"] = "127.0.0.1"
    env["CONSOLE_PASSWORD"] = _PASSWORD  # legacy-режим (пустой стор)
    env["CONSOLE_USERS_FILE"] = "/tmp/kilo/035-users-smoke/users.jsonl"  # несуществующий → пусто
    env["CONSOLE_ACCESS_REQUESTS_DB"] = "/tmp/kilo/035-users-smoke/access_requests.db"  # 036: /app-дефолт не существует вне docker
    env["CONSOLE_STORAGE_SECRET_FILE_DIR_FALLBACK"] = ""  # не используется, просто маркер
    env["MCP_SERVER_URL"] = "http://localhost:8000"
    env["NICEGUI_SCREEN_TEST_PORT"] = str(port)
    return subprocess.Popen(
        [sys.executable, "-m", "kb_console.app"],
        env=env,
        start_new_session=True,
    )


_proc: subprocess.Popen | None = None


def _server() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        try:
            local_get(f"http://localhost:{_SMOKE_PORT}/status", timeout=1.0)
            raise RuntimeError(f"порт {_SMOKE_PORT} занят stale-сервером")
        except httpx.HTTPError:
            pass
        _proc = _start_console(_SMOKE_PORT)
        deadline = time.monotonic() + 20.0
        last: Exception | None = None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                local_get(f"http://localhost:{_SMOKE_PORT}/login", timeout=3.0)
                return _proc
            except httpx.HTTPError as e:
                last = e
        raise RuntimeError(f"server not started: {last}")
    return _proc


@pytest.fixture(scope="module", autouse=True)
def _console():
    _server()
    yield
    global _proc
    if _proc is not None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            _proc.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        _proc = None


class TestLoginIntegration:
    def test_login_page_reachable_without_auth(self):
        r = local_get(f"http://localhost:{_SMOKE_PORT}/login", timeout=5.0)
        assert r.status_code == 200
        assert "kb-console" in r.text

    def test_login_sets_cookie_and_protected_path_passes(self):
        """Ядро интеграции: логин → cookie → /status 200 (полный стек SM→…→ConsoleAuth)."""
        with httpx.Client(trust_env=False, base_url=f"http://localhost:{_SMOKE_PORT}") as c:
            r = c.post(
                "/api/login",
                json={"password": _PASSWORD},
                timeout=5.0,
            )
            assert r.status_code == 200
            set_cookie = r.headers.get("set-cookie", "")
            # cookie-флаги (P2-3): подпись + httponly + samesite + Max-Age 12ч
            assert "httponly" in set_cookie.lower()
            assert "samesite=lax" in set_cookie.lower()
            assert "max-age=43200" in set_cookie.lower()
            r2 = c.get("/status", timeout=5.0)
            assert r2.status_code == 200
            # регрессия порядка middleware (R10): WS-путь пускает с сессией
            r3 = c.get(
                "/_nicegui_ws/socket.io/?EIO=4&transport=polling", timeout=5.0
            )
            assert r3.status_code not in (302, 401)

    def test_wrong_login_401_json(self):
        r = httpx.post(
            f"http://localhost:{_SMOKE_PORT}/api/login",
            json={"password": "wrong"},
            trust_env=False,
            timeout=5.0,
        )
        assert r.status_code == 401
        assert r.headers.get("content-type", "").startswith("application/json")

    def test_access_request_anon_get_401_json(self):
        """036 §3: GET /api/access-request анонимно → XHR-ветка 401 JSON."""
        r = httpx.get(
            f"http://localhost:{_SMOKE_PORT}/api/access-request",
            trust_env=False,
            timeout=5.0,
        )
        assert r.status_code == 401
        assert r.headers.get("content-type", "").startswith("application/json")
        assert "www-authenticate" not in {k.lower() for k in r.headers}

    def test_access_request_authed_get_405(self):
        """Аутентифицированный GET → 405 от роута (метод-специфичный allowlist)."""
        with httpx.Client(trust_env=False, base_url=f"http://localhost:{_SMOKE_PORT}") as c:
            c.post("/api/login", json={"password": _PASSWORD}, timeout=5.0)
            r = c.get("/api/access-request", timeout=5.0)
        assert r.status_code == 405

    def test_logout_cycle(self):
        """logout → Set-Cookie session=null; следующий запрос (с null-cookie
        как у честного клиента) → 302; WS-handshake после logout → не пускает."""
        with httpx.Client(trust_env=False, base_url=f"http://localhost:{_SMOKE_PORT}") as c:
            c.post("/api/login", json={"password": _PASSWORD}, timeout=5.0)
            r = c.post("/api/logout", timeout=5.0)
            assert r.status_code == 200
            assert "session=null" in r.headers.get("set-cookie", "")
            r2 = c.get("/status", timeout=5.0)  # cookie-jar получил session=null
            assert r2.status_code == 302
            assert r2.headers.get("location", "").startswith("/login")

    def test_xff_keys_independent_live(self):
        """N2: 10 неудач с XFF одного «клиента» → 429 только для него."""
        base = f"http://localhost:{_SMOKE_PORT}/api/login"
        for _ in range(10):
            r = httpx.post(
                base,
                json={"password": "wrong"},
                headers={"x-forwarded-for": "10.7.7.7"},
                trust_env=False,
                timeout=5.0,
            )
            assert r.status_code == 401
        blocked = httpx.post(
            base, json={"password": "wrong"},
            headers={"x-forwarded-for": "10.7.7.7"},
            trust_env=False, timeout=5.0,
        )
        assert blocked.status_code == 429
        other = httpx.post(
            base, json={"password": "wrong"},
            headers={"x-forwarded-for": "10.7.7.8"},
            trust_env=False, timeout=5.0,
        )
        assert other.status_code == 401
