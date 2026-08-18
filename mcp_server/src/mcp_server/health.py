"""Health-check эндпоинты (#10, задача 1.7 + Фаза 3 E1: liveness/readiness split).

Фаза 3 E1 (v1.1): /health/live (liveness, всегда 200) + /health (readiness, HTTP 503 degraded).
Deep checks: qdrant + embed + pipeline worker + queue backlog + DLQ overflow.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .config import settings
from .storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone

logger = logging.getLogger("mcp_knowledge.health")

router = APIRouter(tags=["health"])

# Ссылки на компоненты (заполняются при инициализации в lifespan)
_embedding_manager = None
_qdrant_client = None
_pipeline = None
_reconcile_state = {"state": "pending", "checked": 0, "reindexed": 0, "skipped": 0, "orphans": 0, "error": None}

# Пороги для deep checks
QUEUE_UTILIZATION_THRESHOLD = 0.9  # >90% заполнения → degraded
DLQ_OVERFLOW_THRESHOLD = 10  # >10 записей в DLQ → degraded


def set_embedding_manager(mgr):
    global _embedding_manager
    _embedding_manager = mgr


def set_qdrant_client(client):
    global _qdrant_client
    _qdrant_client = client


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


def set_reconcile_state(state: str, result: dict | None = None, error: str | None = None):
    """Обновить статус фоновой reconciliation (для /health)."""
    global _reconcile_state
    _reconcile_state = {
        "state": state,
        "checked": (result or {}).get("checked", 0),
        "reindexed": (result or {}).get("reindexed", 0),
        "skipped": (result or {}).get("skipped", 0),
        "orphans": (result or {}).get("deleted_orphans", 0),
        "error": error,
    }


# ── Liveness probe (Docker healthcheck) ────────────────────

@router.get("/health/live")
async def health_live():
    """Liveness probe — всегда 200, если FastAPI отвечает.

    Для Docker healthcheck. НЕ зависит от embedder/qdrant/pipeline.
    Предотвращает restart storm при degraded embedder (P1-1 fix).
    """
    return {"status": "alive"}


# ── Readiness probe (load balancer) ────────────────────────

@router.get("/health")
async def health():
    """Readiness probe — deep checks всех бэкендов.

    Возвращает HTTP 503 при degraded (load balancer убирает сервер из пула).
    Возвращает HTTP 200 при healthy.
    """
    checks = await _run_deep_checks()

    # Фаза 12: обновить метрики здоровья после deep checks
    from .metrics import update_health_metrics
    update_health_metrics(checks)

    status = "healthy" if all(c.get("ok", False) for c in checks.values()) else "degraded"
    http_code = 200 if status == "healthy" else 503
    return JSONResponse(
        content={
            "status": status,
            "version": "0.1.0",
            "reconcile": _reconcile_state,
            "checks": checks,
        },
        status_code=http_code,
    )


# ── Deep checks ────────────────────────────────────────────

async def _run_deep_checks() -> dict:
    """Выполнить deep health checks всех компонентов.

    Returns:
        dict: {component: {ok: bool, ...}}
    """
    checks = {}

    # 1. Qdrant reachable
    checks["qdrant"] = _check_qdrant()

    # 2. Embedding backend
    checks["embedding"] = _check_embedding()

    # 3. Pipeline worker alive + queue health + DLQ
    checks["pipeline"] = _check_pipeline()
    checks["dlq"] = _check_dlq()

    return checks


def _check_qdrant() -> dict:
    """Проверить доступность Qdrant (по зонам: public + private)."""
    if _qdrant_client is None:
        return {"ok": False, "connected": False, "error": "client not initialized"}

    try:
        zones = {}
        total_points = 0
        total_vectors = 0
        for zone in (ZONE_PUBLIC, ZONE_PRIVATE):
            info = _qdrant_client.collection_info(collection_name=collection_for_zone(zone))
            points = info.get("points_count") or 0
            vectors = info.get("vectors_count") or 0
            zones[zone] = {
                "collection": collection_for_zone(zone),
                "points": points,
                "vectors": vectors,
            }
            total_points += points
            total_vectors += vectors
        return {
            "ok": True,
            "connected": True,
            "points": total_points,
            "vectors": total_vectors,
            "zones": zones,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("Health: Qdrant unreachable: %s", e)
        return {"ok": False, "connected": False, "error": str(e)[:200]}


def _check_embedding() -> dict:
    """Проверить готовность embedding backend."""
    if _embedding_manager is None:
        return {"ok": False, "loaded": False, "error": "manager not initialized"}

    if not _embedding_manager.is_ready:
        return {
            "ok": False,
            "backend": getattr(_embedding_manager, "backend_name", "unknown"),
            "model": settings.EMBEDDING_MODEL,
            "loaded": False,
        }

    emb_info = _embedding_manager.embed_latency_check()
    return {
        "ok": True,
        "backend": emb_info.get("backend", "unknown"),
        "model": emb_info.get("model", settings.EMBEDDING_MODEL),
        "loaded": True,
        "latency_ms": emb_info.get("latency_ms"),
    }


def _check_pipeline() -> dict:
    """Проверить pipeline worker + queue backlog."""
    if _pipeline is None:
        return {"ok": False, "worker": "not initialized"}

    # Worker alive
    worker_task = getattr(_pipeline, "_worker_task", None)
    worker_alive = worker_task is not None and not worker_task.done()

    if not worker_alive:
        logger.warning("Health: pipeline worker dead or not started")
        return {
            "ok": False,
            "worker": "dead" if worker_task and worker_task.done() else "not started",
            "queue_utilization": None,
            "stats": dict(getattr(_pipeline, "stats", {})),
        }

    # Queue utilization
    queue = getattr(_pipeline, "_queue", None)
    max_queue = getattr(queue, "maxsize", 1) if queue else 1
    qsize = queue.qsize() if queue else 0
    utilization = qsize / max(max_queue, 1)
    overloaded = utilization > QUEUE_UTILIZATION_THRESHOLD

    if overloaded:
        logger.warning("Health: pipeline queue %d/%d (%.0f%%)", qsize, max_queue, utilization * 100)

    return {
        "ok": not overloaded,
        "worker": "alive",
        "queue_size": qsize,
        "queue_max": max_queue,
        "queue_utilization": round(utilization, 3),
        "stats": dict(getattr(_pipeline, "stats", {})),
    }


def _check_dlq() -> dict:
    """Проверить переполнение Dead Letter Queue."""
    if _pipeline is None:
        return {"ok": True, "size": 0, "note": "pipeline not initialized"}

    dlq = getattr(_pipeline, "_dlq", None)
    if dlq is None:
        return {"ok": True, "size": 0, "note": "dlq not attached"}

    dlq_size = dlq.size
    overflow = dlq_size > DLQ_OVERFLOW_THRESHOLD

    if overflow:
        logger.warning("Health: DLQ overflow — %d entries > %d threshold",
                        dlq_size, DLQ_OVERFLOW_THRESHOLD)

    return {
        "ok": not overflow,
        "size": dlq_size,
        "threshold": DLQ_OVERFLOW_THRESHOLD,
    }
