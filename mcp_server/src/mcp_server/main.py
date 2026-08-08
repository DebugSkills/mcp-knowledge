# ruff: noqa: BLE001
"""MCP Knowledge Server — точка входа.

Интегрирует все компоненты Фазы 1:
- Qdrant gRPC-клиент (задача 1.3)
- Embedding manager GPU/CPU (задачи 1.5, 1.6)
- Markdown SSOT-хранилище (задача 1.1)
- Indexing pipeline (задача 1.8)
- Health-проверки (задача 1.7)

Фаза 2 дополнения:
- Auth middleware (B1)
- MCP JSON-RPC эндпоинт POST /mcp (B2)

Фаза 3 дополнения:
- E1: Health hardening (liveness/readiness split)
- E2: Rate limiting (token bucket, batch-aware)
- F1: Blue-green migration at startup (legacy collection → aliases)
"""

import asyncio
import faulthandler
import logging

# ── Усиленное логирование (инцидент 2026-08-06) ─────────────
# Сервер стартует через uvicorn БЕЗ basicConfig → INFO от mcp_knowledge
# печатался только через lastResort-handler (WARNING+), диагностика шла
# вслепую. Настраиваем INFO + формат явно. faulthandler даёт python-стек
# при краше (SIGSEGV/SIGABRT) — «тихая смерть» без traceback больше не
# должна оставаться без следов.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
faulthandler.enable()
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request

from .auth import AuthMiddleware
from .config import settings
from .embedding import EmbeddingManager
from .health import router as health_router
from .health import (
    set_embedding_manager,
    set_pipeline,
    set_qdrant_client,
    set_reconcile_state,
)
from .indexing import IndexingPipeline, MarkdownChunker
from .mcp_handler import handle_mcp_request
from .metrics import metrics_endpoint, set_embed_backend
from .progress import ImportProgressTracker
from .rate_limit import TokenBucketLimiter
from .storage import MarkdownStore, QdrantClient

logger = logging.getLogger("mcp_knowledge")


# ── F1: Legacy migration helper ──────────────────────────


async def _migrate_legacy_collection(qdrant: QdrantClient) -> None:
    """F1: Миграция legacy-коллекции на aliases при первом старте Ф3.

    Если коллекция "knowledge" существует как реальная коллекция (не alias),
    переименовываем в knowledge_v1_DATETIME → создаём alias.

    P1-2: rollback на случай сбоя create_alias.
    """
    from .storage.schema import COLLECTION_ALIAS

    # Проверяем: коллекция существует и это НЕ alias
    if not qdrant._client.collection_exists(COLLECTION_ALIAS):
        return  # ничего нет — ensure_collection уже создал

    if qdrant.has_alias(COLLECTION_ALIAS):
        logger.info("Collection '%s' already has alias — migration not needed", COLLECTION_ALIAS)
        return

    # Legacy: коллекция "knowledge" существует как реальная
    logger.info("⚙️  F1 migration: legacy collection 'knowledge' → aliases")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup_name = f"knowledge_v1_{ts}"

    try:
        qdrant.rename_collection(COLLECTION_ALIAS, backup_name)
        logger.info("F1 migration: renamed 'knowledge' → '%s'", backup_name)
    except Exception as exc:
        # Qdrant rename через alias API работает только с aliases.
        # Если 'knowledge' — реальная коллекция (не alias) → 404 "Alias knowledge
        # does not exists!". Тогда blue-green неприменим: деградируем в прямой
        # доступ к коллекции (search/upsert по имени работают), F1 откладывается
        # (P1 backlog). Без этого — restart storm на каждом старте.
        if "does not exists" in str(exc) or "doesn't exist" in str(exc):
            logger.warning(
                "F1 migration skipped: '%s' is a real collection (not alias) — "
                "blue-green deferred (P1). %s",
                COLLECTION_ALIAS, exc,
            )
            return
        logger.critical("F1 migration: rename_collection failed: %s", exc)
        raise

    try:
        qdrant.create_alias(COLLECTION_ALIAS, backup_name)
        logger.info("F1 migration: alias 'knowledge' → '%s' created", backup_name)
    except Exception as exc:
        # 🆕 P1-2: ROLLBACK — иначе alias "knowledge" не существует → всё падает
        logger.error("F1 migration: alias create failed: %s → ROLLBACK rename", exc)
        try:
            qdrant.rename_collection(backup_name, COLLECTION_ALIAS)
            logger.info("F1 migration: rollback successful — '%s' → 'knowledge'", backup_name)
        except Exception as rollback_exc:
            logger.critical(
                "F1 migration: ROLLBACK FAILED! Collection '%s' orphaned, "
                "alias 'knowledge' missing. Manual fix required: %s",
                backup_name, rollback_exc,
            )
            raise RuntimeError(
                f"F1 migration failed and rollback failed: {exc}. "
                f"Orphaned collection: {backup_name}. "
                f"Run: qdrant_client.rename_collection('{backup_name}', 'knowledge')"
            ) from exc
        raise


# ── Lifespan: инициализация и останов ──────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: валидация инвариантов, инициализация компонентов Фазы 1."""
    import resource
    import time as _time
    _start_ts = _time.monotonic()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    logger.info("[START] begin v0.1.0 backend=%s qdrant=%s rss=%.0f MB",
                settings.EMBEDDING_BACKEND, settings.QDRANT_URL, rss0)
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
    created = qdrant.ensure_collection(force_recreate=False)

    # ── F1: Blue-green migration (legacy → aliases) ──────
    if created:
        # Коллекция только что создана — мигрировать нечего.
        # Иначе _migrate_legacy_collection принял бы свежую коллекцию за legacy
        # и попытался rename_alias (404: rename работает только с aliases).
        logger.info("Collection '%s' created fresh — F1 migration skipped", settings.QDRANT_COLLECTION)
    else:
        try:
            await _migrate_legacy_collection(qdrant)
        except Exception as exc:
            logger.warning("⚠️ F1 migration failed (non-fatal): %s", exc)

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
    try:
        await pipeline.start()
    except Exception as exc:
        logger.critical("🔥 Pipeline start failed: %s", exc, exc_info=True)
        raise
    app.state.pipeline = pipeline
    set_pipeline(pipeline)  # E1: прокидываем pipeline в health для deep checks

    # 6. KnowledgeIndex (задача 1.11) — ленивая инициализация,
    #    полная перестройка INDEX при reconciliation (Фаза 2, задача 2.9)
    from .indexing import KnowledgeIndex
    knowledge_index = KnowledgeIndex(store=store)
    app.state.knowledge_index = knowledge_index

    # ── C1: Reconciliation при старте (Фаза 2, задача 2.9) ──
    # Фоновая задача: reindex не должен блокировать старт сервера — иначе
    # healthcheck фейлится → docker restart-loop → процесс убивается в D-state
    # (инцидент 2026-08-06: thrashing/OOM при reindex большого файла).
    logger.info("🔍 Запуск reconciliation Markdown↔Qdrant (фоновая задача)...")
    from .indexing.reconcile import reconcile

    async def _run_reconcile() -> None:
        try:
            # Degraded-режим (Ollama недоступна): reindex пропускается — иначе
            # reindex_all падает на каждом файле и блокирует старт (инцидент 2026-08-06).
            # skip_orphan_detection: children коллекции-книги — секции внутри .md,
            # проверка по файлам даёт тысячи ложных issues и блокирует старт на
            # минуты (инцидент 2026-08-06, P1: проверка по Qdrant scroll).
            reconcile_result = await reconcile(
                store, qdrant, pipeline, knowledge_index,
                skip_reindex=not embedder.is_ready,
                skip_orphan_detection=True,
            )
            logger.info(
                "✅ Reconciliation: checked=%d, reindexed=%d, skipped=%d, orphans=%d",
                reconcile_result["checked"],
                reconcile_result["reindexed"],
                reconcile_result["skipped"],
                reconcile_result["deleted_orphans"],
            )
            set_reconcile_state("done", reconcile_result)
        except Exception as exc:
            logger.exception("⚠️ Reconciliation failed (non-fatal)")
            set_reconcile_state("error", None, str(exc))

    app.state.reconcile_state = "running"
    app.state.reconcile_task = asyncio.create_task(_run_reconcile())

    # D1: Установка метрики embed backend
    set_embed_backend(embedder.backend_name)

    # E2: Rate limiting (token bucket, per-key, batch-aware)
    logger.info("🪣 Инициализация rate limiter (read=%d/min, write=%d/min)...",
                 settings.RATE_LIMIT_READ_PER_MIN, settings.RATE_LIMIT_WRITE_PER_MIN)
    app.state.rate_limiter_read = TokenBucketLimiter(
        refill_rate=settings.RATE_LIMIT_READ_PER_MIN / 60.0,
        burst_size=max(10, settings.RATE_LIMIT_READ_PER_MIN // 10),
    )
    app.state.rate_limiter_write = TokenBucketLimiter(
        refill_rate=settings.RATE_LIMIT_WRITE_PER_MIN / 60.0,
        burst_size=max(5, settings.RATE_LIMIT_WRITE_PER_MIN // 10),
    )
    # Общий fallback rate limiter (для неаутентифицированных)
    app.state.rate_limiter = app.state.rate_limiter_read
    logger.info("✅ Rate limiter готов (read_burst=%d, write_burst=%d)",
                 app.state.rate_limiter_read.burst_size,
                 app.state.rate_limiter_write.burst_size)

    # 13.9: In-memory progress tracker for live import progress (polling)
    app.state.import_progress = ImportProgressTracker()
    logger.info("📊 ImportProgressTracker initialized (max_messages=50, ttl=600s)")

    # 13.15: Scan state — background task + lock (root-фикс зависания event loop)
    app.state.scan_lock = asyncio.Lock()
    app.state.scan_task = None
    app.state.scan_progress = ImportProgressTracker(max_messages=200)
    app.state.scan_id: str | None = None
    app.state.scan_cancel_event = None  # 13.18: asyncio.Event для отмены скана
    logger.info("🔒 Scan lock + progress tracker + cancel event initialized (phase 13.15+13.18)")

    # Task 1: data_version для кеш-инвалидации kb-console
    app.state.data_version = 0
    logger.info("📊 data_version initialized (0)")

    # ── 13.19: Лог сканирования → /app/data/logs (volume → хост) ──
    # Логи quality-скана (scanner + tools.quality) дублируются в файл,
    # проброшенный на хост через docker volume — для изучения после скана.
    try:
        from pathlib import Path

        scan_log_dir = Path(settings.QUALITY_SCAN_LOG_DIR)
        scan_log_dir.mkdir(parents=True, exist_ok=True)
        scan_log_path = scan_log_dir / f"quality-scan-{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d')}.log"
        _scan_fh = logging.FileHandler(scan_log_path, encoding="utf-8")
        _scan_fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        for _logger_name in ("mcp_knowledge.quality.scanner", "mcp_knowledge.tools.quality"):
            logging.getLogger(_logger_name).addHandler(_scan_fh)
        logger.info("📁 Scan log file: %s", scan_log_path)
    except Exception as exc:
        logger.warning("Scan log file setup failed (non-fatal): %s", exc)

    # ── 13.19: Ночной планировщик quality-скана (внутри контейнера) ──
    async def _scan_scheduler() -> None:
        """Периодический (ежедневный) quality scan в заданный час локального времени."""
        if not settings.QUALITY_SCAN_CRON_ENABLED:
            logger.info("📅 Quality scan scheduler DISABLED (QUALITY_SCAN_CRON_ENABLED=false)")
            return
        logger.info(
            "📅 Quality scan scheduler started: daily %02d:%02d (container TZ)",
            settings.QUALITY_SCAN_CRON_HOUR,
            settings.QUALITY_SCAN_CRON_MINUTE,
        )
        while True:
            now = datetime.now().astimezone()
            target = now.replace(
                hour=settings.QUALITY_SCAN_CRON_HOUR,
                minute=settings.QUALITY_SCAN_CRON_MINUTE,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            logger.info("📅 Next scheduled quality scan at %s (in %.0f min)",
                        target.isoformat(), delay / 60)
            await asyncio.sleep(delay)
            try:
                from .tools.quality import run_quality_scan

                result = await run_quality_scan({}, app.state)
                logger.info("📅 Scheduled quality scan: %s", result.get("status", result))
            except Exception:
                logger.exception("📅 Scheduled quality scan failed")
            # Следующая итерация пересчитает следующий день

    app.state.scan_scheduler_task = asyncio.create_task(_scan_scheduler())

    elapsed = _time.monotonic() - _start_ts
    rss_end = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    logger.info("[START] ready backend=%s elapsed=%.1fs rss=%.0f MB",
                embedder.backend_name, elapsed, rss_end)

    yield  # --- сервер работает ---

    # ── Shutdown ──────────────────────────────────────────
    logger.info("[START] shutdown")

    # 13.19: Cancel scheduled scan task (graceful shutdown)
    scheduler_task = getattr(app.state, "scan_scheduler_task", None)
    if scheduler_task is not None and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await asyncio.wait_for(scheduler_task, timeout=3.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.warning("[START] scan scheduler did not finish in 3s (forced)")
        finally:
            app.state.scan_scheduler_task = None

    # 13.15: Cancel background scan task if running (graceful shutdown)
    if app.state.scan_task is not None and not app.state.scan_task.done():
        logger.info("[START] cancelling background scan task...")
        app.state.scan_task.cancel()
        try:
            await asyncio.wait_for(app.state.scan_task, timeout=5.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.warning("[START] scan task did not finish in 5s (forced)")
        finally:
            app.state.scan_task = None

    await pipeline.stop()
    qdrant.close()
    logger.info("[START] shutdown_done")


# ── FastAPI application ────────────────────────────────────

app = FastAPI(
    title="MCP Knowledge Server",
    version="0.1.0",
    description="Семантическая база знаний для AI-агентов (MCP-протокол)",
    lifespan=lifespan,
)

# B1: Auth middleware (X-API-Key, constant-time сравнение)
app.add_middleware(AuthMiddleware)

app.include_router(health_router)


# B2: MCP JSON-RPC 2.0 эндпоинт
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP JSON-RPC 2.0 эндпоинт.

    Поддерживает: initialize, tools/list, tools/call,
    resources/list, resources/read, prompts/list, prompts/get.
    """
    return await handle_mcp_request(request)


# D1: Prometheus /metrics эндпоинт
@app.get("/metrics")
async def metrics_route(request: Request):
    """Prometheus /metrics endpoint — метрики MCP Knowledge Server."""
    return await metrics_endpoint(request)


# 13.9: Live import progress polling endpoint
@app.get("/imports/{import_id}/progress")
async def import_progress(import_id: str, request: Request):
    """GET /imports/{import_id}/progress — снапшот прогресса импорта.

    Возвращает JSON с полями: import_id, status, phase, imported, total,
    failed, messages[], started_at, updated_at.

    Auth: defence-in-depth — проверяет request.state.auth (установлен
    AuthMiddleware). GET-запросы пропускаются middleware, но мы проверяем
    здесь для консистентности с /mcp.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    tracker = getattr(request.app.state, "import_progress", None)
    snapshot = tracker.get(import_id) if tracker else None
    if snapshot is None:
        raise HTTPException(404, "unknown import_id")
    return snapshot


# 13.15: Live scan progress polling endpoint
@app.get("/quality/scan/progress")
async def scan_progress(request: Request):
    """GET /quality/scan/progress — снапшот прогресса quality scan.

    Возвращает JSON с полями: scan_id, status, phase, done, total,
    messages[], started_at, updated_at, metrics? (при status=done).

    Без path-параметра: всегда возвращает текущий/последний скан.
    Если скана нет — 404.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    scan_id = getattr(request.app.state, "scan_id", None)
    if not scan_id:
        raise HTTPException(404, "no scan has been started")

    tracker = getattr(request.app.state, "scan_progress", None)
    snapshot = tracker.get(scan_id) if tracker else None
    if snapshot is None:
        raise HTTPException(404, f"scan {scan_id} not found or expired")
    return snapshot


# Task 1: Data version endpoint (cache invalidation for kb-console)
@app.get("/data-version")
async def data_version(request: Request):
    """GET /data-version — монотонно возрастающий счётчик мутаций данных.

    Используется kb-console DataCache для гибридной инвалидации
    (TTL + version check). Возвращает {"data_version": N}.

    Auth: defence-in-depth — проверяет request.state.auth.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    return {"data_version": getattr(request.app.state, "data_version", 0)}
