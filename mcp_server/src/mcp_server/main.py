"""MCP Knowledge Server — точка входа."""
from fastapi import FastAPI
from .config import settings
from .health import router as health_router

app = FastAPI(
    title="MCP Knowledge Server",
    version="0.1.0",
    description="Семантическая база знаний для AI-агентов (MCP-протокол)",
)
app.include_router(health_router)


@app.on_event("startup")
async def startup():
    print(f"🚀 MCP Knowledge Server v0.1.0")
    print(f"   EMBEDDING_BACKEND: {settings.EMBEDDING_BACKEND}")
    print(f"   QDRANT_URL: {settings.QDRANT_URL}")
    print(f"   GIT_AUDIT: {settings.GIT_AUDIT}")
    print(f"   WORKERS: {settings.WORKERS} (инвариант)")
