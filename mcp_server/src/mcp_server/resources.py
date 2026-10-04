"""B3: MCP Resources — kb:// URI scheme.

kb:// → список доменов (root)
kb://{domain} → список subjects
kb://{domain}/{subject} → список knowledge_ids

Агрегация через app.state.qdrant.scroll() по payload-полям domain, subject, knowledge_id.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from .storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone
from .tools.auth_zone import zones_from_auth

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


async def get_kb_resource(uri: str, app_state, auth=None) -> dict:
    """Получить содержимое kb:// ресурса.

    P2-1 (bibliography B2): зона по политике — admin (write) видит обе зоны,
    ниже admin (read/import/editor) — только public. Жёсткий ZONE_PRIVATE убран.

    Args:
        uri: kb:// URI (kb://, kb://{domain}, kb://{domain}/{subject})
        app_state: FastAPI app.state с qdrant, store
        auth: AuthInfo из get_auth(request) (None → public-only, fail-closed)

    Returns:
        {"uri": str, "contents": [...], "mimeType": "application/json"}
    """
    parts = _parse_kb_uri(uri)
    if parts is None:
        return {"uri": uri, "contents": [], "error": f"Invalid kb:// URI: {uri}"}

    qdrant = app_state.qdrant
    zones = zones_from_auth({"zone": "both", "_auth": auth})

    if len(parts) == 0:
        # kb:// → список доменов
        domains = await _collect_unique_values(qdrant, "domain", zones=zones)
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
        subjects = await _collect_unique_values(qdrant, "subject", domain_filter=domain, zones=zones)
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
        knowledge_ids = await _collect_knowledge_ids(qdrant, domain, subject, zones=zones)
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
    zones: list[str] | None = None,
) -> set[str]:
    """Собрать уникальные значения поля через Qdrant scroll() по зонам.

    Args:
        qdrant: QdrantClient instance
        field: payload-поле для агрегации (domain, subject)
        domain_filter: опциональный фильтр по domain
        max_points: максимум точек для обхода (на зону)
        zones: зоны данных (default: обе зоны; из auth-политики — W3/P2-1)

    Returns:
        Множество уникальных значений (union по зонам)
    """
    if zones is None:
        zones = [ZONE_PUBLIC, ZONE_PRIVATE]
    values: set[str] = set()

    from qdrant_client.http import models as qmodels

    for zone in zones:
        offset = None
        total_scanned = 0

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
                collection_name=collection_for_zone(zone),
                limit=1000,
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=qmodels.PayloadSelectorInclude(include=[field]),
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
        "_collect_unique_values: field=%s, domain=%s, zones=%s, unique=%d",
        field,
        domain_filter or "*",
        zones,
        len(values),
    )
    return values


async def _collect_knowledge_ids(
    qdrant,
    domain: str,
    subject: str,
    max_points: int = 10_000,
    zones: list[str] | None = None,
) -> set[str]:
    """Собрать knowledge_id для конкретного domain/subject по зонам."""
    from qdrant_client.http import models as qmodels

    if zones is None:
        zones = [ZONE_PUBLIC, ZONE_PRIVATE]
    ids: set[str] = set()

    for zone in zones:
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
                collection_name=collection_for_zone(zone),
                limit=1000,
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=qmodels.PayloadSelectorInclude(include=["knowledge_id"]),
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
        "_collect_knowledge_ids: domain=%s, subject=%s, zones=%s, unique=%d",
        domain,
        subject,
        zones,
        len(ids),
    )
    return ids
