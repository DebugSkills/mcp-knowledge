"""Точка входа kb-console — NiceGUI приложение.

Запуск:
    python -m kb_console.app
"""

from __future__ import annotations

from nicegui import core, ui
from starlette.middleware.base import BaseHTTPMiddleware

from .config import CONSOLE_PORT
from .pages import PAGES


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Логирует все входящие HTTP-запросы для отладки."""

    async def dispatch(self, request, call_next):
        print(f"[REQ] {request.method} {request.url.path}")
        response = await call_next(request)
        return response

# ── Routes ──────────────────────────────────────────────────


@ui.page("/")
def index() -> None:
    """Главная страница с табами по реестру PAGES."""
    with ui.tabs().classes("w-full") as tabs:
        for label, _ in PAGES:
            ui.tab(label)

    with ui.tab_panels(tabs, value=PAGES[0][0]).classes("w-full"):
        for label, builder in PAGES:
            with ui.tab_panel(label):
                builder()


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
