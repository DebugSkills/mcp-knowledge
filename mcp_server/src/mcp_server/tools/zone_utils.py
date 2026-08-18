# ruff: noqa: BLE001
"""W1.7: Монозональность книг — разрешение зоны секции относительно родителя.

Двухконтурная модель доступа (public/private зоны). Private доминирует в ОБЕ стороны:
- own=public при parent=private → private (принуждение вниз) + issue zone_violation (critical)
- own=private при parent=public → private + issue zone_violation (warn) + маркер book_partial_public
- parent=None → own без проверки
- неизвестное значение zone → ValueError
"""

from __future__ import annotations

import logging

from ..quality.issues import Issue, create_issue_async

logger = logging.getLogger("mcp_knowledge.tools.zone_utils")

VALID_ZONES = ("public", "private")


def resolve_zone(zone: str | None, parent_zone: str | None) -> tuple[str, bool]:
    """Разрешить итоговую зону записи с учётом зоны родителя (монозональность).

    Args:
        zone: запрошенная зона записи (None → private по умолчанию)
        parent_zone: зона родительской книги (None — записи без родителя)

    Returns:
        (final_zone, was_forced): итоговая зона + флаг принуждения вниз
        (own=public при private-родителе).

    Raises:
        ValueError: неизвестное значение zone.
    """
    if zone is not None and zone not in VALID_ZONES:
        raise ValueError(f"zone '{zone}' must be 'public' or 'private'")
    final = zone or "private"
    if parent_zone is None:
        return final, False
    if final == "public" and parent_zone == "private":
        return "private", True
    return final, False


def is_partial_public(zone: str | None, parent_zone: str | None) -> bool:
    """own=private при public-родителе → книга частично публична (маркер W1.7)."""
    return zone == "private" and parent_zone == "public"


async def emit_zone_violation(
    knowledge_id: str,
    forced: bool,
    parent_zone: str | None,
    own_zone: str | None,
    partial_public: bool,
) -> Issue | None:
    """Эмиссия issue zone_violation (async-safe, best-effort — не роняем write-путь).

    Severity: critical при принуждении вниз (own=public→private),
    warn при partial-public (own=private при public-родителе).
    """
    severity = "critical" if forced else "warn"
    detail = f"zone conflict: own='{own_zone}' vs parent='{parent_zone}'"
    metadata: dict = {}
    if forced:
        metadata["zone_forced"] = True
    if partial_public:
        metadata["book_partial_public"] = True
    try:
        return await create_issue_async(
            "zone_violation", knowledge_id, severity, detail, metadata=metadata,
        )
    except Exception as exc:
        logger.warning(
            "zone_violation issue emission failed for %s: %s", knowledge_id, exc,
        )
        return None
