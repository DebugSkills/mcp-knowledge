"""A3-A4: get_entry (получение записи) + get_knowledge_map (структурная карта).

get_entry: store.read(knowledge_id) → полная запись (frontmatter + content).
get_knowledge_map: knowledge_index.get_map(domain?) → структурная карта (root или per-section).

Variant A (13.10): get_entry возвращает также title (из markdown-заголовка),
content_type, parent_knowledge_id, sequence_number и children (TOC для коллекций).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("mcp_knowledge.tools.read")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _derive_title(content: str, fallback: str) -> str:
    """Извлечь title из первого markdown-заголовка контента.

    Работает для корневых коллекций (`# {title}` в теле) и секций
    (заголовок секции в теле). Fallback — на переданное значение.
    """
    if content:
        m = _HEADING_RE.search(content)
        if m:
            return m.group(1).strip()
    return fallback


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
    children = []
    for child in fm.children or []:
        children.append({
            "knowledge_id": child.get("knowledge_id", ""),
            "title": child.get("title", ""),
            "sequence_number": child.get("sequence_number"),
        })
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
        # Variant A (13.10): информативные поля для UI (список книг + поиск)
        "title": _derive_title(entry.content, fm.knowledge_id),
        "content_type": getattr(fm, "content_type", None),
        "parent_knowledge_id": getattr(fm, "parent_knowledge_id", None),
        "sequence_number": getattr(fm, "sequence_number", None),
        "children": children,
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
