"""Страница «Качество» — review-очередь книг, агрегат по parent, каскадные действия.

Фаза 13.14: вкладка в kb-console для управления качеством контента.
Использует review_queue_books (агрегат книг), resolve_quality_issue (cascade),
run_quality_scan, delete_entry (cascade).

Фаза 13.15: root-фикс зависания сервера — run_quality_scan теперь возвращает
мгновенный ответ {"status": "started", "scan_id": "..."}; прогресс отслеживается
через poll GET /quality/scan/progress с прогресс-баром и счётчиками.
_refreshing флаг (≤1 in-flight refresh), блокировка кнопки скана при running.

Паттерны:
- Авто-обновление: @ui.refreshable + ui.timer (status.py)
- Спиннер: ui.spinner visible toggle (import_page.py)
- HITL-диалог: bare await, non-persistent (НЕ оборачивать в контейнеры)
- on_disconnect cleanup: timer.cancel()
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.mcp_client import MCPClient

# Интервал автообновления для quality (дольше, чем статус — данные тяжелее).
QUALITY_REFRESH_SECONDS = max(REFRESH_SECONDS * 3, 30)

# 13.15: интервал опроса прогресса скана
SCAN_POLL_INTERVAL = 1.0

# Пагинация секций при разворачивании книги
SECTIONS_PER_PAGE = 100


def build_quality() -> None:
    """Построить страницу «Качество».

    Автообновление через @ui.refreshable: перерисовываются ТОЛЬКО элементы
    внутри refreshable-блока (diff), позиция скролла сохраняется.
    """

    latest: dict[str, Any] = {}
    # Кэш развёрнутых книг (book_id → list[section])
    expanded_cache: dict[str, list[dict]] = {}

    # 13.15: состояние скана для блокировки кнопки и прогресс-бара
    scan_state: dict[str, Any] = {"status": None, "scan_id": None}

    @ui.refreshable
    def render_queue() -> None:
        _render_queue(latest.get("data", {}), latest.get("filters", {}), expanded_cache)

    # 13.15: _refreshing флаг — ≤1 in-flight refresh
    _refreshing = False

    async def refresh() -> None:
        nonlocal _refreshing
        if _refreshing:
            return
        _refreshing = True
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
            try:
                filters = latest.get("filters", {})
                data = await client.review_queue_books(
                    domain=filters.get("domain"),
                    subject=filters.get("subject"),
                    limit=50,
                )
                latest["data"] = data
            finally:
                await client.close()
            render_queue.refresh()
        except RuntimeError as exc:
            if "parent slot" in str(exc):
                return
            raise
        finally:
            _refreshing = False

    # ── Layout ─────────────────────────────────────────────

    ui.label("Качество базы знаний").classes("text-h4 q-mb-md")

    # Top bar: скан + фильтры
    with ui.row().classes("gap-4 items-center q-mb-md"):
        scan_btn = ui.button("🔄 Запустить скан", on_click=lambda: _run_scan(
            refresh, scan_state, scan_progress_container, scan_btn,
        )).props("flat")
        ui.separator().props("vertical")
        ui.label("Фильтры:").classes("text-grey")
        domain_input = ui.input("Домен").props("dense").classes("w-32")
        subject_input = ui.input("Тема").props("dense").classes("w-32")
        ui.button("Применить", on_click=lambda: _apply_filters(
            domain_input.value, subject_input.value, latest, refresh
        )).props("flat")
        ui.button("Сбросить", on_click=lambda: _reset_filters(
            domain_input, subject_input, latest, refresh
        )).props("flat")
        ui.space()
        ui.label(f"Автообновление: каждые {QUALITY_REFRESH_SECONDS} сек.").classes("text-grey text-caption")

    # Спиннер глобальной загрузки
    global_spinner = ui.spinner("dots", size="lg").classes("q-mb-md")
    global_spinner.visible = False

    # 13.15: контейнер прогресса скана
    scan_progress_container = ui.column().classes("w-full q-mb-md")
    scan_progress_container.visible = False

    # Refreshable-блок очереди
    render_queue()

    # Автообновление
    refresh_timer = ui.timer(QUALITY_REFRESH_SECONDS, refresh)
    ui.timer(0.1, refresh, once=True)

    # 13.15: таймер опроса прогресса скана
    _scan_poll_timer: ui.timer | None = None

    def _cleanup_timers() -> None:
        refresh_timer.cancel()
        nonlocal _scan_poll_timer
        if _scan_poll_timer is not None:
            _scan_poll_timer.cancel()
            _scan_poll_timer = None
    ui.context.client.on_disconnect(_cleanup_timers)


# ── Helpers ─────────────────────────────────────────────────


async def _run_scan(on_done, scan_state: dict, progress_container, scan_btn) -> None:
    """Запустить quality scan с live-прогрессом (13.15).

    После старта: poll GET /quality/scan/progress раз в ~1 сек,
    рендер прогресс-бара + фазы + счётчика N/M.
    При done/error — остановка poll, показ финальных metrics.
    Кнопка скана блокируется пока status=started/running.
    """
    # Блокируем кнопку на время скана
    scan_btn.disable()

    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        # Шаг 1: запуск скана (мгновенный ответ)
        result = await client.run_quality_scan()
        if result.get("status") == "already_running":
            ui.notify(
                f"Скан уже выполняется (scan_id={result.get('scan_id', '?')})",
                type="warning",
            )
            scan_btn.enable()
            return
        if not result.get("scanned") or result.get("status") != "started":
            ui.notify(
                f"Ошибка запуска: {result.get('error', 'неизвестно')}",
                type="negative",
            )
            scan_btn.enable()
            return

        scan_id = result["scan_id"]
        scan_state["status"] = "running"
        scan_state["scan_id"] = scan_id
        ui.notify(f"Скан {scan_id} запущен", type="info")

        # Показываем контейнер прогресса
        progress_container.visible = True
        progress_container.clear()

        # Шаг 2: poll прогресса
        def stop_poll():
            if scan_state.get("poll_timer") is not None:
                scan_state["poll_timer"].cancel()
                scan_state["poll_timer"] = None

        async def _poll_progress() -> None:
            try:
                snapshot = await client.get_scan_progress()
                if snapshot is None:
                    return
                # Рендер прогресса
                progress_container.clear()
                with progress_container:
                    status = snapshot.get("status", "running")
                    phase = snapshot.get("phase", "?")
                    done_val = snapshot.get("imported", 0)
                    total_val = snapshot.get("total", 0)
                    percent = (done_val / total_val * 100) if total_val else 0
                    is_done = status in ("done", "error")

                    ui.label(
                        f"📊 Скан: {phase}  ·  {done_val}/{total_val} ({percent:.0f}%)"
                        + (f"  ·  {status}" if is_done else "")
                    ).classes("text-body2")
                    ui.linear_progress(
                        value=(done_val / total_val) if total_val else 0,
                    ).props("rounded").classes("w-full")

                    msgs = snapshot.get("messages", [])
                    if msgs:
                        with ui.column().classes("w-full q-mt-xs gap-0"):
                            for m in msgs[-5:]:
                                level = m.get("level", "info")
                                color = _LEVEL_COLORS.get(level, "text-grey")
                                ui.label(
                                    f"[{m.get('t', '')}] {m.get('text', '')}"
                                ).classes(f"text-caption font-mono {color}")

                    # Показ финальных metrics при done
                    if is_done:
                        summary = snapshot.get("summary", {})
                        metrics = summary.get("metrics", {}) if isinstance(summary, dict) else {}
                        if metrics:
                            with ui.row().classes("gap-4 q-mt-sm"):
                                ui.label(
                                    f"Файлов: {metrics.get('files_scanned', 0)}"
                                ).classes("text-caption")
                                ui.label(
                                    f"В очереди: {metrics.get('review_queue_size', 0)}"
                                ).classes("text-caption")
                                ui.label(
                                    f"Дублей: {metrics.get('duplicates_detected', 0)}"
                                ).classes("text-caption")
                                ui.label(
                                    f"Issues: {metrics.get('issues_created', 0)}"
                                ).classes("text-caption")

                        scan_state["status"] = status
                        scan_btn.enable()
                        stop_poll()
                        await on_done()  # обновляем очередь

                        if status == "error":
                            error_text = ""
                            for m in msgs:
                                if m.get("level") == "error":
                                    error_text = m.get("text", "")
                            ui.notify(
                                f"Скан завершился с ошибкой: {error_text or 'неизвестно'}",
                                type="negative",
                            )
                        else:
                            ui.notify("Скан завершён", type="positive")
            except Exception:
                pass  # best-effort poll

        scan_state["poll_timer"] = ui.timer(SCAN_POLL_INTERVAL, _poll_progress)

        # client живёт в замыкании _poll_progress — закрывается при stop_poll()

    except Exception as exc:
        scan_btn.enable()
        ui.notify(f"Ошибка: {exc}", type="negative")


# Уровни логов → CSS-классы (переиспользовано из import_page.py)
_LEVEL_COLORS = {
    "info": "text-grey",
    "warning": "text-orange",
    "error": "text-negative",
}


def _apply_filters(domain: str, subject: str, latest: dict, refresh_fn) -> None:
    """Применить фильтры и обновить очередь."""
    latest["filters"] = {
        "domain": domain.strip() if domain else None,
        "subject": subject.strip() if subject else None,
    }
    ui.notify("Фильтры применены", type="info")
    refresh_fn()


def _reset_filters(domain_input, subject_input, latest: dict, refresh_fn) -> None:
    """Сбросить фильтры и обновить очередь."""
    domain_input.value = ""
    subject_input.value = ""
    latest["filters"] = {}
    ui.notify("Фильтры сброшены", type="info")
    refresh_fn()


def _render_queue(
    data: dict,
    filters: dict,
    expanded_cache: dict[str, list[dict]],
) -> None:
    """Отрисовать очередь книг (review_queue_books).

    Args:
        data: результат review_queue_books (books[], total_books, total_stale_sections)
        filters: текущие фильтры
        expanded_cache: кэш развёрнутых книг
    """
    books = data.get("books", [])
    total_books = data.get("total_books", 0)
    total_stale = data.get("total_stale_sections", 0)

    # Сводка
    domain_filter = filters.get("domain", "")
    subject_filter = filters.get("subject", "")
    filter_text = ""
    if domain_filter:
        filter_text += f" домен={domain_filter}"
    if subject_filter:
        filter_text += f" тема={subject_filter}"

    ui.label(
        f"Всего книг с устаревшими секциями: {total_books} | "
        f"Устаревших секций: {total_stale}"
        + (f" | Фильтр:{filter_text}" if filter_text else "")
    ).classes("text-subtitle1 q-mb-sm")

    if not books:
        with ui.card().classes("w-full q-pa-lg"):
            ui.label("Очередь пуста — запустите скан для проверки качества").classes("text-grey text-h6")
        return

    # Карточки книг
    for book in books:
        _render_book_card(book, expanded_cache)


def _render_book_card(book: dict, expanded_cache: dict[str, list[dict]]) -> None:
    """Отрисовать карточку одной книги."""
    book_id = book.get("book_id", "")
    title = book.get("title", book_id)
    domain = book.get("domain", "")
    subject = book.get("subject", "")
    stale_fraction = book.get("stale_fraction", 0.0)
    max_score = book.get("max_score", 0.0)
    total_sections = book.get("total_sections", 0)
    status = book.get("status", "published")
    top_sections = book.get("top_sections", [])

    with ui.card().classes("w-full q-mb-sm"):
        with ui.row().classes("items-center w-full"):
            # Заголовок + домен/subject
            with ui.column().classes("flex-1"):  # noqa: SIM117
                with ui.row().classes("items-center gap-2"):
                    ui.label(title).classes("text-h6")
                    if domain:
                        ui.chip(domain).props("outline dense size=sm")
                    if subject:
                        ui.chip(subject).props("outline dense size=sm")
                    if status == "deprecated":
                        ui.badge("deprecated").props("color=grey")
                    else:
                        ui.badge("published").props("color=green")

            # Прогресс-бар устаревших секций
            with ui.column().classes("items-end"):
                ui.label(f"{stale_fraction:.0%} устарело").classes("text-caption text-grey")
                ui.linear_progress(stale_fraction).props("size=sm").classes("w-48")
                ui.label(
                    f"max-score: {max_score:.2f} | секций: {total_sections}"
                ).classes("text-caption text-grey")

        # Кнопки действий
        with ui.row().classes("gap-2"):
            # Развернуть / Свернуть секции
            is_expanded = book_id in expanded_cache
            ui.button(
                "▾ Секции" if is_expanded else "▸ Развернуть",
                on_click=lambda bid=book_id, secs=top_sections: _toggle_expand(bid, secs, expanded_cache),
            ).props("flat dense")

            ui.space()

            # «Актуально» — resolve (только для published)
            if status != "deprecated":
                ui.button(
                    "✅ Актуально",
                    on_click=lambda bid=book_id: _resolve_book(bid),
                ).props("flat dense color=positive")

            # «Устарело» — deprecate cascade
            ui.button(
                "📦 Устарело",
                on_click=lambda bid=book_id: _deprecate_book(bid),
            ).props("flat dense color=warning")

            # «Восстановить» — restore cascade (только для deprecated)
            if status == "deprecated":
                ui.button(
                    "♻️ Восстановить",
                    on_click=lambda bid=book_id: _restore_book(bid),
                ).props("flat dense color=info")

            # «Удалить» — delete cascade с HITL-подтверждением
            ui.button(
                "🗑 Удалить",
                on_click=lambda bid=book_id, s_cnt=total_sections: _confirm_delete(bid, s_cnt),
            ).props("flat dense color=negative")

        # Развёрнутые секции книги
        if is_expanded:
            sections = expanded_cache.get(book_id, [])
            _render_sections(sections, book_id)


def _render_sections(sections: list[dict], parent_book_id: str, page: int = 0) -> None:
    """Отрисовать секции книги с пагинацией."""
    if not sections:
        ui.label("Нет устаревших секций").classes("text-grey q-ml-lg")
        return

    start = page * SECTIONS_PER_PAGE
    page_sections = sections[start:start + SECTIONS_PER_PAGE]
    total_pages = (len(sections) - 1) // SECTIONS_PER_PAGE + 1

    ui.label(f"Секции ({len(sections)} всего):").classes("text-subtitle2 q-ml-lg q-mt-sm")

    for sec in page_sections:
        # Переменные замыкаем через default args
        sec_kid = sec.get("knowledge_id", "")
        sec_title = sec.get("title", sec_kid)
        sec_score = sec.get("staleness_score", 0.0)
        sec_reasons = sec.get("reasons", [])
        sec_updated = sec.get("updated_at", "")

        with ui.card().classes("q-ml-xl q-mb-xs w-full").props("flat bordered"), ui.row().classes("items-center w-full"):
                with ui.column().classes("flex-1"):
                    ui.label(sec_title).classes("text-body2 text-bold")
                    with ui.row().classes("gap-1"):
                        chip_label = f"score={sec_score:.2f}"
                        if sec_reasons:
                            chip_label += f" [{', '.join(sec_reasons[:2])}]"
                        ui.chip(chip_label).props("dense size=sm")
                    if sec_updated:
                        ui.label(f"Обновлено: {sec_updated[:19]}").classes("text-caption text-grey")
                # Кнопки для секции (без cascade)
                with ui.row().classes("gap-1"):
                    ui.button(
                        "📦", on_click=lambda kid=sec_kid: _deprecate_single(kid),
                    ).props("flat dense size=sm").tooltip("Пометить устаревшей")
                    ui.button(
                        "♻️", on_click=lambda kid=sec_kid: _restore_single(kid),
                    ).props("flat dense size=sm").tooltip("Восстановить")

    # Пагинация секций
    if total_pages > 1:
        with ui.row().classes("gap-2 q-ml-xl q-mt-sm"):
            if page > 0:
                ui.button(
                    "← Пред.",
                    on_click=lambda p=page-1, s=sections: _render_sections(s, parent_book_id, p),
                ).props("flat dense")
            ui.label(f"Стр. {page + 1}/{total_pages}").classes("text-caption text-grey")
            if page < total_pages - 1:
                ui.button(
                    "След. →",
                    on_click=lambda p=page+1, s=sections: _render_sections(s, parent_book_id, p),
                ).props("flat dense")


# ── Действия с книгами ──────────────────────────────────────


async def _resolve_book(book_id: str) -> None:
    """Пометить книгу актуальной (cascade — все секции)."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="restore",
                knowledge_id=book_id,
                cascade=True,
                reason="Book marked as current by operator",
            )
            if result.get("resolved"):
                ui.notify(
                    f"Книга актуальна (+{result.get('cascade_affected', 0)} секций восстановлено)",
                    type="positive",
                )
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _deprecate_book(book_id: str) -> None:
    """Пометить книгу устаревшей (cascade)."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="deprecate",
                knowledge_id=book_id,
                cascade=True,
                reason="Book deprecated by operator",
            )
            if result.get("resolved"):
                ui.notify(
                    f"Книга помечена устаревшей (+{result.get('cascade_affected', 0)} секций)",
                    type="warning",
                )
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _restore_book(book_id: str) -> None:
    """Восстановить deprecated книгу (cascade)."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="restore",
                knowledge_id=book_id,
                cascade=True,
                reason="Book restored by operator",
            )
            if result.get("resolved"):
                ui.notify(
                    f"Книга восстановлена (+{result.get('cascade_affected', 0)} секций)",
                    type="positive",
                )
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _deprecate_single(knowledge_id: str) -> None:
    """Пометить одну секцию устаревшей."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="deprecate",
                knowledge_id=knowledge_id,
                cascade=False,
                reason="Section deprecated by operator",
            )
            if result.get("resolved"):
                ui.notify("Секция помечена устаревшей", type="warning")
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _restore_single(knowledge_id: str) -> None:
    """Восстановить одну секцию."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="restore",
                knowledge_id=knowledge_id,
                cascade=False,
                reason="Section restored by operator",
            )
            if result.get("resolved"):
                ui.notify("Секция восстановлена", type="positive")
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _confirm_delete(book_id: str, section_count: int) -> None:
    """HITL-подтверждение удаления книги (🔴 TIER 3 irreversible)."""
    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("⚠️ Подтверждение удаления").classes("text-h6 text-negative")
        ui.label(
            f"Книга + до {section_count} секций будут перемещены в .trash/ "
            f"и удалены из поиска. Восстановление — вручную из .trash/."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")
            ui.button(
                "🗑 Удалить безвозвратно",
                on_click=lambda d=dialog, bid=book_id: _do_delete(d, bid),
            ).props("flat color=negative")
    # bare await — диалог non-persistent, без контейнерной обёртки
    await dialog


async def _do_delete(dialog, book_id: str) -> None:
    """Выполнить удаление после подтверждения."""
    dialog.close()
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=60.0)
        try:
            result = await client.delete_entry(book_id, cascade=True)
            if result.get("deleted"):
                ui.notify(
                    f"Удалено: книга + {result.get('cascade_deleted', 0)} секций → .trash/",
                    type="info",
                )
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


def _toggle_expand(
    book_id: str,
    top_sections: list[dict],
    expanded_cache: dict[str, list[dict]],
) -> None:
    """Развернуть/свернуть секции книги."""
    if book_id in expanded_cache:
        del expanded_cache[book_id]
    else:
        expanded_cache[book_id] = top_sections
    # Перерисовать очередь (refreshable не подхватывает изменение кэша автоматически)
    # Но кэш — это module-level dict, он сохраняется
    ui.notify("Обновите страницу для перерисовки секций", type="info")
