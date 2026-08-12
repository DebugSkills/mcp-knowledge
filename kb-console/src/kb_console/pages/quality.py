"""Страница «Качество» — review-очередь книг, агрегат по parent, каскадные действия.

Фаза 13.14: вкладка в kb-console для управления качеством контента.
Использует review_queue_books (агрегат книг), resolve_quality_issue (cascade),
run_quality_scan, delete_entry (cascade).

Фаза 13.15: root-фикс зависания сервера — run_quality_scan теперь возвращает
мгновенный ответ {"status": "started", "scan_id": "..."}; прогресс отслеживается
через poll GET /quality/scan/progress с прогресс-баром и счётчиками.
Фаза 13.16: DRY — build_scan_progress + _LEVEL_COLORS из components/progress_panel.py.
_scanned флаг для однократного on_done обновления очереди.
Паттерны:
- Авто-обновление: @ui.refreshable + ui.timer (status.py)
- Спиннер: ui.spinner visible toggle (import_page.py)
- HITL-диалог: bare await, non-persistent (НЕ оборачивать в контейнеры)
- on_disconnect cleanup: timer.cancel()
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from ..components.progress_panel import build_scan_progress
from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.data_cache import cache
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
    # Пагинация секций: book_id → текущая страница (0-based)
    section_pages: dict[str, int] = {}

    # 13.15: состояние скана для блокировки кнопки и прогресс-бара
    scan_state: dict[str, Any] = {"status": None, "scan_id": None}

    @ui.refreshable
    def render_queue() -> None:
        _render_queue(latest.get("data", {}), latest.get("filters", {}), expanded_cache, render_queue.refresh, section_pages)

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
                # Task 1: check version before cache fetch
                await cache.check_version(client)
                filters = latest.get("filters", {})
                domain = filters.get("domain", "")
                subject = filters.get("subject", "")
                # R4: filter-dependent cache key
                cache_key = f"quality:{domain}:{subject}" if (domain or subject) else "quality"
                data = await cache.get(
                    cache_key,
                    lambda c=client, f=filters: c.review_queue_books(
                        domain=f.get("domain"),
                        subject=f.get("subject"),
                        limit=50,
                    ),
                    ttl=30,
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

    # 13.16: прогресс скана через build_scan_progress (DRY)
    scan_progress_container = ui.column().classes("w-full q-mb-md")

    # Refreshable-блок очереди
    render_queue()

    # Автообновление
    refresh_timer = ui.timer(QUALITY_REFRESH_SECONDS, refresh)
    ui.timer(0.1, refresh, once=True)

    def _cleanup_timers() -> None:
        refresh_timer.cancel()
    ui.context.client.on_disconnect(_cleanup_timers)


# ── Helpers ─────────────────────────────────────────────────


async def _run_scan(on_done, scan_state: dict, progress_container, scan_btn) -> None:
    """Запустить quality scan с live-прогрессом (13.15, DRY 13.16).

    Использует build_scan_progress из progress_panel.py для поллинга
    и рендера прогресс-бара (вместо дублирующего кода).
    Кнопка скана блокируется пока status=started/running.
    """
    scan_btn.disable()

    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
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

        scan_state["status"] = "running"
        scan_state["scan_id"] = result["scan_id"]
        ui.notify(f"Скан {result['scan_id']} запущен", type="info")

        # 13.16: DRY — build_scan_progress вместо дублирующего _poll_progress
        scan_client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        build_scan_progress(
            client=scan_client,
            on_done=lambda: _on_scan_done(scan_btn, scan_state, on_done),
        )

    except Exception as exc:
        scan_btn.enable()
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _on_scan_done(scan_btn, scan_state: dict, on_done) -> None:
    """Callback при завершении скана: разблокировка кнопки + инвалидация кеша + обновление очереди."""
    scan_btn.enable()
    scan_state["status"] = None
    scan_state["scan_id"] = None
    # R4: инвалидация кеша после скана (stale_scores изменились)
    cache.invalidate_all()
    await on_done()


async def _apply_filters(domain: str, subject: str, latest: dict, refresh_fn) -> None:
    """Применить фильтры и обновить очередь."""
    latest["filters"] = {
        "domain": domain.strip() if domain else None,
        "subject": subject.strip() if subject else None,
    }
    ui.notify("Фильтры применены", type="info")
    await refresh_fn()


async def _reset_filters(domain_input, subject_input, latest: dict, refresh_fn) -> None:
    """Сбросить фильтры и обновить очередь."""
    domain_input.value = ""
    subject_input.value = ""
    latest["filters"] = {}
    ui.notify("Фильтры сброшены", type="info")
    await refresh_fn()


def _render_queue(
    data: dict,
    filters: dict,
    expanded_cache: dict[str, list[dict]],
    refresh_fn,
    section_pages: dict[str, int],
) -> None:
    """Отрисовать очередь книг (review_queue_books).

    Args:
        data: результат review_queue_books (books[], total_books, total_stale_sections)
        filters: текущие фильтры
        expanded_cache: кэш развёрнутых книг
        refresh_fn: callable для перерисовки refreshable-блока
        section_pages: book_id → текущая страница пагинации секций
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
        _render_book_card(book, expanded_cache, refresh_fn, section_pages)


def _render_book_card(book: dict, expanded_cache: dict[str, list[dict]], refresh_fn, section_pages: dict[str, int]) -> None:
    """Отрисовать карточку одной книги (компактная однострочная вёрстка).

    P4: одна строка — title+чипы слева, прогресс+кнопки-иконки справа.
    Высота карточки сокращена (без отдельного ряда кнопок и широких прогресс-баров).
    """
    book_id = book.get("book_id", "")
    title = book.get("title", book_id)
    domain = book.get("domain", "")
    subject = book.get("subject", "")
    stale_fraction = book.get("stale_fraction", 0.0)
    max_score = book.get("max_score", 0.0)
    total_sections = book.get("total_sections", 0)
    status = book.get("status", "published")
    top_sections = book.get("top_sections", [])
    is_expanded = book_id in expanded_cache

    with ui.card().classes("w-full q-mb-xs"):
        with ui.row().classes("items-center w-full no-wrap gap-2"):
            # Левая часть: title + чипы + мета (сжато)
            with ui.column().classes("flex-1 min-w-0"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(title).classes("text-subtitle1 text-bold ellipsis")
                    if domain:
                        ui.chip(domain).props("outline dense size=sm")
                    if subject:
                        ui.chip(subject).props("outline dense size=sm")
                    if status == "deprecated":
                        ui.badge("deprecated").props("color=grey")
                    else:
                        ui.badge("published").props("color=green")
                ui.label(
                    f"{stale_fraction:.0%} устарело · max {max_score:.2f} · {total_sections} секц."
                ).classes("text-caption text-grey")

            # Правая часть: компактный прогресс + иконочные кнопки
            with ui.column().classes("items-end gap-1"):
                ui.linear_progress(stale_fraction).props("size=xs").classes("w-40")
                with ui.row().classes("gap-1 no-wrap"):
                    ui.button(
                        "▾" if is_expanded else "▸",
                        on_click=lambda bid=book_id, secs=top_sections: _toggle_expand(bid, secs, expanded_cache, section_pages, refresh_fn),
                    ).props("flat dense").tooltip("Развернуть секции")
                    if status != "deprecated":
                        ui.button("✅", on_click=lambda bid=book_id: _resolve_book(bid)).props("flat dense color=positive").tooltip("Актуально")
                    ui.button("📦", on_click=lambda bid=book_id: _deprecate_book(bid)).props("flat dense color=warning").tooltip("Устарело")
                    if status == "deprecated":
                        ui.button("♻️", on_click=lambda bid=book_id: _restore_book(bid)).props("flat dense color=info").tooltip("Восстановить")
                    ui.button("🗑", on_click=lambda bid=book_id, s_cnt=total_sections: _confirm_delete(bid, s_cnt)).props("flat dense color=negative").tooltip("Удалить")

        # Развёрнутые секции книги
        if is_expanded:
            sections = expanded_cache.get(book_id, [])
            _render_sections(sections, book_id, section_pages, refresh_fn)


def _render_sections(sections: list[dict], parent_book_id: str, section_pages: dict[str, int], refresh_fn) -> None:
    """Отрисовать секции книги с пагинацией.

    Страница хранится в section_pages (closure-переменная build_quality),
    чтобы кнопки пагинации вызывали перерисовку всей очереди, а не
    рендерили элементы в слот кнопки.
    """
    if not sections:
        ui.label("Нет устаревших секций").classes("text-grey q-ml-lg")
        return

    page = section_pages.get(parent_book_id, 0)
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

    # Пагинация секций: через set_page → refresh_fn (перерисовка всей очереди)
    if total_pages > 1:
        with ui.row().classes("gap-2 q-ml-xl q-mt-sm"):
            if page > 0:
                ui.button(
                    "← Пред.",
                    on_click=lambda p=page-1, bid=parent_book_id: _set_section_page(bid, p, section_pages, refresh_fn),
                ).props("flat dense")
            ui.label(f"Стр. {page + 1}/{total_pages}").classes("text-caption text-grey")
            if page < total_pages - 1:
                ui.button(
                    "След. →",
                    on_click=lambda p=page+1, bid=parent_book_id: _set_section_page(bid, p, section_pages, refresh_fn),
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


def _set_section_page(book_id: str, page: int, section_pages: dict[str, int], refresh_fn) -> None:
    """Установить страницу пагинации секций и перерисовать очередь."""
    section_pages[book_id] = page
    refresh_fn()


def _toggle_expand(
    book_id: str,
    top_sections: list[dict],
    expanded_cache: dict[str, list[dict]],
    section_pages: dict[str, int],
    refresh_fn,
) -> None:
    """Развернуть/свернуть секции книги."""
    if book_id in expanded_cache:
        del expanded_cache[book_id]
        section_pages.pop(book_id, None)  # Сброс страницы при сворачивании
    else:
        expanded_cache[book_id] = top_sections
    refresh_fn()
