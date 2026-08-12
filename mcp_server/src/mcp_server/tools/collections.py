# ruff: noqa: BLE001
"""list_collections MCP Tool (#18) — список книг/коллекций с метаданными.

Variant A (Surface & Enrich): новый tool для получения списка всех коллекций
(content_type="collection") с title, domain/subject, tags и счётчиком секций.

Flow:
  1. Qdrant scroll (filter content_type=collection, опционально domain) → точки
  2. Dedupe by knowledge_id (у коллекции может быть несколько чанков)
  3. For each: _build_toc() → section_count = len(toc), updated_at = max(секций)
  4. title — из markdown-заголовка контента (общий helper _derive_title)
"""

from __future__ import annotations

import asyncio
import logging

from .read import _build_toc, _derive_title

logger = logging.getLogger("mcp_knowledge.tools.collections")

_COLLECTION_PAYLOAD_FIELDS = [
    "knowledge_id", "domain", "subject", "project",
    "tags", "updated_at", "content",
]


async def list_collections(params: dict, app_state) -> dict:
    """Получить список всех книг/коллекций с базовой информацией.

    Args:
        params: {
            domain? (str): фильтр по домену
            cursor? (str): курсор пагинации (пока не реализован — next_cursor=None)
            limit? (int): макс. результатов (default 100, max 500)
        }
        app_state: Application state (qdrant, store)

    Returns:
        {
            results: [{collection_id, title, domain, subject, project,
                       tags, section_count, updated_at}]
            next_cursor: str|None
            total: int
        }
    """
    limit = min(params.get("limit", 100), 500)
    domain = params.get("domain")

    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    # Qdrant scroll: content_type=collection (+ опционально domain)
    def _scroll() -> list:
        from qdrant_client.http import models as qmodels

        conditions = [
            qmodels.FieldCondition(
                key="content_type",
                match=qmodels.MatchValue(value="collection"),
            )
        ]
        if domain:
            conditions.append(
                qmodels.FieldCondition(
                    key="domain",
                    match=qmodels.MatchValue(value=domain),
                )
            )
        points, _next_offset = qdrant.scroll(
            scroll_filter=qmodels.Filter(must=conditions),
            limit=limit * 2,  # overscan для dedupe по knowledge_id
            with_payload=_COLLECTION_PAYLOAD_FIELDS,
            with_vectors=False,
        )
        return points

    points = await loop.run_in_executor(None, _scroll)

    # Dedupe by knowledge_id (несколько чанков на коллекцию)
    seen: set[str] = set()
    payloads: list[dict] = []
    for point in points:
        payload = point.payload or {}
        kid = payload.get("knowledge_id", "")
        if kid and kid not in seen:
            seen.add(kid)
            payloads.append(payload)

    results: list[dict] = []
    for payload in payloads[:limit]:
        kid = payload.get("knowledge_id", "")
        section_count = 0
        max_updated = payload.get("updated_at", "")
        try:
            toc = await _build_toc(kid, app_state)
            section_count = len(toc)
            # P1-4: updated_at = max(секций) из TOC scroll
            sec_timestamps = [s.get("updated_at", "") for s in toc if s.get("updated_at")]
            if sec_timestamps:
                max_updated = max(sec_timestamps)
        except Exception as exc:
            logger.debug("list_collections: _build_toc failed for %s: %s", kid, exc)

        results.append({
            "collection_id": kid,
            "title": _derive_title(payload.get("content", ""), kid),
            "domain": payload.get("domain", ""),
            "subject": payload.get("subject", ""),
            "project": payload.get("project"),
            "tags": payload.get("tags", []),
            "section_count": section_count,
            "updated_at": max_updated,
        })

    logger.info(
        "list_collections: domain=%s found=%d returned=%d",
        domain or "all", len(payloads), len(results),
    )
    return {
        "results": results,
        "next_cursor": None,  # пагинация: cursor не реализован (коллекций мало)
        "total": len(payloads),
    }
