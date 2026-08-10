"""Компонент «Замена книги» — модальный диалог замены книги через import_content.

Используется из разных страниц (books, import):
  - books.py: кнопка «♻️ Заменить» на карточке → show_replace_dialog
  - (future) import_page.py: может мигрировать сюда replace-флоу

Паттерн: non-persistent диалог (ui.dialog + ui.card), как show_book_dialog.
Прогресс-поллинг: GET /imports/{id}/progress (паттерн из import_page.py).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient
from ..core.utils import MAX_FILE_SIZE, _read_uploaded_file, render_import_progress

# Таймаут HTTP для import_content (30 минут — крупные учебники).
IMPORT_TIMEOUT = 1800.0
# Интервал опроса прогресса (сек).
PROGRESS_POLL_INTERVAL = 1.0


async def show_replace_dialog(
    collection_id: str,
    title: str,
    domain: str = "",
    subject: str = "",
    on_success: Callable[[], Awaitable[None]] | None = None,
    replace_on_partial: bool = False,
) -> None:
    """Открыть модальный диалог замены книги.

    Flow:
      1. Загрузка нового файла (.md/.txt) через ui.upload
      2. Pre-fill domain/subject из метаданных книги (редактируемые)
      3. Кнопка «♻️ Заменить» → HITL-подтверждение → import_content + replace
      4. Прогресс-поллинг → результат (replaced/cascade_deleted/error)
      5. on_success() callback → закрытие диалога

    Args:
        collection_id: ID заменяемой книги.
        title: Название книги (для UI).
        domain: Pre-fill домен.
        subject: Pre-fill предмет.
        on_success: Callback при успешной замене (для инвалидации кеша/refresh списка).
        replace_on_partial: Default False — при partial-импорте старая книга цела.
            True — заменить даже при частичном успехе (чекбокс в UI).
    """
    import asyncio

    client: MCPClient | None = None
    _progress_timer: ui.timer | None = None
    pending_file: dict | None = None
    active_import_id: str | None = None

    def _stop_timer() -> None:
        nonlocal _progress_timer
        if _progress_timer is not None:
            _progress_timer.cancel()
            _progress_timer = None

    # ── Dialog ──────────────────────────────────────────────────
    with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[90vw]"), ui.column().classes("w-full"):
        # Заголовок + предупреждение
        ui.label(f"♻️ Заменить книгу: {title}").classes("text-h6")
        ui.label(
            "⚠️ Старая версия книги будет УДАЛЕНА после успешного импорта новой. "
            "Удалённая версия сохранится в .trash/."
        ).classes("text-body2 text-warning q-mt-sm")

        # ── File upload ─────────────────────────────────────
        ui.label("Загрузите новый файл (.md / .markdown / .txt)").classes("text-body2 q-mt-md")

        def on_rejected(_e) -> None:
            ui.notify("Файл не принят (проверьте тип и размер)", type="negative")

        file_status = ui.label("").classes("text-body2 text-grey q-mt-xs")

        async def handle_upload(e) -> None:
            nonlocal pending_file
            filename = e.file.name
            raw = await e.file.read()
            text, error = _read_uploaded_file(filename, raw)
            if error is not None:
                ui.notify(f"«{filename}»: {error}", type="negative")
                return
            pending_file = {
                "name": filename,
                "size": len(raw),
                "content": text,
            }
            file_status.set_text(
                f"📄 {filename} — {len(raw) / 1_048_576:.1f} МБ ({len(text):,} символов)"
            )
            _update_button_state()

        ui.upload(
            on_upload=handle_upload,
            on_rejected=on_rejected,
            auto_upload=True,
            max_file_size=MAX_FILE_SIZE,
        ).props('accept=".md,.markdown,.txt"').classes("w-full")

        # ── Domain / Subject (pre-fill из метаданных книги) ──
        domain_input = ui.input(label="Домен", value=domain).classes("w-full q-mt-sm")
        subject_input = ui.input(label="Предмет", value=subject).classes("w-full q-mt-sm")

        # ── Replace on partial checkbox ─────────────────────
        force_partial = ui.checkbox(
            "Заменить даже при частичном успехе (старая книга удаляется при любом результате >0)",
            value=replace_on_partial,
        ).classes("q-mt-sm")

        # ── Progress container (скрыт до начала импорта) ────
        progress_container = ui.column().classes("w-full q-mt-md")
        progress_container.visible = False
        result_container = ui.column().classes("w-full")

        # ── Единый таймер прогресс-поллинга (в контексте диалога, слот гарантирован) ──
        async def _poll_once() -> None:
            """Периодический опрос прогресса импорта (работает вхолостую до старта импорта)."""
            nonlocal client, active_import_id
            if client is None or active_import_id is None:
                return
            try:
                snapshot = await client.get_progress(active_import_id)
                if snapshot is not None:
                    render_import_progress(snapshot, progress_container)
            except Exception:  # noqa: BLE001, S110 — graceful: transient poll error
                pass

        _progress_timer = ui.timer(PROGRESS_POLL_INTERVAL, _poll_once)

        def _update_button_state() -> None:
            """Активировать кнопку только при готовности всех полей."""
            has_file = pending_file is not None
            has_domain = bool(domain_input.value.strip())
            has_subject = bool(subject_input.value.strip())
            replace_btn.set_enabled(has_file and has_domain and has_subject)

        domain_input.on("update:model-value", lambda _e: _update_button_state())
        subject_input.on("update:model-value", lambda _e: _update_button_state())

        # ── Import logic (вызывается после HITL-подтверждения) ──
        async def _run_import() -> None:
            """Выполнить import_content с replace_collection_id."""
            nonlocal client, pending_file, active_import_id
            replace_btn.disable()
            result_container.clear()

            dom = domain_input.value.strip()
            subj = subject_input.value.strip()
            start_time = time.monotonic()

            client = MCPClient(
                base_url=MCP_SERVER_URL,
                api_key=MCP_API_KEY,
                timeout=IMPORT_TIMEOUT,
            )

            import_id = str(uuid.uuid4())
            active_import_id = import_id
            params: dict = {
                "content": pending_file["content"],
                "content_type": "book",
                "domain": dom,
                "subject": subj,
                "import_id": import_id,
                "replace_collection_id": collection_id,
                "replace_on_partial": force_partial.value,
            }

            # ── Показываем прогресс-контейнер ──────────────
            progress_container.visible = True
            progress_container.clear()

            try:
                result = await client.tools_call("import_content", params)
                elapsed = time.monotonic() - start_time

                with result_container:
                    imported = result.get("imported", 0)
                    failed = result.get("failed", 0)

                    if result.get("replaced"):
                        cascade_del = result.get("cascade_deleted", 0)
                        ui.label(
                            f"✅ Книга заменена (удалено старых секций: {cascade_del}) "
                            f"за {elapsed:.1f} сек"
                        ).classes("text-positive text-body2")
                        ui.label(
                            f"Импортировано секций: {imported}"
                            + (f", ошибок: {failed}" if failed else "")
                        ).classes("text-caption text-grey")

                        if on_success is not None:
                            await on_success()

                        ui.timer(2.0, lambda: dialog.close(), once=True)

                    elif result.get("replace_skipped_reason"):
                        reason = result["replace_skipped_reason"]
                        ui.label(
                            f"⚠️ Замена отменена: {reason} (старая книга цела)"
                        ).classes("text-negative text-body2")
                        ui.label(
                            f"Импортировано секций: {imported}"
                            + (f", ошибок: {failed}" if failed else "")
                        ).classes("text-caption text-grey")
                        replace_btn.enable()

                    else:
                        ui.label(
                            "⚠️ Неожиданный результат: replaced=False, skip_reason отсутствует"
                        ).classes("text-negative text-body2")
                        replace_btn.enable()

            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - start_time
                with result_container:
                    ui.label(
                        f"❌ Ошибка импорта (через {elapsed:.1f} сек)"
                    ).classes("text-negative text-body2")
                    ui.label(str(exc)).classes("text-caption text-grey")
                replace_btn.enable()

            finally:
                active_import_id = None
                _stop_timer()
                if client is not None:
                    await client.close()
                    client = None

        # ── HITL-подтверждение + старт импорта ──────────────
        async def _start_replace() -> None:
            """Валидация → HITL-диалог → _run_import (callback chain)."""
            nonlocal pending_file
            if pending_file is None:
                ui.notify("Сначала загрузите файл", type="warning")
                return
            dom = domain_input.value.strip()
            subj = subject_input.value.strip()
            if not dom or not subj:
                ui.notify("Заполните домен и предмет", type="warning")
                return

            # HITL-подтверждение (вложенный диалог, паттерн как rename в books.py)
            with ui.dialog() as confirm_dialog, ui.card():
                ui.label("Подтверждение замены").classes("text-h6")
                ui.label(
                    f"⚠️ Книга «{title}» будет УДАЛЕНА после успешного импорта новой. "
                    "Удалённая версия сохранится в .trash/. Продолжить?"
                ).classes("text-body2 q-mb-md")
                with ui.row().classes("justify-end"):
                    ui.button("Отмена", on_click=lambda: confirm_dialog.close()).props("flat")

                    async def _confirm_replace() -> None:
                        confirm_dialog.close()
                        await _run_import()

                    ui.button("Заменить", icon="warning", on_click=_confirm_replace).props(
                        "color=warning"
                    )

            confirm_dialog.open()

        # ── Кнопки ──────────────────────────────────────────
        with ui.row().classes("gap-2 q-mt-md"):
            replace_btn = ui.button(
                "♻️ Заменить",
                icon="cached",
                on_click=_start_replace,
            ).props("color=warning")
            replace_btn.disable()
            ui.button("Закрыть", on_click=lambda: dialog.close()).props("flat")

    # ── Dialog hide cleanup ─────────────────────────────────
    async def _on_hide() -> None:
        nonlocal client
        _stop_timer()
        if client is not None:
            await client.close()
            client = None

    dialog.on("hide", lambda _d=None: asyncio.create_task(_on_hide()))
    dialog.open()

