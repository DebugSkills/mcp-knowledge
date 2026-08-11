"""Страница «Импорт» — импорт контента в базу знаний.

Новый алгоритм (Фаза 13.8):
  1. Загрузили файл — ничего не происходит (контент в памяти)
  2. Нажали «Обработать» → AI-рекомендации: content_type/domain/subject/tags
  3. Подтверждаем/дополняем/изменяем
  4. Нажимаем «Добавить» → import_content на MCP
"""

# ruff: noqa: ASYNC230
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from nicegui import ui

from ..components.queue_console import build_import_queue
from ..config import MCP_API_KEY, MCP_SERVER_URL
from ..core.mcp_client import MCPClient
from ..core.utils import (
    MAX_FILE_SIZE,
    PDF_BINARY_MARKER,
    _read_uploaded_file,
    _sanitize_title,
    render_import_progress,
)

# Таймаут HTTP для вызова import_content (30 минут — крупные учебники; 7032 секций ≈ 14 мин).
IMPORT_TIMEOUT = 1800.0
# Таймаут для вызова analyze_content (60 секунд — LLM).
ANALYZE_TIMEOUT = 60.0
# Фрагмент для AI-анализа (синхронизирован с серверным ANALYZE_FRAGMENT_CHARS) —
# полный контент не отправляется: и скорость, и размер запроса.
ANALYZE_FRAGMENT_CHARS = 8000
# Интервал опроса прогресса импорта (сек).
PROGRESS_POLL_INTERVAL = 1.0


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
            ext = Path(filename).suffix.lower()
            print(f"[IMPORT-UPLOAD] file={filename} size={len(raw)}B ext={ext}")

            if ext == ".pdf":
                # PDF: multipart upload через POST /upload
                import os
                import tempfile
                # Сохраняем raw bytes во временный файл для upload_pdf
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="kb_upload_")
                os.close(tmp_fd)
                with open(tmp_path, "wb") as f:
                    f.write(raw)
                try:
                    client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
                    upload_result = await client.upload_pdf(tmp_path, filename)
                    await client.close()
                    pdf_path = upload_result.get("pdf_path", "")
                    pending_file = {
                        "name": filename,
                        "size": len(raw),
                        "content": PDF_BINARY_MARKER,
                        "pdf_path": pdf_path,
                        "import_id": upload_result.get("upload_id", ""),
                        "base_id": upload_result.get("upload_id", ""),  # code-2026-08-11: единый base для convert/analyze/import
                    }
                    file_status_label.set_text(
                        f"📄 {filename} — {len(raw) / 1_048_576:.1f} МБ (PDF загружен на сервер)"
                    )
                    title_input.value = Path(filename).stem
                    content_type.value = "pdf"
                    # 3-стадийный PDF-флоу: Преобразовать→Обработать→Добавить.
                    # «Сохранить нельзя» — импорт и анализ заблокированы до конвертации в текст.
                    convert_btn.enable()
                    analyze_btn.disable()
                    import_btn.disable()
                    ui.notify(
                        f"«{filename}» PDF загружен. Нажмите «Преобразовать» для конвертации в текст",
                        type="positive",
                    )
                except Exception as upload_err:
                    ui.notify(f"Ошибка загрузки PDF: {upload_err}", type="negative")
                    print(f"[IMPORT-UPLOAD] PDF upload failed: {upload_err}")
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return

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
                "base_id": str(uuid.uuid4()),  # code-2026-08-11: единый base для analyze/import
            }
            file_status_label.set_text(
                f"📄 {filename} — {len(raw) / 1_048_576:.1f} МБ ({len(text):,} символов)"
            )
            # Pre-fill title_input из имени файла (без расширения)
            title_input.value = Path(filename).stem
            analyze_btn.enable()
            import_btn.enable()
            convert_btn.disable()  # текстовый флоу без конвертации
            ui.notify(f"«{filename}» загружен ({len(text):,} символов)", type="positive")
            print(f"[IMPORT-UPLOAD] OK {len(text)} chars — pending_file set, textarea untouched")

        def on_rejected(e) -> None:
            print("[IMPORT-UPLOAD] REJECTED by Quasar client-side")

        ui.upload(
            on_upload=handle_upload,
            on_rejected=on_rejected,
            auto_upload=True,
            max_file_size=MAX_FILE_SIZE,
        ).props('accept=".md,.markdown,.txt,.pdf"').classes("w-full").tooltip(
            "Выберите файл — контент останется в памяти, кнопка «Обработать» предложит домен/предмет/теги"
        )

    file_status_label = ui.label("").classes("text-body2 text-grey q-mb-sm")

    title_input = ui.input(
        label="Название книги",
        placeholder="Авто: домен/предмет book",
    ).classes("w-full q-mb-sm")
    title_input.tooltip(
        "Введите название книги. Если оставить пустым — сервер сгенерирует автоматически "
        "(домен/предмет book)."
    )

    with ui.row().classes("gap-4"):
        content_type = ui.select(
            label="Тип контента",
            options=["book", "pdf"],
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

    replace_select = ui.select(
        label="Заменить существующую книгу (опционально)",
        options={},
        value=None,
        with_input=True,
    ).classes("w-full q-mb-md")

    # ── Асинхронная загрузка опций для replace_select ──────
    async def _load_replace_options() -> None:
        """Загрузить список коллекций для replace-дропдауна."""
        try:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
            books = await client.list_collections()
            opts: dict[str, str] = {}
            for b in books:
                cid = b.get("collection_id", "")
                if not cid:
                    continue
                label = (
                    f"{b.get('title', cid)} "
                    f"({b.get('domain', '—')}/{b.get('subject', '—')}, "
                    f"{b.get('section_count', 0)} сек.)"
                )
                opts[label] = cid
            replace_select.options = opts
            replace_select.update()
            await client.close()
        except Exception:
            pass  # не критично — можно ввести ID вручную через with_input=True

    ui.timer(0.0, lambda: _load_replace_options(), once=True)

    # ── Кнопки + спиннер ─────────────────────────────────
    # Порядок: Преобразовать → Обработать → Добавить (слева-направо).
    # flex-wrap: на 375px кнопки + спиннер переносятся без горизонтального скролла.
    # Цвета: кнопки всегда цветные; заблокированные — полупрозрачные (opacity .55),
    # активные — непрозрачные (opacity 1). CSS через класс .import-action-btn.
    # Цвета: кнопки всегда цветные; заблокированные — полупрозрачные (opacity .55),
    # активные — непрозрачные (opacity 1).
    # Quasar кладёт .q-btn.disabled{opacity:.7!important} в @layer — CSS-правило вне
    # слоя проигрывает (каскадные слои), поэтому применяем inline-стиль через JS:
    # inline opacity с !important имеет максимальный приоритет.
    ui.add_head_html(
        """
        <script>
        function applyImportBtnOpacity() {
            document.querySelectorAll('button.import-action-btn').forEach(function (b) {
                b.style.setProperty('opacity', b.disabled ? '0.55' : '1', 'important');
            });
        }
        // documentElement — всегда существует (в отличие от body в момент загрузки head)
        new MutationObserver(applyImportBtnOpacity).observe(
            document.documentElement, { subtree: true, attributes: true, attributeFilter: ['disabled', 'class'] }
        );
        document.addEventListener('DOMContentLoaded', applyImportBtnOpacity);
        applyImportBtnOpacity();
        </script>
        """
    )
    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
        convert_btn = ui.button("Преобразовать", icon="picture_as_pdf").props("color=info").classes("import-action-btn")
        analyze_btn = ui.button("Обработать", icon="auto_fix_high").props("color=secondary").classes("import-action-btn")
        import_btn = ui.button("Добавить", icon="save").props("color=primary").classes("import-action-btn")
        spinner = ui.spinner(size="md").props("color=primary")
        spinner.visible = False
        convert_btn.disable()   # disabled пока нет PDF
        analyze_btn.disable()   # disabled пока нет файла
        import_btn.disable()    # disabled пока нет файла (скрытый БАГ: был активен без файла)

    # ── Отчёт об импорте (Variant A: обёртка с очередью + сводкой) ────
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        ui.label("📊 Отчёт об импорте").classes("text-h6 q-mb-sm")
        build_import_queue()
        result_container = ui.column().classes("w-full")

    timer_label = ui.label("").classes("text-caption text-grey")
    timer_label.visible = False
    _timer: ui.timer | None = None
    # 13.9: таймер опроса живого прогресса импорта
    _progress_timer: ui.timer | None = None

    def _start_timer() -> None:
        """Живой секундомер: ⏱ N с, обновляется каждую секунду."""
        nonlocal _timer
        start = time.monotonic()
        timer_label.visible = True
        timer_label.set_text("⏱ 0 с")

        def _tick() -> None:
            # Defensive: при навигации/перезагрузке вкладки parent slot может
            # быть удалён — проглатываем, чтобы не плодить RuntimeError в логах.
            try:
                timer_label.set_text(f"⏱ {time.monotonic() - start:.0f} с")
            except Exception:
                pass

        _timer = ui.timer(1.0, _tick)

    def _stop_timer() -> None:
        nonlocal _timer
        if _timer is not None:
            _timer.cancel()
            _timer = None
        timer_label.visible = False

    def _cleanup_timer() -> None:
        """Отмена секундомера при закрытии/перезагрузке вкладки
        (иначе RuntimeError: parent slot deleted в логах)."""
        nonlocal _timer, _progress_timer
        if _timer is not None:
            _timer.cancel()
            _timer = None
        if _progress_timer is not None:
            _progress_timer.cancel()
            _progress_timer = None
    ui.context.client.on_disconnect(_cleanup_timer)

    # 13.9: контейнер живого прогресса импорта — ВНЕ обёртки «Отчёт об импорте»
    # (visible=True только во время активного импорта; result_container — после)
    progress_container = ui.column().classes("w-full q-mb-md")
    progress_container.visible = False

    def _stop_progress_poll() -> None:
        """Остановить опрос прогресса и скрыть контейнер."""
        nonlocal _progress_timer
        if _progress_timer is not None:
            _progress_timer.cancel()
            _progress_timer = None
        progress_container.visible = False
        progress_container.clear()


    # ── Convert handler (PDF→текст, стадия 1 из 3) ─────────
    async def do_convert() -> None:
        """Преобразовать PDF в текст: операция очереди + поллинг карточки.

        code-2026-08-11-queue: POST /imports/convert → карточка в очереди
        (статус/логи в build_import_queue) → поллинг GET /imports/{id}/progress
        до терминального статуса → текст из snapshot["result"].
        """
        nonlocal pending_file
        if pending_file is None or not pending_file.get("pdf_path"):
            ui.notify("Сначала загрузите PDF", type="warning")
            return

        convert_btn.disable()
        spinner.visible = True
        result_container.clear()
        _start_timer()

        client = MCPClient(
            base_url=MCP_SERVER_URL,
            api_key=MCP_API_KEY,
            timeout=IMPORT_TIMEOUT,
        )
        try:
            resp = await client.start_convert(
                pending_file["pdf_path"], pending_file["base_id"]
            )
            convert_id = resp.get("import_id", f"{pending_file['base_id']}:convert")

            # P2-4: поллинг с deadline (паттерн _run_import) — graceful при 404/None
            deadline = time.monotonic() + IMPORT_TIMEOUT
            snapshot = None
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                snapshot = await client.get_progress(convert_id)
                if snapshot is not None and snapshot.get("status") in ("done", "error", "cancelled"):
                    break
            else:
                raise RuntimeError(f"Таймаут конвертации ({IMPORT_TIMEOUT:.0f} с)")

            if snapshot is None:
                raise RuntimeError("Прогресс конвертации недоступен (сервер не отвечает)")

            status = snapshot.get("status")
            result = snapshot.get("result") or {}

            if status == "done":
                text = result.get("text", "")
                chars = result.get("chars", len(text))
                # Сохраняем текст в pending_file → analyze/import работают как для текста
                pending_file["content"] = text
                file_status_label.set_text(
                    f"📄 {pending_file['name']} — PDF конвертирован: {chars:,} символов текста"
                )
                # Стадия 2 и 3 становятся доступными
                analyze_btn.enable()
                import_btn.enable()
                convert_btn.disable()
                ui.notify(
                    f"PDF конвертирован ({chars:,} символов). Нажмите «Обработать» для авто-классификации",
                    type="positive",
                )
                print(f"[IMPORT-UPLOAD] PDF converted: {chars} chars -> analyze enabled")
            elif status == "error":
                convert_btn.enable()
                file_status_label.set_text("⚠️ Ошибка конвертации PDF")
                ui.notify(f"Ошибка конвертации PDF: {snapshot.get('error', '?')}", type="negative")
            elif status == "cancelled":
                convert_btn.enable()
                file_status_label.set_text("⚠️ Конвертация отменена")
                ui.notify("Конвертация отменена", type="warning")
        except Exception as exc:
            # P1 (critic): при ошибке возвращаем convert (можно повторить),
            # import/analyze остаются заблокированы.
            convert_btn.enable()
            file_status_label.set_text("⚠️ Ошибка конвертации PDF")
            ui.notify(f"Ошибка конвертации PDF: {exc}", type="negative")
            print(f"[IMPORT-UPLOAD] PDF convert failed: {exc}")
        finally:
            spinner.visible = False
            _stop_timer()
            await client.close()

    convert_btn.on_click(do_convert)

    # ── Analyze handler (НОВЫЙ) ───────────────────────────
    async def do_analyze() -> None:
        """Обработать pending_file: операция очереди + поллинг карточки.

        code-2026-08-11-queue: POST /imports/analyze → карточка в очереди →
        поллинг /progress → при done заполняем форму из snapshot["result"].
        """
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
            resp = await client.start_analyze(
                pending_file["content"][:ANALYZE_FRAGMENT_CHARS],
                pending_file["base_id"],
            )
            analyze_id = resp.get("import_id", f"{pending_file['base_id']}:analyze")

            # P2-4: поллинг с deadline (+30s буфер на очередь семафора)
            deadline = time.monotonic() + ANALYZE_TIMEOUT + 30
            snapshot = None
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                snapshot = await client.get_progress(analyze_id)
                if snapshot is not None and snapshot.get("status") in ("done", "error", "cancelled"):
                    break
            else:
                raise RuntimeError("Таймаут анализа")

            if snapshot is None:
                raise RuntimeError("Прогресс анализа недоступен (сервер не отвечает)")

            status = snapshot.get("status")
            result = snapshot.get("result") or {}

            if status == "done":
                # Заполняем поля (редактируемые!) — title_input НЕ трогаем (ручное поле)
                # PDF-флоу: content_type остаётся "pdf" — импорт пойдёт через очередь
                # (submit_import с pdf_path, карточка в build_import_queue).
                # Для текстовых файлов — рекомендация анализатора.
                if pending_file.get("pdf_path"):
                    content_type.value = "pdf"
                else:
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
            elif status == "error":
                ui.notify(f"Ошибка анализа: {snapshot.get('error', '?')}", type="negative")
            elif status == "cancelled":
                ui.notify("Анализ отменён", type="warning")

        except Exception as exc:
            ui.notify(f"Ошибка анализа: {exc}", type="negative")
        finally:
            analyze_btn.enable()
            spinner.visible = False
            _stop_timer()
            await client.close()

    analyze_btn.on_click(do_analyze)

    # ── Import handler ────────────────────────────────────
    async def _run_import(params: dict, domain: str, subject: str) -> None:
        """Выполнить импорт (без валидаций — они уже сделаны в do_import)."""
        nonlocal pending_file, _progress_timer

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
        # 13.9: старт живого прогресса — поллинг GET /imports/{id}/progress
        progress_container.visible = True
        progress_container.clear()

        import_id = params.get("import_id", "")

        async def _poll_once() -> None:
            nonlocal _progress_timer
            try:
                snapshot = await client.get_progress(import_id)
                if snapshot is not None:
                    render_import_progress(snapshot, progress_container)
            except Exception:
                pass  # graceful: no crash on transient poll error

        _progress_timer = ui.timer(PROGRESS_POLL_INTERVAL, _poll_once)

        try:
            result = await client.tools_call("import_content", params)

            # ── Bug C fix: PDF async-флоу — поллинг до терминального статуса ─
            # submit_import возвращает мгновенно {"status":"started"|"queued"} для pdf.
            # Book (sync) возвращает полный результат сразу — status отсутствует или "ok".
            if result.get("status") in ("started", "queued"):
                deadline = time.monotonic() + IMPORT_TIMEOUT
                snapshot = None
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    snapshot = await client.get_progress(import_id)
                    if snapshot is not None:
                        render_import_progress(snapshot, progress_container)
                        if snapshot.get("status") in ("done", "error", "cancelled"):
                            break
                    # Продолжаем поллинг (snapshot может быть None при transient error)
                else:
                    # deadline exceeded
                    raise RuntimeError("Таймаут импорта (сервер не завершил за " + str(IMPORT_TIMEOUT) + "с)")

                if snapshot is None:
                    raise RuntimeError("Прогресс импорта недоступен (сервер не отвечает)")

                if snapshot.get("status") == "done":
                    result = {
                        "imported": snapshot.get("imported", 0),
                        "failed": snapshot.get("failed", 0),
                        "collection_id": snapshot.get("collection_id", "—"),
                        "status": "done",
                        "title": params.get("title", f"{domain}/{subject}"),
                    }
                elif snapshot.get("status") == "error":
                    raise RuntimeError(snapshot.get("error", "Import failed (server error)"))
                elif snapshot.get("status") == "cancelled":
                    raise RuntimeError("Импорт отменён")
                else:
                    raise RuntimeError("Неизвестный статус импорта: " + snapshot.get("status", "?"))

            elapsed = time.monotonic() - start_time
            imported = result.get("imported", 0)
            failed = result.get("failed", 0)
            collection_id = result.get("collection_id", "—")
            result_title = result.get("title", f"{domain}/{subject}")

            # ── Показ результата (яркая зелёная карточка-отчёт) ──
            result_container.clear()
            with result_container:
                with ui.card().classes("bg-green-6 text-white q-pa-md w-full"):
                    ui.label("✅ Импорт выполнен").classes("text-h6")
                    ui.label(f"Коллекция: {collection_id}").classes("text-body2")
                    ui.label(f"Импортировано секций: {imported}").classes("text-body2")
                    ui.label(f"Время обработки: {elapsed:.1f} сек").classes("text-caption")
                    if failed > 0:
                        ui.label(f"Ошибок: {failed}").classes("text-bold")

                # ── Replace result (Фаза 13.22) ──────────────
                if result.get("replaced"):
                    replaced_id = result.get("replaced_collection_id", "—")
                    cascade_del = result.get("cascade_deleted", 0)
                    with ui.card().classes("bg-warning q-pa-sm q-mt-sm w-full"):
                        ui.label(
                            f"♻️ Заменена книга: {replaced_id} (удалено старых секций: {cascade_del})"
                        ).classes("text-body2")
                elif result.get("replace_skipped_reason"):
                    reason = result["replace_skipped_reason"]
                    with ui.card().classes("bg-orange-2 q-pa-sm q-mt-sm w-full"):
                        ui.label(
                            f"⚠️ Замена отменена: {reason}"
                        ).classes("text-body2")

                failed_sections = result.get("failed_sections", [])
                if failed_sections:
                    with ui.card().classes("bg-red-1 q-pa-sm q-mt-sm w-full"):
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
                "title": result_title,
                "elapsed": elapsed,
                "replaced": result.get("replaced", False),
                "_time": datetime.now(UTC).strftime("%H:%M:%S"),
            })
            _render_history()

            # Сбрасываем pending_file и статус
            pending_file = None
            file_status_label.set_text("")
            analyze_btn.disable()
            convert_btn.disable()

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            result_container.clear()
            with result_container, ui.card().classes("bg-red-5 text-white q-pa-md w-full"):
                ui.label(f"❌ Ошибка импорта (через {elapsed:.1f} сек)").classes("text-h6")
                ui.label(str(exc)).classes("text-body2")
            ui.notify(f"Ошибка импорта: {exc}", type="negative")
        finally:
            import_btn.enable()
            analyze_btn.enable()
            spinner.visible = False
            _stop_timer()
            _stop_progress_poll()
            await client.close()

    async def do_import() -> None:
        """Валидация + HITL-диалог при замене → _run_import."""
        nonlocal pending_file

        # Контент: только через загрузку файла (ручной ввод убран — Фаза 1)
        if pending_file is None:
            ui.notify("Сначала загрузите файл", type="warning")
            return
        content = pending_file["content"]

        domain = domain_input.value.strip()
        subject = subject_input.value.strip()
        if not domain or not subject:
            ui.notify("Заполните домен и предмет", type="warning")
            return

        # Парсим теги
        tags_text = tags_input.value or ""
        tags = [t.strip() for t in tags_text.split(",") if t.strip()]

        params: dict = {
            "content": content,
            "content_type": content_type.value,
            "domain": domain,
            "subject": subject,
        }
        # PDF: передаём pdf_path вместо текста контента
        if content_type.value == "pdf" and pending_file and pending_file.get("pdf_path"):
            params["content"] = ""
            params["pdf_path"] = pending_file["pdf_path"]
            params["content_type"] = "pdf"
        # Санитизированный title (пустой → сервер генерит авто)
        title = _sanitize_title(title_input.value or "")
        if title:
            params["title"] = title
        if tags:
            params["tags"] = tags
        # 13.9: идентификатор импорта для живого прогресса (poll GET /imports/{id}/progress).
        # code-2026-08-11-queue: единый base_id (upload_id для PDF / uuid4 для текста) —
        # convert/analyze операции идут с суффиксами ":convert"/":analyze", import — как есть.
        if pending_file and pending_file.get("base_id"):
            import_id = pending_file["base_id"]
        else:
            import_id = str(uuid.uuid4())
        params["import_id"] = import_id

        # replace_collection_id
        if replace_select.value:
            params["replace_collection_id"] = replace_select.value

        # HITL-диалог при замене
        if replace_select.value:
            replace_label = replace_select.value
            # Ищем человекочитаемое название в опциях
            for lbl, val in (replace_select.options or {}).items():
                if val == replace_select.value:
                    replace_label = lbl
                    break

            async def _confirm_and_run() -> None:
                dialog.close()
                await _run_import(params, domain, subject)

            with ui.dialog() as dialog, ui.card():
                ui.label(
                    f"⚠️ Книга «{replace_label}» ({replace_select.value}) будет "
                    f"УДАЛЕНА после успешного импорта новой. "
                    f"Старая версия сохранится в .trash/."
                ).classes("text-body2 q-mb-md")
                ui.label("Продолжить?").classes("text-body2 q-mb-md")
                with ui.row().classes("justify-end"):
                    ui.button("Отмена", on_click=lambda: dialog.close()).props("flat")
                    ui.button("Заменить", on_click=_confirm_and_run).props("color=warning")
            dialog.open()
        else:
            await _run_import(params, domain, subject)

    import_btn.on_click(do_import)

    # ── Начальная отрисовка истории ───────────────────────
    _render_history()
