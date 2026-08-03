"""Lifecycle slice — 2-state модель published|deprecated (4.7).

План §4.7:
- frontmatter.status: "published" (default, backward-compatible — отсутствие = published)
- "deprecated" — запись скрыта из search_knowledge по умолчанию
- resolve_quality_issue(action=deprecate) → status=deprecated
- resolve_quality_issue(action=restore) → status=published (reversibility)

SSOT: статус в Qdrant payload (ключ: status). Frontmatter файла
опционально дублирует для человекочитаемости, но search-фильтр
читает из Qdrant.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

logger = logging.getLogger("mcp_knowledge.quality.lifecycle")

# ── Типы ─────────────────────────────────────────────────────

LifecycleStatus = Literal["published", "deprecated"]

# ── Константы ────────────────────────────────────────────────

DEFAULT_STATUS: LifecycleStatus = "published"
QDRANT_PAYLOAD_KEY: str = "status"


def get_status(payload: Optional[dict]) -> LifecycleStatus:
    """Извлекает статус из Qdrant payload.

    Отсутствие поля = published (backward-compatible с Фазами 0–3).
    """
    if payload is None:
        return DEFAULT_STATUS
    raw = payload.get(QDRANT_PAYLOAD_KEY)
    if raw not in ("published", "deprecated"):
        return DEFAULT_STATUS
    return raw


def build_search_filter(include_deprecated: bool = False) -> Optional[dict]:
    """Строит Qdrant фильтр для search_knowledge.

    По умолчанию исключает deprecated-записи.
    include_deprecated=True → без фильтра (видны все).

    Returns:
        Qdrant Filter dict (совместимый с qdrant_client Filter / storage.qdrant_client.search
        filters-параметром) или None (без фильтра).
    """
    if include_deprecated:
        return None  # без фильтра — видно всё

    # Исключаем deprecated: status != "deprecated"
    return {
        "must_not": [
            {
                "key": QDRANT_PAYLOAD_KEY,
                "match": {"value": "deprecated"},
            }
        ]
    }


def validate_transition(
    current: LifecycleStatus, target: LifecycleStatus
) -> Optional[str]:
    """Проверяет допустимость перехода.

    Возвращает None если переход допустим, или сообщение об ошибке.
    """
    # Все переходы допустимы (reversibility: restore = deprecated→published)
    # Единственное ограничение: нельзя deprecate уже deprecated
    if current == target:
        return f"Already in status '{target}' — no change needed"
    return None


def make_deprecation_payload_update() -> dict:
    """Payload для Qdrant upsert при deprecate."""
    return {QDRANT_PAYLOAD_KEY: "deprecated"}


def make_restore_payload_update() -> dict:
    """Payload для Qdrant upsert при restore."""
    return {QDRANT_PAYLOAD_KEY: "published"}


def make_published_payload_update() -> dict:
    """Payload для новой записи (published по умолчанию)."""
    return {QDRANT_PAYLOAD_KEY: DEFAULT_STATUS}
