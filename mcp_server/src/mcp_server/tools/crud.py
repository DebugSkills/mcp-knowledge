# ruff: noqa: BLE001
"""A5-A6: write_knowledge + update_entry + delete_entry.

Three-way write flow (P1-2):
1. store.write(entry)           → Markdown SSOT (.md + git commit)
2. pipeline.enqueue(entry, ...) → chunk → embed → Qdrant upsert
3. knowledge_index.update_section(domain) → инкрементальный INDEX.gen.yaml

update_entry: store.update(expected_version) + pipeline.enqueue (переиндексация)
  G3-fix: optimistic locking через expected_version параметр
delete_entry: store.delete() (soft-delete в .trash/) + qdrant.delete_by_knowledge_id()
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..metrics import quality_gate_skipped, record_write_latency
from ..models import VersionConflictError, WriteRequest

logger = logging.getLogger("mcp_knowledge.tools.crud")


def _get_qdrant(app_state):
    """Получить Qdrant-клиент: app_state.qdrant_client (raw) или app_state.qdrant (wrapper)."""
    return getattr(app_state, "qdrant_client", None) or getattr(app_state, "qdrant", None)


def _recommended_field_warnings(params: dict, strict: bool) -> tuple[list[dict], list[str], bool]:
    """Проверка recommended-полей (source/evergreen/cross_subjects) — advisory (E5).

    Возвращает (issues, warnings, blocked). При strict=True warn повышается до block.
    """
    issues: list[dict] = []
    warnings: list[str] = []
    blocked = False
    for rec_field in ("source", "evergreen", "cross_subjects"):
        val = params.get(rec_field)
        missing = val is None or val == "" or val is False or val == []
        if missing:
            msg = f"Recommended field '{rec_field}' is missing"
            if strict:
                issues.append({"field": rec_field, "severity": "critical", "message": msg})
                blocked = True
            else:
                warnings.append(msg)
    return issues, warnings, blocked


async def write_knowledge(params: dict, app_state) -> dict:
    """Записать новое знание: SSOT → chunk → embed → Qdrant → INDEX.

    Three-way write flow (P1-2):
    1. store.write → Markdown SSOT
    2. pipeline.enqueue → async indexing
    3. knowledge_index.update_section → INDEX.gen.yaml
    """
    # Валидация обязательных параметров
    content = params.get("content", "")
    domain = params.get("domain", "")
    subject = params.get("subject", "")

    if not content:
        return {"error": "Missing required parameter: 'content'"}
    if not domain:
        return {"error": "Missing required parameter: 'domain'"}
    if not subject:
        return {"error": "Missing required parameter: 'subject'"}

    # ── Phase 4: Pre-write quality checks (GAP-1 fix) ──
    strict = params.get("strict", False)
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    blocked = False

    # 1. knowledge_id collision check (если клиент указал ID)
    provided_kid = params.get("knowledge_id")
    if provided_kid:
        try:
            qdrant = _get_qdrant(app_state)
            if qdrant and hasattr(qdrant, "get_all_knowledge_ids"):
                existing_ids = qdrant.get_all_knowledge_ids()
                if provided_kid in existing_ids:
                    msg = f"knowledge_id '{provided_kid}' already exists (duplicate)"
                    quality_issues.append({"field": "knowledge_id", "severity": "critical", "message": msg})
                    blocked = True
        except Exception as exc:
            logger.warning("Collision check skipped (non-fatal): %s", exc)
            quality_gate_skipped.labels(gate="collision", reason="exception").inc()

    # 2. Recommended-поля (advisory, E5)
    rec_issues, rec_warnings, rec_blocked = _recommended_field_warnings(params, strict)
    quality_issues.extend(rec_issues)
    quality_warnings.extend(rec_warnings)
    blocked = blocked or rec_blocked

    # 3. Semantic dup-gate (advisory, §4.3)
    try:
        embedder = getattr(app_state, "embedder", None)
        qdrant = _get_qdrant(app_state)
        if embedder is not None and qdrant is not None and hasattr(qdrant, "search"):
            from mcp_server.quality.dup_gate import check_duplicates

            quality_duplicates = await check_duplicates(
                content, domain, provided_kid,
                embedder=embedder,
                qdrant_client=qdrant,
            )
            if quality_duplicates and strict:
                msg = f"Semantic duplicates detected: {quality_duplicates}"
                quality_issues.append({"field": "*content", "severity": "critical", "message": msg})
                blocked = True
    except Exception as exc:
        logger.warning("Dup-gate check skipped (non-fatal): %s", exc)
        quality_gate_skipped.labels(gate="dup_gate", reason="exception").inc()

    if blocked:
        logger.warning(
            "write_knowledge blocked by quality gate: %d critical issues, %d duplicates",
            len(quality_issues), len(quality_duplicates),
        )
        return {
            "error": "Quality gate blocked the write",
            "quality_report": {
                "blocked": True,
                "issues": quality_issues,
                "warnings": quality_warnings,
                "duplicates": quality_duplicates,
            },
        }
    # ── End quality gate ──

    wait_for_index = params.get("wait_for_index", False)

    # Шаг 1: SSOT запись
    store = app_state.store
    req = WriteRequest(
        content=content,
        domain=domain,
        subject=subject,
        project=params.get("project"),
        cross_subjects=params.get("cross_subjects", []),
        tags=params.get("tags", []),
        knowledge_id=params.get("knowledge_id"),
        wait_for_index=wait_for_index,
    )
    entry = await store.write(req)
    knowledge_id = entry.frontmatter.knowledge_id

    # Шаг 2: Enqueue в indexing pipeline (с трекингом latency)
    t0 = time.monotonic()
    pipeline = app_state.pipeline
    indexed = False
    try:
        enqueue_result = await pipeline.enqueue(entry, wait_for_index=wait_for_index)
        if wait_for_index:
            indexed = enqueue_result.indexed
            if not indexed:
                logger.warning(
                    "write_knowledge: indexing NOT confirmed for %s (pending=%s) — "
                    "worker may be dead/slow or event loop mismatch",
                    knowledge_id, enqueue_result.pending,
                )
    except Exception as exc:
        logger.error(
            "Pipeline enqueue failed for %s (wait=%s): %s",
            knowledge_id, wait_for_index, exc,
        )
        # Не фатально — SSOT уже записан, Qdrant отстаёт
    finally:
        record_write_latency(time.monotonic() - t0)

    # Шаг 3: INDEX.gen.yaml update (best-effort)
    try:
        knowledge_index = app_state.knowledge_index
        knowledge_index.update_section(domain)
    except Exception as exc:
        logger.warning(
            "INDEX update failed for domain=%s (non-fatal): %s", domain, exc
        )

    pending = not indexed

    logger.info(
        "write_knowledge: %s (domain=%s, subject=%s, wait=%s, indexed=%s)",
        knowledge_id, domain, subject, wait_for_index, indexed,
    )
    return {
        "knowledge_id": knowledge_id,
        "domain": domain,
        "subject": subject,
        "indexed": indexed,
        "pending": pending,
        "quality_report": {
            "blocked": False,
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }


async def update_entry(params: dict, app_state) -> dict:
    """Обновить существующую запись: контент + переиндексация.
    
    G3-fix: поддерживает expected_version для optimistic locking.
    """
    knowledge_id = params.get("knowledge_id", "")
    content = params.get("content")
    expected_version = params.get("version")  # G3-fix: optimistic locking

    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}
    if content is not None and not isinstance(content, str):
        return {"error": "Parameter 'content' must be a string"}
    if content is not None and not content.strip():
        return {"error": "Parameter 'content' must not be empty"}

    # ── Phase 4: Pre-write quality checks (GAP-1 fix) ──
    # Для update контент — только тело markdown (frontmatter сохраняется от
    # существующей записи). Проверяем semantic dup-gate (advisory) + recommended-поля.
    strict = params.get("strict", False)
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    blocked = False

    if content is not None:
        # Semantic dup-gate (advisory, §4.3) — только при обновлении контента
        try:
            embedder = getattr(app_state, "embedder", None)
            qdrant = _get_qdrant(app_state)
            if embedder is not None and qdrant is not None and hasattr(qdrant, "search"):
                from mcp_server.quality.dup_gate import check_duplicates

                quality_duplicates = await check_duplicates(
                    content, "", knowledge_id,  # domain неизвестен — без фильтра
                    embedder=embedder,
                    qdrant_client=qdrant,
                )
                if quality_duplicates and strict:
                    msg = f"Semantic duplicates detected: {quality_duplicates}"
                    quality_issues.append({"field": "*content", "severity": "critical", "message": msg})
                    blocked = True
        except Exception as exc:
            logger.warning("Dup-gate check skipped (non-fatal): %s", exc)
            quality_gate_skipped.labels(gate="dup_gate", reason="exception").inc()

    # Recommended-поля (advisory) — из параметров обновления
    rec_issues, rec_warnings, rec_blocked = _recommended_field_warnings(params, strict)
    quality_issues.extend(rec_issues)
    quality_warnings.extend(rec_warnings)
    blocked = blocked or rec_blocked

    if blocked:
        logger.warning(
            "update_entry blocked by quality gate for %s: %d critical issues",
            knowledge_id, len(quality_issues),
        )
        return {
            "error": "Quality gate blocked the update",
            "quality_report": {
                "blocked": True,
                "issues": quality_issues,
                "warnings": quality_warnings,
                "duplicates": quality_duplicates,
            },
        }
    # ── End quality gate ──

    store = app_state.store
    try:
        entry = await store.update(
            knowledge_id,
            content=content,
            expected_version=expected_version,
        )
    except VersionConflictError as e:
        # Фаза 12: инкремент метрики optimistic_lock_conflicts
        from ..metrics import optimistic_lock_conflicts
        optimistic_lock_conflicts.inc()

        # F2: Optimistic locking conflict — клиент должен перечитать и повторить
        logger.warning(
            "update_entry: version conflict for %s (expected=%s, actual=%s): %s",
            knowledge_id, expected_version, e.actual, e,
        )
        return {
            "message": f"Version conflict: {e}",
            "knowledge_id": knowledge_id,
            "expected_version": expected_version,
            "current_version": e.actual,
            "conflict": True,
        }

    if entry is None:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    # Переиндексация (wait_for_index опционален — контракт: для синхронных
    # сценариев (тесты, blue-green) ждём завершения, иначе drain очереди
    # при stop может записать точку ПОСЛЕ delete_by_knowledge_id)
    wait_for_index = params.get("wait_for_index", False)
    pipeline = app_state.pipeline
    await pipeline.enqueue(entry, wait_for_index=wait_for_index)

    # INDEX update (best-effort)
    try:
        knowledge_index = app_state.knowledge_index
        knowledge_index.update_section(entry.frontmatter.domain)
    except Exception as exc:
        logger.warning("INDEX update failed for %s: %s", knowledge_id, exc)

    logger.info("update_entry: %s v%d", knowledge_id, entry.frontmatter.version)
    # Task 1: инкремент data_version после мутации
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort
    return {
        "knowledge_id": knowledge_id,
        "version": entry.frontmatter.version,
        "updated_at": entry.frontmatter.updated_at.isoformat(),
        "pending": True,
        "quality_report": {
            "blocked": False,
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }


async def delete_entry(params: dict, app_state) -> dict:
    """Удалить запись: soft-delete (→ .trash/) + удаление из Qdrant.

    Фаза 13.14: +cascade param — при cascade=True удалить также все
    дочерние секции по parent_knowledge_id (рекурсивно через scroll).
    """
    knowledge_id = params.get("knowledge_id", "")
    cascade = params.get("cascade", False)

    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}

    cascade_deleted = 0

    # Получаем зависимости (до cascade — нужны для удаления детей)
    store = app_state.store
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    # Шаг 0 (cascade): найти и удалить дочерние секции
    if cascade:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            qdrant_raw = _get_qdrant(app_state)
            # Scroll все точки где parent_knowledge_id = knowledge_id
            child_ids: list[str] = []
            offset = None
            while True:
                points, next_offset = qdrant_raw.scroll(
                    collection_name="knowledge",
                    scroll_filter=Filter(
                        must=[FieldCondition(key="parent_knowledge_id", match=MatchValue(value=knowledge_id))]
                    ),
                    limit=1000,
                    offset=offset,
                    with_payload=["knowledge_id"],
                    with_vectors=False,
                )
                for point in points:
                    kid = point.payload.get("knowledge_id") if point.payload else None
                    if kid:
                        child_ids.append(kid)
                if next_offset is None or len(points) == 0:
                    break
                offset = next_offset

            # Удаляем каждую секцию: store.delete (→ .trash/) + qdrant.delete_by_knowledge_id
            for child_id in child_ids:
                try:
                    await store.delete(child_id)
                    await loop.run_in_executor(None, qdrant.delete_by_knowledge_id, child_id)
                    cascade_deleted += 1
                except Exception as exc:
                    logger.warning("[DELETE] cascade: failed to delete child %s: %s", child_id, exc)

            logger.info("[DELETE] cascade: %d child sections deleted for book %s", cascade_deleted, knowledge_id)
        except Exception as exc:
            logger.error("[DELETE] cascade scroll failed for %s: %s", knowledge_id, exc)

    # Шаг 1: Soft-delete Markdown SSOT
    entry = await store.read(knowledge_id)
    domain = entry.frontmatter.domain if entry else None

    deleted = await store.delete(knowledge_id)
    if not deleted:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    # Шаг 2: Удаление из Qdrant
    await loop.run_in_executor(None, qdrant.delete_by_knowledge_id, knowledge_id)

    # Шаг 3: INDEX update (best-effort)
    if domain:
        try:
            knowledge_index = app_state.knowledge_index
            knowledge_index.update_section(domain)
        except Exception as exc:
            logger.warning("INDEX update failed for domain=%s: %s", domain, exc)

    logger.info(
        "[DELETE] delete_entry: %s → .trash/ + Qdrant removed (cascade_deleted=%d)",
        knowledge_id, cascade_deleted,
    )
    # Task 1: инкремент data_version после мутации
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort
    return {
        "knowledge_id": knowledge_id,
        "deleted": True,
        "cascade_deleted": cascade_deleted,
        "message": f"Entry '{knowledge_id}' moved to .trash/ and removed from Qdrant"
                   + (f" (+{cascade_deleted} child sections)" if cascade_deleted else ""),
    }
