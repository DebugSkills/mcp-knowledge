"""B3: MCP Resources — kb:// URI scheme.

kb:// → список доменов (root)
kb://{domain} → список subjects
kb://{domain}/{subject} → список knowledge_ids

Агрегация через app.state.qdrant.scroll() по payload-полям domain, subject, knowledge_id.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

logger = logging.getLogger("mcp_knowledge.resources")

# ── Resource definitions for resources/list ────────────────

RESOURCES = [
    {
        "uri": "kb://",
        "name": "Knowledge Base Root",
        "description": "Корень базы знаний. Содержит список всех доменов.",
        "mimeType": "application/json",
    },
    {
        "uri": "kb://{domain}",
        "name": "Domain Index",
        "description": "Список subjects в указанном домене.",
        "mimeType": "application/json",
    },
    {
        "uri": "kb://{domain}/{subject}",
        "name": "Subject Index",
        "description": "Список knowledge_id в указанном domain/subject.",
        "mimeType": "application/json",
    },
]


def _parse_kb_uri(uri: str) -> tuple[str, ...] | None:
    """Разобрать kb:// URI в компоненты пути.

    Returns:
        Кортеж компонентов (domain, subject) или None если формат неверный.
    """
    if not uri.startswith("kb://"):
        return None

    path = uri[5:]  # отрезаем "kb://"
    if not path:
        return ()  # root: kb://

    # Декодируем URL-encoded компоненты
    parts = tuple(unquote(p) for p in path.rstrip("/").split("/") if p)
    if len(parts) > 2:
        return None  # максимум 2 уровня: domain/subject

    return parts


async def get_kb_resource(uri: str, app_state) -> dict:
    """Получить содержимое kb:// ресурса.

    Args:
        uri: kb:// URI (kb://, kb://{domain}, kb://{domain}/{subject})
        app_state: FastAPI app.state с qdrant, store

    Returns:
        {"uri": str, "contents": [...], "mimeType": "application/json"}
    """
    parts = _parse_kb_uri(uri)
    if parts is None:
        return {"uri": uri, "contents": [], "error": f"Invalid kb:// URI: {uri}"}

    qdrant = app_state.qdrant

    if len(parts) == 0:
        # kb:// → список доменов
        domains = await _collect_unique_values(qdrant, "domain")
        domains_sorted = sorted(domains)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "contents": [
                {"uri": f"kb://{d}", "name": d, "type": "domain"}
                for d in domains_sorted
            ],
        }

    elif len(parts) == 1:
        # kb://{domain} → список subjects
        domain = parts[0]
        subjects = await _collect_unique_values(qdrant, "subject", domain_filter=domain)
        subjects_sorted = sorted(subjects)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "domain": domain,
            "contents": [
                {"uri": f"kb://{domain}/{s}", "name": s, "type": "subject"}
                for s in subjects_sorted
            ],
        }

    elif len(parts) == 2:
        # kb://{domain}/{subject} → список knowledge_ids
        domain, subject = parts
        knowledge_ids = await _collect_knowledge_ids(qdrant, domain, subject)
        return {
            "uri": uri,
            "mimeType": "application/json",
            "domain": domain,
            "subject": subject,
            "contents": [
                {"uri": f"kb://{domain}/{subject}/{kid}", "name": kid, "type": "knowledge_entry"}
                for kid in sorted(knowledge_ids)
            ],
        }

    return {"uri": uri, "contents": [], "error": "Unreachable"}


async def _collect_unique_values(
    qdrant,
    field: str,
    domain_filter: str | None = None,
    max_points: int = 10_000,
) -> set[str]:
    """Собрать уникальные значения поля через Qdrant scroll().

    Args:
        qdrant: QdrantClient instance
        field: payload-поле для агрегации (domain, subject)
        domain_filter: опциональный фильтр по domain
        max_points: максимум точек для обхода

    Returns:
        Множество уникальных значений
    """
    values: set[str] = set()
    offset = None
    total_scanned = 0

    from qdrant_client.http import models as qmodels

    while total_scanned < max_points:
        # Строим фильтр
        scroll_filter = None
        if domain_filter:
            scroll_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="domain",
                        match=qmodels.MatchValue(value=domain_filter),
                    )
                ]
            )

        points, offset = qdrant._client.scroll(
            collection_name="knowledge",
            limit=1000,
            offset=offset,
            scroll_filter=scroll_filter,
            with_payload=qmodels.WithPayloadSelector(include=[field]),
            with_vectors=False,
        )

        for point in points:
            if point.payload:
                val = point.payload.get(field)
                if val and isinstance(val, str):
                    values.add(val)

        total_scanned += len(points)
        if offset is None or len(points) == 0:
            break

    logger.debug(
        "_collect_unique_values: field=%s, domain=%s, scanned=%d, unique=%d",
        field,
        domain_filter or "*",
        total_scanned,
        len(values),
    )
    return values


async def _collect_knowledge_ids(
    qdrant,
    domain: str,
    subject: str,
    max_points: int = 10_000,
) -> set[str]:
    """Собрать knowledge_id для конкретного domain/subject."""
    from qdrant_client.http import models as qmodels

    ids: set[str] = set()
    offset = None
    total_scanned = 0

    scroll_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="domain",
                match=qmodels.MatchValue(value=domain),
            ),
            qmodels.FieldCondition(
                key="subject",
                match=qmodels.MatchValue(value=subject),
            ),
        ]
    )

    while total_scanned < max_points:
        points, offset = qdrant._client.scroll(
            collection_name="knowledge",
            limit=1000,
            offset=offset,
            scroll_filter=scroll_filter,
            with_payload=qmodels.WithPayloadSelector(include=["knowledge_id"]),
            with_vectors=False,
        )

        for point in points:
            if point.payload:
                kid = point.payload.get("knowledge_id")
                if kid and isinstance(kid, str):
                    ids.add(kid)

        total_scanned += len(points)
        if offset is None or len(points) == 0:
            break

    logger.debug(
        "_collect_knowledge_ids: domain=%s, subject=%s, scanned=%d, unique=%d",
        domain,
        subject,
        total_scanned,
        len(ids),
    )
    return ids
