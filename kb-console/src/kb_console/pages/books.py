"""Страница «Книги» — список коллекций с метаданными + просмотр содержимого.

Variant A (13.10):
  - Список книг (list_collections): title, domain/subject, section_count, tags, updated_at.
  - Деталь книги (get_entry): TOC из children (sorted by sequence_number).
  - Секция (get_entry): полный markdown-контент.

13.11 UX:
  - Модалка non-persistent (крестик/Esc/фон) вместо inline master-detail.
  - Прелоадеры (spinners) при TOC/секции/списке.
  - Кнопка «Переименовать» в модалке (update_entry с санитизацией).

code-2026-08-11-queue-delete-emoji:
  - Кнопка «Удалить» с confirm-диалогом (delete_entry cascade=True).
  - Только nicegui icons (без emoji-дублей в кнопках/заголовках).

code-2026-08-11-book-fragments (Фаза 13.23):
  - Кнопка «Добавить раздел» в заголовке модалки.
  - Кнопки «Изменить»/«Удалить» на каждой строке TOC.
  - VersionConflict-обработка в «Изменить».
  - Удалён workaround прямого fetch (TOC теперь on-the-fly из Qdrant).
  - Сохранён fallback: TOC + notify при ненайденном фрагменте (NH-iter3-7).

render_book_detail / show_book_dialog переиспользуются страницей «Поиск»
(кнопка «Открыть книгу» в результатах).
"""

from __future__ import annotations

import re

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.data_cache import cache
from ..core.mcp_client import MCPClient
from ..core.utils import _sanitize_title

# Пагинация TOC: книги бывают на тысячи секций — рендерим постранично,
# иначе NiceGUI-слот перегружается и рвётся websocket-handshake.
TOC_PAGE_SIZE = 100


# ── Вспомогательные чистые функции ──

def _find_section_child(children: list[dict], section_id: str | None) -> dict | None:
    """Найти child в списке children по knowledge_id.

    Вынесена из замыкания для unit-тестируемости (ui.* требует page-context).
    Чистая функция, без зависимостей от NiceGUI.

    Args:
        children: Список children (dict с полем knowledge_id).
        section_id: Искомый knowledge_id секции (или None).

    Returns:
        Найденный child-dict или None.
    """
    if section_id is None:
        return None
    return next((c for c in children if c.get("knowledge_id") == section_id), None)


# ── Переиспользуемый рендер детали книги (для «Книги» и диалога «Поиска») ──

async def render_book_detail(
    container: ui.element,
    client: MCPClient,
    collection_id: str,
    initial_section_id: str | None = None,
) -> None:
    """Отрисовать в container: TOC книги (постранично) или контент найденной секции.

    Args:
        container: Контейнер NiceGUI для рендера.
        client: MCPClient (собственный, не переиспользуемый).
        collection_id: knowledge_id коллекции.
        initial_section_id: Если задан — открыть эту секцию вместо TOC
            (Фаза 13.13: кнопка «Открыть фрагмент» в поиске).
    """
    container.clear()
    try:
        entry = await client.get_entry(collection_id)
    except Exception as exc:
        with container:
            ui.label(f"❌ Не удалось загрузить книгу: {exc}").classes("text-negative")
        return
    if "error" in entry:
        with container:
            ui.label(f"❌ {entry['error']}").classes("text-negative")
        return

    title = entry.get("title") or collection_id
    children = entry.get("children") or []
    children.sort(key=lambda c: c.get("sequence_number") or 0)
    total = len(children)

    async def _show_section(section: dict) -> None:
        """Показать контент секции (с защитой от закрытия диалога во время загрузки)."""
        # Прелоадер (Фаза C2): спиннер в dialog-context перед загрузкой секции
        container.clear()
        with container:
            ui.spinner(size="md").props("color=primary")
            ui.label("Загрузка секции…").classes("text-grey q-ml-sm")

        sec = None
        try:
            sec = await client.get_entry(section["knowledge_id"])
        except Exception as exc:
            sec = {"error": str(exc)}

        # Убираем спиннер и рендерим секцию (Фаза B4: guard от RuntimeError)
        try:
            container.clear()
            with container:
                async def _back_to_toc() -> None:
                    await render_book_detail(container, client, collection_id)

                ui.button("← Назад к оглавлению", on_click=_back_to_toc)
                ui.label(section.get("title", "—")).classes("text-h5 q-mt-md")
                ui.separator()

                if sec and "error" in sec:
                    ui.label(f"❌ {sec['error']}").classes("text-negative")
                    return
                ui.markdown(sec.get("content", "_(пусто)_") if sec else "_(пусто)_")
        except RuntimeError as e:
            if "parent slot" in str(e) or "has been deleted" in str(e):
                return  # диалог закрыт пользователем — молча выходим
            raise

    async def _show_edit_dialog(section: dict) -> None:
        """Диалог «Изменить раздел» — textarea с префиллом, VersionConflict-обработка."""
        section_id = section["knowledge_id"]
        # Загружаем актуальный контент секции
        sec_entry = None
        try:
            sec_entry = await client.get_entry(section_id)
        except Exception as exc:
            ui.notify(f"Не удалось загрузить раздел: {exc}", type="negative")
            return
        if sec_entry is None or "error" in sec_entry:
            ui.notify("Раздел не найден", type="warning")
            return

        current_content = sec_entry.get("content", "")
        current_version = sec_entry.get("version", 1)
        # Префилл: контент минус первый # заголовок
        heading_match = re.match(r"^#\s+.+?\n\n?", current_content)
        prefilled = current_content[heading_match.end():] if heading_match else current_content

        _edit_textarea = None
        _edit_save_btn = None

        async def _save_edit() -> None:
            if _edit_save_btn is not None:
                _edit_save_btn.disable()
            new_content = _edit_textarea.value or ""
            if not new_content.strip():
                ui.notify("Содержание не может быть пустым", type="warning")
                if _edit_save_btn is not None:
                    _edit_save_btn.enable()
                return
            try:
                result = await client.update_fragment(
                    section_id, content=new_content, version=current_version,
                )
            except Exception as exc:
                ui.notify(f"Ошибка обновления: {exc}", type="negative")
                if _edit_save_btn is not None:
                    _edit_save_btn.enable()
                return
            if result.get("conflict"):
                # VersionConflict → notify + закрыть диалог, перечитать секцию
                edit_dialog.close()
                ui.notify(
                    "Запись изменена кем-то другим — перечитайте и повторите",
                    type="warning",
                )
                await render_book_detail(container, client, collection_id)
                return
            edit_dialog.close()
            ui.notify("Раздел обновлён", type="positive")
            cache.invalidate("books")
            await render_book_detail(container, client, collection_id)

        with ui.dialog() as edit_dialog, ui.card().classes("w-[600px] max-w-[90vw]"):
            ui.label(f"Изменить: {section.get('title', '—')}").classes("text-h6")
            ui.label(f"ID: {section_id}  ·  Версия: {current_version}").classes("text-caption text-grey")
            _edit_textarea = ui.textarea(value=prefilled).classes("w-full").props("autogrow")
            with ui.row().classes("gap-2 q-mt-md"):
                _edit_save_btn = ui.button("Сохранить", on_click=_save_edit, icon="save").props("color=primary")
                ui.button("Отмена", on_click=edit_dialog.close).props("flat")
        edit_dialog.open()

    async def _show_delete_confirm(section: dict) -> None:
        """Confirm-диалог «Удалить раздел» (паттерн books.py:332)."""
        section_id = section["knowledge_id"]
        section_title = section.get("title", "—")

        async def _confirm_delete_section() -> None:
            confirm_dialog.close()
            try:
                result = await client.delete_fragment(section_id)
            except Exception as exc:
                ui.notify(f"Ошибка удаления: {exc}", type="negative")
                return
            if result.get("deleted"):
                cache.invalidate("books")
                ui.notify(f"Раздел «{section_title}» удалён", type="positive")
                await render_book_detail(container, client, collection_id)
            else:
                ui.notify(f"Ошибка: {result.get('error', '?')}", type="negative")

        with ui.dialog() as confirm_dialog, ui.card().classes("q-pa-md"):
            ui.label("Удаление раздела").classes("text-h6")
            ui.label(f"Раздел «{section_title}» ({section_id}) будет удалён. Продолжить?").classes("text-body2 q-mb-md")
            with ui.row().classes("justify-end"):
                ui.button("Отмена", on_click=confirm_dialog.close).props("flat")
                ui.button("Удалить", icon="delete", on_click=_confirm_delete_section).props("color=negative")
        confirm_dialog.open()

    def _render_toc_page(page: int) -> None:
        """Отрисовать страницу TOC (children[page*SIZE:(page+1)*SIZE])."""
        container.clear()
        pages_count = max(1, (total + TOC_PAGE_SIZE - 1) // TOC_PAGE_SIZE)
        start = page * TOC_PAGE_SIZE
        end = min(start + TOC_PAGE_SIZE, total)
        with container:
            ui.label(title).classes("text-h5")
            ui.label(f"{entry.get('domain', '—')}/{entry.get('subject', '—')}"
                     f"  ·  секций: {total}").classes("text-caption text-grey")
            ui.separator()
            if not children:
                ui.label("В книге нет секций (TOC пуст)").classes("text-grey")
                return
            with ui.list().classes("w-full"):
                for child in children[start:end]:
                    async def _open(c=child) -> None:
                        await _show_section(c)

                    with ui.item().props("clickable").on("click", _open).classes("text-body2"):
                        ui.label(f"#{child.get('sequence_number', '?')}  {child.get('title', '—')}")
                    # Кнопки действий на строке TOC (Фаза 13.23)
                    async def _edit_section(c=child) -> None:
                        await _show_edit_dialog(c)
                    async def _delete_section(c=child) -> None:
                        await _show_delete_confirm(c)
                    with ui.row().classes("gap-1"):
                        ui.button(icon="edit", on_click=_edit_section).props("flat dense size=sm")
                        ui.button(icon="delete", on_click=_delete_section).props("flat dense size=sm color=negative")
            with ui.row().classes("items-center q-mt-sm"):
                ui.button("← Пред.", on_click=lambda: _render_toc_page(max(0, page - 1))) \
                    .props("flat dense").set_enabled(page > 0)
                ui.label(f"Секции {start + 1}–{end} из {total}").classes("text-caption text-grey q-mx-md")
                ui.button("След. →", on_click=lambda: _render_toc_page(min(pages_count - 1, page + 1))) \
                    .props("flat dense").set_enabled(page < pages_count - 1)

    if initial_section_id:
        child = _find_section_child(children, initial_section_id)
        if child:
            await _show_section(child)
        else:
            # NH-iter3-7: фрагмент может быть в SSOT, но не в Qdrant (DLQ/сбой) — показать TOC + notify
            _render_toc_page(0)
            ui.notify("Фрагмент не найден в оглавлении — показана книга", type="warning")
    else:
        _render_toc_page(0)


async def show_book_dialog(collection_id: str, title: str | None = None, initial_section_id: str | None = None) -> None:
    """Открыть модальный диалог с содержимым книги или конкретной секции.

    Диалог создаётся прямо здесь (как в search.py — проверенный паттерн).
    Non-persistent: крестик, Esc, клик по фону.

    Args:
        collection_id: knowledge_id коллекции (книги).
        title: Заголовок диалога (опционально, fallback на collection_id).
        initial_section_id: Если задан — открыть эту секцию вместо TOC
            (Фаза 13.13: кнопка «Открыть фрагмент» в поиске).
    """
    import asyncio
    client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
    current_title: str = title or collection_id

    async def _close() -> None:
        dialog.close()
        await client.close()

    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-[90vw]"), ui.column().classes("w-full"):
        with ui.row().classes("items-center w-full justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("menu_book").classes("text-h6 text-primary")
                title_label = ui.label(current_title).classes("text-h6")
            with ui.row().classes("items-center gap-2"):
                rename_btn = ui.button("Переименовать", icon="edit").props("flat dense")
                add_section_btn = ui.button("Добавить раздел", icon="add").props("flat dense")
                async def _do_add_section() -> None:
                    _title_input = None
                    _content_textarea = None
                    _add_save_btn = None
                    async def _confirm_add() -> None:
                        raw_title = (_title_input.value or "").strip()
                        raw_content = (_content_textarea.value or "").strip()
                        if not raw_title:
                            ui.notify("Заголовок не может быть пустым", type="warning")
                            return
                        if not raw_content:
                            ui.notify("Содержание не может быть пустым", type="warning")
                            return
                        if _add_save_btn is not None:
                            _add_save_btn.disable()
                        try:
                            result = await client.add_fragment(collection_id, raw_title, raw_content)
                        except Exception as exc:
                            ui.notify(f"Ошибка добавления: {exc}", type="negative")
                            if _add_save_btn is not None:
                                _add_save_btn.enable()
                            return
                        if "error" in result:
                            ui.notify(f"Ошибка: {result['error']}", type="negative")
                            if _add_save_btn is not None:
                                _add_save_btn.enable()
                            return
                        add_dialog.close()
                        ui.notify(
                            f"Раздел «{raw_title}» добавлен (seq={result.get('sequence_number')})",
                            type="positive",
                        )
                        cache.invalidate("books")
                        await render_book_detail(detail_container, client, collection_id)

                    with ui.dialog() as add_dialog, ui.card().classes("w-[600px] max-w-[90vw]"):
                        ui.label("Добавить раздел").classes("text-h6")
                        _title_input = ui.input(label="Заголовок раздела").classes("w-full")
                        _content_textarea = ui.textarea(label="Содержание (Markdown)").classes("w-full").props("autogrow")
                        with ui.row().classes("gap-2 q-mt-md"):
                            _add_save_btn = ui.button("Добавить", on_click=_confirm_add, icon="add").props("color=primary")
                            ui.button("Отмена", on_click=add_dialog.close).props("flat")
                    add_dialog.open()
                add_section_btn.on("click", _do_add_section)
                async def _do_rename() -> None:
                    nonlocal current_title
                    _rename_input = None
                    _save_btn_ref = None
                    async def _confirm_rename() -> None:
                        nonlocal current_title
                        raw = _rename_input.value or ""
                        sanitized = _sanitize_title(raw)
                        if not sanitized:
                            ui.notify("Название не может быть пустым", type="warning")
                            return
                        if _save_btn_ref is not None:
                            _save_btn_ref.disable()
                        ui.notify("Переименовываю книгу…", type="info")
                        try:
                            await client.update_entry(collection_id, content=f"# {sanitized}\n\nКоллекция импортированных секций. Оглавление — в frontmatter.children.")
                            current_title = sanitized
                            title_label.set_text(sanitized)
                            ui.notify(f"Книга переименована в «{sanitized}»", type="positive")
                            rename_dialog.close()
                        except Exception as exc:
                            ui.notify(
                                f"Ошибка переименования: {exc}\n\n"
                                f"Книга могла быть переименована — обновите список.",
                                type="negative",
                            )
                        finally:
                            if _save_btn_ref is not None:
                                _save_btn_ref.enable()
                    with ui.dialog() as rename_dialog, ui.card():
                        ui.label("Переименовать книгу").classes("text-h6")
                        _rename_input = ui.input(label="Новое название", value=current_title).classes("w-full")
                        with ui.row().classes("gap-2 q-mt-md"):
                            _save_btn_ref = ui.button("Сохранить", on_click=_confirm_rename, icon="save").props("color=primary")
                            ui.button("Отмена", on_click=rename_dialog.close).props("flat")
                    rename_dialog.open()
                rename_btn.on("click", _do_rename)
                ui.button(icon="close", on_click=_close).props("flat round dense")
        detail_container = ui.column().classes("w-full")
        with ui.row().classes("q-mt-md"):
            ui.button("Закрыть", on_click=_close).props("flat")
    dialog.on("hide", lambda: asyncio.create_task(client.close()))
    await render_book_detail(detail_container, client, collection_id, initial_section_id=initial_section_id)
    dialog.open()


# ── Страница «Книги» ─────────────────────────────────────────

def build_books() -> None:
    """Построить страницу «Книги»: список → модалка (13.11: модалка вместо inline)."""

    view_container = ui.column().classes("w-full")
    _client: MCPClient | None = None

    async def _show_list() -> None:
        nonlocal _client
        view_container.clear()
        if _client is None:
            _client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)

        # Прелоадер (Фаза C3): spinner при загрузке списка
        with view_container:
            ui.spinner(size="md").props("color=primary")
            ui.label("Загрузка списка книг…").classes("text-grey")

        try:
            # Task 1: server-side version check → инвалидация при внешних мутациях
            try:
                await cache.check_version(_client)
            except Exception:
                pass  # version-check не должен ломать загрузку списка
            books = await cache.get(
                "books",
                lambda: _client.list_collections(),
                ttl=60,
            )
        except Exception as exc:
            view_container.clear()
            with view_container:
                ui.label(f"❌ Ошибка загрузки списка книг: {exc}").classes("text-negative")
            return

        view_container.clear()

        if not books:
            with view_container:
                ui.label("Книг пока нет — импортируйте контент на вкладке «Импорт»").classes("text-grey q-mt-md")
                return

        with view_container:
            ui.label(f"Найдено книг: {len(books)}").classes("text-subtitle1 q-mt-md")
            for b in sorted(books, key=lambda x: x.get("title", "")):
                cid = b["collection_id"]
                btitle = b.get("title") or cid

                with ui.card().classes("w-full"), ui.row().classes("items-center w-full"), ui.column().classes("flex-1"):
                    ui.label(b.get("title", b.get("collection_id", "—"))).classes("text-subtitle1")
                    ui.label(
                        f"{b.get('domain', '—')}/{b.get('subject', '—')}"
                        + (f"  ·  {b.get('project')}" if b.get("project") else "")
                    ).classes("text-caption text-grey")
                    with ui.row().classes("items-center"):
                        with ui.row().classes("items-center"):
                            ui.icon("article").classes("text-caption text-grey q-mr-xs")
                            ui.label(f"{b.get('section_count', 0)} секций").classes("text-caption text-grey q-mr-md")
                        async def _open_btn(cid: str = cid, t: str = btitle) -> None:
                            await _open_book(cid, t)
                        ui.button("Открыть", on_click=_open_btn, icon="menu_book").props("flat dense")
                        async def _replace_btn(
                            cid: str = cid, t: str = btitle,
                            dom: str = b.get("domain", ""), subj: str = b.get("subject", ""),
                        ) -> None:
                            from ..components.replace_dialog import show_replace_dialog

                            async def _on_success() -> None:
                                cache.invalidate("books")
                                await _show_list()

                            await show_replace_dialog(cid, t, domain=dom, subject=subj, on_success=_on_success)

                        ui.button("Заменить", on_click=_replace_btn, icon="cached").props("flat dense")

                        async def _delete_btn(cid: str = cid, t: str = btitle) -> None:
                            """Удалить книгу-коллекцию с подтверждением (каскад).

                            known limitation (P2-2): если книга открыта в модалке —
                            модалка покажет stale-данные после удаления (non-persistent,
                            закрывается без краша). Accept для MVP.
                            """
                            with ui.dialog() as confirm_dialog, ui.card().classes("q-pa-md"):
                                ui.label("Удаление книги").classes("text-h6")
                                ui.label(
                                    f"Книга «{t}» ({cid}) будет удалена вместе со всеми секциями. "
                                    f"Сохранится в .trash/. Продолжить?"
                                ).classes("text-body2 q-mb-md")
                                with ui.row().classes("justify-end"):
                                    ui.button("Отмена", on_click=lambda: confirm_dialog.close()).props("flat")

                                    async def _confirm_delete() -> None:
                                        confirm_dialog.close()
                                        ui.notify("Удаляю книгу…", type="info")
                                        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
                                        try:
                                            result = await client.delete_entry(cid, cascade=True)
                                            if result.get("deleted"):
                                                cascade_del = result.get("cascade_deleted", 0)
                                                cache.invalidate("books")
                                                await _show_list()
                                                ui.notify(
                                                    f"Книга удалена (каскад: {cascade_del} секций)",
                                                    type="positive",
                                                )
                                            else:
                                                ui.notify(
                                                    f"Ошибка: {result.get('error', '?')}",
                                                    type="negative",
                                                )
                                        except Exception as exc:
                                            ui.notify(f"Ошибка удаления: {exc}", type="negative")
                                        finally:
                                            await client.close()

                                    ui.button("Удалить", icon="delete", on_click=_confirm_delete).props("color=negative")
                            confirm_dialog.open()

                        ui.button("Удалить", on_click=_delete_btn, icon="delete").props("color=negative flat dense")

    async def _open_book(collection_id: str, title: str = "") -> None:
        """Открыть модалку книги (вызов напрямую, без обёрток view_container).

        ВАЖНО: в async-обработчиках NiceGUI контекст слота сохраняется через
        contextvar — обёртки (спиннер/clear в view_container) создавали dialog
        внутри view_container, и его clear() удалял диалог из DOM. Паттерн как в search.py.
        """
        await show_book_dialog(collection_id, title)

    # Таймер безопасности: закрыть MCPClient при дисконнекте
    def _cleanup() -> None:
        nonlocal _client
        if _client is not None:
            import asyncio
            asyncio.create_task(_client.close())
            _client = None
    ui.context.client.on_disconnect(_cleanup)

    ui.button("Обновить список", on_click=_show_list, icon="refresh").props("color=primary")
    ui.timer(0.0, _show_list, once=True)
