"""Страница «Книги» — список коллекций с метаданными + просмотр содержимого.

Variant A (13.10):
  - Список книг (list_collections): title, domain/subject, section_count, tags, updated_at.
  - Деталь книги (get_entry): TOC из children (sorted by sequence_number).
  - Секция (get_entry): полный markdown-контент.

render_book_detail / show_book_dialog переиспользуются страницей «Поиск»
(кнопка «Открыть книгу» в результатах).
"""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient

# Пагинация TOC: книги бывают на тысячи секций — рендерим постранично,
# иначе NiceGUI-слот перегружается и рвётся websocket-handshake.
TOC_PAGE_SIZE = 100


# ── Переиспользуемый рендер детали книги (для «Книги» и диалога «Поиска») ──

async def render_book_detail(container: ui.element, client: MCPClient, collection_id: str) -> None:
    """Отрисовать в container: заголовок книги + TOC (children, постранично) → контент секции."""
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
        container.clear()
        with container:
            async def _back_to_toc() -> None:
                await render_book_detail(container, client, collection_id)
            ui.button("← Назад к оглавлению", on_click=_back_to_toc)
            ui.label(section.get("title", "—")).classes("text-h5 q-mt-md")
            ui.separator()
        try:
            sec = await client.get_entry(section["knowledge_id"])
        except Exception as exc:
            sec = {"error": str(exc)}
        with container:
            if "error" in sec:
                ui.label(f"❌ {sec['error']}").classes("text-negative")
                return
            ui.markdown(sec.get("content", "_(пусто)_"))

    def _render_toc_page(page: int) -> None:
        """Отрисовать страницу TOC (children[page*SIZE:(page+1)*SIZE])."""
        container.clear()
        pages = max(1, (total + TOC_PAGE_SIZE - 1) // TOC_PAGE_SIZE)
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
                    .props("flat dense").enabled(page > 0)
                ui.label(f"Секции {start + 1}–{end} из {total}").classes("text-caption text-grey q-mx-md")
                ui.button("След. →", on_click=lambda: _render_toc_page(min(pages - 1, page + 1))) \
                    .props("flat dense").enabled(page < pages - 1)

    _render_toc_page(0)


async def show_book_dialog(collection_id: str, title: str | None = None) -> None:
    """Открыть модальный диалог с содержимым книги (для результатов поиска).

    Создаёт собственный MCPClient (не переиспользует чужой — тот может быть
    уже закрыт после поискового вызова) и закрывает его при закрытии диалога.
    persistent: закрытие только через кнопку — контролируем lifecycle клиента.
    """
    client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)

    async def _close() -> None:
        dialog.close()
        await client.close()

    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-[90vw]"), ui.column().classes("w-full"):
        ui.label(f"📖 {title or collection_id}").classes("text-h6")
        detail_container = ui.column().classes("w-full")
        with ui.row().classes("q-mt-md"):
            ui.button("Закрыть", on_click=_close).props("flat")
    dialog.props("persistent")
    await render_book_detail(detail_container, client, collection_id)
    dialog.open()


# ── Страница «Книги» ─────────────────────────────────────────

def build_books() -> None:
    """Построить страницу «Книги»: список → деталь → секция (master-detail в табе)."""

    ui.label("Книги").classes("text-h4 q-mb-md")

    view_container = ui.column().classes("w-full")
    _client: MCPClient | None = None

    async def _show_list() -> None:
        nonlocal _client
        view_container.clear()
        if _client is None:
            _client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            books = await _client.list_collections()
        except Exception as exc:
            with view_container:
                ui.label(f"❌ Ошибка загрузки списка книг: {exc}").classes("text-negative")
            return

        if not books:
            with view_container:
                ui.label("Книг пока нет — импортируйте контент на вкладке «Импорт»").classes("text-grey q-mt-md")
                return

        with view_container:
            ui.label(f"Найдено книг: {len(books)}").classes("text-subtitle1 q-mt-md")
            for b in sorted(books, key=lambda x: x.get("title", "")):
                async def _open(cid: str = b["collection_id"]) -> None:
                    await _open_book(cid)
                with ui.card().props("clickable").classes("w-full cursor-pointer").on("click", _open), ui.row().classes("items-center w-full"), ui.column().classes("flex-1"):
                    ui.label(b.get("title", b.get("collection_id", "—"))).classes("text-subtitle1")
                    ui.label(
                        f"{b.get('domain', '—')}/{b.get('subject', '—')}"
                        + (f"  ·  {b.get('project')}" if b.get("project") else "")
                    ).classes("text-caption text-grey")
                    ui.label(f"📄 {b.get('section_count', 0)} секций").classes("text-caption text-grey q-mr-md")

    async def _open_book(collection_id: str) -> None:
        view_container.clear()
        with view_container:
            ui.button("← Назад к списку книг", on_click=_show_list)
        await render_book_detail(view_container, _client, collection_id)

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
