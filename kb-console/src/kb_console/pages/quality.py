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

import asyncio
from typing import Any

from nicegui import ui

from ..components.progress_panel import build_scan_progress
from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.data_cache import cache
from ..core.mcp_client import MCPClient

# Фаза 1 dedup: выбранные issue_id для пакетного скрытия дублей (сессия)
_selected_issues: set[str] = set()

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

    @ui.refreshable
    def render_issues() -> None:
        # refresh_fn — полный async refresh (повторный fetch + инвалидация кэша),
        # чтобы после resolve/ignore счётчик и список обновлялись немедленно.
        _render_issues(latest.get("issues", {}), refresh)

    # Фаза 2 dedup: ревью-очередь dup-пар (🟢 пачка / 🟡 сомнительные)
    @ui.refreshable
    def render_review_pairs() -> None:
        _render_review_pairs(latest.get("review_pairs", {}), refresh)

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
                # Параллельная загрузка очереди книг, issues и dup-ревью (Фаза 2)
                data, issues, review_pairs = await asyncio.gather(
                    cache.get(
                        cache_key,
                        lambda c=client, f=filters: c.review_queue_books(
                            domain=f.get("domain"),
                            subject=f.get("subject"),
                            limit=50,
                        ),
                        ttl=30,
                    ),
                    cache.get(
                        "quality:issues",
                        lambda c=client: c.list_quality_issues(status="open", limit=50),
                        ttl=30,
                    ),
                    cache.get(
                        "quality:review_pairs",
                        lambda c=client: c.review_duplicate_pairs(limit=200),
                        ttl=30,
                    ),
                )
                latest["data"] = data
                latest["issues"] = issues
                latest["review_pairs"] = review_pairs
            finally:
                await client.close()
            render_queue.refresh()
            render_issues.refresh()
            render_review_pairs.refresh()
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

    # Refreshable-блок найденных проблем (issues) — над очередью книг
    render_issues()

    # Фаза 2 dedup: ревью-очередь dup-пар (🟢 пачка / 🟡 сомнительные)
    render_review_pairs()

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


def _diff_highlight(snippet_a: str, snippet_b: str) -> str:
    """Визуальный diff двух сниппетов через difflib.ndiff (Фаза 2 dedup).

    Возвращает строки вида '+ добавлено', '- удалено', '  общее' —
    рендер через ui.code с моноширинным шрифтом.
    """
    import difflib

    lines_a = (snippet_a or "").splitlines()
    lines_b = (snippet_b or "").splitlines()
    diff = list(difflib.ndiff(lines_a, lines_b))
    # Ограничиваем вывод (сниппеты до 30 строк — diff до ~60 строк)
    return "\n".join(diff[:60])


async def _approve_green_batch(green_batch: list[dict], refresh_fn) -> None:
    """HITL: утвердить 🟢-пачку (Фаза 2) → bulk_deprecate_duplicates."""
    issue_ids = [p["issue_id"] for p in green_batch]
    if not issue_ids:
        ui.notify("Нет пар для утверждения", type="warning")
        return

    async def _do() -> None:
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=120.0)
            try:
                result = await client.bulk_deprecate_duplicates(
                    issue_ids=issue_ids,
                    reason="Green batch approved by operator (dedup review)",
                )
                if result.get("resolved"):
                    ui.notify(
                        f"Скрыто: {result.get('deprecated_count', 0)}, "
                        f"закрыто issues: {result.get('issues_closed', 0)}",
                        type="positive",
                    )
                    cache.invalidate("quality:issues")
                    cache.invalidate("quality:review_pairs")
                    await refresh_fn()
                else:
                    ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
            finally:
                await client.close()
        except Exception as exc:
            ui.notify(f"Ошибка: {exc}", type="negative")

    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("✅ Утвердить 🟢-пачку дублей").classes("text-h6")
        ui.label(
            f"{len(issue_ids)} пар с высокой уверенностью (exact content-hash "
            f"или cosine ≥ 0.97 с guards). Записи-дубли будут скрыты из поиска "
            f"(обратимо через ♻️ restore), все их dup-issues закрыты. "
            f"Контент .md не удаляется."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm(dlg=dialog):
                dlg.close()
                await _do()

            ui.button(
                f"✅ Утвердить все ({len(issue_ids)})",
                on_click=_confirm,
            ).props("flat color=positive")
    await dialog


async def _resolve_yellow_pair(pair: dict, action: str, refresh_fn) -> None:
    """Действие по 🟡-паре: 'deprecate' | 'not_dup' (Фаза 2).

    - deprecate: скрыть source (bulk_deprecate_duplicates по issue_id)
    - not_dup:   закрыть issue как «не дубль» (resolve, обратимо по статусу)
    """
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=60.0)
        try:
            if action == "deprecate":
                result = await client.bulk_deprecate_duplicates(
                    issue_ids=[pair["issue_id"]],
                    reason="Yellow pair: source hidden by operator (dedup)",
                )
                ok = result.get("resolved")
                msg = f"Скрыто: {result.get('deprecated_count', 0)} записей"
            else:
                result = await client.resolve_quality_issue(
                    action="resolve",
                    issue_id=pair["issue_id"],
                    reason="not a duplicate by operator",
                )
                ok = result.get("resolved")
                msg = "Issue закрыта (не дубль)"
            if ok:
                ui.notify(msg, type="positive" if action == "deprecate" else "info")
                cache.invalidate("quality:issues")
                cache.invalidate("quality:review_pairs")
                await refresh_fn()
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


def _render_review_pairs(data: dict, refresh_fn) -> None:
    """Отрисовать ревью-очередь dup-пар: 🟢 пачка + 🟡 сомнительные (Фаза 2)."""
    if not isinstance(data, dict) or not data:
        return
    green = data.get("green_batch", [])
    yellow = data.get("yellow_pairs", [])
    total_open = data.get("total_open", 0)

    if not green and not yellow:
        return

    ui.label("Дубли: ревью-очередь").classes("text-h6 q-mb-sm q-mt-md")

    # 🟢 Пачка «Утвердить все»
    if green:
        with ui.card().classes("w-full q-mb-sm q-pa-sm"), ui.row().classes(
            "items-center gap-2 no-wrap"
        ):
            ui.badge("🟢 высокая уверенность").props("color=green")
            ui.label(
                f"{len(green)} пар — exact content-hash или cosine ≥ 0.97 "
                f"(без антонимов, standalone)"
            ).classes("text-body2")
            ui.space()
            ui.button(
                f"✅ Утвердить все ({len(green)})",
                on_click=lambda g=green: _approve_green_batch(g, refresh_fn),
            ).props("flat dense color=positive")

    # 🟡 Сомнительные — каждая пара с diff
    for pair in yellow:
        with ui.card().classes("w-full q-mb-xs q-pa-sm"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.badge("🟡 требует внимания").props("color=orange")
                ui.label(pair.get("source_kid", "")).classes("text-subtitle2 ellipsis")
                ui.label("≈").classes("text-grey")
                ui.label(pair.get("target_kid", "")).classes("text-subtitle2 ellipsis")
                if pair.get("cosine") is not None:
                    ui.chip(f"cosine={pair['cosine']:.3f}").props("outline dense size=sm")
                ui.label(f"канон: {pair.get('recommended_canonical', '?')}").classes(
                    "text-caption text-grey"
                )
            # Визуальный diff (difflib.ndiff)
            diff_text = _diff_highlight(
                pair.get("source_snippet", ""), pair.get("target_snippet", "")
            )
            if diff_text:
                ui.code(diff_text, language="diff").classes("w-full text-caption")
            with ui.row().classes("gap-2 q-mt-sm"):
                ui.button(
                    "✅ Скрыть source",
                    on_click=lambda p=pair: _resolve_yellow_pair(p, "deprecate", refresh_fn),
                ).props("flat dense color=positive").tooltip(
                    f"Скрыть {pair.get('source_kid', '')} (обратимо через restore)"
                )
                ui.button(
                    "❌ Не дубль",
                    on_click=lambda p=pair: _resolve_yellow_pair(p, "not_dup", refresh_fn),
                ).props("flat dense color=negative").tooltip("Закрыть issue: записи различны")
                ui.button(
                    "⏭ Позже",
                    on_click=lambda: ui.notify("Отложено — issue остаётся в очереди", type="info"),
                ).props("flat dense color=grey")

    if total_open and (green or yellow):
        ui.label(f"Показаны: {len(green) + len(yellow)} из {total_open} open dup-issues").classes(
            "text-caption text-grey q-mb-sm"
        )


def _render_issues(data: dict, refresh_fn) -> None:
    """Отрисовать панель найденных проблем качества (issues).

    Args:
        data: результат list_quality_issues (issues[], total)
        refresh_fn: callable для перерисовки refreshable-блока
    """
    issues = data.get("issues", []) if isinstance(data, dict) else []
    total = data.get("total", len(issues)) if isinstance(data, dict) else 0

    ui.label("Найденные проблемы (issues)").classes("text-h6 q-mb-sm q-mt-md")

    if not issues:
        ui.label("Issues не найдены").classes("text-grey text-caption")
        return

    ui.label(f"Всего issues: {total}").classes("text-subtitle2 q-mb-sm")

    # P0 (B1): bulk-кнопки «Игнорировать все <тип>» — чистка накопленного шума
    types_present = sorted({i.get("type", "") for i in issues if i.get("type")})
    if types_present:
        with ui.row().classes("gap-2 q-mb-sm items-center"):
            for itype in types_present:
                count = sum(1 for i in issues if i.get("type") == itype)
                ui.button(
                    f"⊘ Игнорировать все {itype} ({count})",
                    on_click=lambda t=itype: _bulk_ignore_type(t, refresh_fn),
                ).props("flat dense color=grey").tooltip(
                    f"Пакетно пометить все open-issues типа {itype} как ignored (обратимо, только issues.jsonl)"
                )
            # Фаза 1 dedup: пакетное скрытие выбранных дублей (checkbox на карточках)
            dup_count = sum(1 for i in issues if i.get("type") == "duplicate")
            if dup_count:
                ui.button(
                    "📦 Пакетно скрыть выбранные",
                    on_click=lambda: _bulk_deprecate_selected(refresh_fn),
                ).props("flat dense color=warning").tooltip(
                    "Скрыть отмеченные дубликаты (deprecate, обратимо через restore; закрывает все их dup-issues)"
                )

    for issue in issues:
        _render_issue_card(issue, refresh_fn)


def _render_issue_card(issue: dict, refresh_fn) -> None:
    """Отрисовать карточку одного issue (type-chip, knowledge_id, detail, действия).

    Фаза 1 dedup: для duplicate-issues добавляются 📦 (deprecate дубля,
    обратимо) и checkbox для пакетного скрытия выбранных.
    """
    issue_id = issue.get("issue_id", "")
    issue_type = issue.get("type", "")
    knowledge_id = issue.get("knowledge_id", "")
    detail = issue.get("detail", "")
    detected_at = issue.get("detected_at", "")
    severity = issue.get("severity", "")
    is_duplicate = issue_type == "duplicate"

    with ui.card().classes("w-full q-mb-xs"), ui.row().classes("items-center w-full no-wrap gap-2"):
        # Checkbox для пакетного скрытия (только duplicate)
        if is_duplicate:
            ui.checkbox(on_change=lambda e, iid=issue_id: _set_selected(iid, e.value)).props(
                "dense size=sm"
            )
        with ui.column().classes("flex-1 min-w-0"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                if issue_type:
                    ui.chip(issue_type).props("outline dense size=sm color=orange")
                ui.label(knowledge_id).classes("text-subtitle2 ellipsis")
                if severity:
                    ui.chip(severity).props("outline dense size=sm")
            if detail:
                ui.label(detail).classes("text-caption text-grey")
            if detected_at:
                ui.label(f"Обнаружено: {detected_at[:19]}").classes("text-caption text-grey")
        with ui.row().classes("gap-1 no-wrap"):
            if is_duplicate:
                ui.button(
                    "📦", on_click=lambda iid=issue_id, kid=knowledge_id, d=detail: _deprecate_duplicate(
                        iid, kid, d, refresh_fn
                    ),
                ).props("flat dense color=warning").tooltip("Скрыть дубликат (deprecate, обратимо)")
            ui.button(
                "✅", on_click=lambda iid=issue_id: _resolve_issue(iid, refresh_fn),
            ).props("flat dense color=positive").tooltip("Исправлено")
            ui.button(
                "⊘", on_click=lambda iid=issue_id: _ignore_issue(iid, refresh_fn),
            ).props("flat dense color=grey").tooltip("Игнорировать")


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


async def _resolve_issue(issue_id: str, refresh_fn) -> None:
    """Разрешить issue (action=resolve — помечена исправленной)."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="resolve",
                issue_id=issue_id,
                reason="Issue resolved by operator",
            )
            if result.get("resolved"):
                ui.notify("Issue помечена исправленной", type="positive")
                cache.invalidate("quality:issues")
                await refresh_fn()
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _ignore_issue(issue_id: str, refresh_fn) -> None:
    """Игнорировать issue (action=ignore — пропущена)."""
    try:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.resolve_quality_issue(
                action="ignore",
                issue_id=issue_id,
                reason="Issue ignored by operator",
            )
            if result.get("resolved"):
                ui.notify("Issue игнорирована", type="info")
                cache.invalidate("quality:issues")
                await refresh_fn()
            else:
                ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
        finally:
            await client.close()
    except Exception as exc:
        ui.notify(f"Ошибка: {exc}", type="negative")


async def _bulk_ignore_type(issue_type: str, refresh_fn) -> None:
    """Пакетно игнорировать все open-issues данного типа (P0 B1).

    HITL-подтверждение с preview-счётчиком (число open issues типа).
    Действие обратимо (status=ignored, только issues.jsonl — контент
    и Qdrant не трогаются).
    """
    # Preview: сколько open issues типа (через list_quality_issues с фильтром)
    try:
        preview_client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            preview = await preview_client.list_quality_issues(
                types=[issue_type], status="open", limit=1,
            )
            total = preview.get("total", 0)
        finally:
            await preview_client.close()
    except Exception:
        total = 0

    async def _do_bulk() -> None:
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=60.0)
            try:
                result = await client.bulk_resolve_issues(
                    types=[issue_type],
                    action="ignore",
                    reason=f"Bulk ignore of {issue_type} issues by operator",
                )
                if result.get("resolved"):
                    ui.notify(
                        f"Игнорировано: {result.get('count', 0)}/{result.get('total', 0)} "
                        f"issues типа {issue_type}",
                        type="info",
                    )
                    cache.invalidate("quality:issues")
                    await refresh_fn()
                else:
                    ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
            finally:
                await client.close()
        except Exception as exc:
            ui.notify(f"Ошибка: {exc}", type="negative")

    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label(f"⚠️ Пакетное игнорирование: {issue_type}").classes("text-h6")
        ui.label(
            f"Будут помечены как ignored все open-issues типа «{issue_type}» "
            f"({total} шт.). Только issues.jsonl — контент и поиск не изменятся. "
            f"Обратимо: статус можно вернуть."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm_ignore(dlg=dialog):
                dlg.close()
                await _do_bulk()

            ui.button(
                f"⊘ Игнорировать {total}",
                on_click=_confirm_ignore,
            ).props("flat color=grey")
    await dialog


def _set_selected(issue_id: str, value: bool) -> None:
    """Отметить/снять issue_id в сессии для пакетного скрытия (Фаза 1 dedup)."""
    if value:
        _selected_issues.add(issue_id)
    else:
        _selected_issues.discard(issue_id)


async def _deprecate_duplicate(issue_id: str, knowledge_id: str, detail: str, refresh_fn) -> None:
    """HITL-подтверждение и deprecate одного дубля (📦, Фаза 1 dedup).

    Обратимо через restore; сервер сам закрывает ВСЕ open dup-issues записи.
    """
    # Извлекаем target из detail («Possible duplicate of <target> (cosine=...)»)
    target = ""
    cosine = ""
    if "Possible duplicate of " in detail:
        rest = detail.split("Possible duplicate of ", 1)[1]
        target = rest.split(" (", 1)[0]
    if "cosine=" in detail:
        cosine = detail.split("cosine=", 1)[1].rstrip(")")

    async def _do() -> None:
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=60.0)
            try:
                result = await client.bulk_deprecate_duplicates(
                    issue_ids=[issue_id],
                    reason="Duplicate hidden by operator (dedup)",
                )
                if result.get("resolved"):
                    ui.notify(
                        f"Скрыто: {result.get('deprecated_count', 0)} записей, "
                        f"закрыто issues: {result.get('issues_closed', 0)}",
                        type="positive",
                    )
                    cache.invalidate("quality:issues")
                    await refresh_fn()
                else:
                    ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
            finally:
                await client.close()
        except Exception as exc:
            ui.notify(f"Ошибка: {exc}", type="negative")

    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("📦 Скрыть дубликат").classes("text-h6")
        ui.label(
            f"Запись: {knowledge_id}\n"
            f"Дубликат: {target or '?'}"
            + (f" (cosine={cosine})" if cosine else "")
            + "\n\nБудет помечена как deprecated (скрыта из поиска). "
            "Обратимо через ♻️ restore. Контент .md не удаляется. "
            "Все её open dup-issues будут закрыты."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm_single(dlg=dialog):
                dlg.close()
                await _do()

            ui.button(
                "📦 Скрыть",
                on_click=_confirm_single,
            ).props("flat color=warning")
    await dialog


async def _bulk_deprecate_selected(refresh_fn) -> None:
    """HITL-подтверждение и пакетное скрытие выбранных дублей (Фаза 1 dedup)."""
    selected = list(_selected_issues)
    if not selected:
        ui.notify("Ничего не выбрано — отметьте checkbox на карточках дублей", type="warning")
        return

    async def _do() -> None:
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY, timeout=120.0)
            try:
                result = await client.bulk_deprecate_duplicates(
                    issue_ids=selected,
                    reason="Batch hide duplicates by operator (dedup)",
                )
                if result.get("resolved"):
                    ui.notify(
                        f"Скрыто: {result.get('deprecated_count', 0)} записей, "
                        f"закрыто issues: {result.get('issues_closed', 0)}",
                        type="positive",
                    )
                    _selected_issues.clear()
                    cache.invalidate("quality:issues")
                    await refresh_fn()
                else:
                    ui.notify(f"Ошибка: {result.get('error', 'неизвестно')}", type="negative")
            finally:
                await client.close()
        except Exception as exc:
            ui.notify(f"Ошибка: {exc}", type="negative")

    with ui.dialog() as dialog, ui.card().classes("q-pa-md"):
        ui.label("📦 Пакетное скрытие дублей").classes("text-h6")
        ui.label(
            f"Будут скрыты (deprecated) дубликаты по {len(selected)} отмеченным issue(s). "
            "Обратимо через ♻️ restore. Контент .md не удаляется."
        ).classes("q-mb-md")
        with ui.row().classes("gap-2"):
            ui.button("Отмена", on_click=dialog.close).props("flat")

            async def _confirm_multi(dlg=dialog):
                dlg.close()
                await _do()

            ui.button(
                f"📦 Скрыть {len(selected)}",
                on_click=_confirm_multi,
            ).props("flat color=warning")
    await dialog


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
