"""Страница «Книги» — список коллекций с метаданными + просмотр содержимого.

Variant A (13.10):
  - Список книг (list_collections): title, domain/subject, section_count, tags, updated_at.
  - Деталь книги (get_entry): TOC из children (sorted by sequence_number).
  - Секция (get_entry): полный markdown-контент.

13.11 UX:
  - Модалка non-persistent (крестик/Esc/фон) вместо inline master-detail.
  - Прелоадеры (spinners) при TOC/секции/списке.
  - Кнопка «✏️ Переименовать» в модалке (update_entry с санитизацией).

render_book_detail / show_book_dialog переиспользуются страницей «Поиск»
(кнопка «Открыть книгу» в результатах).
"""

from __future__ import annotations

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
                    ui.item(
                        f"#{child.get('sequence_number', '?')}  {child.get('title', '—')}",
                    ).props("clickable").on("click", _open).classes("text-body2")
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
            # Фрагмент может отсутствовать в TOC (импортирован отдельно, не в frontmatter.children).
            # Грузим его напрямую по knowledge_id — иначе «Открыть фрагмент» покажет TOC (баг 13.13).
            sec = None
            try:
                sec = await client.get_entry(initial_section_id)
            except Exception:
                sec = None
            if sec and "error" not in sec:
                await _show_section({
                    "knowledge_id": initial_section_id,
                    "title": sec.get("title", "Фрагмент"),
                })
            else:
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
            title_label = ui.label(f"📖 {current_title}").classes("text-h6")
            with ui.row().classes("items-center gap-2"):
                rename_btn = ui.button("✏️ Переименовать", icon="edit").props("flat dense")
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
                            title_label.set_text(f"📖 {sanitized}")
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
                        ui.label(f"📄 {b.get('section_count', 0)} секций").classes("text-caption text-grey q-mr-md")
                        async def _open_btn(cid: str = cid, t: str = btitle) -> None:
                            await _open_book(cid, t)
                        ui.button("📖 Открыть", on_click=_open_btn, icon="menu_book").props("flat dense")
                        async def _replace_btn(
                            cid: str = cid, t: str = btitle,
                            dom: str = b.get("domain", ""), subj: str = b.get("subject", ""),
                        ) -> None:
                            from ..components.replace_dialog import show_replace_dialog

                            async def _on_success() -> None:
                                cache.invalidate("books")
                                await _show_list()

                            await show_replace_dialog(cid, t, domain=dom, subject=subj, on_success=_on_success)

                        ui.button("♻️ Заменить", on_click=_replace_btn, icon="cached").props("flat dense")

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

    ui.button("🔄 Обновить список", on_click=_show_list).props("color=primary")
    ui.timer(0.0, _show_list, once=True)
