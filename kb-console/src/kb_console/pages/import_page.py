"""Страница «Импорт» — импорт контента в базу знаний.

Новый алгоритм (Фаза 13.8):
  1. Загрузили файл — ничего не происходит (контент в памяти)
  2. Нажали «Обработать» → AI-рекомендации: content_type/domain/subject/tags
  3. Подтверждаем/дополняем/изменяем
  4. Нажимаем «Добавить» → import_content на MCP
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient

# Расширения, поддерживаемые файловым импортом (.md/.txt, PDF — в перспективе).
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt"}
# Максимальный размер файла для импорта, байт (50 МБ — учебники).
MAX_FILE_SIZE = 52_428_800
# Таймаут HTTP для вызова import_content (5 минут — большой файл/учебник).
IMPORT_TIMEOUT = 300.0
# Таймаут для вызова analyze_content (60 секунд — LLM).
ANALYZE_TIMEOUT = 60.0
# Фрагмент для AI-анализа (синхронизирован с серверным ANALYZE_FRAGMENT_CHARS) —
# полный контент не отправляется: и скорость, и размер запроса.
ANALYZE_FRAGMENT_CHARS = 8000


def _read_uploaded_file(name: str, data: bytes) -> tuple[str | None, str | None]:
    """Прочитать загруженный файл в текст.

    Returns:
        (content, error): success → (text, None); failure → (None, error_msg).
    """
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None, f"Неподдерживаемый тип файла «{ext or 'без расширения'}». Ожидаются: .md, .markdown, .txt"
    if len(data) > MAX_FILE_SIZE:
        return None, f"Файл слишком большой (макс. {MAX_FILE_SIZE // 1_048_576} МБ)"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Пробуем windows-1251 для русскоязычных .txt из Windows.
        try:
            text = data.decode("windows-1251")
        except UnicodeDecodeError:
            return None, "Не удалось прочитать файл (кодировка не поддерживается)"
    return text, None


def build_import() -> None:
    """Построить страницу «Импорт»."""

    # ── История импортов (в памяти, на сессию) ─────────────
    import_history: list[dict] = []
    history_container = ui.column().classes("w-full")

    def _render_history() -> None:
        """Отрисовать список успешных импортов."""
        history_container.clear()
        if not import_history:
            return
        with history_container:
            ui.label("📋 История импортов").classes("text-h6 q-mt-lg")
            with ui.list().classes("w-full"):
                for entry in reversed(import_history[-10:]):  # последние 10
                    prefix = "✅" if entry["failed"] == 0 else "⚠️"
                    with ui.item(), ui.item_section():
                        ui.item_label(
                            f"{prefix} {entry['domain']}/{entry['subject']}"
                        ).classes("text-body2")
                        ui.item_label(
                            f"{entry['title']} — {entry['imported']} секций"
                            + (f", ошибок: {entry['failed']}" if entry["failed"] else "")
                            + f" | {entry['_time']}"
                        ).classes("text-caption text-grey")

    # ── Closure: pending file (в памяти, не в textarea) ─────
    pending_file: dict | None = None

    # ── Form fields ───────────────────────────────────────
    ui.label("Импорт контента").classes("text-h4 q-mb-md")

    with ui.column().classes("w-full q-mb-sm"):
        ui.label("Загрузить файл (.md / .markdown / .txt)").classes("text-body2")

        async def handle_upload(e) -> None:
            """Обработать загруженный файл: сохранить в pending_file (НЕ трогать textarea!)."""
            nonlocal pending_file

            filename = e.file.name
            raw = await e.file.read()
            print(f"[IMPORT-UPLOAD] file={filename} size={len(raw)}B")
            text, error = _read_uploaded_file(filename, raw)
            if error is not None:
                print(f"[IMPORT-UPLOAD] REJECTED: {error}")
                ui.notify(f"«{filename}»: {error}", type="negative")
                return
            # Сохраняем в памяти — textarea НЕ трогаем (root cause краша Фазы 13.7)
            pending_file = {
                "name": filename,
                "size": len(raw),
                "chars": len(text),
                "content": text,
            }
            file_status_label.set_text(
                f"📄 {filename} — {len(raw) / 1_048_576:.1f} МБ ({len(text):,} символов)"
            )
            analyze_btn.enable()
            ui.notify(f"«{filename}» загружен ({len(text):,} символов)", type="positive")
            print(f"[IMPORT-UPLOAD] OK {len(text)} chars — pending_file set, textarea untouched")

        def on_rejected(e) -> None:
            print("[IMPORT-UPLOAD] REJECTED by Quasar client-side")

        ui.upload(
            on_upload=handle_upload,
            on_rejected=on_rejected,
            auto_upload=True,
            max_file_size=MAX_FILE_SIZE,
        ).props('accept=".md,.markdown,.txt"').classes("w-full").tooltip(
            "Выберите файл — контент останется в памяти, кнопка «Обработать» предложит домен/предмет/теги"
        )

    file_status_label = ui.label("").classes("text-body2 text-grey q-mb-sm")

    content_input = ui.textarea(
        label="Контент (Markdown/plain) — или вставьте текст вручную",
        placeholder="Введите текст для импорта или загрузите файл выше...",
    ).classes("w-full").props("rows=10")
    content_input.tooltip(
        "Выберите файл — контент останется в памяти, кнопка «Обработать» предложит домен/предмет/теги. "
        "Это поле — для ручного ввода (fallback)."
    )

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

    # ── Кнопки + спиннер ─────────────────────────────────
    with ui.row().classes("gap-4 items-center"):
        import_btn = ui.button("Добавить", icon="save").props("color=primary")
        analyze_btn = ui.button("Обработать", icon="auto_fix_high").props("color=secondary")
        spinner = ui.spinner(size="md").props("color=primary")
        spinner.visible = False
        analyze_btn.disable()  # disabled пока нет файла

    timer_label = ui.label("").classes("text-caption text-grey")
    timer_label.visible = False
    _timer: ui.timer | None = None

    def _start_timer() -> None:
        """Живой секундомер: ⏱ N с, обновляется каждую секунду."""
        nonlocal _timer
        start = time.monotonic()
        timer_label.visible = True
        timer_label.set_text("⏱ 0 с")
        _timer = ui.timer(1.0, lambda: timer_label.set_text(f"⏱ {time.monotonic() - start:.0f} с"))

    def _stop_timer() -> None:
        nonlocal _timer
        if _timer is not None:
            _timer.cancel()
            _timer = None
        timer_label.visible = False

    def _cleanup_timer() -> None:
        """Отмена секундомера при закрытии/перезагрузке вкладки
        (иначе RuntimeError: parent slot deleted в логах)."""
        nonlocal _timer
        if _timer is not None:
            _timer.cancel()
            _timer = None
    ui.context.client.on_disconnect(_cleanup_timer)

    result_container = ui.column().classes("w-full")

    # ── Analyze handler (НОВЫЙ) ───────────────────────────
    async def do_analyze() -> None:
        """Обработать pending_file через analyze_content и заполнить поля."""
        nonlocal pending_file
        if pending_file is None:
            ui.notify("Сначала загрузите файл", type="warning")
            return

        analyze_btn.disable()
        spinner.visible = True
        result_container.clear()
        _start_timer()

        client = MCPClient(
            base_url=MCP_SERVER_URL,
            api_key=MCP_API_KEY,
            timeout=ANALYZE_TIMEOUT,
        )
        try:
            result = await client.tools_call(
                "analyze_content",
                {"content": pending_file["content"][:ANALYZE_FRAGMENT_CHARS]},
            )

            # Заполняем поля (редактируемые!)
            content_type.value = result.get("content_type", "book")
            domain_input.value = result.get("domain", "")
            subject_input.value = result.get("subject", "")
            tags = result.get("tags", [])
            tags_input.value = ", ".join(tags)

            source = result.get("source", "?")
            ui.notify(
                f"Рекомендации получены (источник: {source}, "
                f"фрагмент: {result.get('fragment_chars', 0)} симв.)",
                type="positive",
            )

        except Exception as exc:
            ui.notify(f"Ошибка анализа: {exc}", type="negative")
        finally:
            analyze_btn.enable()
            spinner.visible = False
            _stop_timer()
            await client.close()

    analyze_btn.on_click(do_analyze)

    # ── Import handler ────────────────────────────────────
    async def do_import() -> None:
        nonlocal pending_file

        # Контент: из pending_file или textarea
        if pending_file is not None:
            content = pending_file["content"]
        else:
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

        # Блокируем кнопки, показываем спиннер
        import_btn.disable()
        analyze_btn.disable()
        spinner.visible = True
        result_container.clear()
        start_time = time.monotonic()
        _start_timer()

        client = MCPClient(
            base_url=MCP_SERVER_URL,
            api_key=MCP_API_KEY,
            timeout=IMPORT_TIMEOUT,
        )
        try:
            result = await client.tools_call("import_content", params)

            elapsed = time.monotonic() - start_time
            imported = result.get("imported", 0)
            failed = result.get("failed", 0)
            collection_id = result.get("collection_id", "—")
            title = result.get("title", f"{domain}/{subject}")

            # ── Показ результата ──────────────────────────
            result_container.clear()
            with result_container:
                ui.label("✅ Импорт выполнен").classes("text-positive text-h6")
                ui.label(f"Коллекция: {collection_id}").classes("text-body2")
                ui.label(f"Импортировано секций: {imported}").classes("text-body2")
                ui.label(f"Время обработки: {elapsed:.1f} сек").classes("text-caption text-grey")
                if failed > 0:
                    ui.label(f"Ошибок: {failed}").classes("text-negative text-body2")

                failed_sections = result.get("failed_sections", [])
                if failed_sections:
                    with ui.card().classes("q-mt-md"):
                        ui.label("Ошибки по секциям:").classes("text-subtitle2")
                        for fs in failed_sections:
                            seq = fs.get("sequence_number", "?")
                            sec_title = fs.get("title", "—")
                            error = fs.get("error", "?")
                            ui.label(f"  #{seq} «{sec_title}»: {error}").classes("text-body2 text-negative")

            # ── История импортов ───────────────────────────
            import_history.append({
                "collection_id": collection_id,
                "imported": imported,
                "failed": failed,
                "domain": domain,
                "subject": subject,
                "title": title,
                "elapsed": elapsed,
                "_time": datetime.now(UTC).strftime("%H:%M:%S"),
            })
            _render_history()

            # Сбрасываем pending_file и статус
            pending_file = None
            file_status_label.set_text("")
            analyze_btn.disable()

            # Очищаем форму для следующего импорта
            content_input.value = ""
            content_input.update()

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            result_container.clear()
            with result_container:
                ui.label(f"❌ Ошибка импорта (через {elapsed:.1f} сек)").classes("text-negative text-h6")
                ui.label(str(exc)).classes("text-body2 text-negative")
            ui.notify(f"Ошибка импорта: {exc}", type="negative")
        finally:
            import_btn.enable()
            analyze_btn.enable()
            spinner.visible = False
            _stop_timer()
            await client.close()

    import_btn.on_click(do_import)

    # ── Начальная отрисовка истории ───────────────────────
    _render_history()
