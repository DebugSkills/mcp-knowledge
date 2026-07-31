"""MCP Knowledge Server — точка входа.

Интегрирует все компоненты Фазы 1:
- Qdrant gRPC-клиент (задача 1.3)
- Embedding manager GPU/CPU (задачи 1.5, 1.6)
- Markdown SSOT-хранилище (задача 1.1)
- Indexing pipeline (задача 1.8)
- Health-проверки (задача 1.7)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .health import router as health_router, set_embedding_manager, set_qdrant_client
from .storage import MarkdownStore, QdrantClient
from .embedding import EmbeddingManager
from .indexing import IndexingPipeline, MarkdownChunker

logger = logging.getLogger("mcp_knowledge")


# ── Lifespan: инициализация и останов ──────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: валидация инвариантов, инициализация компонентов Фазы 1."""
    logger.info("🚀 MCP Knowledge Server v0.1.0 starting")
    logger.info("   EMBEDDING_BACKEND: %s", settings.EMBEDDING_BACKEND)
    logger.info("   QDRANT_URL: %s", settings.QDRANT_URL)
    logger.info("   GIT_AUDIT: %s", settings.GIT_AUDIT)
    logger.info("   WORKERS: %d (инвариант)", settings.WORKERS)

    # Инвариант: ровно 1 worker
    if settings.WORKERS != 1:
        raise ValueError(f"WORKERS must be 1, got {settings.WORKERS}")

    # ── P0-1: Инициализация компонентов ──────────────────

    # 1. Markdown SSOT хранилище (задача 1.1)
    logger.info("📄 Инициализация MarkdownStore (SSOT)...")
    store = MarkdownStore()
    app.state.store = store

    # 2. Qdrant gRPC-клиент (задача 1.3)
    logger.info("🗄️  Подключение к Qdrant: %s", settings.QDRANT_URL)
    qdrant = QdrantClient()
    qdrant.ensure_collection(force_recreate=False)
    app.state.qdrant = qdrant
    set_qdrant_client(qdrant)  # P1-2: прокидываем в health

    # 3. Embedding manager (задачи 1.5, 1.6)
    logger.info("🧠 Инициализация EmbeddingManager (backend=%s)...", settings.EMBEDDING_BACKEND)
    embedder = EmbeddingManager()
    await embedder.initialize()
    app.state.embedder = embedder
    set_embedding_manager(embedder)  # P1-2: прокидываем в health

    # 4. Chunker (задача 1.4)
    chunker = MarkdownChunker()
    app.state.chunker = chunker

    # 5. Indexing pipeline (задача 1.8)
    logger.info("⚙️  Запуск IndexingPipeline...")
    pipeline = IndexingPipeline(
        store=store,
        qdrant=qdrant,
        embedder=embedder,
        chunker=chunker,
    )
    await pipeline.start()
    app.state.pipeline = pipeline

    # 6. KnowledgeIndex (задача 1.11) — ленивая инициализация,
    #    полная перестройка INDEX при reconciliation (Фаза 2, задача 2.9)
    from .indexing import KnowledgeIndex
    knowledge_index = KnowledgeIndex(store=store)
    app.state.knowledge_index = knowledge_index

    logger.info("✅ MCP Knowledge Server готов (backend=%s)", embedder.backend_name)

    yield  # --- сервер работает ---

    # ── Shutdown ──────────────────────────────────────────
    logger.info("🛑 MCP Knowledge Server shutting down")
    await pipeline.stop()
    qdrant.close()
    logger.info("👋 Shutdown complete")


# ── FastAPI application ────────────────────────────────────

app = FastAPI(
    title="MCP Knowledge Server",
    version="0.1.0",
    description="Семантическая база знаний для AI-агентов (MCP-протокол)",
    lifespan=lifespan,
)
app.include_router(health_router)
