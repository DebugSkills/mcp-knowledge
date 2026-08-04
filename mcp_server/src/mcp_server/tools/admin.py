"""A8: reindex — перестроить индекс: перечитать все Markdown → переиндексировать в Qdrant.

Flow:
1. pipeline.reindex_blue_green() (default) или pipeline.reindex_all() (legacy)
2. knowledge_index.rebuild_all() → root + per-section INDEX.gen.yaml

Фаза 3 F1: blue_green=True default → zero-downtime через Qdrant aliases.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("mcp_knowledge.tools.admin")


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
