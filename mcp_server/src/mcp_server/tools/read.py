# ruff: noqa: BLE001, B023
"""A3-A4: get_entry (получение записи) + get_knowledge_map (структурная карта).

get_entry: store.read(knowledge_id) → полная запись (frontmatter + content).
get_knowledge_map: knowledge_index.get_map(domain?) → структурная карта (root или per-section).

Variant A (13.10): get_entry возвращает также title (из markdown-заголовка),
content_type, parent_knowledge_id, sequence_number и children (TOC для коллекций).

M2 (on-the-fly TOC): children теперь из Qdrant scroll (_build_toc), не из frontmatter.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from ..storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone
from .auth_zone import is_subscriber

logger = logging.getLogger("mcp_knowledge.tools.read")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)

# ── M2: on-the-fly TOC кэш ──────────────────────────────────

# Ключ кэша: (zone, collection_id) → (data_version, timestamp, toc_list)
_TOC_CACHE: dict[tuple, tuple[int, float, list[dict]]] = {}
_TOC_TTL: float = 30.0  # секунд — defence-in-depth (P1-3)
MAX_TOC_SECTIONS: int = 10_000  # guard (P1-2)


def _toc_cache_key(zone: str, collection_id: str) -> tuple:
    """Ключ TOC-кэша: (zone, collection_id) — W2: зона входит в ключ."""
    return (zone, collection_id)


def _get_qdrant(app_state):
    """Получить Qdrant-клиент: app_state.qdrant_client (raw) или app_state.qdrant (wrapper)."""
    return getattr(app_state, "qdrant_client", None) or getattr(app_state, "qdrant", None)


async def _build_toc(collection_id: str, app_state, zone: str = ZONE_PRIVATE) -> list[dict]:
    """On-the-fly TOC из Qdrant scroll по parent_knowledge_id.

    Возвращает список [{knowledge_id, title, sequence_number}],
    отсортированный по sequence_number (с fallback по title).

    Кэш: ключ (zone, collection_id, data_version) + TTL {_TOC_TTL}s.
    """
    data_version = getattr(app_state, "data_version", 0)
    now = time.monotonic()

    # Cache hit: data_version совпадает + TTL не истёк
    cached = _TOC_CACHE.get(_toc_cache_key(zone, collection_id))
    if cached and cached[0] == data_version and (now - cached[1]) < _TOC_TTL:
        return cached[2]

    # Scroll по parent_knowledge_id (паттерн crud.py:369-385)
    qdrant_raw = _get_qdrant(app_state)

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    sections: dict[str, dict] = {}  # knowledge_id → {title, sequence_number, _chunk_idx}
    offset = None
    total_scrolled = 0
    loop = asyncio.get_running_loop()

    while True:
        def _scroll():
            return qdrant_raw.scroll(
                scroll_filter=Filter(must=[FieldCondition(
                    key="parent_knowledge_id",
                    match=MatchValue(value=collection_id),
                )]),
                limit=1000,
                offset=offset,
                with_payload=["knowledge_id", "sequence_number", "content", "chunk_index", "updated_at", "section_header"],
                with_vectors=False,
                collection_name=collection_for_zone(zone),
            )

        points, next_offset = await loop.run_in_executor(None, _scroll)

        for point in points:
            # Guard: жёсткий лимит обработанных точек (защита от книг-гигантов)
            if total_scrolled >= MAX_TOC_SECTIONS:
                logger.warning(
                    "_build_toc: hit MAX_TOC_SECTIONS=%d for %s, truncating",
                    MAX_TOC_SECTIONS, collection_id,
                )
                break
            payload = point.payload or {}
            kid = payload.get("knowledge_id", "")
            if not kid:
                continue
            chunk_idx = payload.get("chunk_index", 999)
            # Dedupe: сохраняем точку с минимальным chunk_index (==0 содержит # Title)
            if kid not in sections or chunk_idx < sections[kid].get("_chunk_idx", 999):
                # Title: chunker выносит заголовок секции в section_header (payload content
                # его НЕ содержит); fallback — _derive_title(content) → knowledge_id.
                sec_header = (payload.get("section_header") or "").strip()
                title = sec_header or _derive_title(payload.get("content", ""), kid)
                sections[kid] = {
                    "knowledge_id": kid,
                    "title": title,
                    "sequence_number": payload.get("sequence_number"),
                    "updated_at": payload.get("updated_at", ""),
                    "_chunk_idx": chunk_idx,
                }
            total_scrolled += 1

        # Guard сработал → стоп (вне зависимости от next_offset)
        if total_scrolled >= MAX_TOC_SECTIONS:
            break

        if next_offset is None or not points:
            break
        offset = next_offset

    # Сортировка: sequence_number ASC (None в конец), fallback по title
    toc = list(sections.values())
    missing_seq = [s for s in toc if s.get("sequence_number") is None]
    if missing_seq:
        logger.warning(
            "_build_toc: %d sections without sequence_number for %s, fallback sort by title",
            len(missing_seq), collection_id,
        )
    toc.sort(key=lambda s: (
        s.get("sequence_number") is None,
        s.get("sequence_number") or 0,
        s.get("title", ""),
    ))

    # Очистка служебного поля
    for s in toc:
        s.pop("_chunk_idx", None)

    _TOC_CACHE[_toc_cache_key(zone, collection_id)] = (data_version, now, toc)
    return toc


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
    # Fail-closed (W3 C5): subscriber не видит даже существования private-записей
    if is_subscriber(params) and getattr(fm, "zone", ZONE_PRIVATE) != ZONE_PUBLIC:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}
    children: list[dict] = []
    if getattr(fm, "content_type", None) == "collection":
        # M2: on-the-fly TOC из Qdrant (frontmatter.children — legacy)
        try:
            zone = getattr(fm, "zone", ZONE_PRIVATE)
            if is_subscriber(params):
                zone = ZONE_PUBLIC  # TOC для subscriber строится только по public-зоне
            children = await _build_toc(knowledge_id, app_state, zone=zone)
        except Exception as exc:
            logger.warning("get_entry: _build_toc failed for %s: %s", knowledge_id, exc)
            children = []
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


async def _public_knowledge_map(domain: str | None, app_state) -> dict:
    """Subscriber-safe карта знаний: пагинированный scroll public-коллекции.

    Формат совпадает с KnowledgeIndex.get_map (упрощённый):
    - без domain → {"total_entries", "sections": [{section, files, path}], "how_to_orient"}
    - с domain → {"domain", "files": [{knowledge_id, subject, project, tags}], "total_files"}
    """
    qdrant_raw = _get_qdrant(app_state)
    loop = asyncio.get_running_loop()

    entries: dict[str, dict] = {}  # knowledge_id → payload (dedupe по knowledge_id)
    offset = None

    while True:
        def _scroll(offset=offset):
            return qdrant_raw.scroll(
                collection_name=collection_for_zone(ZONE_PUBLIC),
                limit=1000,
                offset=offset,
                with_payload=["knowledge_id", "domain", "subject", "project", "tags"],
                with_vectors=False,
            )

        points, next_offset = await loop.run_in_executor(None, _scroll)

        for point in points:
            payload = point.payload or {}
            kid = payload.get("knowledge_id", "")
            if not kid:
                continue
            entries.setdefault(kid, payload)

        if next_offset is None or not points:
            break
        offset = next_offset

    all_entries = list(entries.values())

    if domain:
        files = [
            {
                "knowledge_id": p.get("knowledge_id", ""),
                "subject": p.get("subject", ""),
                "project": p.get("project"),
                "tags": p.get("tags", []),
            }
            for p in all_entries
            if p.get("domain") == domain
        ]
        return {
            "domain": domain,
            "files": files,
            "total_files": len(files),
        }

    sections_map: dict[str, int] = {}
    for p in all_entries:
        d = p.get("domain", "") or "_ungrouped"
        sections_map[d] = sections_map.get(d, 0) + 1

    return {
        "total_entries": len(all_entries),
        "sections": [
            {"section": name, "files": count, "path": name}
            for name, count in sorted(sections_map.items())
        ],
        "how_to_orient": (
            "Используйте get_knowledge_map(domain) для просмотра секции. "
            "search_knowledge() для семантического поиска по контенту."
        ),
    }


async def get_knowledge_map(params: dict, app_state) -> dict:
    """Получить структурную карту знаний.

    Без domain → root INDEX.gen.yaml (все секции).
    С domain → per-section _INDEX.gen.yaml (файлы секции).
    In-memory cache (<5ms при попадании).
    Subscriber → _public_knowledge_map (только public-зона, из Qdrant).
    """
    domain = params.get("domain")

    if is_subscriber(params):
        return await _public_knowledge_map(domain, app_state)

    knowledge_index = app_state.knowledge_index

    map_data = knowledge_index.get_map(domain)

    if not map_data:
        if domain:
            return {"domain": domain, "message": f"No entries found for domain '{domain}'", "files": []}
        else:
            return {"message": "Knowledge base is empty. Use write_knowledge to add entries.", "sections": []}

    return map_data
