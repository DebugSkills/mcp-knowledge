"""Страница «Поиск» — семантический поиск по базе знаний.

Variant A (13.10 → 13.13): информативные результаты — Title (не slug), Книга
(parent коллекция), Score, сниппет контента, теги (ui.chip), кнопка
«Открыть фрагмент» (диалог с секцией, не TOC).
Кэш названий книг: один list_collections на первую выдачу (без N+1).

13.27: блок прогресса quality scan УБРАН со страницы — прогресс показывается
только на «Качестве» (страница управления сканом); дублирующая панель на
«Поиске» сбивала с толку (ранее 13.16 показывала скан на всех страницах).

Ф4-fix1 (P1-1): поиск шлёт ключ по роли (mcp_api_key) — сервер строит
citation по ключу вызывающего (§3.4); private-source citation ниже admin
не презентуется (кнопка «Документ» не рендерится), admin получает ссылку
с маркером ?zone=private (гейт прокси documents_proxy).
"""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_SERVER_URL
from ..core.data_cache import cache
from ..core.identity import ROLE_LEVEL, current_role, mcp_api_key
from ..core.mcp_client import MCPClient
from ..core.utils import render_source_card
from ..documents_proxy import citation_viewer_url
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


async def _load_source_card(source_id: str, container) -> None:
    """Ф5c2: лениво загрузить Source и отрисовать карточку (on-demand, no N+1).

    Собственный клиент (search-клиент закрыт в `_do_search` finally); ключ —
    `mcp_api_key()` (роль сессии), НЕ base — сервер строит ответ по ключу
    вызывающего (§3.4). Сетевой сбой/гейт-отказ → `render_source_card`
    покажет нейтральный «нет данных» без утечки.
    """
    client = MCPClient(base_url=MCP_SERVER_URL, api_key=mcp_api_key())
    try:
        source = await client.source_get(source_id)
    except Exception:
        source = None
    finally:
        await client.close()
    render_source_card(source, container)


async def _do_search(query: str, top_k: int, results_container) -> None:
    """Поиск + рендер выдачи (module-level — тестируемо, паттерн quality).

    Ф4-fix1 (P1-1): ключ — mcp_api_key() (роль запроса): сервер строит
    citation по ключу вызывающего (§3.4), base-ключ давал бы admin-эквивалент
    и private-утечку. Private-source citation (zone="private") ниже admin НЕ
    презентуется — кнопка «Документ» не рендерится (§3.4: private → только
    admin); admin получает ссылку с маркером ?zone=private (гейт прокси).
    """
    client = MCPClient(base_url=MCP_SERVER_URL, api_key=mcp_api_key())
    try:
        book_titles = await _load_book_titles(client)
        result = await client.tools_call(
            "search_knowledge",
            {"query": query, "top_k": int(top_k)},
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

            ui.label(f"Найдено результатов: {len(items)}").classes(
                "text-subtitle1 q-mt-md"
            )

            # P1-1: роль один раз на выдачу — гейт приватных viewer-ссылок.
            admin_viewer = ROLE_LEVEL.get(current_role(), 0) >= ROLE_LEVEL["admin"]

            for it in items:
                title = (
                    it.get("title")
                    or it.get("section_header")
                    or it.get("knowledge_id", "—")
                )
                knowledge_id = it.get(
                    "knowledge_id"
                )  # ID найденного ФРАГМЕНТА (секции)
                book_id = it.get("parent_knowledge_id")
                book = book_titles.get(book_id, "—") if book_id else "—"
                score = round(it.get("score", 0), 4) if "score" in it else "—"
                excerpt = (it.get("content") or "")[:200].strip()
                tags = it.get("tags") or []

                async def _open(
                    cid: str = book_id, btitle: str = book, sid: str = knowledge_id
                ) -> None:
                    if cid:
                        await show_book_dialog(cid, btitle, initial_section_id=sid)
                    else:
                        ui.notify("Секция не привязана к книге", type="warning")

                with (
                    ui.card().classes("w-full q-mt-sm"),
                    ui.row().classes("items-center w-full no-wrap"),
                    ui.column().classes("flex-1"),
                ):
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
                                ui.label(f"+{len(tags) - 8}").classes(
                                    "text-caption text-grey self-center"
                                )
                    if excerpt:
                        ui.label(f"…{excerpt}…").classes("text-caption text-grey-7")
                    if book_id:
                        ui.button(
                            "Открыть фрагмент", on_click=_open, icon="article"
                        ).props("flat")
                    # bibliography Ф4c: viewer-ссылка из citation уровня (б)
                    # (нет viewer_url/sha256 — кнопки нет: частичный рендер запрещён)
                    citation = it.get("citation")
                    doc_url = citation_viewer_url(citation)
                    # Ф4-fix1 (P1-1, §3.4 «private → только admin»): private-source
                    # ссылку ниже admin НЕ презентуем; admin получает ?zone=private
                    if (
                        doc_url
                        and not admin_viewer
                        and isinstance(citation, dict)
                        and citation.get("zone") == "private"
                    ):
                        doc_url = None
                    if doc_url:
                        ui.button("Документ", icon="picture_as_pdf").props(
                            "flat"
                        ).on_click(lambda u=doc_url: ui.open(u, new_tab=True))
                    # Ф5c2: Source-карточка on-demand (no N+1): кнопка НЕ
                    # вызывает source_get при построении списка — только по
                    # клику (лениво). Нет source_id в citation → карточки нет.
                    source_id = (
                        citation.get("source_id")
                        if isinstance(citation, dict)
                        else None
                    )
                    # P1-1 (§3.4 «private → только admin»): ниже admin кнопку
                    # «Источник» у private-источника НЕ презентуем — нет намёка
                    # на существование источника (серверный гейт source_get —
                    # вторая линия обороны, а не единственная).
                    if source_id and not (
                        isinstance(citation, dict)
                        and citation.get("zone") == "private"
                        and not admin_viewer
                    ):
                        source_container = ui.column().classes("w-full q-mt-sm")

                        async def _toggle_source(
                            sid: str = source_id, sc=source_container
                        ) -> None:
                            await _load_source_card(sid, sc)

                        ui.button("Источник", on_click=_toggle_source, icon="travel_explore").props("flat")

    except Exception as exc:
        ui.notify(f"Ошибка поиска: {exc}", type="negative")
    finally:
        await client.close()


def build_search() -> None:
    """Построить страницу «Поиск» (хендлер — module-level _do_search)."""

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

        await _do_search(query, int(top_k_input.value or 5), results_container)

    with ui.row().classes("gap-4"):
        ui.button("🔍 Искать", on_click=do_search, icon="search").props("color=primary")
        query_input.on("keydown.enter", lambda: do_search())
