"""Страница «Поиск» — семантический поиск по базе знаний."""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient


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

                columns = [
                    {"name": "title", "label": "Заголовок", "field": "title", "sortable": True, "align": "left"},
                    {"name": "score", "label": "Score", "field": "score", "sortable": True, "align": "left"},
                    {"name": "domain", "label": "Домен", "field": "domain", "sortable": True, "align": "left"},
                    {"name": "subject", "label": "Предмет", "field": "subject", "sortable": True, "align": "left"},
                ]
                rows = [
                    {
                        "title": it.get("title", it.get("knowledge_id", "—")),
                        "score": round(it.get("score", 0), 4) if "score" in it else "—",
                        "domain": it.get("domain", "—"),
                        "subject": it.get("subject", "—"),
                    }
                    for it in items
                ]
                ui.table(columns=columns, rows=rows, row_key="title").classes("w-full")

        except Exception as exc:
            ui.notify(f"Ошибка поиска: {exc}", type="negative")
        finally:
            await client.close()

    with ui.row().classes("gap-4"):
        ui.button("🔍 Искать", on_click=do_search, icon="search").props("color=primary")
        query_input.on("keydown.enter", lambda: do_search())
