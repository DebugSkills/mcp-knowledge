"""Quality MCP Tools — review_queue, list_quality_issues, resolve_quality_issue (4.6).

Thin wrappers: делегируют доменную логику в quality/ пакет.
Регистрируются в tools/__init__.py → TOOLS + TOOL_HANDLERS.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server.quality.issues import list_issues, update_issue_status
from mcp_server.quality.scoring import REVIEW_THRESHOLD, staleness_score, StalenessInput

logger = logging.getLogger("mcp_knowledge.tools.quality")

# ── Допустимые action для resolve_quality_issue ──────────────────────────────
VALID_ACTIONS: frozenset[str] = frozenset({"merge", "deprecate", "restore", "resolve", "ignore"})


async def review_queue(params: dict, app_state) -> dict:
    """Получить топ устаревших записей (по staleness_score DESC).

    Читает staleness_score из Qdrant payload, сортирует DESC,
    возвращает top-N для review.

    Args:
        params:
            domain (optional): фильтр по домену
            subject (optional): фильтр по subject
            limit (default 20): макс. число записей
    """
    domain = params.get("domain")
    subject = params.get("subject")
    limit = min(params.get("limit", 20), 100)

    try:
        client = app_state.qdrant_client
        # Qdrant scroll с фильтром и сортировкой по payload.staleness_score DESC
        from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

        must_conditions: list[FieldCondition] = []
        if domain:
            must_conditions.append(
                FieldCondition(key="domain", match=MatchValue(value=domain))
            )
        if subject:
            must_conditions.append(
                FieldCondition(key="subject", match=MatchValue(value=subject))
            )

        # Добавляем условие: staleness_score существует (не null)
        must_conditions.append(
            FieldCondition(
                key="staleness_score",
                range=Range(gte=0.0),
            )
        )

        scroll_filter = Filter(must=must_conditions) if must_conditions else None

        # Scroll все точки с staleness_score, затем сортируем в Python
        points, _ = client.scroll(
            collection_name="knowledge",
            scroll_filter=scroll_filter,
            limit=limit * 3,  # берём с запасом для сортировки
            with_payload=True,
            with_vectors=False,
        )

        # Сортируем DESC по staleness_score
        scored = []
        for point in points:
            payload = point.payload or {}
            score = payload.get("staleness_score", 0.0)
            if score >= REVIEW_THRESHOLD:
                scored.append({
                    "knowledge_id": payload.get("knowledge_id", str(point.id)),
                    "title": payload.get("subject", "") + "/" + payload.get("knowledge_id", ""),
                    "staleness_score": score,
                    "reasons": payload.get("quality_flags", []),
                    "updated_at": payload.get("updated_at", ""),
                })

        scored.sort(key=lambda x: x["staleness_score"], reverse=True)
        result = scored[:limit]

        logger.info("review_queue: %d records returned (domain=%s)", len(result), domain)
        return {"queue": result, "total_in_queue": len(scored)}

    except Exception as exc:
        logger.error("review_queue failed: %s", exc)
        return {"queue": [], "error": str(exc)}


async def list_quality_issues(params: dict, app_state) -> dict:
    """Получить список quality issues с фильтрацией.

    Args:
        params:
            types (optional): список типов (duplicate, missing_field, edit_war, broken_link, conflicting)
            status (default "open"): open | resolved | ignored
            limit (default 50): макс. число
    """
    types = params.get("types")
    status = params.get("status", "open")
    limit = min(params.get("limit", 50), 200)

    try:
        issues = list_issues(types=types, status=status, limit=limit)
        result = [
            {
                "issue_id": i.issue_id,
                "type": i.type,
                "knowledge_id": i.knowledge_id,
                "severity": i.severity,
                "detail": i.detail,
                "detected_at": i.detected_at.isoformat() if i.detected_at else None,
                "status": i.status,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                "resolution": i.resolution,
            }
            for i in issues
        ]
        logger.info("list_quality_issues: %d issues (status=%s)", len(result), status)
        return {"issues": result, "total": len(result)}

    except Exception as exc:
        logger.error("list_quality_issues failed: %s", exc)
        return {"issues": [], "error": str(exc)}


async def resolve_quality_issue(params: dict, app_state) -> dict:
    """Разрешить quality issue: resolve/ignore/merge/deprecate/restore.

    Args:
        params:
            issue_id (str): ID issue для разрешения
            action (str): merge | deprecate | restore | resolve | ignore
            target_id (optional str): target knowledge_id для merge
            reason (optional str): причина решения
    """
    issue_id = params.get("issue_id", "")
    action = params.get("action", "")
    target_id = params.get("target_id")
    reason = params.get("reason", "")

    # Валидация
    if not issue_id:
        return {"resolved": False, "error": "issue_id is required"}

    if action not in VALID_ACTIONS:
        return {
            "resolved": False,
            "error": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(VALID_ACTIONS))}",
        }

    side_effects: list[str] = []

    try:
        if action == "resolve":
            updated = update_issue_status(issue_id, "resolved", reason)
            return {"resolved": True, "issue_id": issue_id, "status": "resolved", "side_effects": side_effects}

        elif action == "ignore":
            updated = update_issue_status(issue_id, "ignored", reason)
            return {"resolved": True, "issue_id": issue_id, "status": "ignored", "side_effects": side_effects}

        elif action == "deprecate":
            # TODO: lifecycle integration (4.7) — set status=deprecated in frontmatter + Qdrant payload
            updated = update_issue_status(issue_id, "resolved", reason or "deprecated")
            side_effects.append("marked issue as resolved; lifecycle deprecation pending (4.7)")
            return {"resolved": True, "issue_id": issue_id, "status": "resolved", "side_effects": side_effects}

        elif action == "restore":
            # TODO: lifecycle integration (4.7) — set status=published
            updated = update_issue_status(issue_id, "resolved", reason or "restored")
            side_effects.append("marked issue as resolved; lifecycle restore pending (4.7)")
            return {"resolved": True, "issue_id": issue_id, "status": "resolved", "side_effects": side_effects}

        elif action == "merge":
            if not target_id:
                return {"resolved": False, "error": "target_id is required for merge action"}
            # TODO: lifecycle + markdown merge (4.7) — merge content into target, deprecate source
            updated = update_issue_status(issue_id, "resolved", f"merged into {target_id}. {reason}")
            side_effects.append(f"marked issue as resolved; content merge pending (4.7)")
            return {
                "resolved": True,
                "issue_id": issue_id,
                "status": "resolved",
                "side_effects": side_effects,
            }

    except Exception as exc:
        logger.error("resolve_quality_issue(%s, %s) failed: %s", issue_id, action, exc)
        return {"resolved": False, "error": str(exc)}

    return {"resolved": False, "error": f"Unknown action: {action}"}
