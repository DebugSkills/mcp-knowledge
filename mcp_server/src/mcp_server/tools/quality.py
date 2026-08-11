# ruff: noqa: BLE001
"""Quality MCP Tools — review_queue, review_queue_books, list_quality_issues, resolve_quality_issue, run_quality_scan (4.6+4.8+13.14+13.15).

Thin wrappers: делегируют доменную логику в quality/ пакет.
Регистрируются в tools/__init__.py → TOOLS + TOOL_HANDLERS.

Фаза 13.14: +review_queue_books (агрегация по книгам), resolve_quality_issue
расширен параметрами knowledge_id + cascade.

Фаза 13.15: run_quality_scan → фоновая задача (asyncio.create_task) + lock
(root-фикс зависания event loop); run_in_executor для sync Qdrant-вызовов.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from mcp_server.quality import REVIEW_THRESHOLD
from mcp_server.quality.issues import list_issues, update_issue_status

logger = logging.getLogger("mcp_knowledge.tools.quality")

# ── Допустимые action для resolve_quality_issue ──────────────────────────────
VALID_ACTIONS: frozenset[str] = frozenset({"merge", "deprecate", "restore", "resolve", "ignore"})

# ── Константы пагинации ──────────────────────────────────────────────────────
_SCROLL_BATCH = 1000  # точек за один scroll
_MAX_BOOKS = 100  # макс. книг в ответе
_PARENT_TITLE_BATCH = 50  # макс. parent_id в одном запросе для резолва title


async def review_queue(params: dict, app_state) -> dict:
    """Получить топ устаревших записей (по staleness_score DESC).

    Читает staleness_score из Qdrant payload, сортирует DESC,
    возвращает top-N для review.

    Args:
        params:
            domain (optional): фильтр по домену
            subject (optional): фильтр по subject
            limit (default 20): макс. число записей
    """
    domain = params.get("domain")
    subject = params.get("subject")
    limit = min(params.get("limit", 20), 100)

    try:
        client = app_state.qdrant
        loop = asyncio.get_running_loop()
        # Qdrant scroll с фильтром и сортировкой по payload.staleness_score DESC
        from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

        must_conditions: list[FieldCondition] = []
        if domain:
            must_conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domain))
            )
        if subject:
            must_conditions.append(
                FieldCondition(key="subject", match=MatchValue(value=subject))
            )

        # Добавляем условие: staleness_score существует (не null)
        must_conditions.append(
            FieldCondition(
                key="staleness_score",
                range=Range(gte=0.0),
            )
        )

        scroll_filter = Filter(must=must_conditions) if must_conditions else None

        # Scroll все точки с staleness_score, затем сортируем в Python
        # 13.15: run_in_executor — не блокируем event loop
        points, _ = await loop.run_in_executor(
            None,
            lambda: client.scroll(
                scroll_filter=scroll_filter,
                limit=limit * 3,  # берём с запасом для сортировки
                with_payload=True,
                with_vectors=False,
            ),
        )

        # Сортируем DESC по staleness_score
        scored = []
        for point in points:
            payload = point.payload or {}
            score = payload.get("staleness_score", 0.0)
            if score >= REVIEW_THRESHOLD:
                scored.append({
                    "knowledge_id": payload.get("knowledge_id", str(point.id)),
                    "title": payload.get("subject", "") + "/" + payload.get("knowledge_id", ""),
                    "staleness_score": score,
                    "reasons": payload.get("quality_flags", []),
                    "updated_at": payload.get("updated_at", ""),
                })

        scored.sort(key=lambda x: x["staleness_score"], reverse=True)
        result = scored[:limit]

        logger.info("review_queue: %d records returned (domain=%s)", len(result), domain)
        return {"queue": result, "total_in_queue": len(scored)}

    except Exception as exc:
        logger.error("review_queue failed: %s", exc)
        return {"queue": [], "error": str(exc)}


async def review_queue_books(params: dict, app_state) -> dict:
    """Топ устаревших КНИГ (агрегат по parent_knowledge_id).

    Фаза 13.14: paginated scroll всех точек с staleness_score,
    группировка по parent_knowledge_id → per-book агрегат:
    title, domain/subject, stale_fraction, max_score, total_sections, status.

    В отличие от review_queue (индивидуальные секции), этот tool
    возвращает КНИГИ — коллекции семантически связанных записей.

    Args:
        params:
            domain (optional): фильтр по домену
            subject (optional): фильтр по subject
            limit (default 20): макс. число книг
    """
    domain = params.get("domain")
    subject = params.get("subject")
    limit = min(params.get("limit", 20), _MAX_BOOKS)

    try:
        client = app_state.qdrant
        loop = asyncio.get_running_loop()
        from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

        must_conditions: list[FieldCondition] = []
        if domain:
            must_conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domain))
            )
        if subject:
            must_conditions.append(
                FieldCondition(key="subject", match=MatchValue(value=subject))
            )

        # Только точки с staleness_score
        must_conditions.append(
            FieldCondition(
                key="staleness_score",
                range=Range(gte=0.0),
            )
        )

        scroll_filter = Filter(must=must_conditions) if must_conditions else None

        # Paginated scroll — все точки с staleness_score (не limit*3!)
        # 13.15: run_in_executor — не блокируем event loop
        all_points: list = []
        offset = None
        while True:
            _filt = scroll_filter
            _off = offset
            points, next_offset = await loop.run_in_executor(
                None,
                lambda f=_filt, o=_off: client.scroll(
                    scroll_filter=f,
                    limit=_SCROLL_BATCH,
                    offset=o,
                    with_payload=True,
                    with_vectors=False,
                ),
            )
            all_points.extend(points)
            if next_offset is None or len(points) == 0:
                break
            offset = next_offset

        # Группировка по parent_knowledge_id
        from collections import defaultdict

        books: dict[str, dict] = defaultdict(lambda: {
            "book_id": "",
            "title": "",
            "domain": "",
            "subject": "",
            "status": "published",
            "total_sections": 0,
            "stale_sections": 0,
            "max_score": 0.0,
            "top_sections": [],
        })

        for point in all_points:
            payload = point.payload or {}
            parent_id = payload.get("parent_knowledge_id")
            if not parent_id:
                # Точки без parent — самостоятельные записи (не секции книг)
                continue

            score = payload.get("staleness_score", 0.0)
            kid = payload.get("knowledge_id", str(point.id))
            section_title = (
                payload.get("section_header")
                or payload.get("title", "")
                or kid
            )

            book = books[parent_id]
            if not book["book_id"]:
                book["book_id"] = parent_id
                book["domain"] = payload.get("domain", "")
                book["subject"] = payload.get("subject", "")
                book["status"] = payload.get("status", "published")
                # Title книги — из section_header первой секции как fallback
                # (будет заменён при batch_resolve_book_titles)
                book["title"] = (
                    payload.get("section_header")
                    or payload.get("subject", "")
                    or parent_id
                )

            book["total_sections"] += 1
            if score >= REVIEW_THRESHOLD:
                book["stale_sections"] += 1
                book["max_score"] = max(book["max_score"], score)
                # Top-5 наиболее устаревших секций
                book["top_sections"].append({
                    "knowledge_id": kid,
                    "title": section_title,
                    "staleness_score": score,
                    "reasons": payload.get("quality_flags", []),
                    "updated_at": payload.get("updated_at", ""),
                })

        # ── R1: резолв title книг из родительских записей ──────
        parent_ids = list(books.keys())
        if parent_ids:
            parent_titles = await _batch_resolve_book_titles(client, parent_ids, loop)
            for parent_id, title in parent_titles.items():
                if parent_id in books and title:
                    books[parent_id]["title"] = title

        # Сортируем top_sections по score DESC и обрезаем до 5
        for book in books.values():
            book["top_sections"].sort(key=lambda s: s["staleness_score"], reverse=True)
            book["top_sections"] = book["top_sections"][:5]
            # Вычисляем долю
            total = book["total_sections"]
            book["stale_fraction"] = round(book["stale_sections"] / total, 3) if total > 0 else 0.0

        # Сортируем книги: сначала по stale_fraction DESC, затем по max_score DESC
        sorted_books = sorted(
            books.values(),
            key=lambda b: (b["stale_fraction"], b["max_score"]),
            reverse=True,
        )

        # R3: фильтруем книги с stale_sections == 0 (нет устаревших секций)
        stale_books = [b for b in sorted_books if b["stale_sections"] > 0]

        # Обрезаем до limit
        result_books = stale_books[:limit]

        # total_stale_sections — по ВСЕМ книгам со stale секциями
        total_stale = sum(b["stale_sections"] for b in stale_books)

        logger.info(
            "[REVIEW] review_queue_books: %d books found (%d with stale), %d returned, %d stale sections total",
            len(books), len(stale_books), len(result_books), total_stale,
        )
        return {
            "books": result_books,
            "total_books": len(stale_books),  # R3: только книги со stale секциями
            "total_stale_sections": total_stale,
        }

    except Exception as exc:
        logger.error("[REVIEW] review_queue_books failed: %s", exc)
        return {"books": [], "error": str(exc)}


async def list_quality_issues(params: dict, app_state) -> dict:
    """Получить список quality issues с фильтрацией.

    Args:
        params:
            types (optional): список типов (duplicate, missing_field, edit_war, broken_link, conflicting)
            status (default "open"): open | resolved | ignored
            limit (default 50): макс. число
    """
    types = params.get("types")
    status = params.get("status", "open")
    limit = min(params.get("limit", 50), 200)

    try:
        issues = list_issues(types=types, status=status, limit=limit)
        result = [
            {
                "issue_id": i.issue_id,
                "type": i.type,
                "knowledge_id": i.knowledge_id,
                "severity": i.severity,
                "detail": i.detail,
                "detected_at": i.detected_at.isoformat() if i.detected_at else None,
                "status": i.status,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                "resolution": i.resolution,
            }
            for i in issues
        ]
        logger.info("list_quality_issues: %d issues (status=%s)", len(result), status)
        return {"issues": result, "total": len(result)}

    except Exception as exc:
        logger.error("list_quality_issues failed: %s", exc)
        return {"issues": [], "error": str(exc)}


async def resolve_quality_issue(params: dict, app_state) -> dict:
    """Разрешить quality issue: resolve/ignore/merge/deprecate/restore.

    Фаза 13.14: +knowledge_id (прямая операция без issue-lookup) + cascade
    (применить ко всем секциям книги по parent_knowledge_id).

    Args:
        params:
            issue_id (str): ID issue для разрешения (или knowledge_id для прямой операции)
            action (str): merge | deprecate | restore | resolve | ignore
            knowledge_id (optional str): прямая операция на запись (вместо issue_id)
            target_id (optional str): target knowledge_id для merge
            reason (optional str): причина решения
            cascade (optional bool): применить к дочерним секциям (deprecate/restore)
    """
    issue_id = params.get("issue_id", "")
    action = params.get("action", "")
    target_id = params.get("target_id")
    reason = params.get("reason", "")
    knowledge_id = params.get("knowledge_id")  # Фаза 13.14: прямая операция
    cascade = params.get("cascade", False)      # Фаза 13.14: каскад на секции

    # Валидация
    if not issue_id and not knowledge_id:
        return {"resolved": False, "error": "Either issue_id or knowledge_id is required"}

    if action not in VALID_ACTIONS:
        return {
            "resolved": False,
            "error": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(VALID_ACTIONS))}",
        }

    side_effects: list[str] = []
    cascade_affected = 0

    try:
        # Определяем knowledge_id: прямая передача ИЛИ lookup через issue
        if knowledge_id:
            # Прямая операция (Фаза 13.14) — без issue lookup
            pass
        elif action in ("deprecate", "restore", "merge"):
            all_issues = list_issues(limit=None)
            issue_entry = next((i for i in all_issues if i.issue_id == issue_id), None)
            if not issue_entry:
                return {"resolved": False, "error": f"Issue {issue_id} not found"}
            knowledge_id = issue_entry.knowledge_id

        if action == "resolve":
            if not issue_id:
                return {"resolved": False, "error": "issue_id is required for resolve action"}
            update_issue_status(issue_id, "resolved", reason)
            return {"resolved": True, "issue_id": issue_id, "status": "resolved", "side_effects": side_effects}

        elif action == "ignore":
            if not issue_id:
                return {"resolved": False, "error": "issue_id is required for ignore action"}
            update_issue_status(issue_id, "ignored", reason)
            return {"resolved": True, "issue_id": issue_id, "status": "ignored", "side_effects": side_effects}

        elif action == "deprecate":
            # Lifecycle: установить status=deprecated в Qdrant payload
            try:
                qdrant = getattr(app_state, 'qdrant', None)
                if qdrant:
                    loop = asyncio.get_running_loop()
                    from qdrant_client.models import FieldCondition, Filter, MatchValue

                    from mcp_server.quality.lifecycle import (
                        make_deprecation_payload_update,
                    )
                    payload = make_deprecation_payload_update()
                    await loop.run_in_executor(
                        None,
                        lambda: qdrant.set_payload(
                            payload=payload,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=knowledge_id))]
                            ),
                        ),
                    )
                    side_effects.append(f"Qdrant payload status set to 'deprecated' for knowledge_id={knowledge_id}")

                    # Cascade: deprecate все дочерние секции (Фаза 13.14)
                    if cascade:
                        cascade_affected = await _cascade_set_payload(
                            qdrant, knowledge_id, payload, "deprecated"
                        )
                        side_effects.append(
                            f"LIFECYCLE cascade: {cascade_affected} child sections set to 'deprecated' for book {knowledge_id}"
                        )
                        logger.info(
                            "[LIFECYCLE] cascade deprecate: %d sections for book %s",
                            cascade_affected, knowledge_id,
                        )

                if issue_id:
                    update_issue_status(issue_id, "resolved", reason or "deprecated")
                # Task 1: инкремент data_version после мутации
                try:
                    app_state.data_version += 1
                except Exception:  # noqa: S110
                    pass  # best-effort
                return {
                    "resolved": True,
                    "issue_id": issue_id,
                    "knowledge_id": knowledge_id,
                    "status": "resolved",
                    "cascade_affected": cascade_affected,
                    "side_effects": side_effects,
                }
            except Exception as exc:
                logger.error("deprecate lifecycle failed for %s: %s", knowledge_id, exc)
                return {"resolved": False, "error": f"Deprecate failed: {exc}"}

        elif action == "restore":
            # Lifecycle: установить status=published в Qdrant payload (reversibility)
            try:
                qdrant = getattr(app_state, 'qdrant', None)
                if qdrant:
                    loop = asyncio.get_running_loop()
                    from qdrant_client.models import FieldCondition, Filter, MatchValue

                    from mcp_server.quality.lifecycle import make_restore_payload_update
                    payload = make_restore_payload_update()
                    await loop.run_in_executor(
                        None,
                        lambda: qdrant.set_payload(
                            payload=payload,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=knowledge_id))]
                            ),
                        ),
                    )
                    side_effects.append(f"Qdrant payload status set to 'published' for knowledge_id={knowledge_id}")

                    # Cascade: restore все дочерние секции (Фаза 13.14)
                    if cascade:
                        cascade_affected = await _cascade_set_payload(
                            qdrant, knowledge_id, payload, "published"
                        )
                        side_effects.append(
                            f"LIFECYCLE cascade: {cascade_affected} child sections set to 'published' for book {knowledge_id}"
                        )
                        logger.info(
                            "[LIFECYCLE] cascade restore: %d sections for book %s",
                            cascade_affected, knowledge_id,
                        )

                if issue_id:
                    update_issue_status(issue_id, "resolved", reason or "restored")
                # Task 1: инкремент data_version после мутации
                try:
                    app_state.data_version += 1
                except Exception:  # noqa: S110
                    pass  # best-effort
                return {
                    "resolved": True,
                    "issue_id": issue_id,
                    "knowledge_id": knowledge_id,
                    "status": "resolved",
                    "cascade_affected": cascade_affected,
                    "side_effects": side_effects,
                }
            except Exception as exc:
                logger.error("restore lifecycle failed for %s: %s", knowledge_id, exc)
                return {"resolved": False, "error": f"Restore failed: {exc}"}

        elif action == "merge":
            if not target_id:
                return {"resolved": False, "error": "target_id is required for merge action"}
            # Merge: deprecate source + update issue (content merge — future)
            try:
                qdrant = getattr(app_state, 'qdrant', None)
                if qdrant:
                    loop = asyncio.get_running_loop()
                    from qdrant_client.models import FieldCondition, Filter, MatchValue

                    from mcp_server.quality.lifecycle import (
                        make_deprecation_payload_update,
                    )
                    payload = make_deprecation_payload_update()
                    await loop.run_in_executor(
                        None,
                        lambda: qdrant.set_payload(
                            payload=payload,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=knowledge_id))]
                            ),
                        ),
                    )
                    side_effects.append(f"Source '{knowledge_id}' deprecated via Qdrant payload")
            except Exception as exc:
                logger.error("merge lifecycle failed for %s: %s", knowledge_id, exc)
                return {"resolved": False, "error": f"Merge failed: {exc}"}
            update_issue_status(issue_id, "resolved", f"merged into {target_id}. {reason}")
            side_effects.append(f"Content merge into '{target_id}' pending — markdown merge not yet implemented")
            # Task 1: инкремент data_version после мутации
            try:
                app_state.data_version += 1
            except Exception:  # noqa: S110
                pass  # best-effort
            return {
                "resolved": True,
                "issue_id": issue_id,
                "status": "resolved",
                "side_effects": side_effects,
            }

    except Exception as exc:
        logger.error("resolve_quality_issue(%s, %s) failed: %s", issue_id, action, exc)
        return {"resolved": False, "error": str(exc)}

    return {"resolved": False, "error": f"Unknown action: {action}"}


async def _cascade_set_payload(
    qdrant,
    parent_knowledge_id: str,
    payload: dict,
    new_status: str,
) -> int:
    """Фаза 13.14+13.15: применить set_payload ко всем дочерним секциям книги.

    Scroll по parent_knowledge_id → set_payload на каждую секцию.
    Все Qdrant-вызовы — через run_in_executor (не блокируют event loop).
    Возвращает число затронутых секций.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    loop = asyncio.get_running_loop()
    affected = 0
    offset = None
    while True:
        _off = offset
        points, next_offset = await loop.run_in_executor(
            None,
            lambda o=_off: qdrant.scroll(
                scroll_filter=Filter(
                    must=[FieldCondition(key="parent_knowledge_id", match=MatchValue(value=parent_knowledge_id))]
                ),
                limit=1000,
                offset=o,
                with_payload=["knowledge_id"],
                with_vectors=False,
            ),
        )
        for point in points:
            kid = point.payload.get("knowledge_id") if point.payload else None
            if kid:
                try:
                    await loop.run_in_executor(
                        None,
                        lambda k=kid, p=payload: qdrant.set_payload(
                            payload=p,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=k))]
                            ),
                        ),
                    )
                    affected += 1
                except Exception as exc:
                    logger.warning(
                        "[LIFECYCLE] cascade set_payload failed for child %s (parent=%s): %s",
                        kid, parent_knowledge_id, exc,
                    )
        if next_offset is None or len(points) == 0:
            break
        offset = next_offset

    return affected


async def _batch_resolve_book_titles(
    qdrant,
    parent_ids: list[str],
    loop,
) -> dict[str, str]:
    """R1: резолв title книг из payload родительских записей в Qdrant.

    Для каждого parent_knowledge_id делает scroll с фильтром по knowledge_id,
    извлекает поле title из payload. Пагинированный: батчи по _PARENT_TITLE_BATCH,
    все Qdrant-вызовы через run_in_executor.

    Args:
        qdrant: QdrantClient wrapper (app_state.qdrant).
        parent_ids: список parent_knowledge_id для резолва.
        loop: asyncio event loop.

    Returns:
        dict parent_id → title (str).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    titles: dict[str, str] = {}

    # Батчинг: до _PARENT_TITLE_BATCH parent_id в одном scroll-запросе
    for i in range(0, len(parent_ids), _PARENT_TITLE_BATCH):
        batch = parent_ids[i:i + _PARENT_TITLE_BATCH]

        # Строим should-условия: knowledge_id IN batch
        should_conditions = [
            FieldCondition(key="knowledge_id", match=MatchValue(value=pid))
            for pid in batch
        ]

        try:
            _batch_len = len(batch)
            points, _ = await loop.run_in_executor(
                None,
                lambda sc=should_conditions, bl=_batch_len: qdrant.scroll(
                    scroll_filter=Filter(should=sc) if len(sc) > 0 else None,
                    limit=bl,
                    with_payload=["title"],
                    with_vectors=False,
                ),
            )
        except Exception as exc:
            logger.warning(
                "[REVIEW] _batch_resolve_book_titles: scroll failed for batch %d..%d: %s",
                i, i + len(batch), exc,
            )
            continue

        for point in points:
            payload = point.payload or {}
            kid = payload.get("knowledge_id", str(point.id))
            title = payload.get("title", "")
            if kid in titles:
                continue  # уже есть (первый приоритет)
            if title:
                titles[kid] = str(title)

    if titles:
        logger.info(
            "[REVIEW] _batch_resolve_book_titles: resolved %d/%d book titles",
            len(titles), len(parent_ids),
        )
    return titles


# ── 4.8/13.15: Background quality scan ────────────────────────


async def _bg_scan(
    scan_id: str,
    knowledge_dir,
    qdrant_client,
    scan_progress,
    scan_state: dict,
    cancel_event: asyncio.Event | None = None,  # 13.18: отмена скана
) -> None:
    """Фоновая задача quality scan (13.15 + 13.18).

    Выполняет run_scan под scan_lock, обновляет прогресс через
    scan_progress. При ошибке — scan_progress.error().
    scan_task очищается в finally.

    13.18: принимает cancel_event и пробрасывает в run_scan.
    """
    from mcp_server.quality.scanner import run_scan

    try:
        async with scan_state["lock"]:
            scan_progress.set_phase(scan_id, "scanning_fs", "Обход файлов...")
            metrics = await run_scan(
                knowledge_dir=knowledge_dir,
                qdrant_client=qdrant_client,
                progress=scan_progress,
                progress_id=scan_id,
                cancel_event=cancel_event,
            )
            # run_scan сам вызывает progress.done() в нормальном потоке (13.18)
            # но если он НЕ вызвал (cancel без progress), делаем здесь
            scan_progress.done(scan_id, {"metrics": metrics})
            logger.info(
                "Background scan %s complete: %d files, %d in review queue, %d dups, %d issues",
                scan_id,
                metrics["files_scanned"],
                metrics["review_queue_size"],
                metrics["duplicates_detected"],
                metrics["issues_created"],
            )
    except asyncio.CancelledError:
        logger.info("Background scan %s cancelled (shutdown)", scan_id)
        scan_progress.error(scan_id, "scan cancelled (server shutdown)")
        raise
    except Exception as exc:
        logger.exception("Background scan %s failed", scan_id)
        scan_progress.error(scan_id, str(exc))
    finally:
        scan_state["task_ref"][0] = None


async def run_quality_scan(params: dict, app_state) -> dict:
    """Запускает quality scan как фоновую задачу (13.15 + 13.18).

    Возвращает мгновенный ответ — скан выполняется асинхронно.
    Прогресс: GET /quality/scan/progress.

    13.18: при старте — создаёт новый asyncio.Event() для отмены (сброс),
    прокидывает в _bg_scan. Параметр domain пока не используется
    (run_scan не фильтрует по домену).

    Новый контракт (13.15):
        {"scanned": true, "status": "started", "scan_id": "..."}  — скан запущен
        {"scanned": false, "status": "already_running", "scan_id": "..."}  — уже идёт
        {"scanned": false, "status": "error", "error": "..."}  — сбой старта

    Args:
        params:
            domain (optional): скан только одного домена
    """
    try:
        # Проверяем lock — если уже залочен, скан активен
        scan_lock = app_state.scan_lock
        if scan_lock.locked():
            return {
                "scanned": False,
                "status": "already_running",
                "scan_id": getattr(app_state, "scan_id", None),
            }

        knowledge_dir = (
            app_state.settings.KNOWLEDGE_DIR
            if hasattr(app_state, "settings") and app_state.settings
            else None
        )
        qdrant_client = getattr(app_state, "qdrant", None)
        scan_progress = getattr(app_state, "scan_progress", None)

        if scan_progress is None:
            return {"scanned": False, "status": "error", "error": "scan_progress not initialized"}

        # 13.19: удаляем завершённые записи прошлых сканов — оставляем только текущий
        try:
            removed = scan_progress.prune_finished()
            if removed:
                logger.info("Pruned %d finished scan progress record(s)", removed)
        except Exception as exc:
            logger.warning("prune_finished failed (non-fatal): %s", exc)

        scan_id = uuid.uuid4().hex[:16]
        app_state.scan_id = scan_id

        # 13.18: Новый cancel_event на каждый скан (сброс от предыдущего)
        app_state.scan_cancel_event = asyncio.Event()

        # Инициализируем прогресс (total — неизвестен до обхода, ставим 0)
        scan_progress.start(scan_id, total=0)

        # task_ref: list чтобы _bg_scan мог мутировать app_state.scan_task
        task_ref: list = [None]

        task = asyncio.create_task(
            _bg_scan(
                scan_id=scan_id,
                knowledge_dir=knowledge_dir,
                qdrant_client=qdrant_client,
                scan_progress=scan_progress,
                scan_state={"lock": scan_lock, "task_ref": task_ref},
                cancel_event=app_state.scan_cancel_event,
            )
        )
        task_ref[0] = task
        app_state.scan_task = task

        logger.info("Quality scan %s started (background task)", scan_id)
        return {"scanned": True, "status": "started", "scan_id": scan_id}

    except Exception as exc:
        logger.exception("run_quality_scan failed to start")
        return {"scanned": False, "status": "error", "error": str(exc)}


async def cancel_quality_scan(params: dict, app_state) -> dict:
    """Отменить активный quality scan (13.18).

    Устанавливает scan_cancel_event → run_scan проверяет между фазами
    и возвращает частичные метрики. Lock освобождается автоматически
    при выходе из async with scan_state["lock"] в _bg_scan.

    Returns:
        {"cancelled": True, "scan_id": "..."}  — отмена отправлена
        {"cancelled": False, "reason": "no active scan"}  — нечего отменять
    """
    scan_lock = getattr(app_state, "scan_lock", None)
    if scan_lock is None or not scan_lock.locked():
        return {"cancelled": False, "reason": "no active scan"}

    cancel_event = getattr(app_state, "scan_cancel_event", None)
    if cancel_event is None:
        # Активный скан, но cancel_event не создан (edge case) — создаём и ставим
        app_state.scan_cancel_event = asyncio.Event()
        cancel_event = app_state.scan_cancel_event

    cancel_event.set()
    logger.info("Cancel signal sent for scan %s", getattr(app_state, "scan_id", "?"))
    return {
        "cancelled": True,
        "scan_id": getattr(app_state, "scan_id", None),
    }
