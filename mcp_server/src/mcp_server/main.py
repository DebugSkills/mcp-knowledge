# ruff: noqa: BLE001, ASYNC230
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
- W2: Двухконтурная модель доступа — зоны public/private,
  двухветочная зональная миграция legacy-коллекции 'knowledge'
"""

import asyncio
import faulthandler
import logging
import os
from pathlib import Path

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
from .storage import DocumentStore, MarkdownStore, QdrantClient
from .token_store import TokenStore  # W3: SSOT токенов двухконтурной модели
from .tools.content import (  # code-2026-08-11-queue: convert/analyze операции
    _bg_analyze,
    _bg_convert,
    _import_queue,
)

logger = logging.getLogger("mcp_knowledge")


# ── W2: Legacy migration helper (двухветочная зональная) ──


async def _migrate_legacy_collection(qdrant: QdrantClient) -> None:
    """W2.11: Двухветочная зональная миграция legacy-коллекции 'knowledge'.

    Ветка A — 'knowledge' = alias (F1 blue-green legacy): private-alias
    наводится на коллекцию с данными (zero-copy), legacy-alias удаляется
    (R1). Данные уже в Qdrant — reindex не нужен.
    Ветка B — 'knowledge' = реальная коллекция (типичный случай):
    создаётся knowledge_private_v1 + alias 'knowledge_private',
    legacy-коллекция удаляется (SSOT — источник правды, данные не теряются).
    Наполнение private-зоны — через существующий reconcile-механизм в
    lifespan (фоновая задача: пустая зона → pipeline.reindex_all()).

    Идемпотентность: legacy отсутствует → return; partial-состояния
    долечиваются (swap/delete пропускаются, если уже выполнены).
    P1-2 rollback: при сбое swap — удаляется созданная коллекция.
    """
    from .storage.schema import COLLECTION_PRIVATE, LEGACY_ALIAS, PRIVATE_V1

    # 1. Legacy не существует (ни коллекции, ни алиаса) — свежая установка.
    #    ensure_zonal_collections() (вызывается в lifespan ПОСЛЕ миграции)
    #    создаст обе зоны с нуля.
    if not qdrant._client.collection_exists(LEGACY_ALIAS) and not qdrant.has_alias(LEGACY_ALIAS):
        logger.info("W2 migration: '%s' отсутствует — свежая установка, пропуск", LEGACY_ALIAS)
        return

    # ── Ветка A: 'knowledge' — alias ──────────────────────
    if qdrant.has_alias(LEGACY_ALIAS):
        legacy_active = qdrant.get_active_collection(LEGACY_ALIAS)
        if legacy_active == LEGACY_ALIAS:
            # has_alias=True, но alias не резолвится — некогерентное состояние;
            # повторный старт повторит попытку.
            logger.warning("W2 migration (A): alias '%s' не резолвится — пропуск", LEGACY_ALIAS)
            return
        logger.info("⚙️  W2 migration (A): '%s' — alias → перенос в private-зону", LEGACY_ALIAS)
        # Private-alias → коллекция с данными (zero-copy: данные не двигаются).
        private_active = qdrant.get_active_collection(COLLECTION_PRIVATE)
        if private_active == COLLECTION_PRIVATE:
            try:
                qdrant.swap_alias(COLLECTION_PRIVATE, legacy_active)
            except Exception as exc:
                logger.error(
                    "W2 migration (A): swap_alias('%s', '%s') failed: %s",
                    COLLECTION_PRIVATE, legacy_active, exc,
                )
                raise
            logger.info(
                "W2 migration (A): alias '%s' → '%s' (legacy-данные в private-зоне)",
                COLLECTION_PRIVATE, legacy_active,
            )
        else:
            logger.info(
                "W2 migration (A): private-зона уже настроена ('%s' → '%s') — пропуск swap",
                COLLECTION_PRIVATE, private_active,
            )
        # R1: удалить legacy-alias. При сбое состояние безопасно: оба алиаса
        # указывают на данные; повторный старт повторит удаление.
        qdrant.delete_alias(LEGACY_ALIAS)
        logger.info("W2 migration (A): legacy alias '%s' удалён (R1)", LEGACY_ALIAS)
        return

    # ── Ветка B: 'knowledge' — реальная коллекция ─────────
    logger.info("⚙️  W2 migration (B): legacy коллекция '%s' → private-зона", LEGACY_ALIAS)
    created_v1 = qdrant.create_collection_named(PRIVATE_V1)
    private_active = qdrant.get_active_collection(COLLECTION_PRIVATE)
    if private_active == COLLECTION_PRIVATE:
        try:
            qdrant.swap_alias(COLLECTION_PRIVATE, PRIVATE_V1)
        except Exception as exc:
            # P1-2 rollback: удалить созданную v1 (иначе останется пустая
            # коллекция без alias; legacy при этом цел и продолжает работать).
            if created_v1:
                try:
                    qdrant.delete_collection_named(PRIVATE_V1)
                    logger.info("W2 migration (B): rollback — '%s' удалена", PRIVATE_V1)
                except Exception as rollback_exc:
                    logger.critical(
                        "W2 migration (B): rollback delete '%s' failed: %s",
                        PRIVATE_V1, rollback_exc,
                    )
            logger.error(
                "W2 migration (B): swap_alias('%s', '%s') failed: %s",
                COLLECTION_PRIVATE, PRIVATE_V1, exc,
            )
            raise
        logger.info("W2 migration (B): alias '%s' → '%s' создан", COLLECTION_PRIVATE, PRIVATE_V1)
    else:
        logger.info(
            "W2 migration (B): private-зона уже настроена ('%s') — пропуск swap",
            private_active,
        )
    # Наполнение: pipeline на момент миграции ещё не создан → reindex
    # делегируется существующему reconcile-механизму в lifespan (фоновая
    # задача: пустая зона → pipeline.reindex_all() из SSOT).
    logger.info("W2 migration (B): reindex отложен на reconcile (pipeline.reindex_all)")
    # SSOT — источник правды: legacy-коллекция удаляется, данные не теряются.
    qdrant.delete_collection_named(LEGACY_ALIAS)
    logger.info("W2 migration (B): legacy коллекция '%s' удалена", LEGACY_ALIAS)


# ── 13.27 + code-2026-09-25-020: scan-трекер ────────────────

# TTL терминальной записи скана (done/error/cancelled) — 7 суток.
# До 020 действовал дефолт ImportProgressTracker(ttl_seconds=600): завершённый
# скан, восстановленный recovery после рестарта, удалялся первым же GET-ом
# → висячий app.state.scan_id → вечный 404 /quality/scan/progress, финальные
# метрики (намерение 13.27) не показывались. Вечного удержания нет:
# prune_finished() (tools/quality.py, старт нового скана) чистит терминальные
# записи при каждом новом скане.
SCAN_PROGRESS_TTL_SECONDS = 7 * 24 * 3600


def build_scan_progress_tracker(
    persist_path: str | os.PathLike | None = None,
) -> ImportProgressTracker:
    """Собрать scan-трекер с прод-параметрами.

    Единая точка конструкции для lifespan и тестов (code-2026-09-25-020):
    e2e-зеркало трекера не может разъехаться с продом. persist_path можно
    переопределить (тесты пишут в tmp).
    """
    return ImportProgressTracker(
        max_messages=200,
        ttl_seconds=SCAN_PROGRESS_TTL_SECONDS,  # 020 (Fix1): 7 суток, не дефолт 600
        persist_path=(
            Path(persist_path) if persist_path
            else Path(settings.QUALITY_DIR) / "scan_state.json"
        ),
        persist_every=2.0,
    )


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

    # ── bibliography Ф1: blob-store документов (content-addressed + SQLite-реестр) ──
    # Каталог data/documents монтируется из DATA_ROOT (Ф0); переживает force-recreate.
    logger.info("📦 Инициализация DocumentStore (documents_dir=%s)...", settings.DOCUMENTS_DIR)
    app.state.document_store = DocumentStore(
        settings.DOCUMENTS_DIR, settings.DOCUMENTS_STORE_MAX_GB
    )

    # ── W3.8: Token store (SSOT токенов) + bootstrap seeding ──
    # env-ключи (MCP_READ_KEYS/MCP_IMPORT_KEYS/MCP_WRITE_KEYS) сидятся в сторе
    # как level=read|import|write, zone=both, source=env — идемпотентно по key_hash.
    # Приоритет аутентификации: токен-стор > env (auth.authenticate_key).
    logger.info("🔑 Инициализация TokenStore (tokens_dir=%s)...", settings.TOKENS_DIR)
    token_store = TokenStore(tokens_dir=settings.TOKENS_DIR)
    try:
        seeded = token_store.seed_from_env(
            {
                "read": settings.MCP_READ_KEYS,
                "import": settings.MCP_IMPORT_KEYS,
                "write": settings.MCP_WRITE_KEYS,
            }
        )
        logger.info("🔑 TokenStore готов (seeded=%d env-токенов)", seeded)
    except Exception as exc:
        logger.warning("⚠️ TokenStore seeding failed (non-fatal): %s", exc)
    app.state.token_store = token_store

    # 2. Qdrant gRPC-клиент (задача 1.3)
    logger.info("🗄️  Подключение к Qdrant: %s", settings.QDRANT_URL)
    qdrant = QdrantClient()

    # ── W2: Двухветочная зональная миграция (legacy 'knowledge' → зоны) ──
    # Порядок важен: СНАЧАЛА миграция legacy (если 'knowledge' существует),
    # ЗАТЕМ ensure_zonal_collections — идемпотентно создаёт отсутствующие
    # зоны (обе — на свежей установке; пустую public — после миграции).
    try:
        await _migrate_legacy_collection(qdrant)
    except Exception as exc:
        logger.warning("⚠️ W2 legacy migration failed (non-fatal): %s", exc)

    qdrant.ensure_zonal_collections()

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

    # ── bibliography Ф3b2: SourceRefIndex (in-memory sha256 → Source-refs) ──
    # Availability-кеш для blob-доступа (least-strict зоны + license fail-closed).
    # Startup-скан SSOT ДО reconcile: индекс строится из .md (Qdrant не нужен);
    # fail-safe — ошибка скана оставляет ПУСТОЙ индекс (fail-closed: блобы
    # недоступны, но не «ложно выданы»). Ведение — write-path-хуки
    # (ingest/lifecycle/set_zone/update/delete) через tools.source_ref_runtime.
    from .tools.source_ref_runtime import init_source_ref_index

    app.state.source_ref_index = await init_source_ref_index(store)
    logger.info("📚 source_ref_index ready: %s", app.state.source_ref_index.size())

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
                # code-2026-09-25-023 Block C: передача лока + owner-ручек.
                # get_heavy_lock — ре-рид объекта на момент acquire (не замыкание
                # объекта): _recover_stalled_scan (022) атомно заменяет
                # heavy_ops_lock новым объектом — заранее захваченный старый стал
                # бы фантомным. set/get_lock_owner — протокол compare-and-clear.
                get_heavy_lock=lambda: app.state.heavy_ops_lock,
                set_lock_owner=lambda v: setattr(app.state, "heavy_lock_owner", v),
                get_lock_owner=lambda: getattr(app.state, "heavy_lock_owner", None),
                # bibliography Ф3c1: integrity Source-refs ↔ blobs после
                # reindex-цикла (шаг 5 reconcile; fail-safe внутри).
                document_store=app.state.document_store,
            )
            logger.info(
                "✅ Reconciliation: checked=%d, reindexed=%d, skipped=%d, orphans=%d, mode=%s",
                reconcile_result["checked"],
                reconcile_result["reindexed"],
                reconcile_result["skipped"],
                reconcile_result["deleted_orphans"],
                reconcile_result.get("mode", "none"),
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
    # Subscriber rate limiter (W3.7: subscriber-ключи — отдельный bucket, 45 req/min)
    app.state.rate_limiter_subscriber = TokenBucketLimiter(
        refill_rate=settings.RATE_LIMIT_SUBSCRIBER_PER_MIN / 60.0,
        burst_size=max(5, settings.RATE_LIMIT_SUBSCRIBER_PER_MIN // 10),
    )
    # Общий fallback rate limiter (для неаутентифицированных)
    app.state.rate_limiter = app.state.rate_limiter_read
    logger.info("✅ Rate limiter готов (read_burst=%d, write_burst=%d, subscriber_burst=%d)",
                 app.state.rate_limiter_read.burst_size,
                 app.state.rate_limiter_write.burst_size,
                 app.state.rate_limiter_subscriber.burst_size)

    # 13.9: In-memory progress tracker for live import progress (polling)
    app.state.import_progress = ImportProgressTracker()
    logger.info("📊 ImportProgressTracker initialized (max_messages=50, ttl=600s)")

    # 13.15/13.21: Heavy-ops lock — единый для import+scan+reindex (один за раз)
    # Инвариант: все тяжёлые фоновые операции сериализуются через этот lock.
    app.state.heavy_ops_lock = asyncio.Lock()
    # legacy alias (используется в quality.py, tools/__init__.py)
    app.state.scan_lock = app.state.heavy_ops_lock
    # code-2026-09-25-023 Block C: маркер владельца лока — кто держит heavy_ops_lock
    # в данный момент ("scan"|"reconcile"|"admin_reindex"|"import"|"convert"|None).
    # Guard в _recover_stalled_scan (C-2) проверяет owner перед recovery.
    app.state.heavy_lock_owner = None
    app.state.scan_task = None
    # 13.27: scan_progress пишется на диск (QUALITY_DIR/scan_state.json) —
    # состояние скана переживает рестарт сервера (recovery ниже).
    app.state.scan_progress = build_scan_progress_tracker()
    app.state.scan_id: str | None = None
    app.state.scan_cancel_event = None  # 13.18: asyncio.Event для отмены скана
    # code-2026-09-25-022: generation-guard — зомби-_bg_scan (после stale-recovery)
    # видит смену generation и пропускает терминальные write (done/audit/error),
    # не перезаписывая запись НОВОГО скана.
    app.state.scan_generation = 0
    logger.info("🔒 Heavy-ops lock + scan state initialized (phase 13.15+13.18+13.21+13.27)")

    # ── 13.27: Recovery персистентного состояния скана после рестарта ──
    # Завершённый скан → показываем финальные метрики в UI (scan_id сохраняем;
    # запись удерживается SCAN_PROGRESS_TTL_SECONDS = 7 суток либо до prune
    # при старте нового скана — 020/Fix1).
    # Прерванный (status=running при старте = сервер упал/рестартнулся в
    # середине скана) → помечаем как прерванный и АВТО-перезапускаем, чтобы
    # качество не простаивало. Авто-рестарт идемпотентен: скан перечитывает
    # все .md и пересчитывает scores/issues.
    try:
        persisted = app.state.scan_progress.load()
        interrupted: list[str] = []
        for sid, entry in persisted.items():
            status = entry.get("status")
            if status == "running":
                interrupted.append(sid)
            elif status in ("done", "error", "cancelled") and app.state.scan_id is None:
                app.state.scan_id = sid  # UI покажет финальные метрики прошлого скана

        async def _resume_interrupted() -> None:
            # Пауза: даём lifespan доинициализироваться; скан стартует фоном.
            await asyncio.sleep(1.0)
            from .tools.quality import run_quality_scan

            result = await run_quality_scan({}, app.state)
            logger.info(
                "🔄 Auto-resumed scan after restart: %s",
                result.get("status", result),
            )

        if interrupted:
            for sid in interrupted:
                # code-2026-09-24-011 (P1-D): прерванный рестартом скан — НЕ ошибка.
                # Раньше писался как error → scan_state.json копил status=error без
                # реальной ошибки. Решение оператора №2: честный статус cancelled
                # + СОХРАНИТЬ авто-ресюм 13.27 (после фикса скан ~20 мин — ресюм полезен).
                app.state.scan_progress.cancel(
                    sid, "scan interrupted by server restart (auto-resume)"
                )
                logger.warning(
                    "🔄 Scan %s was interrupted by restart → cancelled, will auto-resume",
                    sid,
                )
            app.state.scan_resume_task = asyncio.create_task(_resume_interrupted())
    except Exception as exc:
        logger.warning("Scan state recovery failed (non-fatal): %s", exc)

    # 13.21: Import queue state — фоновая задача + cancel + очередь (паттерн scan)
    app.state.import_task = None
    app.state.import_cancel_event = None
    app.state.import_queue: list[dict] = []  # ImportRecord[] — сессионная очередь
    # code-2026-09-25-022 (N1): ref на convert-таск для orphan-lock guard
    app.state.convert_task = None
    # code-2026-08-11-queue: лимит одновременных analyze-операций (P2-1, перегруз Ollama)
    app.state.analyze_semaphore = asyncio.Semaphore(3)
    logger.info("📦 Import queue state initialized (phase 13.21 + convert/analyze ops)")

    # Task 1: data_version для кеш-инвалидации kb-console
    app.state.data_version = 0
    logger.info("📊 data_version initialized (0)")

    # ── 13.19: Лог сканирования → /app/data/logs (volume → хост) ──
    # Логи quality-скана (scanner + tools.quality) дублируются в файл,
    # проброшенный на хост через docker volume — для изучения после скана.
    try:
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

    # 13.27: Cancel auto-resume task (graceful shutdown до его старта)
    resume_task = getattr(app.state, "scan_resume_task", None)
    if resume_task is not None and not resume_task.done():
        resume_task.cancel()
        try:
            await asyncio.wait_for(resume_task, timeout=3.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.warning("[START] scan resume task did not finish in 3s (forced)")
        finally:
            app.state.scan_resume_task = None

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

    # 13.21: Cancel background import task if running (graceful shutdown)
    if app.state.import_task is not None and not app.state.import_task.done():
        logger.info("[START] cancelling background import task...")
        app.state.import_task.cancel()
        try:
            await asyncio.wait_for(app.state.import_task, timeout=5.0)
        except (asyncio.CancelledError, TimeoutError):
            logger.warning("[START] import task did not finish in 5s (forced)")
        finally:
            app.state.import_task = None

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
app.add_middleware(AuthMiddleware, fastapi_app=app)

app.include_router(health_router)

# W5: admin API токенов (kb-console backend)
from .tokens_api import router as tokens_router

app.include_router(tokens_router)


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


def _import_provenance(rec: dict) -> dict:
    """Ф5a1: sparse-провенанс canonical из записи очереди → снапшот (additive).

    Ключи добавляются ТОЛЬКО при фактическом значении (никаких None-заглушек);
    служебные поля (включая `_params`) не пробрасываются (whitelist-рендер).
    """
    out: dict = {}
    if rec.get("source_id"):
        out["source_id"] = rec["source_id"]
    if rec.get("canonical_present") is not None:
        out["canonical_present"] = rec["canonical_present"]
    if rec.get("canonical_sha256"):
        out["canonical_sha256"] = rec["canonical_sha256"]
    if rec.get("canonical_error"):
        out["canonical_error"] = rec["canonical_error"]
    return out


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
    
    # Fallback to import_queue: _bg_import обновляет очередь, но tracker.start()
    # мог не отработать (баг A) — ищем запись по import_id в очереди.
    if snapshot is None:
        import_queue = getattr(request.app.state, "import_queue", None)
        if import_queue:
            for rec in import_queue:
                if rec.get("import_id") == import_id:
                    snapshot = {
                        "import_id": rec.get("import_id", ""),
                        "status": rec.get("status", "unknown"),
                        "phase": rec.get("phase", ""),
                        "imported": rec.get("imported", 0),
                        "total": rec.get("total", 0),
                        "failed": rec.get("failed", 0),
                        "error": rec.get("error"),
                        "collection_id": rec.get("collection_id", ""),
                        "name": rec.get("name", ""),
                        "finished_at": rec.get("finished_at"),
                        # code-2026-08-11-queue: convert/analyze операции
                        "result": rec.get("result"),
                        "operation_type": rec.get("operation_type", "import"),
                        "messages": [],
                        "_source": "queue",
                    }
                    snapshot.update(_import_provenance(rec))
                    break

    if snapshot is None:
        raise HTTPException(404, "unknown import_id")

    # code-2026-08-11-queue: синхронизация двух хранилищ (tracker vs queue-rec).
    # Queue-rec — SSOT для терминальных статусов/result (tracker может отставать
    # на race между update_queue(done) и tracker.done).
    queue = getattr(request.app.state, "import_queue", [])
    rec = next((r for r in queue if r.get("import_id") == import_id), None)
    if rec is not None and rec.get("status") in ("done", "error", "cancelled"):
        # терминальный статус в queue — queue-версия полнее (result/summary_text)
        messages = snapshot.get("messages", []) if snapshot else []
        snapshot = {
            "import_id": rec.get("import_id", ""),
            "status": rec.get("status", "unknown"),
            "phase": rec.get("phase", ""),
            "imported": rec.get("imported", 0),
            "total": rec.get("total", 0),
            "failed": rec.get("failed", 0),
            "error": rec.get("error"),
            "collection_id": rec.get("collection_id", ""),
            "name": rec.get("name", ""),
            "finished_at": rec.get("finished_at"),
            "result": rec.get("result"),
            "operation_type": rec.get("operation_type", "import"),
            "summary_text": rec.get("summary_text"),
            "messages": messages,
            "_source": "queue",
        }
        snapshot.update(_import_provenance(rec))
    elif rec is not None:
        # running-версия: дополняем tracker-поля свежими полями из queue-rec
        if (snapshot.get("result") is None and rec.get("result") is not None):
            snapshot["result"] = rec["result"]
        if "operation_type" not in snapshot:
            snapshot["operation_type"] = rec.get("operation_type", "import")
        if snapshot.get("summary_text") is None and rec.get("summary_text"):
            snapshot["summary_text"] = rec["summary_text"]
        snapshot.update(_import_provenance(rec))

    # Нормализация: tracker.done() пишет "summary", queue-rec пишет "result".
    # Клиент всегда читает snapshot["result"].
    if "summary" in snapshot and "result" not in snapshot:
        snapshot["result"] = snapshot.pop("summary")
    return snapshot


# P2: Import log endpoint — построчный лог из ring-буфера записи очереди
@app.get("/imports/{import_id}/log")
async def import_log(import_id: str, request: Request):
    """GET /imports/{import_id}/log — построчный лог импорта.

    Возвращает {"import_id": "...", "log": [...]} где log — список
    {"ts": "HH:MM:SS", "level": "info|warning|error", "text": "..."}.
    404 если запись с import_id не найдена.

    Auth: defence-in-depth — проверяет request.state.auth
    (как /progress).
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    import_queue = getattr(request.app.state, "import_queue", None)
    if import_queue:
        for rec in import_queue:
            if rec.get("import_id") == import_id:
                return {
                    "import_id": import_id,
                    "log": rec.get("log", []),
                }
    raise HTTPException(404, f"Import {import_id} not found")


# 13.15: Live scan progress polling endpoint
@app.get("/quality/scan/progress")
async def scan_progress(request: Request):
    """GET /quality/scan/progress — снапшот прогресса quality scan.

    Возвращает JSON с полями: import_id (ключ записи; kb-console читает его
    через fallback scan_id/import_id), status, phase, imported, total,
    failed, messages[], started_at, updated_at; при терминальном статусе —
    summary.metrics {files_scanned, review_queue_size,
    duplicates_detected, issues_created}.

    Без path-параметра: всегда возвращает текущий/последний скан.
    Терминальная запись удерживается трекером SCAN_PROGRESS_TTL_SECONDS
    (7 суток) либо до prune при старте нового скана — не бессрочно.
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
        # 020 (Fix3): self-heal висячего указателя — запись удалена
        # TTL-прунингом (или файл потерян), сбрасываем scan_id, чтобы роут
        # не отвечал вечно 404 «scan ... not found or expired», а вёл себя
        # как ветка «скан не запускался». Гард: сброс только если указатель
        # всё ещё указывает на прочитанный скан. Гонки нет — между get()
        # и сбросом нет await (кооперативный asyncio не переключает контекст);
        # running-записи TTL не прунятся → живой скан сбросить нельзя.
        if getattr(request.app.state, "scan_id", None) == scan_id:
            request.app.state.scan_id = None
        raise HTTPException(404, "no scan has been started")
    return snapshot


# Task 1: Data version endpoint (cache invalidation for kb-console)
@app.get("/data-version")
async def data_version(request: Request):
    """GET /data-version — монотонно возрастающий счётчик мутаций данных.

    Используется kb-console DataCache для гибридной инвалидации
    (TTL + version check). Возвращает {"data_version": N}.

    Инвариант: single-worker + await-free increment → race-free.
    При изменении workers или выносе инкремента за await —
    обновить guard-тесты в test_data_version.py.

    Auth: defence-in-depth — проверяет request.state.auth.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    return {"data_version": getattr(request.app.state, "data_version", 0)}


# ═══════════════════════════════════════════════════════════════
# 13.21: PDF Import endpoints — upload + queue + progress
# ═══════════════════════════════════════════════════════════════

import os as _os
import uuid as _uuid

_UPLOAD_DIR = "/tmp/pdf_uploads"


@app.post("/upload")
async def upload_pdf(request: Request):
    """POST /upload — multipart PDF upload (stream to disk).

    Фаза 13.21: принимает PDF файл через multipart/form-data,
    сохраняет во временный файл /tmp/pdf_uploads/<uuid>.pdf,
    возвращает путь для последующего import_content.

    Auth: import/write ключ (X-API-Key header).
    Size limit: MAX_PDF_FILE_SIZE (100 MB).
    """

    # Auth check
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required for PDF upload")

    # Content-Type must be multipart
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=415, detail="Expected multipart/form-data")

    # Read form: Starlette UploadFile
    form = await request.form()
    uploaded_file = form.get("file")
    if uploaded_file is None:
        raise HTTPException(status_code=400, detail="Missing 'file' field in multipart form")

    filename = getattr(uploaded_file, "filename", "upload.pdf") or "upload.pdf"

    # Size validation (stream to temp file)
    _os.makedirs(_UPLOAD_DIR, exist_ok=True)
    upload_id = _uuid.uuid4().hex[:12]
    dest_path = _os.path.join(_UPLOAD_DIR, f"{upload_id}_{filename}")

    try:
        total = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = await uploaded_file.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.MAX_PDF_FILE_SIZE:
                    f.close()
                    _os.unlink(dest_path)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"PDF too large: {total / (1024 * 1024):.1f} MB "
                            f"(max {settings.MAX_PDF_FILE_SIZE / (1024 * 1024):.0f} MB)"
                        ),
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if _os.path.exists(dest_path):
            _os.unlink(dest_path)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    # Compute content hash
    import hashlib
    sha = hashlib.sha256()
    with open(dest_path, "rb") as f:
        sha.update(f.read(65536))
    sha.update(str(_os.path.getsize(dest_path)).encode())
    content_hash = sha.hexdigest()

    logger.info(
        "[UPLOAD] %s saved: %s (%.1f MB, hash=%s)",
        filename, dest_path, total / (1024 * 1024), content_hash[:12],
    )

    return {
        "pdf_path": dest_path,
        "filename": filename,
        "content_hash": content_hash,
        "size": total,
        "upload_id": upload_id,
    }


@app.post("/imports/convert")
async def start_convert(request: Request):
    """POST /imports/convert — операция «Преобразовать» (PDF→текст) с карточкой очереди.

    code-2026-08-11-queue: создаёт запись в _import_queue (ДО ответа, P1-1),
    запускает _bg_convert (фоновая задача, heavy_ops_lock — сериализация с import).
    Если lock занят — операция ставится в очередь (status="queued").

    Body: {pdf_path: str, base_id: str}
    Auth: import/write key (как POST /upload).
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required")

    body = await request.json()
    pdf_path = body.get("pdf_path", "")
    base_id = body.get("base_id", "")

    if not pdf_path:
        raise HTTPException(status_code=400, detail="Missing 'pdf_path'")
    if not base_id:
        raise HTTPException(status_code=400, detail="Missing 'base_id'")
    if not pdf_path.startswith("/tmp/pdf_uploads"):
        raise HTTPException(status_code=400, detail="Invalid pdf_path (must be under /tmp/pdf_uploads)")
    if not _os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail=f"PDF file not found: {pdf_path}")

    import_id = f"{base_id}:convert"
    filename = _os.path.basename(pdf_path)
    # /tmp/pdf_uploads/<upload_id>_<filename> — вытаскиваем читаемое имя
    display_name = filename.split("_", 1)[1] if "_" in filename else filename

    now = datetime.now(timezone.utc).isoformat()
    cancel_event = asyncio.Event()
    rec = {
        "import_id": import_id,
        "name": f"Преобразовать: {display_name}",
        "status": "running",
        "phase": "",
        "imported": 0,
        "total": 0,
        "collection_id": "",
        "error": None,
        "created_at": now,
        "finished_at": None,
        "operation_type": "convert",
        "result": None,
        "summary_text": None,
        "log": [],
        "_cancel_event": cancel_event,
    }

    # P1-1: запись в очереди ДО запуска задачи и ДО ответа.
    # ВАЖНО: единый модульный _import_queue (content.py) — _update_queue ищет по нему.
    # app.state.import_queue синхронизируется ссылкой (паттерн submit_import).
    _import_queue.append(rec)
    request.app.state.import_queue = _import_queue
    tracker = getattr(request.app.state, "import_progress", None)
    if tracker is not None:
        tracker.start(import_id, 0, {"file": display_name, "content_type": "pdf"})

    heavy_ops_lock = getattr(request.app.state, "heavy_ops_lock", None)
    # code-2026-09-25-023 Block C (P1-4): фолбэк `or asyncio.Lock()` убран —
    # фантомный лок вне состояния ломает сериализацию. Fail-closed по паттерну
    # submit_import (content.py:136-138).
    if heavy_ops_lock is None:
        return {"import_id": import_id, "status": "error", "error": "heavy_ops_lock not initialized"}
    if heavy_ops_lock.locked():
        rec["status"] = "queued"
        logger.info("[CONVERT] lock busy — queued %s", import_id)
        return {"import_id": import_id, "status": "queued"}

    # code-2026-09-25-022 (N1): ref на convert-таск для orphan-lock guard в
    # _recover_stalled_scan — иначе orphan-ветка не отличит «lock держит convert»
    # от «lock держит зависший скан» и освободит lock вслепую.
    request.app.state.convert_task = asyncio.create_task(
        _bg_convert(
            import_id=import_id,
            pdf_path=pdf_path,
            app_state=request.app.state,
            cancel_event=cancel_event,
            lock=heavy_ops_lock,
        )
    )
    logger.info("[CONVERT] started %s (%s)", import_id, display_name)
    return {"import_id": import_id, "status": "started"}


@app.post("/imports/analyze")
async def start_analyze(request: Request):
    """POST /imports/analyze — операция «Обработать» (AI-классификация) с карточкой очереди.

    code-2026-08-11-queue: запись в очереди + фоновая задача _bg_analyze
    (без heavy_ops_lock — I/O-bound; с analyze_semaphore, P2-1).
    result (рекомендации) клиент получает через GET /imports/{id}/progress.

    Body: {content: str, base_id: str}
    Auth: import/write key.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required")

    body = await request.json()
    content = body.get("content", "")
    base_id = body.get("base_id", "")
    if not content:
        raise HTTPException(status_code=400, detail="Missing 'content'")
    if not base_id:
        raise HTTPException(status_code=400, detail="Missing 'base_id'")

    import_id = f"{base_id}:analyze"
    now = datetime.now(timezone.utc).isoformat()
    cancel_event = asyncio.Event()
    rec = {
        "import_id": import_id,
        "name": f"Обработать: {base_id[:8]}",
        "status": "running",
        "phase": "",
        "imported": 0,
        "total": 0,
        "collection_id": "",
        "error": None,
        "created_at": now,
        "finished_at": None,
        "operation_type": "analyze",
        "result": None,
        "summary_text": None,
        "log": [],
        "_cancel_event": cancel_event,
    }

    _import_queue.append(rec)
    request.app.state.import_queue = _import_queue
    tracker = getattr(request.app.state, "import_progress", None)
    if tracker is not None:
        tracker.start(import_id, 0, {"content_type": "analyze"})

    asyncio.create_task(
        _bg_analyze(
            import_id=import_id,
            content=content,
            app_state=request.app.state,
            cancel_event=cancel_event,
        )
    )
    logger.info("[ANALYZE] started %s", import_id)
    return {"import_id": import_id, "status": "started"}


@app.get("/imports")
async def list_imports(request: Request):
    """GET /imports — список всех импортов (сессионная очередь).

    Возвращает JSON-массив ImportRecord[] с полями:
    import_id, name, status, phase, progress, error, created_at, finished_at.

    Auth: defence-in-depth.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    queue = getattr(request.app.state, "import_queue", [])
    # Санитизация: _params содержит контент — исключаем из ответа.
    # "log" и "result" тоже исключаем — lean payload (лог через GET /imports/{id}/log,
    # result (текст PDF до сотен KB) через GET /imports/{id}/progress).
    sanitized = []
    for rec in queue:
        out = {k: v for k, v in rec.items() if not k.startswith("_") and k not in ("log", "result")}
        sanitized.append(out)
    return sanitized


@app.get("/imports/active")
async def imports_active(request: Request):
    """GET /imports/active — текущий running-импорт (P1-7: F5-recovery).

    Возвращает {import_id, status, phase, imported, total, name}
    или {"active": false} если нет активного импорта.

    Auth: defence-in-depth.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")

    queue = getattr(request.app.state, "import_queue", [])
    for rec in queue:
        if rec.get("status") == "running":
            return {
                "import_id": rec.get("import_id"),
                "name": rec.get("name"),
                "status": "running",
                "phase": rec.get("phase"),
                "imported": rec.get("imported", 0),
                "total": rec.get("total", 0),
            }
    return {"active": False}


@app.post("/imports/{import_id}/cancel")
async def cancel_import_endpoint(import_id: str, request: Request):
    """POST /imports/{import_id}/cancel — отменить running-импорт.

    Устанавливает import_cancel_event → _bg_import проверяет между фазами
    и завершает с status=cancelled.

    Auth: defence-in-depth (import/write key).
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required")

    queue = getattr(request.app.state, "import_queue", [])
    for rec in queue:
        if rec.get("import_id") != import_id:
            continue
        status = rec.get("status", "")
        if status in ("done", "error", "cancelled"):
            raise HTTPException(409, f"Already {status}")
        # queued → без event (задача ещё не стартовала)
        if status == "queued":
            rec["status"] = "cancelled"
            rec["error"] = "Cancelled by user"
            logger.info("[IMPORT] queued %s cancelled", import_id)
            return {"cancelled": True, "import_id": import_id}
        # running → per-ID event (convert/analyze) или глобальный (import)
        event = rec.get("_cancel_event") or getattr(request.app.state, "import_cancel_event", None)
        if event is not None:
            event.set()
        rec["status"] = "cancelled"
        rec["error"] = "Cancelled by user"
        logger.info("[IMPORT] cancel signal sent for import %s", import_id)
        return {"cancelled": True, "import_id": import_id}

    raise HTTPException(404, f"Import {import_id} not found")


@app.post("/imports/{import_id}/remove")
async def remove_import_endpoint(import_id: str, request: Request):
    """POST /imports/{import_id}/remove — удалить запись импорта из очереди.

    Удаляет запись из request.app.state.import_queue по import_id.
    Только для статусов done/error/cancelled/queued — running требует
    предварительной отмены через /cancel.

    Auth: defence-in-depth (import/write key).
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required")

    queue = getattr(request.app.state, "import_queue", [])
    for i, rec in enumerate(queue):
        if rec.get("import_id") == import_id:
            if rec.get("status") == "running":
                raise HTTPException(
                    status_code=409,
                    detail="Running import must be cancelled first",
                )
            queue.pop(i)
            logger.info("[IMPORT] removed %s from queue", import_id)
            return {"removed": True, "import_id": import_id}

    raise HTTPException(status_code=404, detail=f"Import {import_id} not found")


@app.post("/imports/remove-finished")
async def remove_finished_endpoint(request: Request):
    """POST /imports/remove-finished — удалить все завершённые записи из очереди.

    Удаляет все записи со статусом done/error/cancelled.
    Running-записи остаются нетронутыми.

    Auth: defence-in-depth (import/write key).
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(status_code=401, detail="Authentication required")
    key_level = getattr(auth, "key_level", "none")
    if key_level not in ("import", "write"):
        raise HTTPException(status_code=403, detail="Import or write key required")

    queue = getattr(request.app.state, "import_queue", [])
    remove_statuses = {"done", "error", "cancelled"}
    removed = sum(1 for r in queue if r.get("status") in remove_statuses)
    # In-place mutation (не переприсваиваем — content.py держит ту же ссылку)
    queue[:] = [r for r in queue if r.get("status") not in remove_statuses]

    logger.info("[IMPORT] removed %d finished from queue", removed)
    return {"removed": removed}


# ═══════════════════════════════════════════════════════════════
# bibliography Ф4a: HTTP-выдача blob — GET/HEAD /documents/{sha256} (план §3.4)
# ═══════════════════════════════════════════════════════════════

import re as _re
from urllib.parse import quote as _url_quote

from fastapi import Response as _Response
from fastapi.responses import JSONResponse as _JSONResponse
from fastapi.responses import StreamingResponse as _StreamingResponse

from .metrics import documents_served_total
from .tools.availability import blob_available_indexed

# Строгий формат id: ровно 64 hex-символа в нижнем регистре (§7 R7 — oracle-имени).
_DOCUMENT_SHA256_RE = _re.compile(r"\A[0-9a-f]{64}\Z")

# Whitelist Content-Type (§3.4:175): только типы, безопасные к inline-отдаче.
# text/html СОЗНАТЕЛЬНО исключён: прямой браузерный hit на origin mcp-server
# с inline html исполняет скрипты на нашем origin даже под nosniff — html
# отдаётся как octet-stream (скачивание, не исполнение).
_DOCUMENT_MIME_WHITELIST = frozenset({
    "application/pdf",
    "application/epub+zip",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
})
_DOCUMENT_DEFAULT_MIME = "application/octet-stream"
_DOCUMENT_MIME_EXTENSION = {
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/plain": ".txt",
    "text/markdown": ".md",
    _DOCUMENT_DEFAULT_MIME: ".bin",
}

# Чанк потоковой отдачи (§3.3:157 — чанковый async-итератор, не FileResponse).
_DOCUMENT_CHUNK_BYTES = 1024 * 1024


def _document_mime(info) -> str:
    """Канонический Content-Type из строки реестра (whitelist; дефолт octet-stream).

    Параметры (`; charset=…`) срезаются, регистр нормализуется; всё вне
    whitelist (вкл. None и text/html) → application/octet-stream.
    """
    raw = getattr(info, "mime", None) if info is not None else None
    if not isinstance(raw, str):
        return _DOCUMENT_DEFAULT_MIME
    normalized = raw.split(";", 1)[0].strip().lower()
    return normalized if normalized in _DOCUMENT_MIME_WHITELIST else _DOCUMENT_DEFAULT_MIME


def _sanitize_download_name(raw, sha256: str, mime: str) -> tuple[str, str]:
    """Имя для Content-Disposition: (utf8-имя, ascii-fallback).

    Источник — реестр (original_filename; при rebuild восстанавливается из
    Source SSOT). Вырезаются control-символы (C0/DEL, вкл. CRLF), кавычки и
    оба слэша; пустой/враждебный результат → fallback `<sha256><ext>`.
    """
    fallback = f"{sha256}{_DOCUMENT_MIME_EXTENSION.get(mime, '.bin')}"
    if not isinstance(raw, str):
        return fallback, fallback
    cleaned = "".join(
        ch for ch in raw
        if ord(ch) >= 0x20 and ord(ch) != 0x7F and ch not in '"\\/'
    ).strip()
    return (cleaned or fallback), fallback


def _content_disposition(name_utf8: str, fallback_ascii: str) -> str:
    """inline + RFC 5987: ASCII-fallback `filename` + `filename*=UTF-8''…`."""
    return (
        f'inline; filename="{fallback_ascii}"; '
        f"filename*=UTF-8''{_url_quote(name_utf8, safe='')}"
    )


def _parse_range_header(value: str, size: int) -> tuple[int, int] | None:
    """Разбор ОДНОГО диапазона `bytes=<s>-<e>` (суффикс `-N`, открытый `s-`).

    Returns:
        (start, end) включительно; None → невалидный (multi-range, не-bytes,
        s>e, `-0`) или непересекающийся (start ≥ size) → 416.
    """
    spec = value.strip()
    if not spec.lower().startswith("bytes="):
        return None
    body = spec[6:].strip()
    if "," in body or "-" not in body:
        return None  # multi-range не поддерживается → невалидный
    first, _, last = body.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if first == "":
            if not last:
                return None
            suffix = int(last)  # bytes=-N — последние N байт
            if suffix <= 0:
                return None
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(first)
            if start < 0:
                return None
            end = size - 1 if not last else int(last)
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


async def _stream_document(fh, offset: int, length: int):
    """Чанковый async-итератор по file-object (blocking read → run_in_executor)."""
    loop = asyncio.get_running_loop()
    remaining = length
    try:
        if offset:
            await loop.run_in_executor(None, fh.seek, offset)
        while remaining > 0:
            chunk = await loop.run_in_executor(
                None, fh.read, min(_DOCUMENT_CHUNK_BYTES, remaining)
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        await loop.run_in_executor(None, fh.close)


async def _serve_document(request: Request, sha256: str, *, head: bool):
    """Единая логика GET/HEAD: валидация → auth → availability → Range → отдача.

    Порядок проверок строго до отдачи тела. 404-семантика (не 403): недоступен/
    deprecated/отсутствует — не раскрываем существование blob (§3.4:175).
    """
    # 1. Синтаксис id → 400 (до auth: ошибка синтаксиса публична, ничего не раскрывает)
    if _DOCUMENT_SHA256_RE.match(sha256) is None:
        raise HTTPException(status_code=400, detail="invalid document id")

    # 2. Auth → 401 + WWW-Authenticate (AuthMiddleware кладёт AuthInfo в state)
    auth = getattr(request.state, "auth", None)
    if auth is None or not getattr(auth, "authenticated", False):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'ApiKey realm="documents"'},
        )

    # 3. Availability — ЕДИНЫЙ существующий предикат (не дублируем): blob_exists ∧
    #    ∃ ref: зона/статус/license fail-closed. Отсутствие контура → fail-closed 404.
    store = getattr(request.app.state, "document_store", None)
    index = getattr(request.app.state, "source_ref_index", None)
    if store is None or index is None:
        raise HTTPException(status_code=404, detail="document not found")
    loop = asyncio.get_running_loop()
    available = await loop.run_in_executor(
        None,
        lambda: blob_available_indexed(sha256, auth, exists_fn=store.exists, index=index),
    )
    if not available:
        raise HTTPException(status_code=404, detail="document not found")

    # 4. Размер + метаданные реестра (race: blob мог быть удалён GC после exists)
    size = await loop.run_in_executor(None, store.blob_size, sha256)
    if size is None:
        raise HTTPException(status_code=404, detail="document not found")
    info = await loop.run_in_executor(None, store.info, sha256)
    mime = _document_mime(info)
    name_utf8, fallback_ascii = _sanitize_download_name(
        getattr(info, "original_filename", None), sha256, mime
    )

    # 5. Range — строго после проверок доступности (Range/HEAD не обходят гейты)
    start, length, status_code = 0, size, 200
    range_header = request.headers.get("range")
    if range_header is not None:
        parsed = _parse_range_header(range_header, size)
        if parsed is None:
            return _JSONResponse(
                status_code=416,
                content={"detail": "requested range not satisfiable"},
                headers={
                    "Content-Range": f"bytes */{size}",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        start, end = parsed
        status_code, length = 206, end - start + 1

    headers = {
        "Content-Type": mime,
        "Content-Length": str(length),
        "Content-Disposition": _content_disposition(name_utf8, fallback_ascii),
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"

    documents_served_total.inc()  # метрика выдачи (200/206)

    if head:
        return _Response(status_code=status_code, headers=headers)

    fh = await loop.run_in_executor(None, store.open_blob, sha256)
    if fh is None:
        raise HTTPException(status_code=404, detail="document not found")
    return _StreamingResponse(
        _stream_document(fh, start, length),
        status_code=status_code,
        headers=headers,
    )


@app.get("/documents/{sha256}")
async def get_document(sha256: str, request: Request):
    """GET /documents/{sha256} — выдача blob документа (стриминг, Range/206)."""
    return await _serve_document(request, sha256, head=False)


@app.head("/documents/{sha256}")
async def head_document(sha256: str, request: Request):
    """HEAD /documents/{sha256} — те же заголовки и проверки, без тела."""
    return await _serve_document(request, sha256, head=True)
