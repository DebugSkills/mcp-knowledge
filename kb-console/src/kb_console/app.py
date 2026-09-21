"""Точка входа kb-console — NiceGUI приложение.

Запуск:
    python -m kb_console.app

Многостраничная архитектура (V4a): каждая вкладка — отдельный @ui.page.
Reload сохраняет раздел (в отличие от ui.tabs, которые сбрасываются на первую).
"""

from __future__ import annotations

from nicegui import core, ui
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import ConsoleAuthMiddleware, resolve_auth_mode
from .components.header import render_header
from .config import (
    CONSOLE_ADMIN_PASSWORD,
    CONSOLE_ADMIN_USER,
    CONSOLE_AUTH,
    CONSOLE_HOST,
    CONSOLE_PASSWORD,
    CONSOLE_PORT,
    CONSOLE_USERS_FILE,
)
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
def page_users() -> None:
    """Страница «Пользователи» — учётные записи консоли (admin-only, Ф3.2)."""
    render_header("users")
    from .pages.users_page import build_users
    build_users()


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

# Порядок middleware: Starlette add_middleware = insert(0) → последний
# добавленный = самый внешний. ConsoleAuth регистрируем ПЕРВОЙ (внутренняя),
# RequestLog — ПОСЛЕДНЕЙ (внешняя) → RequestLog логирует и 401-отказы
# (brute-force-видимость в [REQ]-логах). Безусловная регистрация:
# режим off = чистый транзит (нулевой оверхед).
core.app.add_middleware(
    ConsoleAuthMiddleware,
    password=CONSOLE_PASSWORD,
    mode=AUTH_MODE,
    users=USERS_STORE,
)

# Глобальный request logger для отладки upload.
core.app.add_middleware(RequestLogMiddleware)

ui.run(
    host=CONSOLE_HOST,
    port=CONSOLE_PORT,
    title="MCP Knowledge Console",
    reload=False,
    show=False,
)
