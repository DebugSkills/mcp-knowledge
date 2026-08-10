"""Консоль очереди импортов (13.21) — карточки операций с poll + отмена.

Карточки: ✅ done / ❌ error + причина / 🔄 running (фаза+прогресс) / ⏳ queued.
Крестик: для done/error/queued → удалить на сервере (POST /imports/{id}/remove) + локальная
  перерисовка; для running → отмена (POST cancel).
Кнопка «убрать все» → удалить все done/error/cancelled на сервере (POST /imports/remove-finished)
  + локальная перерисовка.

P0-4: ui.context.client.on_disconnect(_cleanup_timers) — паттерн import_page.py:227-237.
"""

# ruff: noqa: BLE001, S110
from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient

# Интервал опроса: 1s когда есть running, 5s в idle
POLL_FAST = 1.0
POLL_SLOW = 5.0

# Статус → иконка
_STATUS_ICONS: dict[str, str] = {
    "queued": "⏳",
    "running": "🔄",
    "done": "✅",
    "error": "❌",
    "cancelled": "🚫",
}

# Статус → CSS-класс текста
_STATUS_COLORS: dict[str, str] = {
    "queued": "text-grey",
    "running": "text-primary",
    "done": "text-positive",
    "error": "text-negative",
    "cancelled": "text-orange",
}


def build_import_queue() -> ui.element:
    """Создать консоль очереди импортов.

    Returns:
        Контейнер (ui.column), который можно разместить на странице.
    """
    container = ui.column().classes("w-full")
    _timers: list[ui.timer] = []
    _client_cache: dict[str, Any] = {}  # mutable ref for client

    async def _fetch_queue(client: MCPClient) -> list[dict]:
        """GET /imports → список операций."""
        try:
            return await client.list_imports()
        except Exception:
            return []

    async def _cancel_running(import_id: str, client: MCPClient) -> None:
        """POST /imports/{id}/cancel."""
        try:
            await client.cancel_import(import_id)
            ui.notify(f"Отмена импорта {import_id[:8]}...", type="warning")
        except Exception as exc:
            ui.notify(f"Ошибка отмены: {exc}", type="negative")

    def _render_cards(records: list[dict]) -> None:
        """Отрисовать карточки из records."""
        container.clear()
        if not records:
            with container:
                ui.label("Очередь импорта пуста").classes("text-grey text-body2 q-pa-sm")
            return

        # Кнопка «убрать все»
        has_done_or_error = any(r.get("status") in ("done", "error", "cancelled") for r in records)
        with container:
            top_row = ui.row().classes("w-full items-center q-mb-sm")
            with top_row:
                ui.label("📦 Очередь импорта").classes("text-subtitle2")
                ui.space()
                if has_done_or_error:
                    def _clear_finished() -> None:
                        task = asyncio.ensure_future(_do_clear_finished(records))
                        task.add_done_callback(lambda t: t.exception())

                    ui.button("Убрать все", icon="clear_all", on_click=_clear_finished).props("flat dense size=sm")

            # Карточки
            for rec in records:
                _render_card(records, rec)

    def _render_card(records: list[dict], rec: dict) -> None:
        """Отрисовать одну карточку операции."""
        status = rec.get("status", "queued")
        name = rec.get("name", "—")
        import_id = rec.get("import_id", "")
        phase = rec.get("phase", "")
        imported = rec.get("imported", 0)
        total = rec.get("total", 0)
        error_text = rec.get("error", "")

        icon = _STATUS_ICONS.get(status, "❓")
        color = _STATUS_COLORS.get(status, "")

        with ui.card().classes("w-full q-pa-sm q-mb-xs"), ui.row().classes("w-full items-center"):
            # Иконка + название
            with ui.column().classes("col-grow"):
                ui.label(f"{icon} {name}").classes(f"text-body2 {color}")
                # Подробности
                detail_parts: list[str] = []
                if status == "running" and phase:
                    detail_parts.append(f"фаза: {phase}")
                if status == "running" and total:
                    pct = (imported / total * 100) if total else 0
                    detail_parts.append(f"{imported}/{total} ({pct:.0f}%)")
                if status == "error" and error_text:
                    detail_parts.append(error_text[:100])
                if status == "queued":
                    detail_parts.append("ожидание...")
                if detail_parts:
                    ui.label(" · ".join(detail_parts)).classes("text-caption text-grey")

            # Прогресс-бар для running
            if status == "running" and total:
                ui.linear_progress(
                    value=(imported / total) if total else 0,
                ).props("rounded").classes("w-24")

            # Крестик (убрать/отмена)
            if status == "running":
                ui.button(
                    icon="close",
                    on_click=lambda rid=import_id: _handle_cancel(rid),
                ).props("flat dense round size=sm").tooltip("Отменить импорт")
            elif status in ("done", "error", "cancelled"):
                def _remove_local(rid: str = import_id) -> None:
                    task = asyncio.ensure_future(_do_remove(rid, records))
                    task.add_done_callback(lambda t: t.exception())

                ui.button(
                    icon="close",
                    on_click=_remove_local,
                ).props("flat dense round size=sm").tooltip("Удалить из списка")
            elif status == "queued":
                def _remove_queued(rid: str = import_id) -> None:
                    task = asyncio.ensure_future(_do_remove(rid, records))
                    task.add_done_callback(lambda t: t.exception())

                ui.button(
                    icon="close",
                    on_click=_remove_queued,
                ).props("flat dense round size=sm").tooltip("Убрать из очереди")

    def _handle_cancel(import_id: str) -> None:
        """Запустить отмену running-импорта."""
        task = asyncio.ensure_future(_do_cancel(import_id))
        task.add_done_callback(lambda t: t.exception())  # prevent unhandled exception

    async def _do_cancel(import_id: str) -> None:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            await _cancel_running(import_id, client)
        finally:
            await client.close()

    async def _do_remove(import_id: str, records: list[dict]) -> None:
        """POST /imports/{id}/remove → удалить запись на сервере + перерисовать."""
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.remove_import(import_id)
            if result.get("removed"):
                records[:] = [r for r in records if r.get("import_id") != import_id]
                _render_cards(records)
        except Exception as exc:
            ui.notify(f"Ошибка удаления: {exc}", type="negative")
        finally:
            await client.close()

    async def _do_clear_finished(records: list[dict]) -> None:
        """POST /imports/remove-finished → удалить все завершённые на сервере + перерисовать."""
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.remove_finished()
            removed = result.get("removed", 0)
            if removed > 0:
                ui.notify(f"Удалено записей: {removed}", type="positive")
            records[:] = [r for r in records if r.get("status") not in ("done", "error", "cancelled")]
            _render_cards(records)
        except Exception as exc:
            ui.notify(f"Ошибка очистки: {exc}", type="negative")
        finally:
            await client.close()

    # ── Poll timer ──────────────────────────────────────────
    _poll_timer: ui.timer | None = None

    async def _poll() -> None:
        """Периодический опрос очереди."""
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            queue = await _fetch_queue(client)
            _render_cards(queue)
        except Exception:
            pass
        finally:
            await client.close()

    def _start_poll() -> None:
        nonlocal _poll_timer
        if _poll_timer is not None:
            return
        _poll_timer = ui.timer(POLL_FAST, _poll)
        _timers.append(_poll_timer)

    def _cleanup_timers() -> None:
        """P0-4: отмена всех таймеров при закрытии/навигации."""
        for t in _timers:
            try:
                t.cancel()
            except Exception:
                pass
        _timers.clear()

    ui.context.client.on_disconnect(_cleanup_timers)
    _start_poll()

    return container
