"""Точка входа kb-console — NiceGUI приложение.

Запуск:
    python -m kb_console.app

Многостраничная архитектура (V4a): каждая вкладка — отдельный @ui.page.
Reload сохраняет раздел (в отличие от ui.tabs, которые сбрасываются на первую).
"""

from __future__ import annotations

from nicegui import core, ui
from starlette.middleware.base import BaseHTTPMiddleware

from .components.header import render_header
from .config import CONSOLE_PORT


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


# ── Start ───────────────────────────────────────────────────

# Глобальный request logger для отладки upload.
core.app.add_middleware(RequestLogMiddleware)

ui.run(
    host="0.0.0.0",
    port=CONSOLE_PORT,
    title="MCP Knowledge Console",
    reload=False,
    show=False,
)
