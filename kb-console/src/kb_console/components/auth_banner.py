"""auth_banner — баннер отказа ключа (401/403) с ручным возобновлением.

Q9-паттерн (tokens.py:149-167): баннер живёт в собственном refreshable,
показ/скрытие — через refresh(). Не зависит от identity (R7).

Кнопка «Проверить и продолжить» = ручной liveness-пинг GET /data-version
(ровно 1 запрос на нажатие — AC#1/OQ-5):
  - 200 → блок снят, баннер скрыт, on_resume() (страница перезапускает
    таймер/поллеры и обновляет данные);
  - 401/403 → ключ всё ещё мёртв — баннер остаётся;
  - транспорт → notify «сервер недоступен», баннер остаётся.

Возобновление ТОЛЬКО ручное (кнопка или F5) — авто-зондов нет (§7.10).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.auth_state import AuthError, ForbiddenError, get_auth_state
from ..core.mcp_client import MCPClient

_log = logging.getLogger(__name__)

_TEXT_401 = (
    "❌ Ошибка аутентификации: неверный API-ключ (401). "
    "Автообновление остановлено. Проверьте ключ и нажмите "
    "«Проверить и продолжить» или обновите страницу (F5)."
)
_TEXT_403 = (
    "⛔ Доступ запрещён: недостаточно прав (403). "
    "Автообновление остановлено. Обратитесь к администратору "
    "за ключом с нужным уровнем доступа."
)


class AuthBanner:
    """Баннер отказа ключа для одной страницы/компонента.

    Использование (в контексте слота страницы)::

        banner = AuthBanner(key_ref="global", on_resume=my_restart)
        banner.mount()          # монтирует контейнер (скрыт)
        poll_step(..., on_auth_blocked=banner.show)
    """

    def __init__(
        self,
        *,
        key_ref: str = "global",
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        self._key_ref = key_ref
        self._on_resume = on_resume
        self._visible = False
        self._message = ""

    # ── mount / render ──────────────────────────────────────

    def mount(self) -> None:
        """Смонтировать контейнер баннера в текущий NiceGUI-слот."""
        self._container = ui.column().classes("w-full")
        with self._container:
            self._render_inner()

    def _render_inner(self) -> None:
        if not self._visible:
            self._container.visible = False
            return
        self._container.visible = True
        with ui.banner(type="negative").classes("w-full"):
            ui.label(self._message)
            with ui.row().classes("gap-2"):
                ui.button(
                    "Проверить и продолжить",
                    on_click=self._on_check_and_continue,
                ).props("dense outline")
                ui.button("Обновить страницу (F5)", on_click=lambda: ui.navigate.reload()).props(
                    "dense flat"
                )

    # ── show / hide ─────────────────────────────────────────

    def show(self, error: AuthError) -> None:
        """Показать баннер (колбэк on_auth_blocked из poll_step)."""
        self._visible = True
        self._message = _TEXT_403 if isinstance(error, ForbiddenError) else _TEXT_401
        self._refresh()

    def hide(self) -> None:
        """Скрыть баннер (успешное возобновление)."""
        self._visible = False
        self._message = ""
        self._refresh()

    def _refresh(self) -> None:
        container = getattr(self, "_container", None)
        if container is None:
            return
        container.clear()
        with container:
            self._render_inner()

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def message(self) -> str:
        return self._message

    # ── ручной liveness-пинг (AC#1: ровно 1 запрос) ─────────

    async def _on_check_and_continue(self) -> None:
        state = get_auth_state()
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            # Ровно один GET /data-version; 200 → жив, 401/403 → нет.
            await client.get_data_version()
        except AuthError:
            ui.notify("Ключ всё ещё не проходит проверку", type="warning")
            return
        except Exception as exc:  # noqa: BLE001 — транспорт и прочее: не возобновляем
            ui.notify(f"Сервер недоступен: {exc}", type="negative")
            return
        finally:
            await client.close()

        state.unblock(key_ref=self._key_ref)
        self.hide()
        ui.notify("Соединение восстановлено — обновление возобновлено", type="positive")
        if self._on_resume is not None:
            self._on_resume()
