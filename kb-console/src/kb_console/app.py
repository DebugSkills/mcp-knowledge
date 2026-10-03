"""Точка входа kb-console — NiceGUI приложение.

Запуск:
    python -m kb_console.app

Многостраничная архитектура (V4a): каждая вкладка — отдельный @ui.page.
Reload сохраняет раздел (в отличие от ui.tabs, которые сбрасываются на первую).
"""

from __future__ import annotations

from nicegui import core, ui
from starlette.middleware.base import BaseHTTPMiddleware

from . import login_page
from .auth import ConsoleAuthMiddleware, resolve_auth_mode
from .components import theme
from .components.header import render_header
from .config import (
    CONSOLE_ADMIN_CONTACT,
    CONSOLE_ADMIN_PASSWORD,
    CONSOLE_ADMIN_USER,
    CONSOLE_AUTH,
    CONSOLE_HOST,
    CONSOLE_PASSWORD,
    CONSOLE_PORT,
    CONSOLE_STORAGE_SECRET,
    CONSOLE_TRUST_XFF,
    CONSOLE_USERS_FILE,
)
from .core.storage_secret import resolve_storage_secret
from .core.users import UserStore


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Логирует все входящие HTTP-запросы для отладки."""

    async def dispatch(self, request, call_next):
        print(f"[REQ] {request.method} {request.url.path}")
        response = await call_next(request)
        return response


# ── Routes ──────────────────────────────────────────────────


@ui.page("/")
def index() -> None:
    """Корневой URL — редирект на /status.

    NiceGUI ui.navigate.to выполняет клиентский редирект (не цикл).
    """
    ui.navigate.to("/status")


@ui.page("/status")
def page_status() -> None:
    """Страница «Статус» — liveness, health, метрики, инструменты."""
    render_header("status")
    from .pages.status import build_status
    build_status()


@ui.page("/books")
def page_books() -> None:
    """Страница «Книги» — список коллекций + модалка деталей."""
    render_header("books")
    from .pages.books import build_books
    build_books()


@ui.page("/import")
def page_import() -> None:
    """Страница «Импорт» — загрузка и обработка контента."""
    render_header("import")
    from .pages.import_page import build_import
    build_import()


@ui.page("/search")
def page_search() -> None:
    """Страница «Поиск» — семантический поиск по базе знаний."""
    render_header("search")
    from .pages.search import build_search
    build_search()


@ui.page("/quality")
def page_quality() -> None:
    """Страница «Качество» — review-очередь книг, каскадные действия, удаление."""
    render_header("quality")
    from .pages.quality import build_quality
    build_quality()


@ui.page("/tokens")
def page_tokens() -> None:
    """Страница «Токены» — управление subscriber/read/import/write токенами (W5)."""
    render_header("tokens")
    from .pages.tokens import build_tokens
    build_tokens()


@ui.page("/users")
def page_users(from_request: str = "") -> None:
    """Страница «Пользователи» — учётные записи консоли (admin-only, Ф3.2).

    036 Ф2 (Q2): `?from_request=<id>` — предзаполнение формы создания
    из одобренной заявки (передаётся в build_users).
    """
    render_header("users")
    from .pages.users_page import build_users
    build_users(prefill_request_id=from_request)


@ui.page("/requests")
def page_requests() -> None:
    """Страница «Заявки на доступ» — окно админа (036 Ф2)."""
    render_header("requests")
    from .pages.requests_page import build_requests
    build_requests()


@ui.page("/documents")
def page_documents() -> None:
    """Страница «Документы» — администрирование хранилища (bibliography Ф5c1, admin-only)."""
    render_header("documents")
    from .pages.documents import build_documents
    build_documents()


# ── Start ───────────────────────────────────────────────────

# kb-console-roles Ф2: users-стор + bootstrap админа из env (идемпотентно).
USERS_STORE = UserStore(users_file=CONSOLE_USERS_FILE or None)
USERS_STORE.bootstrap_from_env(CONSOLE_ADMIN_USER, CONSOLE_ADMIN_PASSWORD)
_USERS_PRESENT = USERS_STORE.has_users()

# Ф3.2: стор в runtime-модуле для identity-хелперов (импорт app.py из
# unit-контекста невозможен — ui.run side-effect; runtime чист).
from .core import runtime as _runtime

_runtime.USERS_STORE = USERS_STORE

# Interlock-режим auth: module-level ДО ui.run — невалидный CONSOLE_AUTH
# (ValueError) или required без пароля и без юзеров (RuntimeError) роняют
# процесс на старте, а не на первом запросе. Непустой users-стор → per-user
# auth ON; CONSOLE_PASSWORD при этом игнорируется (warning в interlock).
AUTH_MODE = resolve_auth_mode(
    CONSOLE_PASSWORD, CONSOLE_AUTH, CONSOLE_HOST, users_present=_USERS_PRESENT
)

# Ф3.2 + 035: стор и режим auth в runtime-модуле (identity-хелперы, header).
_runtime.USERS_STORE = USERS_STORE
_runtime.AUTH_MODE = AUTH_MODE

# 035: роуты страницы входа (GET /login, POST /api/login, POST /api/logout)
# + 036: публичная заявка (POST /api/access-request, 3-слойный guard).
# Стор заявок: SQLite volume, миграции на старте (fail-fast user_version).
from .config import (
    CONSOLE_ACCESS_REQUESTS_DB,
    CONSOLE_ACCESS_REQUESTS_MAX,
    CONSOLE_ACCESS_REQUESTS_RETENTION_DAYS,
)
from .core.access_requests import AccessRequestStore

REQUESTS_STORE = AccessRequestStore(
    CONSOLE_ACCESS_REQUESTS_DB,
    cap=CONSOLE_ACCESS_REQUESTS_MAX,
    retention_days=CONSOLE_ACCESS_REQUESTS_RETENTION_DAYS,
)
REQUESTS_STORE.migrate()

# 036 Ф2: стор в runtime для страниц /requests, /users (prefill), печати.
_runtime.REQUESTS_STORE = REQUESTS_STORE

login_page.register_routes(
    auth_mode=AUTH_MODE,
    users=USERS_STORE,
    password=CONSOLE_PASSWORD,
    admin_contact=CONSOLE_ADMIN_CONTACT,
    trust_xff=CONSOLE_TRUST_XFF,
    requests_store=REQUESTS_STORE,
)

# 036 Ф2: API статусов + печатная карточка (admin-гейты в хендлерах).
from .pages import requests_page, requests_print

requests_page.register_api(core.app)
requests_print.register_print_route(core.app)

# bibliography Ф4c: консоль-прокси выдачи документов — GET/HEAD /documents/{sha256}.
# Гейт зоны/роль-ключ — в хендлере (documents_proxy); неаутентифицированные —
# 302 /login от ConsoleAuthMiddleware (существующее поведение консоли).
from .documents_proxy import register_documents_proxy

register_documents_proxy(core.app)

# 035 §3б: секрет подписи cookie-сессий (env → файл в volume → ephemeral)
# и ЯВНЫЕ параметры cookie: абсолютные 12ч (дефолт Starlette 14 суток
# отклонён), SameSite=Lax, https_only=False — двойной контур доступа
# (TLS-фасад :8443 и loopback HTTP :8085; cookie всегда подписан+httponly).
STORAGE_SECRET = resolve_storage_secret(CONSOLE_STORAGE_SECRET, base_path=CONSOLE_USERS_FILE)
_SESSION_KWARGS = {"max_age": 12 * 3600, "same_site": "lax", "https_only": False}

# Порядок middleware: Starlette add_middleware = insert(0) → последний
# добавленный = самый внешний. ConsoleAuth регистрируем ПЕРВОЙ (внутренняя),
# RequestLog — ПОСЛЕДНЕЙ (внешняя) → RequestLog логирует и 401-отказы
# (brute-force-видимость в [REQ]-логах). Безусловная регистрация:
# режим off = чистый транзит (нулевой оверхед). SessionMiddleware будет
# добавлена ПОЗЖЕ всех через ui.run(storage_secret=…) — самая внешняя:
# ConsoleAuth уже видит распакованный scope["session"] (http и websocket).
core.app.add_middleware(
    ConsoleAuthMiddleware,
    password=CONSOLE_PASSWORD,
    mode=AUTH_MODE,
    users=USERS_STORE,
)

# Глобальный request logger для отладки upload.
core.app.add_middleware(RequestLogMiddleware)

# 037 Ф0: тёмно-зелёная тема консоли (SSOT components/theme.py) —
# Quasar-brand всех @ui.page + глобальный CSS (shared=True) до ui.run.
theme.apply(core.app)

ui.run(
    host=CONSOLE_HOST,
    port=CONSOLE_PORT,
    title="MCP Knowledge Console",
    reload=False,
    show=False,
    storage_secret=STORAGE_SECRET,
    session_middleware_kwargs=_SESSION_KWARGS,
)
