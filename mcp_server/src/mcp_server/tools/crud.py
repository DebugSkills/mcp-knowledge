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
from typing import Any

from ..models import WriteRequest, VersionConflictError
from ..metrics import record_write_latency

logger = logging.getLogger("mcp_knowledge.tools.crud")


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
        await pipeline.enqueue(entry, wait_for_index=wait_for_index)
        if wait_for_index:
            # Пайплайн завершился без исключения → считаем indexed
            indexed = True
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

    store = app_state.store
    try:
        entry = await store.update(
            knowledge_id,
            content=content,
            expected_version=expected_version,
        )
    except VersionConflictError as e:
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

    # Переиндексация
    pipeline = app_state.pipeline
    await pipeline.enqueue(entry, wait_for_index=False)

    # INDEX update (best-effort)
    try:
        knowledge_index = app_state.knowledge_index
        knowledge_index.update_section(entry.frontmatter.domain)
    except Exception as exc:
        logger.warning("INDEX update failed for %s: %s", knowledge_id, exc)

    logger.info("update_entry: %s v%d", knowledge_id, entry.frontmatter.version)
    return {
        "knowledge_id": knowledge_id,
        "version": entry.frontmatter.version,
        "updated_at": entry.frontmatter.updated_at.isoformat(),
        "pending": True,
    }


async def delete_entry(params: dict, app_state) -> dict:
    """Удалить запись: soft-delete (→ .trash/) + удаление из Qdrant."""
    knowledge_id = params.get("knowledge_id", "")

    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}

    # Шаг 1: Soft-delete Markdown SSOT
    store = app_state.store
    entry = await store.read(knowledge_id)
    domain = entry.frontmatter.domain if entry else None

    deleted = await store.delete(knowledge_id)
    if not deleted:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    # Шаг 2: Удаление из Qdrant
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, qdrant.delete_by_knowledge_id, knowledge_id)

    # Шаг 3: INDEX update (best-effort)
    if domain:
        try:
            knowledge_index = app_state.knowledge_index
            knowledge_index.update_section(domain)
        except Exception as exc:
            logger.warning("INDEX update failed for domain=%s: %s", domain, exc)

    logger.info("delete_entry: %s → .trash/ + Qdrant removed", knowledge_id)
    return {
        "knowledge_id": knowledge_id,
        "deleted": True,
        "message": f"Entry '{knowledge_id}' moved to .trash/ and removed from Qdrant",
    }
