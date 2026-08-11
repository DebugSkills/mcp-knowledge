# ruff: noqa: BLE001, S110, ASYNC230
"""import_content MCP Tool (#16) — импорт крупных текстов в SSOT.

Фаза 5 §5: отдельный tool для декомпозиции + best-effort batch записи.

Flow:
  1. Валидация параметров (content, content_type, domain, subject)
  2. Registry lookup → ContentPreprocessor
  3. preprocessor.validate() → проверка контента
  4. preprocessor.decompose() → list[Section]
  5. Создание root-коллекции (linking.build_collection)
  6. Batch write: per-section store.write + pipeline.enqueue
  7. Partial_success контракт: failed_sections[], cleanup_orphans опция

Tasks: 5.1, 5.4, 5.5

13.21: PDF import queue — submit_import (instant), _bg_import (async task with lock),
cancel_import (cancel_event), base64 decode path.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os as _os
import uuid as _uuid
from datetime import datetime, timezone

from ..config import settings
from ..content.linking import build_collection
from ..content.preprocessor import ImportMeta
from ..content.registry import get as get_preprocessor
from ..models import KnowledgeEntry, KnowledgeFrontmatter

logger = logging.getLogger("mcp_knowledge.tools.content")

# ── Конфигурация ──────────────────────────────────────────

# Частота прогресс-строк «N/M sections written» в логе/tracker (НЕ git-коммитов!).
# Git-коммит делается ОДИН на книгу в конце импорта — иначе на 7000-секционную книгу
# уходило ~700 коммитов (batch #1..#700) и кеш .git переполнялся.
IMPORT_BATCH_COMMIT = 10

# P2: максимальное число строк в лог-буфере импорта (ring buffer)
LOG_CAP = 200
# P2: частота лог-строк «N/M indexing» (каждые N% прогресса)
LOG_INDEXING_MODULUS = 25  # каждые 25%


# ── Progress tracker helper (Фаза 13.9) ───────────────────

def _p(tracker, import_id: str, method: str, *args) -> None:  # type: ignore[no-untyped-def]
    """Best-effort вызов метода tracker'а — никогда не падает."""
    if tracker is None or not import_id:
        return
    try:
        getattr(tracker, method)(import_id, *args)
    except Exception:
        pass


# ── 13.21: Import queue (submit + bg task + cancel) ──────────

# In-memory import queue records (сессионная)
# Поля: import_id, name, status (queued|running|done|error|cancelled),
# phase (parsing|indexing|done), imported, total, error, created_at, finished_at
_import_queue: list[dict] = []


async def submit_import(params: dict, app_state) -> dict:
    """Запустить импорт как фоновую задачу (13.21).

    Контракт (как run_quality_scan):
        {"import_id": "...", "status": "started"} — импорт запущен
        {"import_id": "...", "status": "queued"} — lock занят, в очереди
        {"import_id": "...", "status": "error", "error": "..."} — ошибка

    Для book: выполняет синхронно (как раньше).
    Для pdf: запускает _bg_import.
    """
    import_id = params.get("import_id") or _uuid.uuid4().hex[:12]
    content_type = params.get("content_type", "book")
    pdf_path = params.get("pdf_path", "")
    title = params.get("title", "")
    name = title or (pdf_path.split("/")[-1] if pdf_path else content_type)

    # materialize source_path for pdf
    source_path = None

    if content_type == "pdf":
        if pdf_path and _os.path.exists(pdf_path):
            source_path = pdf_path
            # If the file is not in our upload dir, copy it so we own the lifecycle
            if not source_path.startswith("/tmp/pdf_upload"):
                tmp_dir = "/tmp/pdf_uploads"
                _os.makedirs(tmp_dir, exist_ok=True)
                dest = _os.path.join(tmp_dir, f"copy_{import_id}.pdf")
                import shutil as _shutil
                _shutil.copy2(source_path, dest)
                source_path = dest
        else:
            # base64-путь: декодируем контент во временный файл
            content = params.get("content", "")
            if content:
                try:
                    raw = base64.b64decode(content)
                except Exception:
                    # может быть не base64 — попробуем как текст
                    raw = content.encode("utf-8", errors="replace")

                tmp_dir = "/tmp/pdf_uploads"
                _os.makedirs(tmp_dir, exist_ok=True)
                source_path = _os.path.join(tmp_dir, f"base64_{import_id}.pdf")
                with open(source_path, "wb") as f:
                    f.write(raw)
                logger.info(
                    "[IMPORT] base64 decoded: %s (%.1f KB)",
                    source_path, len(raw) / 1024,
                )
            else:
                return {
                    "import_id": import_id,
                    "status": "error",
                    "error": "PDF import requires 'content' (base64) or 'pdf_path' parameter",
                }

        params["_source_path"] = source_path

    # book: синхронный возврат (backward compat)
    if content_type != "pdf":
        return await import_content(params, app_state)

    # pdf: фоновая задача
    heavy_ops_lock = getattr(app_state, "heavy_ops_lock", None)
    if heavy_ops_lock is None:
        return {"import_id": import_id, "status": "error", "error": "heavy_ops_lock not initialized"}

    # Создаём запись в очереди
    now = datetime.now(timezone.utc).isoformat()
    rec = {
        "import_id": import_id,
        "name": name,
        "status": "queued",
        "phase": "",
        "imported": 0,
        "total": 0,
        "collection_id": "",
        "error": None,
        "created_at": now,
        "finished_at": None,
        "log": [],  # P2: построчный лог импорта (ring buffer, cap 200)
    }

    # Проверяем lock
    if heavy_ops_lock.locked():
        rec["status"] = "queued"
        rec["_params"] = params  # для отложенного запуска (продвижение очереди)
        _import_queue.append(rec)
        # Синхронизируем с app_state для HTTP endpoint
        if hasattr(app_state, "import_queue"):
            app_state.import_queue = _import_queue
        return {"import_id": import_id, "status": "queued"}

    rec["status"] = "running"
    _import_queue.append(rec)
    if hasattr(app_state, "import_queue"):
        app_state.import_queue = _import_queue

    # Новый cancel_event
    app_state.import_cancel_event = asyncio.Event()

    task = asyncio.create_task(
        _bg_import(
            import_id=import_id,
            params=params,
            app_state=app_state,
            cancel_event=app_state.import_cancel_event,
            lock=heavy_ops_lock,
        )
    )
    app_state.import_task = task

    logger.info("[IMPORT] task started: %s (name=%s)", import_id, name)
    return {"import_id": import_id, "status": "started"}


async def _bg_import(
    import_id: str,
    params: dict,
    app_state,
    cancel_event: asyncio.Event,
    lock: asyncio.Lock,
) -> None:
    """Фоновая задача импорта (13.21).

    Паттерн: quality.py _bg_scan.
    - acquire lock → status=running
    - phase "parsing": PDFPreprocessor.decompose (checkpoint+OCR)
    - phase "indexing": batch-write sections → pipeline
    - finally: cleanup task_ref + temp file [P0-3]
    """
    source_path = params.get("_source_path", "")
    tracker = getattr(app_state, "import_progress", None)
    content_type = params.get("content_type", "pdf")
    _start_ts = datetime.now(timezone.utc)  # P2: для elapsed в done

    def _update_queue(**kwargs):
        """Обновить запись в очереди."""
        for rec in _import_queue:
            if rec.get("import_id") == import_id:
                rec.update(kwargs)
                break

    try:
        async with lock:
            _update_queue(status="running", phase="starting")
            _update_log(import_id, "info", "import started")

            # Phase 1: Parsing
            _update_queue(phase="parsing")
            # Bug A fix: tracker.start() создаёт запись, без неё set_phase/get — no-op → GET /progress 404
            _p(tracker, import_id, "start", 0, {"file": params.get("title", ""), "content_type": content_type})
            _p(tracker, import_id, "set_phase", "parsing")

            domain = params.get("domain", "")
            subject = params.get("subject", "")
            title = params.get("title", "")
            project = params.get("project")
            tags = params.get("tags", [])
            cross_subjects = params.get("cross_subjects", [])

            metadata = ImportMeta(
                domain=domain,
                subject=subject,
                project=project,
                title=title or source_path.split("/")[-1] if source_path else "pdf_import",
                tags=tags,
                cross_subjects=cross_subjects,
                source_path=source_path,
            )

            preprocessor = get_preprocessor(content_type)

            # Validate
            validation = preprocessor.validate("", metadata)
            if not validation.valid:
                err = f"Content validation failed: {validation.error}"
                _update_queue(status="error", error=err, collection_id="")
                _update_log(import_id, "error", f"validate FAIL: {validation.error}")
                _p(tracker, import_id, "error", err)
                return
            _update_log(import_id, "info", "validate OK")

            # Decompose (with cancel_event)
            try:
                sections = await preprocessor.decompose("", metadata, cancel_event=cancel_event)
            except asyncio.CancelledError:
                _update_queue(status="cancelled", error="Cancelled during parsing", collection_id="")
                _update_log(import_id, "error", "cancelled: during parsing")
                _p(tracker, import_id, "error", "Cancelled during parsing")
                return

            if not sections:
                _update_queue(status="error", error="Decomposition produced 0 sections", collection_id="")
                _update_log(import_id, "error", "decompose produced 0 sections")
                return

            _update_queue(total=len(sections))
            _update_log(import_id, "info", f"decompose: {len(sections)} sections")

            # Cancel check before indexing
            if cancel_event.is_set():
                _update_queue(status="cancelled", error="Cancelled before indexing", collection_id="")
                _update_log(import_id, "error", "cancelled: before indexing")
                return

            # Phase 2: Indexing (reuse existing batch-write)
            _update_queue(phase="indexing")
            _update_log(import_id, "info", f"indexing: {len(sections)} sections")
            _p(tracker, import_id, "set_phase", "indexing")

            result = await _batch_write_sections(
                sections=sections,
                params=params,
                app_state=app_state,
                import_id=import_id,
                cancel_event=cancel_event,
            )

            if result.get("error"):
                _update_queue(status="error", error=result["error"], collection_id=result.get("collection_id", ""))
                _update_log(import_id, "error", f"batch write FAIL: {result['error'][:200]}")
            elif result.get("partial_success"):
                _update_queue(
                    status="done",
                    phase="done",
                    imported=result.get("imported", 0),
                    total=result.get("imported", 0),
                    collection_id=result.get("collection_id", ""),
                    error=f"Partial: {result.get('failed', 0)} sections failed",
                )
                elapsed = (datetime.now(timezone.utc) - _start_ts).total_seconds()
                _update_log(import_id, "warning", f"done (partial): {result.get('imported', 0)} sections, {result.get('failed', 0)} failed in {elapsed:.0f}s")
                _p(tracker, import_id, "done", {"imported": result.get("imported", 0), "collection_id": result.get("collection_id", "")})
            else:
                _update_queue(
                    status="done",
                    phase="done",
                    imported=result.get("imported", 0),
                    total=result.get("imported", 0),
                    collection_id=result.get("collection_id", ""),
                )
                elapsed = (datetime.now(timezone.utc) - _start_ts).total_seconds()
                _update_log(import_id, "info", f"done: {result.get('imported', 0)} sections in {elapsed:.0f}s")
                _p(tracker, import_id, "done", {"imported": result.get("imported", 0), "collection_id": result.get("collection_id", "")})

            _update_queue(finished_at=datetime.now(timezone.utc).isoformat())

            logger.info(
                "[IMPORT] done import=%s imported=%d failed=%d",
                import_id, result.get("imported", 0), result.get("failed", 0),
            )

    except asyncio.CancelledError:
        logger.info("[IMPORT] task cancelled (shutdown): %s", import_id)
        _update_queue(status="cancelled", error="Server shutdown", collection_id="")
        _update_log(import_id, "error", "cancelled: server shutdown")
    except Exception as exc:
        logger.exception("[IMPORT] task failed: %s", import_id)
        _update_queue(status="error", error=str(exc), collection_id="")
        _update_log(import_id, "error", f"error: {str(exc)[:250]}")
    finally:
        # [P0-3] Cleanup temp file — only if in /tmp/ (server-owned temp files)
        if source_path and _os.path.exists(source_path) and source_path.startswith("/tmp/"):
            try:
                _os.unlink(source_path)
                logger.info("[IMPORT] temp file deleted: %s", source_path)
            except OSError as e:
                logger.warning("[IMPORT] failed to delete temp file %s: %s", source_path, e)

        # Cleanup task reference
        try:
            app_state.import_task = None
            app_state.import_cancel_event = None
        except Exception:
            pass

        # Продвижение очереди: запустить следующий queued импорт (если есть)
        try:
            _start_next_import(app_state)
        except Exception as e:
            logger.warning("[IMPORT] queue advance failed: %s", e)


async def _batch_write_sections(
    sections,
    params: dict,
    app_state,
    import_id: str,
    cancel_event: asyncio.Event,
) -> dict:
    """Выделенный хелпер: batch-write секций (reuse из import_content)."""
    store = app_state.store
    pipeline = app_state.pipeline
    tracker = getattr(app_state, "import_progress", None)

    domain = params.get("domain", "")
    subject = params.get("subject", "")
    title = params.get("title", "")
    project = params.get("project")
    tags = params.get("tags", [])
    cross_subjects = params.get("cross_subjects", [])
    params.get("cleanup_orphans", False)
    params.get("wait_for_index", False)

    if not title:
        title = f"{domain}/{subject} pdf"

    # Collect knowledge_ids
    section_titles = [s.title for s in sections]
    section_ids = [s.meta["knowledge_id"] for s in sections]

    collection = build_collection(
        domain=domain,
        subject=subject,
        project=project,
        title=title,
        section_titles=section_titles,
        section_ids=section_ids,
        tags=tags,
        cross_subjects=cross_subjects,
    )

    # Write root collection
    try:
        root_fm = collection.to_frontmatter()
        root_entry = KnowledgeEntry(
            frontmatter=root_fm,
            content=f"# {title}\n\nКоллекция импортированных секций. Оглавление — в frontmatter.children.",
        )
        await store.write_entry(root_entry)
        await pipeline.enqueue(root_entry, wait_for_index=False)
    except Exception as e:
        return {"error": f"Failed to create collection root: {e}"}

    imported = 0
    failed = 0
    failed_sections: list[dict] = []

    for i, section in enumerate(sections):
        # Cancel check
        if cancel_event.is_set():
            break

        try:
            meta = section.meta
            fm = KnowledgeFrontmatter(
                knowledge_id=meta["knowledge_id"],
                domain=meta["domain"],
                subject=meta["subject"],
                project=meta.get("project"),
                content_type=meta.get("content_type", "book"),
                parent_knowledge_id=collection.knowledge_id,
                sequence_number=section.sequence_number,
                tags=section.tags,
                cross_subjects=meta.get("cross_subjects", []),
            )
            entry = KnowledgeEntry(frontmatter=fm, content=section.body)
            await store.write_entry(entry)

            try:
                await pipeline.enqueue(entry, wait_for_index=False)
            except Exception:
                pass  # best-effort

            imported += 1
            _p(tracker, import_id, "section_done", section.sequence_number, section.title)

            if imported % IMPORT_BATCH_COMMIT == 0:
                _p(tracker, import_id, "log", "info",
                   f"{imported}/{len(sections)} sections written")

            # P2: indexing progress log (каждые 25% или каждые 10 секций)
            if import_id:
                total_s = len(sections)
                pct = (imported * 100) // total_s if total_s else 0
                if pct > 0 and pct % LOG_INDEXING_MODULUS == 0 and pct != ((imported - 1) * 100) // total_s:
                    _update_log(import_id, "info",
                                f"indexing: {imported}/{total_s} ({pct}%)")

            if imported % settings.IMPORT_PERIODIC_COMMIT == 0:
                try:
                    await store.flush(
                        f"import_content: {collection.knowledge_id} "
                        f"periodic commit ({imported}/{len(sections)})"
                    )
                except Exception:
                    pass


        except Exception as e:
            failed += 1
            failed_sections.append({
                "sequence_number": section.sequence_number,
                "title": section.title,
                "error": str(e),
            })
            logger.error(
                "import_content: section %d '%s' failed: %s",
                section.sequence_number, section.title, e,
            )
            _p(tracker, import_id, "section_failed", section.sequence_number, section.title, str(e))
            # Продолжаем best-effort

    # Final git commit — ОДИН на книгу (P2: с логом)
    try:
        await store.flush(
            f"import_content: {collection.knowledge_id} ({imported} sections)"
        )
        # P2: git commit log (только при успехе)
        if import_id:
            _update_log(import_id, "info",
                        f"git commit: {collection.knowledge_id} ({imported} sections)")
    except Exception:
        pass

    # INDEX update
    knowledge_index = getattr(app_state, "knowledge_index", None)
    if knowledge_index:
        try:
            knowledge_index.update_section(domain)
        except Exception:
            pass

    # data_version increment
    try:
        app_state.data_version += 1
    except Exception:
        pass

    partial_success = failed > 0

    return {
        "collection_id": collection.knowledge_id,
        "imported": imported,
        "failed": failed,
        "failed_sections": failed_sections,
        "partial_success": partial_success,
    }


# ── P2: Log buffer helpers (ring buffer, cap 200) ──────────


def _append_log(rec: dict, level: str, text: str) -> None:
    """Добавить строку в ring-буфер лога импорта.

    Args:
        rec: запись очереди (мутируется in-place).
        level: "info" | "warning" | "error".
        text: текст строки (≤300 символов — обрезается).
    """
    log = rec.setdefault("log", [])
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "level": level,
        "text": text[:300],
    }
    # Ring buffer: drop oldest при переполнении
    if len(log) >= LOG_CAP:
        # Маркер «log truncated» при ПЕРВОМ переполнении (не дублировать)
        if not any(e.get("level") == "warning" and "truncated" in e.get("text", "") for e in log):
            # Вставляем маркер В НАЧАЛО, затем удаляем ВТОРОЙ элемент (не маркер)
            log.insert(0, {
                "ts": "",
                "level": "warning",
                "text": f"… log truncated (showing last {LOG_CAP} entries)",
            })
            if len(log) > LOG_CAP:
                log.pop(1)  # drop oldest non-marker entry (сохраняем маркер)
        else:
            # Маркер уже есть — удаляем старейшую НЕ-маркер запись
            if log[0].get("level") == "warning" and "truncated" in log[0].get("text", ""):
                log.pop(1)  # skip marker, drop next-oldest
            else:
                log.pop(0)  # no marker at front — drop oldest
    log.append(entry)


def _update_log(import_id: str, level: str, text: str) -> None:
    """Обновить лог в записи очереди по import_id (best-effort)."""
    for rec in _import_queue:
        if rec.get("import_id") == import_id:
            _append_log(rec, level, text)
            break


def _start_next_import(app_state) -> None:
    """Запустить первый queued импорт после завершения текущего (продвижение очереди)."""
    for rec in _import_queue:
        if rec.get("status") != "queued":
            continue
        import_id = rec["import_id"]
        params = rec.get("_params")
        if params is None:
            rec["status"] = "error"
            rec["error"] = "queue params lost (retry import)"
            logger.warning("[IMPORT] queued %s lost params — skipped", import_id)
            continue
        rec.pop("_params", None)
        rec["status"] = "running"
        # Новый cancel_event
        app_state.import_cancel_event = asyncio.Event()
        task = asyncio.create_task(
            _bg_import(
                import_id=import_id,
                params=params,
                app_state=app_state,
                cancel_event=app_state.import_cancel_event,
                lock=app_state.heavy_ops_lock,
            )
        )
        app_state.import_task = task
        logger.info("[IMPORT] queue advanced: %s (next from queue)", import_id)
        return


async def cancel_import(params: dict, app_state) -> dict:
    """Отменить активный импорт (13.21).

    Устанавливает import_cancel_event → _bg_import проверяет между фазами.

    Returns:
        {"cancelled": True, "import_id": "..."}
        {"cancelled": False, "reason": "..."}
    """
    cancel_event = getattr(app_state, "import_cancel_event", None)
    if cancel_event is None:
        return {"cancelled": False, "reason": "no active import"}

    cancel_event.set()
    import_id = params.get("import_id", "")
    logger.info("[IMPORT] cancel signal sent for import %s", import_id)

    # Обновим очередь
    for rec in _import_queue:
        if import_id and rec.get("import_id") == import_id:
            rec["status"] = "cancelled"
            rec["error"] = "Cancelled by user"
            break

    return {"cancelled": True, "import_id": import_id}


async def _collect_quality_report(
    section_body: str,
    fm: KnowledgeFrontmatter,
    domain: str,
    embedder,
    qdrant_client,
) -> dict:
    """Прогнать quality gates на секции (advisory, не блокирует импорт).

    Returns:
        {"issues": [...], "warnings": [...], "duplicates": [...]}
    """
    issues: list[dict] = []
    warnings: list[str] = []
    duplicates: list[dict] = []

    # 1. Frontmatter validation (evaluate_frontmatter)
    try:
        from mcp_server.quality.gates import evaluate_frontmatter

        fm_text = f"---\n{fm.model_dump_json(indent=2)}\n---\n{section_body}"
        gate_result = evaluate_frontmatter(fm_text, strict=False)
        for issue in gate_result.issues:
            if issue.severity == "critical":
                issues.append({
                    "field": issue.field,
                    "severity": issue.severity,
                    "message": issue.message,
                })
            else:
                warnings.append(issue.message)
        warnings.extend(gate_result.warnings)
    except Exception as exc:
        warnings.append(f"Frontmatter gate skipped: {exc}")

    # 2. Semantic duplicate check (check_duplicates)
    try:
        if embedder is not None and qdrant_client is not None:
            from mcp_server.quality.dup_gate import check_duplicates

            dup_result = await check_duplicates(
                section_body, domain, fm.knowledge_id,
                embedder=embedder,
                qdrant_client=qdrant_client,
            )
            if dup_result:
                duplicates.extend(dup_result)
                warnings.append(f"Semantic duplicates detected: {len(dup_result)}")
    except Exception as exc:
        warnings.append(f"Dup-gate check skipped: {exc}")

    return {"issues": issues, "warnings": warnings, "duplicates": duplicates}


async def extract_pdf_text(params: dict, app_state) -> dict:
    """MCP Tool: convert PDF → текст (для авто-классификации на клиенте).

    Извлекает полный текст PDF через PDFPreprocessor.extract_text()
    (pdfplumber + OCR fallback, checkpoint-кеш по content_hash).

    Args:
        params: {
            pdf_path (str): путь к PDF на сервере (из POST /upload)
        }
        app_state: Application state.

    Returns:
        {
            text: str,           # извлечённый полный текст
            chars: int,          # число символов
            source_path: str,    # путь к PDF
        }
        Или {"error": ...} при ошибке валидации/извлечения.
    """
    from ..content.pdf_preprocessor import PDFPreprocessor

    pdf_path = params.get("pdf_path", "")
    if not pdf_path:
        return {"error": "Missing required parameter: 'pdf_path'"}

    # P2 (critic): защита от чтения произвольных файлов — только upload-директория.
    if not pdf_path.startswith("/tmp/pdf_uploads"):
        return {"error": f"Invalid pdf_path (must be under /tmp/pdf_uploads): {pdf_path}"}

    if not _os.path.exists(pdf_path):
        return {"error": f"PDF file not found: {pdf_path}"}

    try:
        preprocessor = PDFPreprocessor()
        text = await preprocessor.extract_text(pdf_path)
        return {
            "text": text,
            "chars": len(text),
            "source_path": pdf_path,
        }
    except Exception as exc:  # ruff: noqa: BLE001
        logger.error("[EXTRACT_PDF] failed for %s: %s", pdf_path, exc)
        return {"error": f"PDF text extraction failed: {exc}"}



async def import_content(params: dict, app_state) -> dict:
    """MCP Tool #16: import_content — декомпозиция + batch запись в SSOT.

    Args:
        params: {
            content (str): исходный текст (Markdown/plain)
            content_type (str): тип контента → выбор препроцессора ("book")
            domain (str): первичная классификация
            subject (str): вторичная классификация
            project? (str): опциональный проект
            title? (str): заголовок коллекции (авто если не указан)
            tags? (list[str]): унаследованные теги
            cross_subjects? (list[str]): кросс-теги
            max_chunk_tokens? (int): лимит токенов (default 512)
            wait_for_index? (bool): ждать индексации (default false)
            cleanup_orphans? (bool): удалить orphan-детей при failure (default false)
            quality_checks? (bool): включить quality gates (default true, отключить для массового импорта)
            replace_collection_id? (str): ID коллекции для ЗАМЕНЫ — после успешного импорта
                старая книга удаляется (cascade). Import-first: старая цела до успеха новой.
            replace_on_partial? (bool): удалить старую книгу даже при partial_success (default false)
        }
        app_state: Application state (store, pipeline, embedder, qdrant, ...)

    Returns:
        {
            collection_id: str,
            imported: int,
            failed: int,
            failed_sections: [{sequence_number, title, error}],
            partial_success: bool,
            indexed: bool,
            pending: bool,
            quality_report?: dict,
            replaced: bool,
            replaced_collection_id: str | None,
            cascade_deleted: int,
            replace_skipped_reason: str,
        }
    """
    # ── Параметры ──────────────────────────────────────────
    content = params.get("content", "")
    content_type = params.get("content_type", "book")
    domain = params.get("domain", "")
    subject = params.get("subject", "")
    project = params.get("project")
    title = params.get("title", "")
    tags = params.get("tags", [])
    cross_subjects = params.get("cross_subjects", [])
    
    import_id = params.get("import_id", "")
    wait_for_index = params.get("wait_for_index", False)
    cleanup_orphans = params.get("cleanup_orphans", False)
    quality_checks = params.get("quality_checks", True)  # 6.4: опциональное отключение для mass-import

    # ── Replace params (Фаза 13.x) ──────────────────────────
    replace_collection_id = params.get("replace_collection_id", "")
    replace_on_partial = params.get("replace_on_partial", False)

    # ── Progress tracker (Фаза 13.9) ────────────────────────
    tracker = getattr(app_state, "import_progress", None)

    # ── PDF path (13.21) — source_path для бинарных типов ──
    pdf_path = params.get("pdf_path", "")

    # ── PDF: асинхронная очередь (13.21 Фаза 3) ──────────────
    # submit_import запускает фоновую задачу с heavy_ops_lock (1 импорт за раз),
    # фазами parsing→indexing, cancel_event и checkpoint-resume.
    # Возвращает {"import_id": ..., "status": "started"|"queued"} мгновенно.
    if content_type == "pdf":
        return await submit_import(params, app_state)

    source_path: str | None = None

    if content_type == "pdf" and not content:
        return {"error": "PDF import requires 'content' (base64 encoded PDF) or 'pdf_path' parameter"}
    if content_type == "pdf" and pdf_path and _os.path.exists(pdf_path):
        source_path = pdf_path
    elif content_type == "pdf" and content:
        # Decode base64 → temp file
        try:
            raw = base64.b64decode(content)
        except Exception:
            raw = content.encode("utf-8", errors="replace")
        tmp_dir = "/tmp/pdf_uploads"
        _os.makedirs(tmp_dir, exist_ok=True)
        source_path = _os.path.join(tmp_dir, f"mcp_{_uuid.uuid4().hex[:12]}.pdf")
        with open(source_path, "wb") as f:
            f.write(raw)
        import_id = import_id or _uuid.uuid4().hex[:12]
        logger.info(
            "[IMPORT] base64 decoded for MCP: %s (%.1f KB)",
            source_path, len(raw) / 1024,
        )

    # ── Валидация обязательных параметров ──────────────────
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    if not content:
        return {"error": "Missing required parameter: 'content'"}
    if not domain:
        return {"error": "Missing required parameter: 'domain'"}
    if not subject:
        return {"error": "Missing required parameter: 'subject'"}

    # ── Early validation: replace_collection_id ──────────────
    # Проверяем существование и тип ДО декомпозиции (экономия ресурсов).
    if replace_collection_id:
        store = app_state.store
        try:
            existing = await store.read(replace_collection_id)
        except Exception as e:
            return {"error": f"replace_collection_id read failed: {e}"}
        if existing is None:
            return {"error": f"replace_collection_id not found: {replace_collection_id}"}
        if existing.frontmatter.content_type != "collection":
            return {
                "error": f"replace_collection_id is not a collection: "
                f"{replace_collection_id} (type={existing.frontmatter.content_type})"
            }

    # Registry lookup
    try:
        preprocessor = get_preprocessor(content_type)
    except ValueError as e:
        return {"error": str(e)}

    # ── Валидация контента препроцессором ──────────────────
    metadata = ImportMeta(
        domain=domain,
        subject=subject,
        project=project,
        title=title or content_type.capitalize(),
        tags=tags,
        cross_subjects=cross_subjects,
        source_path=source_path,
    )

    validation = preprocessor.validate(content, metadata)
    if not validation.valid:
        return {
            "error": f"Content validation failed: {validation.error}",
            "content_size": validation.content_size,
        }

    # ── Декомпозиция ────────────────────────────────────────
    try:
        cancel_event = getattr(app_state, "import_cancel_event", None)
        if content_type == "pdf":
            sections = await preprocessor.decompose(content, metadata, cancel_event=cancel_event)
        else:
            sections = await preprocessor.decompose(content, metadata)
    except asyncio.CancelledError:
        return {"error": "Import cancelled", "import_id": import_id}
    except Exception as e:
        logger.exception("Decomposition failed")
        return {"error": f"Decomposition failed: {e}"}

    if not sections:
        return {"error": "Decomposition produced 0 sections"}

    # Фаза 13.21 P1-3: для больших книг (>500 секций) quality-чеки удваивают работу
    # (embedding каждой секции отдельно в dup_gate), авто-отключение экономит ~50% времени.
    if quality_checks and len(sections) > 500:
        quality_checks = False
        logger.info(
            "[IMPORT] auto-disabled quality_checks: %d sections > 500 threshold",
            len(sections),
        )

    # [IMPORT] start — размер и число секций для диагностики тяжёлой операции
    # (инцидент 2026-08-06: крупный импорт без общего timing невидим).
    logger.info(
        "[IMPORT] start type=%s domain=%s subject=%s size=%.1f KB sections=%d",
        content_type, domain, subject, len(content.encode("utf-8")) / 1024, len(sections),
    )

    # ── Progress tracker: start (Фаза 13.9) ─────────────────
    _p(tracker, import_id, "start", len(sections), {"file": params.get("title", ""), "content_type": content_type})
    _p(tracker, import_id, "log", "info",
       f"import_content: decomposition -> {len(sections)} sections (type={content_type})")
    _p(tracker, import_id, "set_phase", "collection_created")

    # ── Создание коллекции (linking) ────────────────────────
    section_titles = [s.title for s in sections]
    section_ids = [s.meta["knowledge_id"] for s in sections]

    if not title:
        title = f"{domain}/{subject} {content_type}"

    collection = build_collection(
        domain=domain,
        subject=subject,
        project=project,
        title=title,
        section_titles=section_titles,
        section_ids=section_ids,
        tags=tags,
        cross_subjects=cross_subjects,
    )

    # ── Self-replace guard ───────────────────────────────────
    # После build_collection: новый collection_id не должен совпадать с заменяемым.
    if replace_collection_id and replace_collection_id == collection.knowledge_id:
        return {
            "error": f"Self-replace detected: new collection_id '{collection.knowledge_id}' "
            f"equals replace_collection_id. Use update_entry or change the title."
        }

    # ── Batch write: по секциям ─────────────────────────────
    store = app_state.store
    pipeline = app_state.pipeline
    knowledge_index = getattr(app_state, "knowledge_index", None)

    imported = 0
    failed = 0
    failed_sections: list[dict] = []
    indexed = True
    pending = not wait_for_index

    # Сначала пишем root-коллекцию
    try:
        root_fm = collection.to_frontmatter()
        root_entry = KnowledgeEntry(
            frontmatter=root_fm,
            content=f"# {title}\n\nКоллекция импортированных секций. Оглавление — в frontmatter.children.",
        )
        await store.write_entry(root_entry)
        await pipeline.enqueue(root_entry, wait_for_index=False)
        logger.info("import_content: root collection %s created", collection.knowledge_id)
        _p(tracker, import_id, "log", "info",
           f"import_content: root collection {collection.knowledge_id} created")
    except Exception as e:
        logger.error("Failed to write collection root: %s", e)
        _p(tracker, import_id, "error", f"Failed to create collection root: {e}")
        return {"error": f"Failed to create collection root: {e}"}

    # Batch write детей
    _p(tracker, import_id, "set_phase", "writing")
    for i, section in enumerate(sections):
        try:
            meta = section.meta
            fm = KnowledgeFrontmatter(
                knowledge_id=meta["knowledge_id"],
                domain=meta["domain"],
                subject=meta["subject"],
                project=meta.get("project"),
                content_type=meta.get("content_type", "book"),
                parent_knowledge_id=collection.knowledge_id,
                sequence_number=section.sequence_number,
                tags=section.tags,
                cross_subjects=meta.get("cross_subjects", []),
            )
            entry = KnowledgeEntry(frontmatter=fm, content=section.body)

            # SSOT запись (без git-коммита — батчим ниже)
            await store.write_entry(entry)

            # Индексация (best-effort)
            try:
                await pipeline.enqueue(entry, wait_for_index=False)
            except Exception as idx_err:
                logger.warning(
                    "Pipeline enqueue failed for %s (non-fatal): %s",
                    fm.knowledge_id, idx_err,
                )

            # Quality gate check (advisory — не блокирует; 6.4: опционально)
            if quality_checks:
                try:
                    qdrant = getattr(app_state, "qdrant", None)
                    embedder = getattr(app_state, "embedder", None)
                    qr = await _collect_quality_report(
                        section_body=section.body,
                        fm=fm,
                        domain=domain,
                        embedder=embedder,
                        qdrant_client=qdrant,
                    )
                    quality_issues.extend(qr["issues"])
                    quality_warnings.extend(qr["warnings"])
                    quality_duplicates.extend(qr["duplicates"])
                except Exception as qe:
                    logger.debug("Quality report collection skipped: %s", qe)

            imported += 1
            _p(tracker, import_id, "section_done", section.sequence_number, section.title)

            # Прогресс-строка каждые IMPORT_BATCH_COMMIT секций (лог + tracker).
            if imported % IMPORT_BATCH_COMMIT == 0:
                logger.info(
                    "import_content: %d/%d sections written",
                    imported, len(sections),
                )
                _p(tracker, import_id, "log", "info",
                   f"import_content: {imported}/{len(sections)} sections written")

            # Фаза 13.21 P1-5: периодический git-коммит каждые IMPORT_PERIODIC_COMMIT секций.
            # При git-ошибке: warning и продолжение без коммита (non-fatal).
            # Финальный flush в конце — всегда (стр. ~343).
            if imported % settings.IMPORT_PERIODIC_COMMIT == 0:
                try:
                    await store.flush(
                        f"import_content: {collection.knowledge_id} "
                        f"periodic commit ({imported}/{len(sections)})"
                    )
                except Exception as e:
                    logger.warning(
                        "[IMPORT] periodic git-commit failed (non-fatal): %s", e
                    )

        except Exception as e:
            failed += 1
            failed_sections.append({
                "sequence_number": section.sequence_number,
                "title": section.title,
                "error": str(e),
            })
            logger.error(
                "import_content: section %d '%s' failed: %s",
                section.sequence_number, section.title, e,
            )
            _p(tracker, import_id, "section_failed", section.sequence_number, section.title, str(e))
            # Продолжаем best-effort

    # Финальный git-коммит — ОДИН на книгу (всегда, независимо от числа секций).
    # Ранее: по flush на каждые 10 секций → ~700 коммитов на большой импорт.
    try:
        await store.flush(
            f"import_content: {collection.knowledge_id} ({imported} sections)"
        )
    except Exception:
        pass  # non-fatal (пустой коммит при imported=0 — GitCommandError, не роняем)
    # Итоговая строка прогресса (для малых импортов, где промежуточные строки не срабатывали)
    _p(tracker, import_id, "log", "info",
       f"import_content: {imported}/{len(sections)} sections written")

    # ── INDEX.gen.yaml update (best-effort) ─────────────────
    if knowledge_index:
        try:
            knowledge_index.update_section(domain)
        except Exception as exc:
            logger.warning("INDEX update failed for domain=%s: %s", domain, exc)

    # ── Ожидание индексации (опционально) ──────────────────
    if wait_for_index:
        try:
            result = await pipeline.wait_for_index(  # N1+N3+N5: публичный метод
                collection.knowledge_id, timeout=30.0
            )
            indexed = result.indexed
            pending = result.pending
        except Exception as e:
            logger.warning(
                "import_content: indexing wait failed for %s: %s — pending=true",
                collection.knowledge_id, e,
            )
            pending = True

    # ── Cleanup orphans при partial_success ─────────────────
    partial_success = failed > 0
    orphan_cleanup_count = 0
    if partial_success and cleanup_orphans:
        logger.info(
            "import_content: cleanup_orphans enabled — %d failed sections, "
            "removing orphaned children via soft-delete",
            failed,
        )
        qdrant = getattr(app_state, "qdrant", None)
        # Map failed sequence_numbers to their knowledge_ids
        failed_seqs = {fs["sequence_number"] for fs in failed_sections}
        for i, sid in enumerate(section_ids):
            seq = i + 1  # sequence_number = index + 1
            if seq in failed_seqs:
                try:
                    # Soft-delete: Markdown → .trash/
                    await store.delete(sid)
                    # Удаление из Qdrant
                    if qdrant:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, qdrant.delete_by_knowledge_id, sid
                        )
                    orphan_cleanup_count += 1
                    logger.info(
                        "import_content: soft-deleted orphan child %s (seq=%d)",
                        sid, seq,
                    )
                except Exception as e:
                    logger.warning(
                        "import_content: failed to cleanup orphan %s: %s", sid, e
                    )
        logger.info("import_content: cleanup_orphans removed %d children", orphan_cleanup_count)

    # ── Сборка результата ───────────────────────────────────
    result = {
        "collection_id": collection.knowledge_id,
        "imported": imported,
        "failed": failed,
        "failed_sections": failed_sections,
        "partial_success": partial_success,
        "indexed": indexed,
        "pending": pending,
        "orphan_cleanup_count": orphan_cleanup_count,
        "quality_checks_applied": quality_checks,
        "quality_report": {
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }

    # ── Replace old collection (Фаза 13.22) ────────────────────
    replaced = False
    replaced_collection_id_val = None
    cascade_deleted = 0
    replace_skipped_reason = ""
    if replace_collection_id:
        should_replace = (failed == 0) or (replace_on_partial and imported > 0)
        if should_replace:
            _p(tracker, import_id, "set_phase", "replacing")
            logger.info("[REPLACE] deleting old collection %s (cascade)", replace_collection_id)
            from .crud import (
                delete_entry as _delete_entry,  # локальный import — без циклов
            )
            del_result = await _delete_entry(
                {"knowledge_id": replace_collection_id, "cascade": True}, app_state,
            )
            if not del_result.get("error"):
                replaced = True
                replaced_collection_id_val = replace_collection_id
                cascade_deleted = del_result.get("cascade_deleted", 0)
                logger.info(
                    "[REPLACE] old collection %s deleted (+%d children)",
                    replace_collection_id, cascade_deleted,
                )
            else:
                replace_skipped_reason = f"delete failed: {del_result.get('error')}"
                logger.warning(
                    "[REPLACE] delete of %s failed: %s",
                    replace_collection_id, del_result.get("error"),
                )
        else:
            replace_skipped_reason = "import_partial" if failed > 0 else "import_failed"
            logger.info(
                "[REPLACE] skipped: %s (imported=%d failed=%d)",
                replace_skipped_reason, imported, failed,
            )
    result["replaced"] = replaced
    result["replaced_collection_id"] = replaced_collection_id_val
    result["cascade_deleted"] = cascade_deleted
    result["replace_skipped_reason"] = replace_skipped_reason

    logger.info(
        "[IMPORT] done collection=%s imported=%d failed=%d partial=%s indexed=%s",
        collection.knowledge_id, imported, failed, partial_success, indexed,
    )
    _p(tracker, import_id, "done", {
        "collection_id": collection.knowledge_id,
        "imported": imported,
        "failed": failed,
    })
    # Task 1: инкремент data_version после мутации данных
    try:
        app_state.data_version += 1
    except Exception:
        pass  # best-effort

    # [P0-3] Cleanup mcp_ temp file (base64 decoded)
    if source_path and source_path.startswith("/tmp/pdf_uploads/mcp_"):
        try:
            _os.unlink(source_path)
        except OSError:
            pass

    return result
