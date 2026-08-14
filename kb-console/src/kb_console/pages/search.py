"""Страница «Поиск» — семантический поиск по базе знаний.

Variant A (13.10 → 13.13): информативные результаты — Title (не slug), Книга
(parent коллекция), Score, сниппет контента, теги (ui.chip), кнопка
«Открыть фрагмент» (диалог с секцией, не TOC).
Кэш названий книг: один list_collections на первую выдачу (без N+1).

13.27: блок прогресса quality scan УБРАН со страницы — прогресс показывается
только на «Качестве» (страница управления сканом); дублирующая панель на
«Поиске» сбивала с толку (ранее 13.16 показывала скан на всех страницах).
"""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.data_cache import cache
from ..core.mcp_client import MCPClient
from .books import show_book_dialog


async def _load_book_titles(client: MCPClient) -> dict[str, str]:
    """Загрузить словарь collection_id → title через DataCache."""
    # Task 1: version check → инвалидация при внешних мутациях (rename/delete из API)
    try:
        await cache.check_version(client)
    except Exception:
        pass
    books = await cache.get("book_titles", lambda: client.list_collections(), ttl=300)
    result: dict[str, str] = {}
    for b in books:
        cid = b.get("collection_id")
        if cid:
            result[cid] = b.get("title") or cid
    return result


def build_search() -> None:
    """Построить страницу «Поиск»."""

    ui.label("Поиск по базе знаний").classes("text-h4 q-mb-md")

    with ui.row().classes("gap-4"):
        query_input = ui.input(
            label="Поисковый запрос",
            placeholder="Введите запрос...",
        ).classes("w-96")

        top_k_input = ui.number(
            label="Топ-K результатов",
            value=5,
            min=1,
            max=50,
        ).classes("w-32")

    results_container = ui.column().classes("w-full")

    # ── Search handler ────────────────────────────────────
    async def do_search() -> None:
        query = query_input.value.strip()
        if not query:
            ui.notify("Введите поисковый запрос", type="warning")
            return

        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            book_titles = await _load_book_titles(client)
            result = await client.tools_call(
                "search_knowledge",
                {"query": query, "top_k": int(top_k_input.value or 5)},
            )

            results_container.clear()
            with results_container:
                if isinstance(result, list):
                    items = result
                elif isinstance(result, dict):
                    items = result.get("results", [result])
                else:
                    items = []

                if not items:
                    ui.label("Ничего не найдено").classes("text-grey q-mt-md")
                    return

                ui.label(f"Найдено результатов: {len(items)}").classes("text-subtitle1 q-mt-md")

                for it in items:
                    title = it.get("title") or it.get("section_header") or it.get("knowledge_id", "—")
                    knowledge_id = it.get("knowledge_id")          # ID найденного ФРАГМЕНТА (секции)
                    book_id = it.get("parent_knowledge_id")
                    book = book_titles.get(book_id, "—") if book_id else "—"
                    score = round(it.get("score", 0), 4) if "score" in it else "—"
                    excerpt = (it.get("content") or "")[:200].strip()
                    tags = it.get("tags") or []

                    async def _open(cid: str = book_id, btitle: str = book, sid: str = knowledge_id) -> None:
                        if cid:
                            await show_book_dialog(cid, btitle, initial_section_id=sid)
                        else:
                            ui.notify("Секция не привязана к книге", type="warning")

                    with ui.card().classes("w-full q-mt-sm"), ui.row().classes("items-center w-full no-wrap"), ui.column().classes("flex-1"):
                        ui.label(title).classes("text-subtitle1")
                        ui.label(
                            f"📖 {book}  ·  {it.get('domain', '—')}/{it.get('subject', '—')}"
                            f"  ·  score: {score}"
                        ).classes("text-caption text-grey")
                        if tags:
                            with ui.row().classes("wrap q-mt-xs"):
                                for tag in tags[:8]:
                                    ui.chip(tag).props("outline dense")
                                if len(tags) > 8:
                                    ui.label(f"+{len(tags) - 8}").classes("text-caption text-grey self-center")
                        if excerpt:
                            ui.label(f"…{excerpt}…").classes("text-caption text-grey-7")
                        if book_id:
                            ui.button("Открыть фрагмент", on_click=_open, icon="article").props("flat")

        except Exception as exc:
            ui.notify(f"Ошибка поиска: {exc}", type="negative")
        finally:
            await client.close()

    with ui.row().classes("gap-4"):
        ui.button("🔍 Искать", on_click=do_search, icon="search").props("color=primary")
        query_input.on("keydown.enter", lambda: do_search())
