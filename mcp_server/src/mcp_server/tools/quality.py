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
import time
import uuid

from mcp_server.quality import REVIEW_THRESHOLD
from mcp_server.quality.audit import get_fp_rate, list_audit, write_audit
from mcp_server.quality.issues import (
    bulk_update_status,
    close_all_dup_issues,
    count_issues,
    list_issue_ids,
    list_issues,
    update_issue_status,
)
from mcp_server.storage.schema import ZONE_PRIVATE, collection_for_zone

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
        # TODO: зона из контекста — W3
        points, _ = await loop.run_in_executor(
            None,
            lambda: client.scroll(
                scroll_filter=scroll_filter,
                limit=limit * 3,  # берём с запасом для сортировки
                with_payload=True,
                with_vectors=False,
                collection_name=collection_for_zone(ZONE_PRIVATE),
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
            # TODO: зона из контекста — W3
            points, next_offset = await loop.run_in_executor(
                None,
                lambda f=_filt, o=_off: client.scroll(
                    scroll_filter=f,
                    limit=_SCROLL_BATCH,
                    offset=o,
                    with_payload=True,
                    with_vectors=False,
                    collection_name=collection_for_zone(ZONE_PRIVATE),
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
            # W2.13: зоны книг из scroll-точек → per-zone резолв title.
            zones_by_book: dict[str, str] = {}
            for point in all_points:
                pl = point.payload or {}
                pid = pl.get("parent_knowledge_id")
                if pid and pl.get("zone"):
                    zones_by_book.setdefault(pid, pl["zone"])
            if zones_by_book:
                ids_by_zone: dict[str, list[str]] = {}
                for pid in parent_ids:
                    ids_by_zone.setdefault(zones_by_book.get(pid, ZONE_PRIVATE), []).append(pid)
                parent_titles: dict[str, str] = {}
                for zone, ids in ids_by_zone.items():
                    parent_titles.update(
                        await _batch_resolve_book_titles(client, ids, loop, zone=zone)
                    )
            else:
                # Зоны в payload отсутствуют — единый вызов (default ZONE_PRIVATE).
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
        # Реальный total БЕЗ лимита (13.26): иначе при >limit open issues
        # total == limit и счётчик в UI «застревает», хотя issues закрываются.
        total = count_issues(types=types, status=status)
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
                "metadata": i.metadata,  # Фаза 1 dedup: сигналы для UI (cosine, hash, standalone)
            }
            for i in issues
        ]
        logger.info("list_quality_issues: %d/%d issues (status=%s)", len(result), total, status)
        return {"issues": result, "total": total}

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
            marks_fp (optional bool): явный FP-сигнал «не дубль» (Фаза 3).
                None → автодетект по reason ("not a duplicate" in reason.lower()).
    """
    issue_id = params.get("issue_id", "")
    action = params.get("action", "")
    target_id = params.get("target_id")
    reason = params.get("reason", "")
    knowledge_id = params.get("knowledge_id")  # Фаза 13.14: прямая операция
    cascade = params.get("cascade", False)      # Фаза 13.14: каскад на секции
    marks_fp = params.get("marks_fp")           # Фаза 3 (1c): явный FP-сигнал

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
            # P2-NEW-1: kid-lookup ДО смены статуса — после update_issue_status
            # issue уже не open, list_issues(status="open") его не найдёт → kid="-".
            iss_before = next(
                (i for i in list_issues(limit=None) if i.issue_id == issue_id), None
            )
            kid_from_issue = iss_before.knowledge_id if iss_before else None
            update_issue_status(issue_id, "resolved", reason)
            # Фаза 3 (1c): FP-детект — оператор пометил «не дубль».
            # marks_fp=True ИЛИ reason содержит "not a duplicate" (backward-compat:
            # UI уже шлёт такой reason) → пишем fp_rejection в audit (закрывает гейт).
            is_fp = marks_fp if marks_fp is not None else (
                "not a duplicate" in (reason or "").lower()
            )
            if is_fp:
                kid = kid_from_issue or knowledge_id or "-"
                write_audit(
                    action="fp_rejection",
                    knowledge_id=kid,
                    actor="operator",
                    reason=reason or "not a duplicate",
                    metadata={"issue_id": issue_id},
                )
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
                    # Зона записи из SSOT frontmatter (_entry_zone) — иначе deprecate
                    # public-записи был silent no-op в private-коллекции (P1-1 W2).
                    zone = await _entry_zone(app_state, knowledge_id)
                    await loop.run_in_executor(
                        None,
                        lambda k=knowledge_id, p=payload, z=zone: qdrant.set_payload(
                            payload=p,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=k))]
                            ),
                            collection_name=collection_for_zone(z),
                        ),
                    )
                    side_effects.append(f"Qdrant payload status set to 'deprecated' for knowledge_id={knowledge_id}")

                    # Cascade: deprecate все дочерние секции (Фаза 13.14)
                    if cascade:
                        cascade_affected = await _cascade_set_payload(
                            qdrant,
                            knowledge_id,
                            payload,
                            "deprecated",
                            zone=zone,
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
                # Фаза 1 (1d): закрыть ВСЕ open dup-issues записи (не одну) —
                # иначе «висящие» проблемы на уже скрытой записи.
                closed = close_all_dup_issues(
                    knowledge_id, reason or "record deprecated"
                )
                if closed:
                    side_effects.append(
                        f"Closed {closed} open duplicate-issue(s) for {knowledge_id}"
                    )
                # Фаза 1 (1e): аудит действия
                write_audit(
                    action="deprecate",
                    knowledge_id=knowledge_id,
                    actor="operator",
                    reason=reason or "deprecated by operator",
                    metadata={"cascade_affected": cascade_affected, "issues_closed": closed},
                )
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
                    "issues_closed": closed,
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
                    # Зона записи из SSOT frontmatter (_entry_zone) — P1-1 W2.
                    zone = await _entry_zone(app_state, knowledge_id)
                    await loop.run_in_executor(
                        None,
                        lambda k=knowledge_id, p=payload, z=zone: qdrant.set_payload(
                            payload=p,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=k))]
                            ),
                            collection_name=collection_for_zone(z),
                        ),
                    )
                    side_effects.append(f"Qdrant payload status set to 'published' for knowledge_id={knowledge_id}")

                    # Cascade: restore все дочерние секции (Фаза 13.14)
                    if cascade:
                        cascade_affected = await _cascade_set_payload(
                            qdrant,
                            knowledge_id,
                            payload,
                            "published",
                            zone=zone,
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
                # Фаза 3 (1e, P1-NEW-1): restore-audit — СРАЗУ после update_issue_status,
                # ПЕРЕД data_version++. Без этого cooldown-щит (2d) не находит
                # «ts последнего restore» → восстановленная запись re-auto-deprecate.
                write_audit(
                    action="restore",
                    knowledge_id=knowledge_id,
                    actor="operator",
                    reason=reason or "restored by operator",
                    metadata={
                        "restored_by_operator": True,  # явный сигнал для cooldown-щита
                        "cascade_affected": cascade_affected,
                        "issue_id": issue_id or None,
                    },
                )
                # Task 1: инкремент data_version после мутации
                try:
                    app_state.data_version += 1
                except Exception:  # noqa: S110
                    pass  # best-effort
                # Фаза 3: restore переоткрывает dup-issues записи — пара снова
                # видна в Review Queue, cooldown-щит защищает от авто-re-deprecate.
                # (иначе идемпотентный create_issue возвращает закрытый issue → дубль невидим)
                try:
                    from mcp_server.quality.issues import reopen_dup_issues

                    reopened = reopen_dup_issues(knowledge_id)
                    if reopened:
                        side_effects.append(f"Reopened {reopened} dup-issue(s) for {knowledge_id}")
                except Exception as exc:
                    logger.warning("reopen_dup_issues failed (non-fatal): %s", exc)
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
                    # Зона записи из SSOT frontmatter (_entry_zone) — P1-1 W2.
                    zone = await _entry_zone(app_state, knowledge_id)
                    await loop.run_in_executor(
                        None,
                        lambda k=knowledge_id, p=payload, z=zone: qdrant.set_payload(
                            payload=p,
                            points_filter=Filter(
                                must=[FieldCondition(key="knowledge_id", match=MatchValue(value=k))]
                            ),
                            collection_name=collection_for_zone(z),
                        ),
                    )
                    side_effects.append(f"Source '{knowledge_id}' deprecated via Qdrant payload")
            except Exception as exc:
                logger.error("merge lifecycle failed for %s: %s", knowledge_id, exc)
                return {"resolved": False, "error": f"Merge failed: {exc}"}
            update_issue_status(issue_id, "resolved", f"merged into {target_id}. {reason}")
            # Фаза 1 (1g): честная семантика — merge = deprecate источника
            # (обратимо через restore), контент НЕ консолидируется.
            side_effects.append(
                f"Source '{knowledge_id}' deprecated (hidden from search); "
                f"target '{target_id}' remains canonical. Reversible via restore."
            )
            # Фаза 1 (1d): закрыть ВСЕ open dup-issues источника
            closed = close_all_dup_issues(knowledge_id, f"merged into {target_id}")
            if closed:
                side_effects.append(f"Closed {closed} open duplicate-issue(s) for {knowledge_id}")
            # Фаза 1 (1e): аудит
            write_audit(
                action="merge",
                knowledge_id=knowledge_id,
                actor="operator",
                reason=f"merged into {target_id}. {reason}",
                metadata={"target_id": target_id, "issues_closed": closed},
            )
            # Task 1: инкремент data_version после мутации
            try:
                app_state.data_version += 1
            except Exception:  # noqa: S110
                pass  # best-effort
            return {
                "resolved": True,
                "issue_id": issue_id,
                "status": "resolved",
                "issues_closed": closed,
                "side_effects": side_effects,
            }

    except Exception as exc:
        logger.error("resolve_quality_issue(%s, %s) failed: %s", issue_id, action, exc)
        return {"resolved": False, "error": str(exc)}

    return {"resolved": False, "error": f"Unknown action: {action}"}


async def _entry_zone(app_state, knowledge_id: str) -> str:
    """Зона записи из frontmatter SSOT (best-effort).

    W2.13: паттерн _get_snippet (ниже) и crud.delete_entry:372 — безопасное
    чтение через MarkdownStore.read; при любой ошибке — ZONE_PRIVATE.
    TODO: зона из контекста запроса — W3.
    """
    store = getattr(app_state, "store", None)
    if store is None or not hasattr(store, "read"):
        return ZONE_PRIVATE
    try:
        entry = await store.read(knowledge_id)
    except Exception as exc:
        logger.warning("Entry read failed for %s (zone→private): %s", knowledge_id, exc)
        return ZONE_PRIVATE
    zone = getattr(getattr(entry, "frontmatter", None), "zone", None)
    return zone or ZONE_PRIVATE


async def _cascade_set_payload(
    qdrant,
    parent_knowledge_id: str,
    payload: dict,
    new_status: str,
    zone: str = ZONE_PRIVATE,
) -> int:
    """Фаза 13.14+13.15: применить set_payload ко всем дочерним секциям книги.

    Scroll по parent_knowledge_id → set_payload на каждую секцию.
    Все Qdrant-вызовы — через run_in_executor (не блокируют event loop).
    Возвращает число затронутых секций.

    W2.13: зона параметризована (zone) — вызывающий резолвит её из SSOT-записи
    (_entry_zone). TODO: зона из контекста запроса — W3.
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
                collection_name=collection_for_zone(zone),
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
                            collection_name=collection_for_zone(zone),
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
    zone: str = ZONE_PRIVATE,
) -> dict[str, str]:
    """R1: резолв title книг из payload родительских записей в Qdrant.

    Для каждого parent_knowledge_id делает scroll с фильтром по knowledge_id,
    извлекает поле title из payload. Пагинированный: батчи по _PARENT_TITLE_BATCH,
    все Qdrant-вызовы через run_in_executor.

    W2.13: зона параметризована (zone) — вызывающий резолвит её из payload-зон
    scroll-точек (review_queue_books) или дефолт ZONE_PRIVATE.

    Args:
        qdrant: QdrantClient wrapper (app_state.qdrant).
        parent_ids: список parent_knowledge_id для резолва.
        loop: asyncio event loop.
        zone: зона доступа (default: ZONE_PRIVATE).

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
                    collection_name=collection_for_zone(zone),
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
    embedder=None,  # P0: EmbeddingManager для embedding-dup-детекции
    app_state=None,  # Фаза 3 (2f): app.state для авто-хука (bulk_deprecate_duplicates)
) -> None:
    """Фоновая задача quality scan (13.15 + 13.18).

    Выполняет run_scan под scan_lock, обновляет прогресс через
    scan_progress. При ошибке — scan_progress.error().
    scan_task очищается в finally.

    13.18: принимает cancel_event и пробрасывает в run_scan.
    P0: пробрасывает embedder для embedding-dup (fallback при None).
    Фаза 3: после полного скана пишет scan_completed в audit (1b) и
    запускает пост-скан авто-хук (2f) — весь хук в try/except, сбой
    авто НИКОГДА не помечает успешный скан failed.
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
                embedder=embedder,
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
            # Фаза 3 (1b): scan_completed в audit — только полный скан
            # (отменённый/сбойный скан сюда не доходит). Н2: прогресс-трекер
            # prune_finished удаляет историю → счётчик сканов живёт в audit.
            write_audit(
                action="scan_completed",
                knowledge_id="-",
                actor="system",
                reason="quality scan complete",
                metadata={"scan_id": scan_id, "metrics": metrics},
            )
            # Фаза 3 (2f): пост-скан авто-хук ПОСЛЕ scan_completed-аудита.
            # Порядок критичен: гейт должен ВИДЕТЬ только что завершённый скан.
            try:
                from mcp_server.config import settings

                if settings.AUTO_DEDUP_ENABLED and app_state is not None:
                    await bulk_deprecate_duplicates(
                        {
                            "filter": {"hash_only": True},
                            "actor": "auto",
                            "reason": "auto R1 exact content-hash (FP=0 gate)",
                        },
                        app_state,
                    )
            except Exception as exc:
                # Весь хук внутри try/except: сбой авто НИКОГДА не роняет скан.
                logger.error(
                    "[AUTO-DEDUP] post-scan hook failed (scan result unaffected): %s", exc
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
        embedder = getattr(app_state, "embedder", None)  # P0: embedding-dup

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
                embedder=embedder,
                app_state=app_state,  # Фаза 3 (2f): для авто-хука
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


async def bulk_resolve_issues(params: dict, app_state) -> dict:
    """Пакетно резолвить/игнорировать issues по фильтру (P0 bulk-cleanup).

    Чистит накопленный шум (например 21K false-positive дублей) за один
    вызов. Меняет ТОЛЬКО status в issues.jsonl (issues.jsonl), не трогает
    контент и Qdrant payload. По умолчанию action=ignore (обратимо).

    Args:
        params:
            types (optional): список типов (duplicate, missing_field, orphaned, ...)
            knowledge_id (optional): фильтр по конкретной записи
            status (optional): исходный статус для выборки (default "open")
            action (optional): "ignore" | "resolve" (default "ignore" — обратимо)
            reason (optional): причина

    Returns:
        {"resolved": True, "action": action, "count": N, "total": N, "filtered": {...}}
    """
    types = params.get("types")
    knowledge_id = params.get("knowledge_id")
    src_status = params.get("status", "open")
    action = params.get("action", "ignore")
    reason = params.get("reason", "")

    if action not in ("ignore", "resolve"):
        return {
            "resolved": False,
            "error": f"Invalid action '{action}'. Must be 'ignore' or 'resolve'.",
        }

    try:
        issue_ids = list_issue_ids(
            types=types, status=src_status, knowledge_id=knowledge_id,
        )
        # Маппинг action → целевой статус (issue store использует resolved/ignored)
        target_status = "ignored" if action == "ignore" else "resolved"
        count = bulk_update_status(issue_ids, target_status, reason or None)
        logger.info(
            "bulk_resolve_issues: %d/%d issues -> %s (types=%s)",
            count, len(issue_ids), action, types,
        )
        return {
            "resolved": True,
            "action": action,
            "count": count,
            "total": len(issue_ids),
            "filtered": {
                "types": types,
                "knowledge_id": knowledge_id,
                "status": src_status,
            },
        }
    except Exception as exc:
        logger.error("bulk_resolve_issues failed: %s", exc)
        return {"resolved": False, "error": str(exc)}


def _restored_shielded_kids(audit_records: list[dict] | None = None) -> set[str]:
    """Фаза 3 (2d): kids с активным restore-щитом (cooldown).

    По audit-записям: для каждого kid сравниваем ts последнего restore
    (metadata.restored_by_operator=True) vs ts последнего deprecate-подобного
    (deprecate|bulk_deprecate|merge). Если restore новее — kid в щите, пока
    число scan_completed после restore < AUTO_DEDUP_RESTORE_COOLDOWN_SCANS.

    Данные — только audit.jsonl (развилка 1A): переживает рестарт, 0 миграций.
    """
    from mcp_server.config import settings

    if audit_records is None:
        audit_records = list_audit(limit=10**6)
    records = list(audit_records)
    records.reverse()  # хронологический порядок (старые → новые)

    cooldown = settings.AUTO_DEDUP_RESTORE_COOLDOWN_SCANS

    restore_ts: dict[str, str] = {}
    deprecate_ts: dict[str, str] = {}
    scan_completed_ts: list[str] = []

    for rec in records:
        ts = rec.get("ts", "")
        action = rec.get("action")
        kid = rec.get("knowledge_id")
        meta = rec.get("metadata") or {}
        if action == "restore" and meta.get("restored_by_operator") is True:
            restore_ts[kid] = ts
        elif action in ("deprecate", "bulk_deprecate", "merge"):
            deprecate_ts[kid] = ts
        elif action == "scan_completed":
            scan_completed_ts.append(ts)

    shielded: set[str] = set()
    for kid, rts in restore_ts.items():
        dts = deprecate_ts.get(kid)
        if dts is not None and dts >= rts:
            continue  # deprecate после restore → щит снят (запись заново скрыта)
        scans_after = sum(1 for t in scan_completed_ts if t > rts)
        if scans_after < cooldown:
            shielded.add(kid)
    return shielded


async def bulk_deprecate_duplicates(params: dict, app_state) -> dict:
    """Пакетно deprecate записи-дубликаты (Фаза 1 dedup + Фаза 3 авто-гейт).

    Для каждого целевого knowledge_id: set_payload status=deprecated
    (скрыть из поиска, обратимо через restore) → закрыть ВСЕ его open
    dup-issues → записать в audit.jsonl. Контент .md НЕ трогается.

    Фаза 3: actor="auto" — гейт ЦЕЛИКОМ внутри тула (config + FP=0 за ≥N
    полных сканов + hash_only + cooldown + cap). Вызывающий не доверяется.

    Args:
        params:
            issue_ids (optional): список issue_id → knowledge_id из них
            knowledge_id (optional): прямой список/один ID записи
            actor (optional): "operator-batch" | "auto" (default operator-batch)
            reason (optional): причина
            filter (optional): {"hash_only": bool} — единая форма с review (2e)

    Returns:
        {"resolved": True, "deprecated_count": N, "issues_closed": M,
         "side_effects": [...], "truncated": bool, "audited": True}
    """
    issue_ids = params.get("issue_ids") or []
    kid = params.get("knowledge_id")
    actor = params.get("actor", "operator-batch")
    reason = params.get("reason", "")
    filter_param = params.get("filter") or {}

    is_auto = actor == "auto"
    hash_only = bool(filter_param.get("hash_only", False))

    # ── Фаза 3 (2c): гейт авто-режима — ПЕРВАЯ строка обработки, до мутаций ──
    if is_auto:
        from mcp_server.config import settings

        if not settings.AUTO_DEDUP_ENABLED:
            return {
                "resolved": False,
                "error": "auto-deprecate disabled by config (AUTO_DEDUP_ENABLED=false)",
            }
        if not hash_only:
            return {
                "resolved": False,
                "error": "auto requires hash_only filter; cosine (R3) is never auto-applied",
            }
        fp = get_fp_rate(window_scans=settings.AUTO_DEDUP_FP_FREE_SCANS)
        if not fp["fp_free"]:
            return {
                "resolved": False,
                "error": (
                    f"auto gate closed: FP=0 over {settings.AUTO_DEDUP_FP_FREE_SCANS} scans "
                    f"required (scans_in_window={fp['scans_in_window']}, "
                    f"rejections={fp['rejections']})"
                ),
            }

    # Целевые knowledge_id: filter.hash_only | issue_ids | прямой knowledge_id
    target_kids: list[str] = []
    if hash_only:
        # Фаза 3 (2c): ЕДИНЫЙ предикат is_r1_exact_hash напрямую (не rank_pair).
        # metadata-refresh (0b) гарантирует актуальность сигналов.
        from mcp_server.quality.dup_ranking import is_r1_exact_hash
        from mcp_server.quality.issues import list_issues

        for iss in list_issues(types=["duplicate"], status="open", limit=10**6):
            if is_r1_exact_hash(getattr(iss, "metadata", None) or {}) and iss.knowledge_id not in target_kids:
                target_kids.append(iss.knowledge_id)
    elif issue_ids:
        from mcp_server.quality.issues import list_issues

        for iss in list_issues(status="open", limit=10**6):
            if iss.issue_id in set(issue_ids) and iss.knowledge_id not in target_kids:
                target_kids.append(iss.knowledge_id)
    if kid:
        kids = kid if isinstance(kid, list) else [kid]
        for k in kids:
            if k not in target_kids:
                target_kids.append(k)

    # Фаза 3 (2d): cooldown-щит — исключить восстановленные оператором записи
    if is_auto:
        shielded = _restored_shielded_kids()
        if shielded:
            target_kids = [k for k in target_kids if k not in shielded]

    # Фаза 3 (2c): cap авто-скрытий за один скан
    truncated = False
    if is_auto:
        from mcp_server.config import settings

        if len(target_kids) > settings.AUTO_DEDUP_MAX_PER_SCAN:
            target_kids = target_kids[:settings.AUTO_DEDUP_MAX_PER_SCAN]
            truncated = True

    if not target_kids:
        if is_auto:
            # Фаза 3 (2c): гейт прошёл (config+fp_free), но кандидатов нет —
            # все R1 закрыты cooldown-щитом либо exact-hash дублей не найдено.
            # Это НЕ ошибка: авто ничего не делает, resolved=True с count=0.
            return {
                "resolved": True,
                "deprecated_count": 0,
                "issues_closed": 0,
                "audited": False,
                "truncated": truncated,
            }
        return {"resolved": False, "error": "No targets: provide issue_ids or knowledge_id or filter"}

    qdrant = getattr(app_state, "qdrant", None)
    loop = asyncio.get_running_loop()
    side_effects: list[str] = []
    total_issues_closed = 0
    deprecated_count = 0

    for target in target_kids:
        # Фаза 3 (2c, P2-NEW-4): авто-путь — audit-FIRST strict. Без audit-записи
        # авто-скрытие невозможно; сбой аудита → abort пачки (остаток не трогаем).
        if is_auto and not write_audit(
            action="bulk_deprecate",
            knowledge_id=target,
            actor=actor,
            reason=reason or "auto R1 exact content-hash (FP=0 gate)",
            metadata={"issues_closed": None, "auto": True},
            strict=True,
        ):
            logger.error(
                "[AUTO-DEDUP] strict audit failed for %s — aborting batch", target,
            )
            return {
                "resolved": False,
                "error": f"strict audit failed for {target} (batch aborted)",
                "deprecated_count": deprecated_count,
                "issues_closed": total_issues_closed,
                "side_effects": side_effects,
                "truncated": truncated,
            }
        try:
            if qdrant:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                from mcp_server.quality.lifecycle import make_deprecation_payload_update

                payload = make_deprecation_payload_update()
                # Зона записи из SSOT frontmatter (_entry_zone) — P1-1 W2.
                zone = await _entry_zone(app_state, target)
                await loop.run_in_executor(
                    None,
                    lambda t=target, p=payload, z=zone: qdrant.set_payload(
                        payload=p,
                        points_filter=Filter(
                            must=[FieldCondition(key="knowledge_id", match=MatchValue(value=t))]
                        ),
                        collection_name=collection_for_zone(z),
                    ),
                )
                side_effects.append(f"Deprecated {target}")
            # Закрыть все open dup-issues записи
            closed = close_all_dup_issues(target, reason or "deprecated in batch")
            total_issues_closed += closed
            if closed:
                side_effects.append(f"Closed {closed} dup-issue(s) for {target}")
            # Операторский путь: аудит ПОСЛЕ (как было); авто — уже выше (audit-FIRST)
            if not is_auto:
                write_audit(
                    action="bulk_deprecate",
                    knowledge_id=target,
                    actor=actor,
                    reason=reason or "deprecated in batch",
                    metadata={"issues_closed": closed},
                )
            deprecated_count += 1
        except Exception as exc:
            logger.error("bulk_deprecate failed for %s: %s", target, exc)
            side_effects.append(f"FAILED {target}: {exc}")

    # data_version++ (кэш-инвалидация)
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort

    logger.info(
        "bulk_deprecate_duplicates: %d records deprecated, %d issues closed (actor=%s)",
        deprecated_count, total_issues_closed, actor,
    )
    return {
        "resolved": True,
        "deprecated_count": deprecated_count,
        "issues_closed": total_issues_closed,
        "side_effects": side_effects,
        "audited": True,
        "truncated": truncated,
    }


# ── Фаза 2 dedup: Review Queue ─────────────────────────────────

# Сниппет-кеш (TTL 60s): повторные вызовы не читают SSOT заново
_snippet_cache: dict[str, tuple[float, str]] = {}
_SNIPPET_TTL = 60.0
_SNIPPET_LINES = 30
_SNIPPET_MAX_CHARS = 2000


def _snippet_from_content(content: str) -> str:
    """Первые N строк (≤ max chars) — сниппет для diff-просмотра."""
    lines = content.strip().split("\n")[:_SNIPPET_LINES]
    snippet = "\n".join(lines)
    return snippet[:_SNIPPET_MAX_CHARS]


async def _get_snippet(knowledge_id: str, app_state) -> str:
    """Сниппет записи через MarkdownStore.read с кешем 60s."""
    now = time.time()
    cached = _snippet_cache.get(knowledge_id)
    if cached and now - cached[0] < _SNIPPET_TTL:
        return cached[1]

    store = getattr(app_state, "store", None)
    snippet = ""
    if store is not None and hasattr(store, "read"):
        try:
            entry = await store.read(knowledge_id)
            if entry is not None:
                snippet = _snippet_from_content(entry.content)
        except Exception as exc:
            logger.warning("Snippet read failed for %s: %s", knowledge_id, exc)

    _snippet_cache[knowledge_id] = (now, snippet)
    return snippet


async def review_duplicate_pairs(params: dict, app_state) -> dict:
    """Интерактивная ревью-очередь dup-пар (Фаза 2 dedup).

    Читает open duplicate issues, ранжирует по R1–R6 (dup_ranking.py):
    - green_batch: 🟢 пачка «Утвердить все» (exact hash ИЛИ cosine≥0.97+guards)
    - yellow_pairs: 🟡 сомнительные — с контент-сниппетами для визуального diff
    - red_skipped: 🔴 косвенные (только issues, не в ревью)

    READ-ONLY: не мутирует данные (утверждение — через bulk_deprecate_duplicates).

    Фаза 3 (2e): опциональный filter={"hash_only": bool} — ЕДИНАЯ форма с bulk.
    При hash_only=true green_batch = пары, отобранные is_r1_exact_hash НАПРЯМУЮ
    (не rank_pair) — устраняет расхождение предикатов (exact-hash пара с
    cosine<0.92 видна в green обоими путями). Ответ += filter_echo.

    Args:
        params:
            limit (optional): макс. число open dup-issues для анализа (default 200)
            filter (optional): {"hash_only": bool = false}

    Returns:
        {"green_batch": [...], "yellow_pairs": [...], "red_skipped": N,
         "total_open": M, "filter_echo": {...}}
    """
    from mcp_server.quality.dup_ranking import (
        extract_target_kid,
        is_r1_exact_hash,
        rank_pair,
        recommend_canonical,
        summarize_signals,
    )

    limit = min(params.get("limit", 200), 500)
    filter_param = params.get("filter") or {}
    hash_only = bool(filter_param.get("hash_only", False))
    issues = list_issues(types=["duplicate"], status="open", limit=limit)

    green: list[dict] = []
    yellow: list[dict] = []
    red_skipped = 0

    for iss in issues:
        meta = getattr(iss, "metadata", None) or {}
        detail = iss.detail or ""
        target_kid = extract_target_kid(detail)
        signals = summarize_signals(meta)
        base = {
            "issue_id": iss.issue_id,
            "source_kid": iss.knowledge_id,
            "target_kid": target_kid,
            "cosine": signals["cosine"],
            "signals": signals,
        }
        if hash_only:
            # Фаза 3 (2e): строго R1 (exact hash) — ЕДИНЫЙ предикат, не rank_pair.
            if is_r1_exact_hash(meta):
                base["subject"] = meta.get("subject", "")
                green.append(base)
            else:
                red_skipped += 1
            continue

        flow = rank_pair(meta, detail)
        if flow == "green":
            base["subject"] = meta.get("subject", "")
            green.append(base)
        elif flow == "yellow":
            base["recommended_canonical"] = recommend_canonical(
                meta, iss.knowledge_id, target_kid
            )
            yellow.append(base)
        else:
            red_skipped += 1

    # Сниппеты только для 🟡 (diff-просмотр)
    for pair in yellow:
        pair["source_snippet"] = await _get_snippet(pair["source_kid"], app_state)
        pair["target_snippet"] = await _get_snippet(pair["target_kid"], app_state)

    logger.info(
        "review_duplicate_pairs: %d green, %d yellow, %d red (of %d open)",
        len(green), len(yellow), red_skipped, len(issues),
    )
    result = {
        "green_batch": green,
        "yellow_pairs": yellow,
        "red_skipped": red_skipped,
        "total_open": len(issues),
    }
    if hash_only:
        result["filter_echo"] = {"hash_only": True}
    return result


async def list_audit_log(params: dict, app_state) -> dict:
    """Фаза 3 (3a): read-only журнал действий (аудит) + статус авто-гейта.

    Обёртка над list_audit + get_fp_rate — один вызов для панели «Журнал
    действий» и строки статуса авто-скрытия.

    Args:
        params:
            actor (optional): фильтр по actor (operator | auto | system | ...)
            action (optional): фильтр по действию (deprecate | restore | ...)
            knowledge_id (optional): фильтр по записи
            limit (default 50, max 200): макс. число записей

    Returns:
        {"records": [...], "fp_stats": {...}, "auto_dedup_enabled": bool,
         "auto_dedup_config": {...}}
    """
    actor = params.get("actor")
    action = params.get("action")
    knowledge_id = params.get("knowledge_id")
    limit = min(params.get("limit", 50), 200)

    from mcp_server.config import settings

    records = list_audit(
        actor=actor, action=action, knowledge_id=knowledge_id, limit=limit,
    )
    return {
        "records": records,
        "fp_stats": get_fp_rate(),
        "auto_dedup_enabled": settings.AUTO_DEDUP_ENABLED,
        "auto_dedup_config": {
            "fp_free_scans": settings.AUTO_DEDUP_FP_FREE_SCANS,
            "restore_cooldown_scans": settings.AUTO_DEDUP_RESTORE_COOLDOWN_SCANS,
            "max_per_scan": settings.AUTO_DEDUP_MAX_PER_SCAN,
        },
    }
