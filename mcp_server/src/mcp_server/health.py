"""Health-check эндпоинты (#10)."""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "0.1.0",
        "backend": "placeholder",  # заменится в задаче 1.7
    }
