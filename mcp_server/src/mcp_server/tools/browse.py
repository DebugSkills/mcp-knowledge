"""A7: list_domains / list_subjects / list_projects — структурная навигация.

Агрегация уникальных domain/subject/project через QdrantClient.scroll_unique_values().
Cursor-based пагинация (P1-3): next_cursor = Qdrant offset.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("mcp_knowledge.tools.browse")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_SCAN = 50_000  # configurable limit for large knowledge bases


async def list_domains(params: dict, app_state) -> dict:
    """Список всех доменов знаний с пагинацией."""
    cursor = params.get("cursor")
    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)

    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    domains, next_cursor, total = await loop.run_in_executor(
        None,
        lambda: qdrant.scroll_unique_values(
            "domain", cursor=cursor, limit=limit, max_scan=MAX_SCAN,
        ),
    )

    logger.info("list_domains: found=%d, total=%d, cursor=%s", len(domains), total, next_cursor)
    return {
        "results": sorted(domains),
        "next_cursor": next_cursor,
        "total_count": total,
    }


async def list_subjects(params: dict, app_state) -> dict:
    """Список subjects в заданном домене (или все) с пагинацией."""
    domain = params.get("domain")
    cursor = params.get("cursor")
    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)

    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    subjects, next_cursor, total = await loop.run_in_executor(
        None,
        lambda: qdrant.scroll_unique_values(
            "subject",
            domain_filter=domain,
            cursor=cursor,
            limit=limit,
            max_scan=MAX_SCAN,
        ),
    )

    logger.info(
        "list_subjects: domain=%s, found=%d, total=%d",
        domain or "*", len(subjects), total,
    )
    return {
        "domain": domain,
        "results": sorted(subjects),
        "next_cursor": next_cursor,
        "total_count": total,
    }


async def list_projects(params: dict, app_state) -> dict:
    """Список проектов (опционально: в заданном domain/subject) с пагинацией."""
    domain = params.get("domain")
    subject = params.get("subject")
    cursor = params.get("cursor")
    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)

    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    projects, next_cursor, total = await loop.run_in_executor(
        None,
        lambda: qdrant.scroll_unique_values(
            "project",
            domain_filter=domain,
            subject_filter=subject,
            cursor=cursor,
            limit=limit,
            max_scan=MAX_SCAN,
        ),
    )

    logger.info(
        "list_projects: domain=%s, subject=%s, found=%d, total=%d",
        domain or "*", subject or "*", len(projects), total,
    )
    return {
        "domain": domain,
        "subject": subject,
        "results": sorted([p for p in projects if p]),
        "next_cursor": next_cursor,
        "total_count": total,
    }
