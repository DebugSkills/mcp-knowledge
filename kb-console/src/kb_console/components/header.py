"""Общий навигационный хедер для всех страниц kb-console.

Используется каждой @ui.page для единого интерфейса навигации.
Активная кнопка подсвечена (color="primary"), остальные — flat.
"""

from __future__ import annotations

from nicegui import ui


def render_header(active: str) -> None:
    """Отрисовать навигационный хедер с кнопками-ссылками.

    Args:
        active: slug активной страницы ("status", "books", "import", "search").
                Соответствует последнему сегменту пути (без /).
    """
    from ..pages import ROUTES  # lazy import: ломает circular chain pages↔components

    with ui.row().classes("items-center gap-2 q-mb-md w-full") as _header:
        for path, label, _builder in ROUTES:
            slug = path.lstrip("/")
            is_active = slug == active
            btn = ui.button(
                label,
                on_click=lambda p=path: ui.navigate.to(p),
            )
            if is_active:
                btn.props("flat color=primary")
            else:
                btn.props("flat")
