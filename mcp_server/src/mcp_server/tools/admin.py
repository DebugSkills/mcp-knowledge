"""A8: reindex — перестроить индекс: перечитать все Markdown → переиндексировать в Qdrant.

Flow:
1. pipeline.reindex_blue_green() (default) или pipeline.reindex_all() (legacy)
2. knowledge_index.rebuild_all() → root + per-section INDEX.gen.yaml

Фаза 3 F1: blue_green=True default → zero-downtime через Qdrant aliases.

W4.1: set_zone — перекладка записи между зонами (курирование public-слоя).
Flow (план two-zone-access §2.5):
1. Валидация knowledge_id + zone ∈ {public, private} (+reason).
2. store.read → запись существует; текущая зона из frontmatter.
3. store.update(root, metadata={"zone": zone}) → frontmatter + git-коммит.
4. Promo-чеклист (только public): check_promo_readiness — advisory, не блокирует.
5. Каскад секций: scroll parent_knowledge_id в СТАРОЙ зоне → обновление
   frontmatter секций (write_entry, ОДИН git-коммит) → delete из старой
   коллекции (root + секции) → enqueue в НОВУЮ зону.
6. app_state.data_version += 1 (P2-2 — TOC-кэш инвалидация).
7. Audit: write_audit(action="set_zone").
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..quality.audit import write_audit
from ..quality.issues import list_issue_ids
from ..storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone
from .zone_utils import resolve_zone

logger = logging.getLogger("mcp_knowledge.tools.admin")

# Эвристики sensitive-путей (план §2.5): флаг, не блок (promo-чеклист advisory)
_SENSITIVE_PATH_MARKERS = ("partners/", "_private/", "review-", ".trash")


def _get_qdrant(app_state):
    """Получить Qdrant-клиент: app_state.qdrant_client (raw) или app_state.qdrant (wrapper)."""
    return getattr(app_state, "qdrant_client", None) or getattr(app_state, "qdrant", None)


async def check_promo_readiness(knowledge_id: str, entry, zone: str) -> dict:
    """Promo-чеклист перед переводом записи в public (план §2.5, advisory).

    Проверки:
    (а) zone == "public" (чеклист применим только к promo в public);
    (б) нет open issues типа "sensitive" для записи (эмитятся W4.3/W4.4 —
        до их реализации список пуст, чек безопасен);
    (в) путь файла не содержит PARTNERS/|_private/|REVIEW-|.trash.

    Returns:
        {"ready": bool, "warnings": [str]} — НЕ блокирует set_zone.
    """
    if zone != ZONE_PUBLIC:
        return {"ready": True, "warnings": [], "note": "promo-checklist applies to public zone only"}

    warnings: list[str] = []

    # (б) sensitive-флаги в issues (list_issue_ids — JSONL-чтение, малое)
    try:
        sensitive_ids = list_issue_ids(types=["sensitive"], status="open", knowledge_id=knowledge_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("check_promo_readiness: issues lookup failed for %s: %s", knowledge_id, exc)
        sensitive_ids = []
    if sensitive_ids:
        warnings.append(f"{len(sensitive_ids)} open sensitive issue(s)")

    # (в) путь файла (KnowledgeEntry.file_path — относительный путь в knowledge/)
    file_path = str(getattr(entry, "file_path", "") or "")
    lowered = file_path.lower()
    for marker in _SENSITIVE_PATH_MARKERS:
        if marker in lowered:
            warnings.append(f"file path contains '{marker}': {file_path}")

    return {"ready": not warnings, "warnings": warnings}


async def set_zone(params: dict, app_state) -> dict:
    """Переложить запись между зонами (курирование public-слоя, write-only).

    Args:
        params:
            knowledge_id (required): ID записи (книга/секция).
            zone (required): целевая зона — public | private.
            reason (optional): причина (для audit.jsonl).

    Flow: см. docstring модуля (план §2.5, шаги 1–7).
    """
    knowledge_id = params.get("knowledge_id") or ""
    zone_param = params.get("zone")
    reason = params.get("reason") or "zone change"

    # Шаг 1: валидация
    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}
    if not zone_param:
        return {"error": "Missing required parameter: 'zone' (public | private)"}
    try:
        zone, _ = resolve_zone(zone_param, None)
    except ValueError as exc:
        return {"error": str(exc)}

    store = app_state.store
    loop = asyncio.get_running_loop()

    # Шаг 2: чтение записи + текущая зона
    entry = await store.read(knowledge_id)
    if entry is None:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}
    old_zone = getattr(entry.frontmatter, "zone", None) or ZONE_PRIVATE

    # Идемпотентность: уже в нужной зоне → no-op
    if old_zone == zone:
        return {
            "knowledge_id": knowledge_id,
            "zone": zone,
            "ok": True,
            "note": f"already in zone '{zone}' (no-op)",
        }

    # Монозональность (W1.7): секция public при private-родителе — нельзя.
    parent_kid = getattr(entry.frontmatter, "parent_knowledge_id", None)
    if zone == ZONE_PUBLIC and parent_kid:
        parent = await store.read(parent_kid)
        parent_zone = getattr(parent.frontmatter, "zone", None) or ZONE_PRIVATE if parent else None
        _final_zone, forced = resolve_zone(zone, parent_zone)
        if forced:
            return {
                "error": (
                    f"mono-zone violation: parent book '{parent_kid}' is private — "
                    "promote the book, not the section"
                ),
            }

    # Шаг 4: promo-чеклист (только public, advisory)
    promo = None
    if zone == ZONE_PUBLIC:
        promo = await check_promo_readiness(knowledge_id, entry, zone)

    qdrant = _get_qdrant(app_state)
    pipeline = app_state.pipeline

    # Шаг 5a: каскад — scroll дочерних секций из СТАРОЙ зоны
    child_ids: list[str] = []
    if qdrant is not None:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            offset = None
            while True:
                points, next_offset = await loop.run_in_executor(
                    None,
                    lambda o=offset: qdrant.scroll(
                        scroll_filter=Filter(
                            must=[FieldCondition(
                                key="parent_knowledge_id",
                                match=MatchValue(value=knowledge_id),
                            )]
                        ),
                        limit=1000,
                        offset=o,
                        with_payload=["knowledge_id"],
                        with_vectors=False,
                        collection_name=collection_for_zone(old_zone),
                    ),
                )
                for point in points or []:
                    kid = (point.payload or {}).get("knowledge_id")
                    if kid:
                        child_ids.append(kid)
                if next_offset is None or not points:
                    break
                offset = next_offset
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_zone: child scroll in zone '%s' failed for %s: %s",
                           old_zone, knowledge_id, exc)

    # Шаг 3+5b: root — store.update (frontmatter + git-коммит).
    root_entry = await store.update(knowledge_id, metadata={"zone": zone})

    # Секции: read → mutate zone → write_entry, ОДИН git-коммит на книгу
    # (паттерн import_content; store.update на каждую секцию дал бы N+1
    # git-коммитов — нарушение ограничения «один коммит на книгу»).
    updated_children: list = []
    skipped_children: list[str] = []
    for child_id in child_ids:
        child_entry = await store.read(child_id)
        if child_entry is None:
            skipped_children.append(child_id)
            continue
        child_fm = child_entry.frontmatter
        child_fm.zone = zone
        child_fm.version += 1
        child_fm.updated_at = datetime.now(timezone.utc)
        await store.write_entry(child_entry)
        updated_children.append(child_entry)
    if updated_children:
        await store.flush(
            f"set_zone: {knowledge_id} → {zone} ({len(updated_children)} sections)"
        )

    # Шаг 5c: удаление из СТАРОЙ коллекции (root + секции, best-effort)
    if qdrant is not None:
        for kid in [knowledge_id, *child_ids]:
            try:
                await loop.run_in_executor(
                    None, qdrant.delete_by_knowledge_id, kid,
                    collection_for_zone(old_zone),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("set_zone: delete from old zone '%s' failed for %s: %s",
                               old_zone, kid, exc)

    # Шаг 5d: индексация в НОВУЮ зону (enqueue root + секции; wait_for_index=False —
    # чанки пойдут в коллекцию по обновлённому fm.zone, W2.4-приоритет)
    for entry_to_index in [root_entry, *updated_children]:
        try:
            await pipeline.enqueue(entry_to_index, wait_for_index=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("set_zone: enqueue failed for %s: %s",
                         entry_to_index.frontmatter.knowledge_id, exc)

    # Шаг 6: data_version += 1 (TOC-кэш инвалидация, P2-2)
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort

    # Шаг 7: audit
    write_audit(
        action="set_zone",
        knowledge_id=knowledge_id,
        actor="operator",
        reason=reason,
        metadata={
            "zone": zone,
            "old_zone": old_zone,
            "sections_moved": len(updated_children),
            "skipped_sections": skipped_children,
        },
    )

    logger.info(
        "set_zone: %s %s → %s (sections_moved=%d, skipped=%d)",
        knowledge_id, old_zone, zone, len(updated_children), len(skipped_children),
    )

    response: dict = {
        "knowledge_id": knowledge_id,
        "zone": zone,
        "old_zone": old_zone,
        "sections_moved": len(updated_children),
        "ok": True,
    }
    if promo is not None:
        response["promo"] = promo
    if skipped_children:
        response["skipped_sections"] = skipped_children
    return response


async def reindex(params: dict, app_state) -> dict:
    """Перестроить индекс: все Markdown-файлы → Qdrant + INDEX.gen.yaml.

    Args:
        params:
            domain (optional): переиндексировать только один домен
            blue_green (optional, default=True): использовать blue-green reindex
                (zero-downtime). False → старый delete-all подход.
    """
    domain = params.get("domain")
    blue_green = params.get("blue_green", True)

    pipeline = app_state.pipeline
    knowledge_index = app_state.knowledge_index

    logger.info("reindex: starting full reindex (domain=%s, blue_green=%s)",
                 domain or "all", blue_green)

    if blue_green:
        result = await pipeline.reindex_blue_green()
    else:
        result = await pipeline.reindex_all()

    total_docs = result.get("total_docs", 0) or result.get("reindex_result", {}).get("total_docs", 0)
    total_chunks = result.get("total_chunks", 0) or result.get("reindex_result", {}).get("total_chunks", 0)
    failed = result.get("failed", 0) or result.get("reindex_result", {}).get("failed", 0)

    # Извлечь вложенный результат из blue_green
    reindex_data = result.get("reindex_result", {})
    if not total_docs and reindex_data:
        total_docs = reindex_data.get("total_docs", 0)
        total_chunks = reindex_data.get("total_chunks", 0)
        failed = reindex_data.get("failed", 0)

    logger.info("reindex: %d docs, %d chunks, %d failed", total_docs, total_chunks, failed)

    # Шаг 2: Перестройка INDEX.gen.yaml
    index_result = knowledge_index.rebuild_all()
    section_count = len(index_result.get("sections", {}))
    total_entries = index_result.get("root", {}).get("total_entries", 0)

    response = {
        "domain": domain,
        "blue_green": blue_green,
        "total_docs": total_docs,
        "total_chunks": total_chunks,
        "failed": failed,
        "index_sections": section_count,
        "index_total_entries": total_entries,
        "message": f"Reindex complete: {total_chunks} chunks in Qdrant, {section_count} INDEX sections",
    }

    # Для blue-green добавляем информацию о коллекциях
    if blue_green and "active" in result:
        response["collection_active"] = result.get("active")
        response["collection_target"] = result.get("target")
        response["alias_swapped"] = result.get("alias_swapped", False)

    return response
