"""Страница входа /login + REST-эндпоинты сессий kb-console (035, план §3е).

GET  /login       — статический inline-HTML (без socket.io — «Риск №1» плана:
                    мёртвая NiceGUI-страница при закрытом WS). Полный UI — Ф2;
                    здесь финальная структура роутов и механика.
POST /api/login   — verify через run_in_executor (pbkdf2 CPU-bound) + аудит
                    log_login + запись session["identity"]; in-memory
                    rate-limit 10 неудач/5 мин по ключу IP (XFF-aware, N2)
                    с clock-швом (N5: clock инъектируется для тестов).
POST /api/logout  — session.clear() (Starlette шлёт Set-Cookie session=null;
                    stateless signed-cookie — replay старого значения валиден
                    до max_age, документировано в README).

Auth off: /login → 302 /status (страница не висит зомби), /api/login → 403.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

_AUTH_FAILURE_DELAY = 0.5
"""Та же фиксированная задержка отказа, что в auth.py (anti-brute-force)."""

_FALLBACK_NEXT = "/status"
"""Куда вести после логина, если next невалиден."""


def sanitize_next(raw: str | None) -> str:
    """Допустить только same-origin путь: начинается с '/' и НЕ с '//'.

    Закрывает open-redirect через /login?next=//evil.example и абсолютные URL.
    """
    if not raw:
        return _FALLBACK_NEXT
    value = raw.strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return _FALLBACK_NEXT


def client_key(request: Request, trust_xff: bool = True) -> str:
    """Ключ rate-limit (N2): первый IP из X-Forwarded-For (наш Caddy-фасад
    ставит XFF и игнорирует incoming-значения — спуфинг закрыт дефолтом);
    нет XFF / CONSOLE_TRUST_XFF=0 → transport client host."""
    if trust_xff:
        xff = request.headers.get("x-forwarded-for", "")
        first = next((part.strip() for part in xff.split(",") if part.strip()), "")
        if first:
            return first
    client = request.client
    return client.host if client else "unknown"


class LoginRateLimiter:
    """In-memory фиксированное окно: 10 неудач / 5 мин / ключ → 429.

    WORKERS=1 — гонок нет. clock инъекцируется (N5: тесты двигают время
    без sleep). Успешный вход сбрасывает счётчик ключа.
    """

    def __init__(
        self,
        max_attempts: int = 10,
        window_sec: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window_sec
        self._clock = clock
        self._fails: dict[str, tuple[int, float]] = {}

    def _fresh(self, key: str) -> tuple[int, float] | None:
        """Запись без истёкшего окна (иначе чистит её и возвращает None)."""
        entry = self._fails.get(key)
        if entry is None:
            return None
        _count, started = entry
        if self._clock() - started >= self._window:
            del self._fails[key]
            return None
        return entry

    def blocked(self, key: str) -> bool:
        entry = self._fresh(key)
        return entry is not None and entry[0] >= self._max

    def retry_after(self, key: str) -> int:
        """Секунды до конца окна (для Retry-After; минимум 1)."""
        entry = self._fails.get(key)
        if entry is None:
            return 1
        remaining = self._window - (self._clock() - entry[1])
        return max(1, int(remaining) + 1)

    def fail(self, key: str) -> None:
        now = self._clock()
        entry = self._fails.get(key)
        if entry is None or now - entry[1] >= self._window:
            self._fails[key] = (1, now)
            return
        self._fails[key] = (entry[0] + 1, entry[1])

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


@dataclass
class LoginContext:
    """Зависимости эндпоинтов (app.py передаёт при register_routes)."""

    auth_mode: str
    users: Any  # UserStore | None
    password: str = ""
    admin_contact: str = ""
    trust_xff: bool = True
    failure_delay: float = _AUTH_FAILURE_DELAY
    limiter: LoginRateLimiter = field(default_factory=LoginRateLimiter)


# ── HTML: сплит-лейаут + табы «Вход»/«Заявка» (Svyazi-канон, Ф2) ──


def render_login_html(
    *,
    legacy: bool,
    next_path: str,
    admin_contact: str = "",
    template: str = "",
) -> str:
    """HTML /login: inline-CSS/JS, 0 внешних запросов (air-gap, план §4).

    Сплит-лейаут: слева бренд-панель, справа карточка с CSS-only табами
    «Вход»/«Заявка на доступ» (работают даже без JS). Каналы заявки
    (textarea + mailto + t.me) строятся из одних SSOT-констант — контент-
    контракт тестируется на согласованность с ACCESS_REQUEST_FIELDS.
    favicon — data:-URI; шрифты системные; единственный fetch —
    same-origin /api/login.
    """
    import json
    from html import escape as _esc

    from .config import (
        ACCESS_REQUEST_EMAIL,
        ACCESS_REQUEST_SUBJECT,
        APP_COPYRIGHT,
        access_request_template,
    )

    template = template or access_request_template()
    tpl_esc = _esc(template)
    tpl_js = json.dumps(template)
    mailto = (
        f"mailto:{ACCESS_REQUEST_EMAIL}"
        f"?subject={quote(ACCESS_REQUEST_SUBJECT)}&body={quote(template)}"
    )
    tme_html = (
        f"<a id='tme-link' class='btn ghost' href=\"https://t.me/{_esc(admin_contact)}"
        f"?text={quote(template)}\">Написать администратору в чате</a>"
        if admin_contact
        else ""
    )
    next_js = json.dumps(sanitize_next(next_path))
    user_field = (
        ""
        if legacy
        else (
            "<input id='f-user' type='text' name='username' autocomplete='username' "
            "placeholder='Логин' required autocapitalize='none'>"
        )
    )
    hint = "🔑 Пароль выдаёт администратор" if legacy else "🔑 Логин и пароль выдаёт администратор"

    css = (
        "*{box-sizing:border-box;margin:0}body{font-family:system-ui,-apple-system,"
        "'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;"
        "background:#f0f2f5;color:#1f2937}"
        ".split{display:flex;width:100%;min-height:100vh}"
        ".brand{flex:1 1 46%;background:linear-gradient(160deg,#0d1b2a 0%,"
        "#1b3a5c 60%,#2563eb 140%);color:#e5edf6;display:flex;flex-direction:"
        "column;justify-content:center;padding:64px;gap:14px}"
        ".brand h1{font-size:42px;letter-spacing:-.5px}.brand .logo{font-size:52px}"
        ".brand p{color:#b8c7d9;line-height:1.55;max-width:44ch}"
        ".brand ul{list-style:none;margin-top:18px;display:flex;"
        "flex-direction:column;gap:10px;color:#cdd9e5;font-size:15px}"
        ".brand li:before{content:'✓  ';color:#60a5fa;font-weight:700}"
        ".pane{flex:1 1 54%;display:flex;align-items:center;justify-content:center;padding:36px}"
        ".card{width:100%;max-width:460px;background:#fff;border-radius:18px;"
        "box-shadow:0 12px 40px rgba(13,27,42,.14);padding:34px 34px 28px}"
        ".tabs input[type=radio]{position:absolute;opacity:0;pointer-events:none}"
        ".tablabels{display:flex;gap:6px;margin-bottom:22px;border-bottom:1px solid #e5e7eb}"
        ".tablabels label{flex:1;text-align:center;padding:10px 6px;cursor:pointer;"
        "font-weight:600;color:#6b7280;border-bottom:2px solid transparent;"
        "transition:color .15s,border-color .15s}"
        "#tab-login:checked~.tablabels label[for=tab-login],"
        "#tab-req:checked~.tablabels label[for=tab-req]{color:#1d4ed8;"
        "border-bottom-color:#1d4ed8}"
        ".panels section{display:none}#tab-login:checked~.panels #p-login,"
        "#tab-req:checked~.panels #p-req{display:block;animation:fade .18s ease-in}"
        "@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}"
        "input[type=text],input[type=password]{width:100%;padding:12px 14px;"
        "margin:8px 0;border:1px solid #cbd5e1;border-radius:10px;font-size:15px}"
        "input:focus{outline:2px solid #93c5fd;border-color:#3b82f6}"
        ".toggle{display:flex;align-items:center;gap:8px;font-size:14px;"
        "color:#546e7a;margin:6px 0 4px;cursor:pointer}"
        ".btn{display:inline-flex;align-items:center;justify-content:center;"
        "width:100%;padding:12px 18px;margin-top:14px;border:0;border-radius:10px;"
        "background:#1d4ed8;color:#fff;font-size:15px;font-weight:600;"
        "cursor:pointer;transition:background .15s;text-decoration:none}"
        ".btn:hover{background:#1e40af}.btn.ghost{background:#eef2ff;color:#1e40af}"
        ".err{color:#c62828;min-height:22px;font-size:14px;margin-top:10px}"
        ".hint{font-size:13.5px;color:#6b7280;margin:10px 0 2px}"
        "textarea{width:100%;min-height:190px;padding:12px;border:1px solid #cbd5e1;"
        "border-radius:10px;font-family:ui-monospace,Consolas,monospace;"
        "font-size:13px;resize:vertical;color:#374151}"
        ".channels{display:flex;flex-direction:column;gap:8px;margin-top:6px}"
        ".req-note{font-size:13.5px;color:#6b7280;margin:12px 0 8px;line-height:1.5}"
        "a.mailto{color:#1d4ed8;font-weight:600;text-decoration:none}"
        "a.mailto:hover{text-decoration:underline}"
        ".copy{margin-top:18px;text-align:center;font-size:12.5px;color:#9ca3af}"
        "@media(max-width:860px){.split{flex-direction:column}.brand{padding:34px;"
        "flex-basis:auto}.brand h1{font-size:30px}.brand ul{display:none}}"
    )

    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Вход — kb-console</title>"
        "<link rel='icon' href='data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww"
        ".w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20100%20100%22%3E%3Ctext%20y%3D%22"
        ".9em%22%20font-size%3D%2290%22%3E%F0%9F%97%82%3C%2Ftext%3E%3C%2Fsvg%3E'>"
        f"<style>{css}</style></head><body><div class='split'>"
        "<aside class='brand'><div class='logo'>🗂</div><h1>kb-console</h1>"
        "<p>Консоль управления базой знаний MCP: книги, импорт, поиск, "
        "качество и токены — в одном интерфейсе.</p>"
        "<ul><li>Вход по личному логину</li><li>Роли: admin / editor / contributor"
        "</li><li>Сессия действует 12 часов</li></ul></aside>"
        "<main class='pane'><div class='card'>"
        "<div class='tabs'>"
        "<input type='radio' name='tab' id='tab-login' checked>"
        "<input type='radio' name='tab' id='tab-req'>"
        "<div class='tablabels'><label for='tab-login'>Вход</label>"
        "<label for='tab-req'>Заявка на доступ</label></div>"
        "<div class='panels'>"
        "<section id='p-login'>"
        f"<form id='lf'>{user_field}"
        "<input id='f-pass' type='password' name='password' "
        "autocomplete='current-password' placeholder='Пароль' required>"
        "<label class='toggle'><input type='checkbox' onclick="
        "\"document.getElementById('f-pass').type=this.checked?'text':'password'\">"
        "показать пароль</label>"
        "<button class='btn' type='submit'>Войти</button>"
        "<div class='err' id='err'></div>"
        f"<p class='hint'>{hint}</p>"
        "</form></section>"
        "<section id='p-req'>"
        "<p class='req-note'>Скопируйте шаблон, заполните обязательные поля "
        "и отправьте заявителю доступа по любому каналу ниже.</p>"
        f"<textarea id='req-tpl' readonly rows='9' onclick='this.select()'>{tpl_esc}</textarea>"
        "<div class='channels'>"
        "<button class='btn ghost' type='button' id='copy-btn'>Скопировать заявку</button>"
        f"<a class='mailto' href=\"{mailto}\">Отправить по почте</a>{tme_html}"
        "</div></section>"
        "</div></div></div></main></div>"
        f"<footer class='copy'>{APP_COPYRIGHT}</footer>"
        "<script>const N=" + next_js + ",T=" + tpl_js + ";"
        "document.getElementById('lf').addEventListener('submit',async(e)=>{e.preventDefault();"
        "const u=document.getElementById('f-user');"
        "const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':"
        "'application/json'},body:JSON.stringify({username:u?u.value:'',password:"
        "document.getElementById('f-pass').value})});"
        "if(r.ok){window.location=N}else{document.getElementById('err').textContent="
        "'Неверный логин или пароль'}});"
        "document.getElementById('copy-btn').addEventListener('click',function(){"
        "navigator.clipboard.writeText(T).then(()=>{this.textContent='Скопировано ✓'},"
        "()=>{const t=document.getElementById('req-tpl');t.focus();t.select()})});"
        "</script></body></html>"
    )


# ── Имплы эндпоинтов (unit-тестируемые напрямую, без nicegui) ──


async def _login_get_impl(ctx: LoginContext, next_raw: str | None) -> Response:
    if ctx.auth_mode != "on":
        return RedirectResponse("/status", status_code=302)
    legacy = not (ctx.users is not None and ctx.users.has_users())
    html = render_login_html(
        legacy=legacy, next_path=next_raw or "", admin_contact=ctx.admin_contact
    )
    return HTMLResponse(html)


async def _login_post_impl(ctx: LoginContext, request: Request) -> Response:
    if ctx.auth_mode != "on":
        return JSONResponse(
            {"ok": False, "error": "auth выключен (CONSOLE_AUTH=off)"}, status_code=403
        )

    key = client_key(request, ctx.trust_xff)
    if ctx.limiter.blocked(key):
        return JSONResponse(
            {"ok": False, "error": "Слишком много неудачных попыток входа, попробуйте позже"},
            status_code=429,
            headers={"Retry-After": str(ctx.limiter.retry_after(key))},
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — некорректный JSON это валидный 400, не 500
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "error": "Ожидается JSON-тело {username, password}"}, status_code=400
        )
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")

    async def _reject_login(actor: str) -> Response:
        if ctx.users is not None:
            ctx.users.log_login(actor, ok=False)
        ctx.limiter.fail(key)
        await asyncio.sleep(ctx.failure_delay)
        return JSONResponse({"ok": False, "error": "Неверный логин или пароль"}, status_code=401)

    users = ctx.users
    if users is not None and users.has_users():
        # Per-user ветка (pbkdf2 → executor, P2-4).
        if not username or not password:
            return await _reject_login(username or "unknown")
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(None, users.verify, username, password)
        if record is None:
            return await _reject_login(username)
        users.log_login(username, ok=True)
        ctx.limiter.reset(key)
        request.session["identity"] = {
            "user_id": record.id,
            "username": record.username,
            "role": record.role,
            "store_version": users.store_version,
            "legacy": False,
        }
        return JSONResponse({"ok": True, "username": record.username, "role": record.role})

    # Legacy-ветка (пустой стор + CONSOLE_PASSWORD): username не участвует.
    if not ctx.password or not password or not hmac.compare_digest(
        password.encode("utf-8"), ctx.password.encode("utf-8")
    ):
        return await _reject_login(username or "admin")
    ctx.limiter.reset(key)
    request.session["identity"] = {
        "user_id": "",
        "username": "admin",
        "role": "admin",
        "store_version": users.store_version if users is not None else 0,
        "legacy": True,
    }
    if users is not None:
        users.log_login(username or "admin", ok=True)
    return JSONResponse({"ok": True, "username": "admin", "role": "admin"})


async def _logout_post_impl(request: Request) -> Response:
    """session.clear() → Starlette шлёт Set-Cookie session=null (expires=1970)."""
    session = request.scope.get("session")
    if isinstance(session, dict):
        session.clear()
    return JSONResponse({"ok": True})


# ── Регистрация на nicegui-приложении (FastAPI add_api_route) ──

_registered = False


def register_routes(
    *,
    auth_mode: str,
    users: Any,
    password: str,
    admin_contact: str = "",
    trust_xff: bool = True,
) -> None:
    """Зарегистрировать /login, /api/login, /api/logout (идемпотентно)."""
    global _registered
    if _registered:
        return
    ctx = LoginContext(
        auth_mode=auth_mode,
        users=users,
        password=password,
        admin_contact=admin_contact,
        trust_xff=trust_xff,
    )

    async def login_get(next: str = "") -> Response:
        return await _login_get_impl(ctx, next)

    async def login_post(request: Request) -> Response:
        return await _login_post_impl(ctx, request)

    async def logout_post(request: Request) -> Response:
        return await _logout_post_impl(request)

    from nicegui import app as nicegui_app

    nicegui_app.add_api_route("/login", login_get, methods=["GET"])
    nicegui_app.add_api_route("/api/login", login_post, methods=["POST"])
    nicegui_app.add_api_route("/api/logout", logout_post, methods=["POST"])
    _registered = True
