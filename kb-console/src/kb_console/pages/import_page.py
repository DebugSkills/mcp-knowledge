"""Страница «Импорт» — импорт контента в базу знаний."""

from __future__ import annotations

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient


def build_import() -> None:
    """Построить страницу «Импорт»."""

    # ── Form fields ───────────────────────────────────────
    ui.label("Импорт контента").classes("text-h4 q-mb-md")

    content_input = ui.textarea(
        label="Контент (Markdown/plain)",
        placeholder="Введите текст для импорта...",
    ).classes("w-full").props("rows=10")

    with ui.row().classes("gap-4"):
        content_type = ui.select(
            label="Тип контента",
            options=["book"],
            value="book",
        ).classes("w-48")

        domain_input = ui.input(
            label="Домен",
            placeholder="например: programming",
        ).classes("w-48")

        subject_input = ui.input(
            label="Предмет",
            placeholder="например: python",
        ).classes("w-48")

    tags_input = ui.input(
        label="Теги (через запятую)",
        placeholder="python, tutorial, basics",
    ).classes("w-full q-mb-md")

    result_container = ui.column().classes("w-full")

    # ── Import handler ────────────────────────────────────
    async def do_import() -> None:
        content = content_input.value
        if not content.strip():
            ui.notify("Введите контент для импорта", type="warning")
            return

        domain = domain_input.value.strip()
        subject = subject_input.value.strip()
        if not domain or not subject:
            ui.notify("Заполните домен и предмет", type="warning")
            return

        # Парсим теги
        tags_text = tags_input.value or ""
        tags = [t.strip() for t in tags_text.split(",") if t.strip()]

        params = {
            "content": content,
            "content_type": content_type.value,
            "domain": domain,
            "subject": subject,
        }
        if tags:
            params["tags"] = tags

        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.tools_call("import_content", params)

            result_container.clear()
            with result_container:
                ui.label("✅ Импорт выполнен").classes("text-positive text-h6")

                imported = result.get("imported", 0)
                failed = result.get("failed", 0)
                collection_id = result.get("collection_id", "—")

                ui.label(f"Коллекция: {collection_id}").classes("text-body2")
                ui.label(f"Импортировано секций: {imported}").classes("text-body2")
                if failed > 0:
                    ui.label(f"Ошибок: {failed}").classes("text-negative text-body2")

                failed_sections = result.get("failed_sections", [])
                if failed_sections:
                    with ui.card().classes("q-mt-md"):
                        ui.label("Ошибки по секциям:").classes("text-subtitle2")
                        for fs in failed_sections:
                            seq = fs.get("sequence_number", "?")
                            title = fs.get("title", "—")
                            error = fs.get("error", "?")
                            ui.label(f"  #{seq} «{title}»: {error}").classes("text-body2 text-negative")

        except Exception as exc:
            ui.notify(f"Ошибка импорта: {exc}", type="negative")
        finally:
            await client.close()

    ui.button("Импортировать", on_click=do_import, icon="upload").props("color=primary")
