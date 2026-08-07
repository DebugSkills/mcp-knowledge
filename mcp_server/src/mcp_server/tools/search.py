"""A1-A2: search_knowledge (семантический поиск) + search_by_tags (поиск по тегам).

search_knowledge: query → embed (in-process) → Qdrant search с фильтрами, top_k=5, score_threshold.
search_by_tags: exhaustive поиск через Qdrant payload filter по tags[]. AND/OR семантика. Без GPU.

Issue-#5-fix: latency tracking — search_latency и tag_search_latency гистограммы.
Variant A (13.10): результаты обогащены title/parent_knowledge_id/content_type;
фильтры collection_id (→ parent_knowledge_id) и content_type.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..metrics import search_latency, tag_search_latency
from .read import _derive_title

logger = logging.getLogger("mcp_knowledge.tools.search")


async def search_knowledge(params: dict, app_state) -> dict:
    """Семантический поиск по базе знаний.

    Flow: query → embed (run_in_executor) → Qdrant search → форматирование результатов.

    Фаза 13.14: deprecated-записи исключаются из поиска по умолчанию
    (include_deprecated=False). Использует exclude_statuses=["deprecated"]
    в qdrant.search() через must_not по полю status.
    """
    query = params.get("query", "")
    if not query:
        return {"error": "Missing required parameter: 'query'"}

    top_k = min(params.get("top_k", 5), 50)
    score_threshold = params.get("score_threshold", 0.0)

    # Строим фильтры
    filters = {}
    for key in ("domain", "subject", "project"):
        val = params.get(key)
        if val:
            filters[key] = val

    tags = params.get("tags")
    if tags and isinstance(tags, list):
        filters["tags"] = tags  # OR-семантика через MatchAny в search()

    # Variant A (13.10): поиск внутри книги / по типу контента
    collection_id = params.get("collection_id")
    if collection_id:
        filters["parent_knowledge_id"] = collection_id
    content_type = params.get("content_type")
    if content_type:
        filters["content_type"] = content_type
    # Root-заглушки коллекций (content_type=collection) — шум в результатах:
    # исключаем по умолчанию, если пользователь явно не ищет коллекции.
    exclude_content_types = None if content_type == "collection" else ["collection"]

    # Фаза 13.14: исключаем deprecated-записи из поиска по умолчанию
    include_deprecated = params.get("include_deprecated", False)
    exclude_statuses = None if include_deprecated else ["deprecated"]

    # Embedding (CPU-bound → run_in_executor)
    embedder = app_state.embedder
    loop = asyncio.get_running_loop()
    vector = await loop.run_in_executor(None, embedder.embed_sync, query)

    # Qdrant search (Issue-#5-fix: latency tracking)
    t0 = time.monotonic()
    qdrant = app_state.qdrant
    filter_dict = filters if filters else None
    results = await loop.run_in_executor(
        None,
        lambda: qdrant.search(
            vector=vector,
            top_k=top_k,
            filters=filter_dict,
            score_threshold=score_threshold,
            exclude_content_types=exclude_content_types,
            exclude_statuses=exclude_statuses,
        ),
    )

    # Форматирование результатов
    formatted = []
    for point in results:
        payload = point.payload or {}
        formatted.append({
            "knowledge_id": payload.get("knowledge_id", ""),
            "chunk_id": payload.get("chunk_id", str(point.id)),
            "content": payload.get("content", ""),
            "score": round(point.score, 4),
            "section_header": payload.get("section_header", ""),
            "domain": payload.get("domain", ""),
            "subject": payload.get("subject", ""),
            "tags": payload.get("tags", []),
            # Variant A (13.10): title + какая книга (parent) + тип
            "title": (
                payload.get("section_header")
                or _derive_title(payload.get("content", ""), payload.get("knowledge_id", ""))
            ),
            "parent_knowledge_id": payload.get("parent_knowledge_id"),
            "content_type": payload.get("content_type"),
        })

    search_elapsed = time.monotonic() - t0
    search_latency.observe(search_elapsed)
    logger.info(
        "search_knowledge: query='%s', top_k=%d, found=%d, latency=%.3fs",
        query[:80], top_k, len(formatted), search_elapsed,
    )
    return {"query": query, "results": formatted, "total": len(formatted)}


async def search_by_tags(params: dict, app_state) -> dict:
    """Поиск записей по тегам через Qdrant payload filter (без GPU).

    match_all=True → AND (все теги), match_all=False → OR (любой тег).
    """
    tags = params.get("tags", [])
    if not tags or not isinstance(tags, list):
        return {"error": "Missing required parameter: 'tags' (non-empty list)"}

    match_all = params.get("match_all", True)
    limit = min(params.get("limit", 500), 1000)

    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    # Issue-#5-fix: latency tracking
    t0 = time.monotonic()
    results = await loop.run_in_executor(
        None,
        lambda: qdrant.search_by_tags(
            tags=tags,
            match_all=match_all,
            limit=limit,
        ),
    )

    formatted = []
    for point in results:
        payload = point.payload or {}
        formatted.append({
            "knowledge_id": payload.get("knowledge_id", ""),
            "chunk_id": payload.get("chunk_id", str(point.id)),
            "content": payload.get("content", ""),
            "domain": payload.get("domain", ""),
            "subject": payload.get("subject", ""),
            "tags": payload.get("tags", []),
            "section_header": payload.get("section_header", ""),
        })

    truncated = len(results) >= limit
    tag_elapsed = time.monotonic() - t0
    tag_search_latency.observe(tag_elapsed)
    logger.info(
        "search_by_tags: tags=%s, match_all=%s, found=%d, truncated=%s, latency=%.3fs",
        tags, match_all, len(formatted), truncated, tag_elapsed,
    )
    return {
        "tags": tags,
        "match_all": match_all,
        "results": formatted,
        "total": len(formatted),
        "truncated": truncated,
    }
