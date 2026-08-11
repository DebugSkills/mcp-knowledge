"""Консоль очереди импортов (13.21) — карточки операций с poll + отмена + лог.

Карточки: ✅ done / ❌ error + причина / 🔄 running (фаза+прогресс) / ⏳ queued.
Крестик: для done/error/queued → удалить на сервере (POST /imports/{id}/remove) + локальная
  перерисовка; для running → отмена (POST cancel).
Кнопка «убрать все» → удалить все done/error/cancelled на сервере (POST /imports/remove-finished)
  + локальная перерисовка.

P3 (code-2026-08-10-305): expand/collapse карточек + lazy log.
- _expanded_ids: set[str] — manual toggle (DBD-паттерн quality.py:253-326).
- _log_cache: dict[str, list] — кеш логов (invalidate при status/phase change).
- _expanded_poll (2s) — обновление логов для running+expanded карточек.
- _LOG_LEVEL_COLORS — цвета строк лога (DRY: расширение _STATUS_COLORS).

P0-4: ui.context.client.on_disconnect(_cleanup_timers) — паттерн import_page.py:227-237.
"""

# ruff: noqa: BLE001, S110, SIM117
from __future__ import annotations

import asyncio

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient

# Интервал опроса: 1s когда есть running, 5s в idle
POLL_FAST = 1.0
POLL_SLOW = 5.0
EXPANDED_POLL = 2.0  # P3: обновление лога для running+expanded

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

# P3: Уровни лога → CSS-класс (DBD: единый словарь со _STATUS_COLORS)
_LOG_LEVEL_COLORS: dict[str, str] = {
    "info": "text-grey",
    "warning": "text-orange",
    "error": "text-negative",
}

# P3: Максимальное число строк лога в развёрнутой карточке
MAX_LOG_LINES = 200


def build_import_queue() -> ui.element:
    """Создать консоль очереди импортов.

    Returns:
        Контейнер (ui.column), который можно разместить на странице.
    """
    container = ui.column().classes("w-full")
    _timers: list[ui.timer] = []

    # P3: состояние expand/collapse (DBD-паттерн из quality.py)
    _expanded_ids: set[str] = set()
    _log_cache: dict[str, list[dict]] = {}  # import_id → log lines
    _prev_status: dict[str, str] = {}  # import_id → last seen status (для cache invalidate)

    async def _fetch_queue(client: MCPClient) -> list[dict]:
        """GET /imports → список операций."""
        try:
            return await client.list_imports()
        except Exception:
            return []

    async def _fetch_log(import_id: str, client: MCPClient) -> list[dict] | None:
        """GET /imports/{id}/log → лог или None."""
        try:
            data = await client.get_import_log(import_id)
            return data.get("log", []) if data else None
        except Exception:
            return None

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
        """Отрисовать одну карточку операции (свёрнутую или развёрнутую)."""
        status = rec.get("status", "queued")
        name = rec.get("name", "—")
        import_id = rec.get("import_id", "")
        phase = rec.get("phase", "")
        imported = rec.get("imported", 0)
        total = rec.get("total", 0)
        error_text = rec.get("error", "")
        is_expanded = import_id in _expanded_ids

        icon = _STATUS_ICONS.get(status, "❓")
        color = _STATUS_COLORS.get(status, "")

        with ui.card().classes("w-full q-pa-sm q-mb-xs"):
            # ── Header row: expand toggle + info + progress + close ──
            with ui.row().classes("w-full items-center"):
                # Шеврон (expand/collapse toggle)
                chevron = "▾" if is_expanded else "▸"
                ui.button(
                    chevron,
                    on_click=lambda rid=import_id: _toggle_expand(rid),
                ).props("flat dense round size=sm").tooltip(
                    "Свернуть" if is_expanded else "Развернуть лог"
                )

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
                    # code-2026-08-11-queue: convert/analyze — итог из summary_text
                    # (result исключён из GET /imports — lean payload)
                    if status == "done" and rec.get("summary_text"):
                        detail_parts.append(rec["summary_text"])
                    elif status == "done" and total:
                        detail_parts.append(f"{total} секций")
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

            # ── Expanded: log section ──
            if is_expanded:
                log_entries = _log_cache.get(import_id, [])
                if log_entries:
                    with ui.scroll_area().classes("w-full q-mt-sm").style("max-height: 300px"):
                        with ui.column().classes("w-full gap-0"):
                            for entry in log_entries[-MAX_LOG_LINES:]:
                                level = entry.get("level", "info")
                                ts = entry.get("ts", "")
                                text = entry.get("text", "")
                                log_color = _LOG_LEVEL_COLORS.get(level, "text-grey")
                                line = f"[{ts}] {text}" if ts else text
                                ui.label(line).classes(
                                    f"text-caption font-mono {log_color}"
                                ).style("white-space: pre-wrap; word-break: break-word; line-height: 1.4")
                else:
                    with ui.row().classes("q-mt-sm"):
                        ui.spinner(size="sm")
                        ui.label("Загрузка лога...").classes("text-caption text-grey")
                        # Trigger async fetch (will appear on next render cycle)
                        task = asyncio.ensure_future(_load_log_async(import_id))
                        task.add_done_callback(lambda t: t.exception())

    def _toggle_expand(import_id: str) -> None:
        """Toggle expand/collapse для карточки (DBD-паттерн quality.py:553)."""
        if import_id in _expanded_ids:
            _expanded_ids.discard(import_id)
        else:
            _expanded_ids.add(import_id)
            # Invalidate cache при раскрытии (чтобы перезагрузить лог)
            _log_cache.pop(import_id, None)

    async def _load_log_async(import_id: str) -> None:
        """Асинхронно загрузить лог для import_id (best-effort)."""
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            log = await _fetch_log(import_id, client)
            if log is not None:
                _log_cache[import_id] = log
        except Exception:
            pass
        finally:
            await client.close()

    def _handle_cancel(import_id: str) -> None:
        """Запустить отмену running-импорта."""
        task = asyncio.ensure_future(_do_cancel(import_id))
        task.add_done_callback(lambda t: t.exception())

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
                _expanded_ids.discard(import_id)
                _log_cache.pop(import_id, None)
                _prev_status.pop(import_id, None)
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
            # Очистка кеша для удалённых
            removed_ids = {r.get("import_id", "") for r in records
                          if r.get("status") in ("done", "error", "cancelled")}
            for rid in removed_ids:
                _expanded_ids.discard(rid)
                _log_cache.pop(rid, None)
                _prev_status.pop(rid, None)
            records[:] = [r for r in records if r.get("status") not in ("done", "error", "cancelled")]
            _render_cards(records)
        except Exception as exc:
            ui.notify(f"Ошибка очистки: {exc}", type="negative")
        finally:
            await client.close()

    # ── P3: Invalidate log cache when status/phase changes ──
    def _invalidate_stale_cache(records: list[dict]) -> None:
        """Сбросить кеш лога для карточек, у которых изменился status или phase."""
        for rec in records:
            import_id = rec.get("import_id", "")
            if not import_id:
                continue
            current_status = rec.get("status", "") + "|" + rec.get("phase", "")
            prev = _prev_status.get(import_id, "")
            if current_status != prev:
                _log_cache.pop(import_id, None)
                _prev_status[import_id] = current_status

    # ── P3: Expanded log poll (2s) — обновление логов для running+expanded ──
    _expanded_timer: ui.timer | None = None

    async def _expanded_poll() -> None:
        """Периодически обновлять логи для running+expanded карточек."""
        if not _expanded_ids:
            return
        # Находим running+expanded import_ids
        # (records доступны только через _poll — храним _last_records)
        # managed inside _poll

    # ── Poll timer ──────────────────────────────────────────
    _poll_timer: ui.timer | None = None
    _last_records: list[dict] = []

    async def _poll() -> None:
        """Периодический опрос очереди (1s)."""
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            queue = await _fetch_queue(client)
            _last_records = queue
            _invalidate_stale_cache(queue)
            _render_cards(queue)
        except Exception:
            pass
        finally:
            await client.close()

    # P3: Expanded poll — fetch logs for running+expanded cards
    async def _expanded_poll_impl() -> None:
        """Обновить логи для running+expanded карточек (2s)."""
        running_expanded = [
            rid for rid in _expanded_ids
            for rec in _last_records
            if rec.get("import_id") == rid and rec.get("status") == "running"
        ]
        if not running_expanded:
            return
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            for import_id in running_expanded:
                log = await _fetch_log(import_id, client)
                if log is not None:
                    _log_cache[import_id] = log
        except Exception:
            pass
        finally:
            await client.close()

    def _start_poll() -> None:
        nonlocal _poll_timer, _expanded_timer
        if _poll_timer is not None:
            return
        _poll_timer = ui.timer(POLL_FAST, _poll)
        _timers.append(_poll_timer)
        # P3: log poll (2s) — separate timer
        _expanded_timer = ui.timer(EXPANDED_POLL, _expanded_poll_impl)
        _timers.append(_expanded_timer)

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
