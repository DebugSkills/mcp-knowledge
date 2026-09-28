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


# ── HTML (минимальная рабочая страница; полный сплит-UI — Ф2) ──


def render_login_html(
    *,
    legacy: bool,
    next_path: str,
    admin_contact: str = "",
    template: str = "",
) -> str:
    """HTML /login: inline-CSS/JS, 0 внешних запросов (air-gap, план §4).

    Полный сплит-лейаут с табами «Вход»/«Заявка на доступ» — Ф2; контракт
    функции (аргументы) финален: каналы заявки строятся из SSOT-констант.
    """
    import json

    from .config import (
        ACCESS_REQUEST_EMAIL,
        ACCESS_REQUEST_SUBJECT,
        access_request_template,
    )

    template = template or access_request_template()
    mailto = f"mailto:{ACCESS_REQUEST_EMAIL}?subject={quote(ACCESS_REQUEST_SUBJECT)}&body={quote(template)}"
    tme = f"https://t.me/{admin_contact}?text={quote(template)}" if admin_contact else ""
    next_js = json.dumps(sanitize_next(next_path))
    user_field = "" if legacy else '<input id="f-user" name="username" autocomplete="username" placeholder="Логин" required>'
    hint = "Пароль выдаёт администратор" if legacy else "Логин и пароль выдаёт администратор"
    tme_html = (
        f'<p><a id="tme-link" href="{tme}">Написать администратору в чате</a> (Telegram)</p>'
        if tme
        else ""
    )
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>Вход — kb-console</title>"
        "<link rel='icon' href='data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww"
        ".w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20100%20100%22%3E%3Ctext%20y%3D%22"
        ".9em%22%20font-size%3D%2290%22%3E%F0%9F%97%82%3C%2Ftext%3E%3C%2Fsvg%3E'>"
        "<style>body{font-family:system-ui,sans-serif;display:flex;justify-content:center;"
        "padding-top:8vh;background:#f4f6f8}.card{background:#fff;border-radius:16px;"
        "padding:32px;max-width:420px;box-shadow:0 4px 24px rgba(0,0,0,.08)}input{display:"
        "block;width:100%;margin:8px 0;padding:10px;border:1px solid #cbd5e1;border-radius:"
        "8px;box-sizing:border-box}button{margin-top:12px;padding:10px 18px;border:0;"
        "border-radius:8px;background:#1976d2;color:#fff;cursor:pointer}.err{color:#c62828;"
        "min-height:20px}label{font-size:14px;color:#546e7a}</style></head><body>"
        "<div class='card'><h1>kb-console</h1>"
        f"<p>🔐 {hint}</p>"
        f"<form id='lf'>{user_field}"
        "<input id='f-pass' type='password' name='password' autocomplete='current-password' "
        "placeholder='Пароль' required>"
        "<label><input type='checkbox' onclick=\"document.getElementById('f-pass').type="
        "this.checked?'text':'password'\"> показать пароль</label>"
        "<button type='submit'>Войти</button><div class='err' id='err'></div></form>"
        f"<p>Заявка на доступ: скопируйте шаблон и отправьте на {mailto}.</p>{tme_html}"
        "<textarea readonly rows='7' style='width:100%' onclick='this.select()'>"
        f"{template}</textarea>"
        "</div><script>const N=" + next_js + ";"
        "document.getElementById('lf').addEventListener('submit',async(e)=>{e.preventDefault();"
        "const u=document.getElementById('f-user');"
        "const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':"
        "'application/json'},body:JSON.stringify({username:u?u.value:'',password:"
        "document.getElementById('f-pass').value})});"
        "if(r.ok){window.location=N}else{document.getElementById('err').textContent="
        "'Неверный логин или пароль'}});</script></body></html>"
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
