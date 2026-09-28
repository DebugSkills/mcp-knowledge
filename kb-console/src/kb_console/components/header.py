"""Общий навигационный хедер для всех страниц kb-console.

Используется каждой @ui.page для единого интерфейса навигации.
Активная кнопка подсвечена (color="primary"), остальные — flat.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

LOGOUT_JS = (
    "fetch('/api/logout',{method:'POST'}).then(()=>{window.location='/login'})"
)
"""Выход (035 Ф2): POST /api/logout → Set-Cookie session=null → /login.

Stateless signed-cookie: старое значение cookie технически replay-валидно
до max_age (12ч) — задокументировано в README (P2-7/R9).
"""


def should_show_logout(session_ident: dict[str, Any] | None, auth_mode: str) -> bool:
    """Кнопка «Выйти» видна только при session-identity в auth-режиме.

    Basic-клиентам кнопка не показывается: браузер шлёт креды на каждом
    запросе — «выход» без очистки кэша кредов бессмыслен.
    """
    return auth_mode == "on" and session_ident is not None


def render_header(active: str) -> None:
    """Отрисовать навигационный хедер с кнопками-ссылками.

    Args:
        active: slug активной страницы ("status", "books", "import", "search").
                Соответствует последнему сегменту пути (без /).

    kb-console-roles Ф3.2: пункты с min_role выше роли текущего
    пользователя скрыты (ROUTES 4-tuple; legacy = admin → видно всё).
    035 Ф2: справа — username из сессии + кнопка «Выйти» (только для
    session-identity; Basic-клиенты её не видят).
    """
    from ..config import APP_COPYRIGHT
    from ..core import runtime as _runtime
    from ..core.identity import ROLE_LEVEL, current_role, session_identity
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
        ui.element("div").classes("grow")
        ui.label(APP_COPYRIGHT).classes("text-grey-5 self-center text-caption")
        ident = session_identity()
        if should_show_logout(ident, _runtime.AUTH_MODE):
            ui.label(ident["username"]).classes("text-grey-7 self-center")
            ui.button(
                "Выйти",
                on_click=lambda: ui.run_javascript(LOGOUT_JS),
            ).props("flat color=negative")
