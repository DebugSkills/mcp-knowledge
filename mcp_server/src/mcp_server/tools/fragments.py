# ruff: noqa: BLE001
"""M2: Фрагментные операции с книгами — add/update/delete/find секций.

add_fragment: создание секции в существующей книге (append-only sequence).
update_fragment: обновление секции (title rewrite + optimistic locking).
delete_fragment: удаление секции (soft-delete + Qdrant cleanup, без cascade).
find_fragment: поиск секций внутри книги (обёртка над search_knowledge).

Все write-тулы: store + pipeline + git + INDEX + data_version (паттерн crud.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from ..content.linking import make_knowledge_id
from ..models import KnowledgeEntry, KnowledgeFrontmatter, VersionConflictError
from .read import _HEADING_RE, _build_toc, _get_qdrant
from .search import search_knowledge

logger = logging.getLogger("mcp_knowledge.tools.fragments")


# ── Helpers ──────────────────────────────────────────────────

def _sanitize_fragment_title(title: str) -> str:
    """Санитизация title для markdown-заголовка + slug.

    \\n/\\r → пробел, ведущие # срезаются, длина ≤120 (лимит slugify).
    """
    title = title.replace("\n", " ").replace("\r", " ").strip()
    title = re.sub(r"\s+", " ", title)  # нормализация пробелов
    title = re.sub(r"^#+\s*", "", title)  # срезать ведущие #
    return title[:120].strip()


def _extract_heading(content: str) -> str | None:
    """Извлечь первую строку '# Title' из content."""
    m = _HEADING_RE.search(content)
    return m.group(0) if m else None


def _rewrite_heading(content: str, new_title: str) -> str:
    """Заменить первый # заголовок на new_title (или prepend если нет)."""
    if _HEADING_RE.search(content):
        return _HEADING_RE.sub(f"# {new_title}", content, count=1)
    return f"# {new_title}\n\n{content}"


# ── Tools ────────────────────────────────────────────────────

async def add_fragment(params: dict, app_state) -> dict:
    """Добавить секцию в книгу (append-only, sequence=max+1).

    Параметры: collection_id, title, content (required); tags (optional).
    """
    collection_id = params.get("collection_id", "")
    title = params.get("title", "")
    content = params.get("content", "")
    extra_tags = params.get("tags", [])

    # ── Валидации ──
    if not collection_id:
        return {"error": "Missing required parameter: 'collection_id'"}
    if not title:
        return {"error": "Missing required parameter: 'title'"}
    if not content or not content.strip():  # NH-iter3-1
        return {"error": "Parameter 'content' must not be empty"}

    title = _sanitize_fragment_title(title)
    if not title:
        return {"error": "Title is empty after sanitization"}

    store = app_state.store

    # Валидация root (P0-4, P1-5)
    root = await store.read(collection_id)
    if root is None:
        return {"error": f"Collection not found: '{collection_id}'"}
    root_fm = root.frontmatter
    if getattr(root_fm, "content_type", None) != "collection":
        return {"error": f"'{collection_id}' is not a book collection"}

    # NH-iter2-1: lifecycle-статус канонически в Qdrant payload — deprecate живёт
    # ТОЛЬКО в payload (quality.py:381 set_payload status=deprecated), SSOT
    # frontmatter.status НЕ обновляется. Проверяем payload с fallback на frontmatter.
    root_status = getattr(root_fm, "status", None) or "published"
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        qdrant_raw = _get_qdrant(app_state)
        scroll_kwargs: dict = {}
        if not hasattr(qdrant_raw, "_client"):
            scroll_kwargs["collection_name"] = "knowledge"
        loop = asyncio.get_running_loop()

        def _status_scroll():
            return qdrant_raw.scroll(
                scroll_filter=Filter(must=[FieldCondition(
                    key="knowledge_id", match=MatchValue(value=collection_id),
                )]),
                limit=1,
                with_payload=["status"],
                with_vectors=False,
                **scroll_kwargs,
            )

        points, _ = await loop.run_in_executor(None, _status_scroll)
        for pt in points or []:
            payload_status = (pt.payload or {}).get("status")
            if payload_status:
                root_status = payload_status
                break
    except Exception as exc:
        logger.warning(
            "add_fragment: status check via Qdrant failed, fallback frontmatter: %s", exc,
        )
    if root_status == "deprecated":
        return {"error": f"Cannot add fragment to deprecated collection '{collection_id}'"}

    # Наследуем domain/subject/... от root
    domain = root_fm.domain
    subject = root_fm.subject
    project = root_fm.project
    tags = list(root_fm.tags) + list(extra_tags)
    cross_subjects = root_fm.cross_subjects

    # Sequence = max+1 из TOC (M4 append-only)
    toc = await _build_toc(collection_id, app_state)
    seq = max((s.get("sequence_number") or 0) for s in toc) + 1 if toc else 1

    # ID-формула (P0-2): sha256(body[:200]) — идентично book_preprocessor.py:107-116
    body = f"# {title}\n\n{content}"
    content_hash = hashlib.sha256(body[:200].encode()).hexdigest()
    knowledge_id = make_knowledge_id(domain, subject, title, seq, content_hash)

    # Dup-gate (advisory, best-effort — как в crud.py:101-118)
    quality_duplicates: list[dict] = []
    try:
        from ..quality.dup_gate import check_duplicates

        embedder = getattr(app_state, "embedder", None)
        qdrant_dup = _get_qdrant(app_state)
        if embedder is not None and qdrant_dup is not None:
            quality_duplicates = await check_duplicates(
                body, domain, None,
                embedder=embedder,
                qdrant_client=qdrant_dup,
            )
    except Exception as exc:
        logger.debug("add_fragment: dup-gate skipped: %s", exc)

    # Write: store.write_entry + pipeline.enqueue + flush (1 git-коммит)
    section_fm = KnowledgeFrontmatter(
        knowledge_id=knowledge_id,
        domain=domain,
        subject=subject,
        project=project,
        content_type="book",
        parent_knowledge_id=collection_id,
        sequence_number=seq,
        tags=tags,
        cross_subjects=cross_subjects,
    )
    entry = KnowledgeEntry(frontmatter=section_fm, content=body)
    await store.write_entry(entry)

    pipeline = app_state.pipeline
    wait_ok = False
    try:
        await pipeline.enqueue(entry, wait_for_index=True)
        wait_ok = True
    except Exception as exc:
        logger.error("add_fragment: pipeline.enqueue failed for %s: %s", knowledge_id, exc)

    await store.flush(f"add_fragment: {knowledge_id} (section {seq} of {collection_id})")

    # INDEX update (best-effort)
    try:
        app_state.knowledge_index.update_section(domain)
    except Exception:  # noqa: S110
        pass

    # data_version += 1 (инвалидация TOC-кэша)
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass

    logger.info(
        "add_fragment: %s seq=%d collection=%s wait_for_index=%s",
        knowledge_id, seq, collection_id, wait_ok,
    )
    return {
        "fragment_id": knowledge_id,
        "collection_id": collection_id,
        "sequence_number": seq,
        "indexed": wait_ok,
        "quality_duplicates": quality_duplicates,
    }


async def update_fragment(params: dict, app_state) -> dict:
    """Обновить секцию книги: content и/или title (перезапись # заголовка).

    Параметры: fragment_id (required); content?, title?, version? (optional).
    VersionConflictError → conflict=True ответ.
    """
    fragment_id = params.get("fragment_id", "")
    content = params.get("content")
    title = params.get("title")
    version = params.get("version")  # optimistic locking

    if not fragment_id:
        return {"error": "Missing required parameter: 'fragment_id'"}
    if content is not None and not content.strip():  # NH-iter3-1
        return {"error": "Parameter 'content' must not be empty"}

    store = app_state.store
    entry = await store.read(fragment_id)
    if entry is None:
        return {"error": f"Fragment not found: '{fragment_id}'"}
    if getattr(entry.frontmatter, "parent_knowledge_id", None) is None:  # P1-5
        return {"error": f"'{fragment_id}' is not a book section (no parent_knowledge_id)"}

    # Построить новый контент
    new_content = entry.content
    if title is not None:
        title = _sanitize_fragment_title(title)
        if not title:
            return {"error": "Title is empty after sanitization"}
        new_content = _rewrite_heading(new_content, title)
    if content is not None:
        if title is not None:
            # Заголовок новый + контент новый → полная пересборка
            new_content = f"# {title}\n\n{content}"
        else:
            # Только тело — сохраняем существующий заголовок
            existing_heading = _extract_heading(entry.content)
            new_content = f"{existing_heading}\n\n{content}" if existing_heading else content

    # store.update с optimistic locking (crud.py:274-297)
    try:
        expected_version = int(version) if version is not None else None
    except (ValueError, TypeError):
        return {"error": f"Invalid version: '{version}'"}
    try:
        updated = await store.update(fragment_id, content=new_content, expected_version=expected_version)
    except VersionConflictError as e:
        from ..metrics import optimistic_lock_conflicts
        optimistic_lock_conflicts.inc()
        logger.warning(
            "update_fragment: version conflict for %s (expected=%s, actual=%s)",
            fragment_id, expected_version, e.actual,
        )
        return {
            "message": f"Version conflict: expected v{e.expected}, actual v{e.actual}",
            "fragment_id": fragment_id,
            "expected_version": e.expected,
            "current_version": e.actual,
            "conflict": True,
        }

    # Переиндексация
    pipeline = app_state.pipeline
    enqueued = False
    try:
        await pipeline.enqueue(updated, wait_for_index=params.get("wait_for_index", False))
        enqueued = True
    except Exception as exc:
        logger.error("update_fragment: pipeline.enqueue failed for %s: %s", fragment_id, exc)

    try:
        app_state.knowledge_index.update_section(updated.frontmatter.domain)
    except Exception:  # noqa: S110
        pass

    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass

    logger.info("update_fragment: %s v%d", fragment_id, updated.frontmatter.version)
    return {
        "fragment_id": fragment_id,
        "version": updated.frontmatter.version,
        "updated_at": updated.frontmatter.updated_at.isoformat(),
        "indexed": enqueued,
    }


async def delete_fragment(params: dict, app_state) -> dict:
    """Удалить секцию книги (soft-delete → .trash/ + Qdrant, без cascade)."""
    fragment_id = params.get("fragment_id", "")
    if not fragment_id:
        return {"error": "Missing required parameter: 'fragment_id'"}

    store = app_state.store
    entry = await store.read(fragment_id)
    if entry is None:
        return {"error": f"Fragment not found: '{fragment_id}'"}
    if getattr(entry.frontmatter, "parent_knowledge_id", None) is None:  # P1-5
        return {"error": f"'{fragment_id}' is not a book section (no parent_knowledge_id)"}

    domain = entry.frontmatter.domain
    loop = asyncio.get_running_loop()

    # store.delete (soft-delete → .trash/)
    deleted = await store.delete(fragment_id)
    if not deleted:
        return {"error": f"Delete failed for '{fragment_id}'"}

    # Qdrant delete_by_knowledge_id (паттерн crud.py:418 — без cascade)
    qdrant = _get_qdrant(app_state)
    try:
        await loop.run_in_executor(None, qdrant.delete_by_knowledge_id, fragment_id)
    except Exception as exc:
        logger.warning("delete_fragment: Qdrant delete failed for %s: %s", fragment_id, exc)

    try:
        app_state.knowledge_index.update_section(domain)
    except Exception:  # noqa: S110
        pass

    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass

    logger.info("delete_fragment: %s → .trash/ + Qdrant", fragment_id)
    return {"fragment_id": fragment_id, "deleted": True}


async def find_fragment(params: dict, app_state) -> dict:
    """Найти секции в книге по запросу (обёртка над search_knowledge).

    Параметры: collection_id, query (required); limit? (default 5, max 50).
    """
    collection_id = params.get("collection_id", "")
    query = params.get("query", "")
    limit = min(params.get("limit", 5), 50)

    if not collection_id:
        return {"error": "Missing required parameter: 'collection_id'"}
    if not query:
        return {"error": "Missing required parameter: 'query'"}

    # Валидация коллекции
    store = app_state.store
    root = await store.read(collection_id)
    if root is None or getattr(root.frontmatter, "content_type", None) != "collection":
        return {"error": f"Collection not found: '{collection_id}'"}

    # Делегирование в search_knowledge с collection_id-фильтром
    result = await search_knowledge(
        {"query": query, "collection_id": collection_id, "top_k": limit},
        app_state,
    )
    if "error" in result:
        return result

    fragments = [
        {
            "fragment_id": r.get("knowledge_id", ""),
            "title": r.get("title", ""),
            "sequence_number": r.get("sequence_number"),
            "score": r.get("score", 0),
            "snippet": (r.get("content", "")[:200]),
        }
        for r in result.get("results", [])
    ]

    return {
        "collection_id": collection_id,
        "query": query,
        "fragments": fragments,
        "total": len(fragments),
    }
