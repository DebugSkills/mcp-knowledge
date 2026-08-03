"""A3-A4: get_entry (получение записи) + get_knowledge_map (структурная карта).

get_entry: store.read(knowledge_id) → полная запись (frontmatter + content).
get_knowledge_map: knowledge_index.get_map(domain?) → структурная карта (root или per-section).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_knowledge.tools.read")


async def get_entry(params: dict, app_state) -> dict:
    """Получить полную запись (frontmatter + Markdown-контент) по knowledge_id."""
    knowledge_id = params.get("knowledge_id", "")
    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}

    store = app_state.store
    entry = await store.read(knowledge_id)

    if entry is None:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    fm = entry.frontmatter
    return {
        "knowledge_id": fm.knowledge_id,
        "domain": fm.domain,
        "subject": fm.subject,
        "project": fm.project,
        "cross_subjects": fm.cross_subjects,
        "tags": fm.tags,
        "version": fm.version,
        "created_at": fm.created_at.isoformat(),
        "updated_at": fm.updated_at.isoformat(),
        "content": entry.content,
    }


async def get_knowledge_map(params: dict, app_state) -> dict:
    """Получить структурную карту знаний.

    Без domain → root INDEX.gen.yaml (все секции).
    С domain → per-section _INDEX.gen.yaml (файлы секции).
    In-memory cache (<5ms при попадании).
    """
    domain = params.get("domain")
    knowledge_index = app_state.knowledge_index

    map_data = knowledge_index.get_map(domain)

    if not map_data:
        if domain:
            return {"domain": domain, "message": f"No entries found for domain '{domain}'", "files": []}
        else:
            return {"message": "Knowledge base is empty. Use write_knowledge to add entries.", "sections": []}

    return map_data
