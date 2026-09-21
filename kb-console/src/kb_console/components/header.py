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

    kb-console-roles Ф3.2: пункты с min_role выше роли текущего
    пользователя скрыты (ROUTES 4-tuple; legacy = admin → видно всё).
    """
    from ..core.identity import ROLE_LEVEL, current_role
    from ..pages import ROUTES  # lazy import: ломает circular chain pages↔components

    role = ROLE_LEVEL.get(current_role(), 0)
    with ui.row().classes("items-center gap-2 q-mb-md w-full") as _header:
        for path, label, _builder, min_role in ROUTES:
            if role < ROLE_LEVEL.get(min_role, 0):
                continue
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
