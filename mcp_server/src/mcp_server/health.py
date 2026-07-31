"""Health-check эндпоинты (#10, задача 1.7)."""
import logging

from fastapi import APIRouter

logger = logging.getLogger("mcp_knowledge.health")

router = APIRouter(tags=["health"])

# Ссылки на компоненты (заполняются при инициализации в lifespan)
_embedding_manager = None
_qdrant_client = None


def set_embedding_manager(mgr):
    global _embedding_manager
    _embedding_manager = mgr


def set_qdrant_client(client):
    global _qdrant_client
    _qdrant_client = client


@router.get("/health")
async def health():
    """Liveness + readiness probe с информацией о бэкендах."""
    response = {
        "status": "healthy",
        "version": "0.1.0",
    }

    # Embedding-статус (задача 1.7)
    if _embedding_manager is not None and _embedding_manager.is_ready:
        emb_info = _embedding_manager.embed_latency_check()
        response["embedding"] = emb_info
    else:
        response["embedding"] = {
            "backend": "none",
            "model": "BAAI/bge-m3",
            "loaded": False,
        }

    # Qdrant-статус
    if _qdrant_client is not None:
        try:
            info = _qdrant_client.collection_info()
            response["qdrant"] = {
                "connected": True,
                "points": info.get("points_count", 0),
                "vectors": info.get("vectors_count", 0),
            }
        except Exception as e:
            response["qdrant"] = {"connected": False, "error": str(e)}
            response["status"] = "degraded"
    else:
        response["qdrant"] = {"connected": False}

    return response
