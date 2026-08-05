"""Страница «Статус» — liveness, health-компоненты, метрики, таблица инструментов."""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.health import get_health, get_liveness, get_metrics
from ..core.mcp_client import MCPClient


async def _fetch_status_data(client: MCPClient):
    """Собрать все данные для страницы статуса."""
    data = {
        "liveness": None,
        "health": None,
        "metrics": {},
        "tools": [],
    }

    try:
        data["liveness"] = await get_liveness(MCP_SERVER_URL)
    except Exception as exc:
        data["liveness"] = {"error": str(exc)}

    try:
        data["health"] = await get_health(MCP_SERVER_URL)
    except Exception as exc:
        data["health"] = {"error": str(exc)}

    try:
        data["metrics"] = await get_metrics(MCP_SERVER_URL)
    except Exception:
        pass

    try:
        data["tools"] = await client.tools_list()
    except Exception:
        pass

    return data


def build_status() -> None:
    """Построить страницу «Статус»."""

    async def refresh() -> None:
        container.clear()
        with container:
            ui.spinner(size="lg").classes("q-mx-auto")
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            data = await _fetch_status_data(client)
        finally:
            await client.close()

        container.clear()
        with container:
            _render_status(data)

    # ── Layout ─────────────────────────────────────────────
    ui.label("Статус MCP Knowledge Server").classes("text-h4 q-mb-md")

    with ui.row().classes("gap-4 items-center"):
        ui.button("🔄 Обновить", on_click=refresh).props("flat")
        ui.label(f"Автообновление: каждые {REFRESH_SECONDS} сек.").classes("text-grey")

    container = ui.column().classes("w-full")

    # Автообновление
    ui.timer(REFRESH_SECONDS, lambda: refresh())

    # Первичная загрузка
    ui.timer(0.1, lambda: refresh(), once=True)


def _render_status(data: dict) -> None:
    """Отрисовать собранные данные статуса."""

    # ── Liveness ──────────────────────────────────────────
    liveness = data.get("liveness", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("🔍 Liveness").classes("text-h6")
        if "error" in liveness:
            ui.label(f"❌ Ошибка: {liveness['error']}").classes("text-negative")
        else:
            status_text = liveness.get("status", "?")
            color = "positive" if status_text == "alive" else "negative"
            ui.label(f"Состояние: {status_text}").classes(f"text-{color}")

    # ── Health Checks ─────────────────────────────────────
    health = data.get("health", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("🩺 Health Checks").classes("text-h6")

        if "error" in health:
            ui.label(f"❌ Ошибка: {health['error']}").classes("text-negative")
        else:
            ui.label(
                f"Статус: {health.get('status', '?')} | "
                f"Версия: {health.get('version', '?')}"
            ).classes("text-subtitle1 q-mb-sm")

            checks = health.get("checks", [])
            if checks:
                with ui.row().classes("gap-4"):
                    for check in checks:
                        comp = check.get("component", "?")
                        ok = check.get("ok", False)
                        detail = check.get("detail", "")

                        icon_text = "✅" if ok else "❌"

                        with ui.card().classes("q-pa-sm"):
                            ui.label(f"{icon_text} {comp}").classes("text-subtitle2")
                            if detail:
                                ui.label(str(detail)).classes("text-caption text-grey")
            else:
                ui.label("Нет данных о компонентах").classes("text-grey")

    # ── Ключевые метрики ──────────────────────────────────
    metrics = data.get("metrics", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("📊 Ключевые метрики").classes("text-h6")

        if not metrics:
            ui.label("Нет данных").classes("text-grey")
        else:
            metric_labels = {
                "mcp_queue_size": "Размер очереди",
                "mcp_collection_size": "Точек в Qdrant",
                "mcp_pipeline_processed_total": "Обработано пайплайном",
                "mcp_pipeline_failed_total": "Ошибок пайплайна",
                "mcp_search_latency_seconds_avg": "Средняя latency поиска (сек)",
            }
            for key, label in metric_labels.items():
                val = metrics.get(key)
                if val is not None:
                    ui.label(f"{label}: {val}").classes("text-body2")

    # ── Инструменты ───────────────────────────────────────
    tools = data.get("tools", [])
    with ui.card().classes("w-full q-mb-md"):
        ui.label(f"🔧 Инструменты ({len(tools)})").classes("text-h6")

        if not tools:
            ui.label("Нет данных об инструментах").classes("text-grey")
        else:
            columns = [
                {"name": "name", "label": "Имя", "field": "name", "sortable": True, "align": "left"},
                {"name": "description", "label": "Описание", "field": "description", "sortable": False, "align": "left"},
            ]
            rows = [{"name": t["name"], "description": t.get("description", "")} for t in tools]
            ui.table(columns=columns, rows=rows, row_key="name").classes("w-full")
