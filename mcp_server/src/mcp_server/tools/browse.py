"""A7: list_domains / list_subjects / list_projects — структурная навигация.

Агрегация уникальных domain/subject/project через QdrantClient.scroll_unique_values().
Cursor-based пагинация (P1-3): next_cursor = Qdrant offset.
W3 C5: агрегация через все разрешённые зоны (subscriber → только public).
"""

from __future__ import annotations

import asyncio
import logging

from ..storage.schema import collection_for_zone
from .auth_zone import zones_from_auth

logger = logging.getLogger("mcp_knowledge.tools.browse")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_SCAN = 50_000  # configurable limit for large knowledge bases


async def _unique_values_across_zones(
    qdrant, loop, params: dict, field: str,
    domain_filter: str | None, subject_filter: str | None,
) -> tuple[list[str], str | None, int]:
    """W3 C5: union уникальных значений field через все разрешённые зоны.

    zones_from_auth(params): subscriber → только public, operator → обе зоны.
    cursor передаётся только первой зоне; next_cursor — с первой зоны, total — сумма.
    """
    cursor = params.get("cursor")
    limit = min(params.get("limit", DEFAULT_LIMIT), MAX_LIMIT)
    zones = zones_from_auth(params)

    seen: set[str] = set()
    first_next_cursor: str | None = None
    total_sum = 0

    for zone in zones:
        def _scroll(cursor=cursor, z=zone):
            return qdrant.scroll_unique_values(
                field,
                domain_filter=domain_filter,
                subject_filter=subject_filter,
                cursor=cursor,
                limit=limit,
                max_scan=MAX_SCAN,
                collection_name=collection_for_zone(z),
            )

        values, next_cursor, total = await loop.run_in_executor(None, _scroll)
        if first_next_cursor is None:
            first_next_cursor = next_cursor
        total_sum += total
        seen.update(values)

        cursor = None  # cursor только для первой зоны

    return sorted(seen), first_next_cursor, total_sum


async def list_domains(params: dict, app_state) -> dict:
    """Список всех доменов знаний с пагинацией."""
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    domains, next_cursor, total = await _unique_values_across_zones(
        qdrant, loop, params, "domain", None, None,
    )

    logger.info("list_domains: found=%d, total=%d, cursor=%s", len(domains), total, next_cursor)
    return {
        "results": domains,
        "next_cursor": next_cursor,
        "total_count": total,
    }


async def list_subjects(params: dict, app_state) -> dict:
    """Список subjects в заданном домене (или все) с пагинацией."""
    domain = params.get("domain")
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    subjects, next_cursor, total = await _unique_values_across_zones(
        qdrant, loop, params, "subject", domain, None,
    )

    logger.info(
        "list_subjects: domain=%s, found=%d, total=%d",
        domain or "*", len(subjects), total,
    )
    return {
        "domain": domain,
        "results": subjects,
        "next_cursor": next_cursor,
        "total_count": total,
    }


async def list_projects(params: dict, app_state) -> dict:
    """Список проектов (опционально: в заданном domain/subject) с пагинацией."""
    domain = params.get("domain")
    subject = params.get("subject")
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    projects, next_cursor, total = await _unique_values_across_zones(
        qdrant, loop, params, "project", domain, subject,
    )

    logger.info(
        "list_projects: domain=%s, subject=%s, found=%d, total=%d",
        domain or "*", subject or "*", len(projects), total,
    )
    return {
        "domain": domain,
        "subject": subject,
        "results": [p for p in projects if p],
        "next_cursor": next_cursor,
        "total_count": total,
    }
