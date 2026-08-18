# ruff: noqa: BLE001
"""Async-пайплайн индексации (#7): asyncio.Queue + worker + DLQ + sync barrier.

Задачи 1.8 и 1.9 плана Фазы 1.

Поток:
  Markdown-событие → chunk → embed (in-process, run_in_executor) → Qdrant upsert
  Backpressure: ограничение размера очереди, batching embed (≥16 чанков)
  DLQ: 3 retry → data/dlq/
  Sync barrier: await для ?wait_for_index=true
"""

from __future__ import annotations

import asyncio
import logging
import resource
import uuid
from datetime import datetime, timezone

from ..config import settings
from ..embedding.manager import EmbeddingManager
from ..models import Chunk, KnowledgeEntry, WriteResult
from ..storage.markdown_store import MarkdownStore
from ..storage.qdrant_client import QdrantClient
from ..storage.schema import (
    COLLECTION_ALIAS,
    COLLECTION_V1,
    COLLECTION_V2,
    ZONE_PRIVATE,
    ZONE_PUBLIC,
    blue_green_names_for_zone,
    build_payload_point,
    collection_for_zone,
)
from .chunker import MarkdownChunker
from .dlq import DeadLetterQueue
from .sync_barrier import SyncBarrier

logger = logging.getLogger("mcp_knowledge.pipeline")


class IndexingPipeline:
    """Асинхронный пайплайн: chunk → embed → Qdrant upsert."""

    def __init__(
        self,
        store: MarkdownStore,
        qdrant: QdrantClient,
        embedder: EmbeddingManager,
        chunker: MarkdownChunker | None = None,
        max_queue: int = 1000,
        batch_size: int = 16,
    ):
        self._store = store
        self._qdrant = qdrant
        self._embedder = embedder
        self._chunker = chunker or MarkdownChunker()

        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._running = False
        self._worker_task: asyncio.Task | None = None

        # C2: Sync barrier (выделен в sync_barrier.py)
        self._sync = SyncBarrier()

        # C3: DLQ (выделен в dlq.py)
        self._dlq = DeadLetterQueue()

        # N1: Кэш обработанных knowledge_id для sequential wait_for_index
        self._completed: set[str] = set()
        self._max_completed_cache = 5000

        # Статистика
        self.stats = {"queued": 0, "processed": 0, "failed": 0, "dlq": 0}

    # ── Public API ─────────────────────────────────────────

    async def start(self):
        """Запустить worker-корутину."""
        if self._running:
            return
        self._running = True
        self._completed.clear()  # N1: сброс кэша при (ре)старте пайплайна
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("IndexingPipeline: worker запущен (batch_size=%d, max_queue=%d)",
                     self._batch_size, self._queue.maxsize)

    async def stop(self):
        """Остановить worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("IndexingPipeline: worker остановлен (processed=%d, failed=%d, dlq=%d)",
                     self.stats["processed"], self.stats["failed"], self.stats["dlq"])

    async def enqueue(self, entry: KnowledgeEntry, wait_for_index: bool = False) -> WriteResult:
        """Поставить запись в очередь на индексацию.

        Args:
            entry: KnowledgeEntry для индексации
            wait_for_index: ждать завершения индексации (GPU ≤5 сек)

        Returns:
            WriteResult с knowledge_id и статусом
        """
        event = asyncio.Event() if wait_for_index else None
        item = {
            "entry": entry,
            "retries": 0,
            "event": event,
        }

        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Очередь переполнена — blocking put")
            await self._queue.put(item)

        self.stats["queued"] += 1

        result = WriteResult(
            knowledge_id=entry.frontmatter.knowledge_id,
            indexed=False,
            pending=True,
        )

        if wait_for_index and event:
            # Сохраняем внешний event (из очереди) вместо создания нового
            self._sync.bind_event(entry.frontmatter.knowledge_id, event)
            result = await self._sync.wait(entry.frontmatter.knowledge_id, timeout=30.0)

        return result

    async def wait_for_index(self, knowledge_id: str, timeout: float = 30.0) -> WriteResult:
        """Дождаться завершения индексации knowledge_id (N1).

        Покрывает ДВА сценария:
        1. Sequential (N1): knowledge_id уже обработан батчем → _completed содержит kid
           → возвращает indexed=True немедленно (например, import_content после drain очереди)
        2. Concurrent: knowledge_id ещё обрабатывается → self-register + wait
           → signal от _process_batch() или timeout

        Args:
            knowledge_id: ID записи
            timeout: таймаут ожидания (default 30s)

        Returns:
            WriteResult с indexed/pending статусом
        """
        # N1 — sequential case: уже обработан
        if knowledge_id in self._completed:
            return WriteResult(
                knowledge_id=knowledge_id,
                indexed=True,
                pending=False,
            )

        # Concurrent case: self-register + wait
        if not self._sync.is_registered(knowledge_id):  # N3: публичный API
            self._sync.register(knowledge_id)

        result = await self._sync.wait(knowledge_id, timeout=timeout)

        if result.indexed:
            self._completed.add(knowledge_id)

        return result

    async def reindex_all(self) -> dict:
        """Полный переиндекс обеих зон доступа из Markdown SSOT (W2.6).

        Для каждой зоны (public, private) — blue-green переиндекс своей пары
        коллекций. Результат агрегируется в плоский контракт
        {total_docs, total_chunks, failed} + вложенный zones{}.
        """
        logger.info("reindex_all: начало полного переиндекса (обе зоны)")
        zones: dict[str, dict] = {}
        total_docs = 0
        total_chunks = 0
        failed = 0

        for zone in (ZONE_PUBLIC, ZONE_PRIVATE):
            zone_result = await self.reindex_zone(zone)
            zones[zone] = zone_result
            total_docs += zone_result.get("total_docs", 0)
            total_chunks += zone_result.get("total_chunks", 0)
            failed += zone_result.get("failed", 0)

        return {
            "total_docs": total_docs,
            "total_chunks": total_chunks,
            "failed": failed,
            "zones": zones,
        }

    async def reindex_zone(self, zone: str) -> dict:
        """W2.7: Blue-green переиндекс одной зоны доступа.

        Args:
            zone: "public" | "private" (неизвестная зона → ValueError).

        Returns:
            {total_docs, total_chunks, failed, active, target,
             alias_swapped, elapsed_sec}
        """
        v1, v2, alias = blue_green_names_for_zone(zone)  # ValueError на unknown zone

        result = await self.reindex_blue_green(
            alias_name=alias,
            collection_v1=v1,
            collection_v2=v2,
            zone_filter=zone,
        )
        reindex_result = result.get("reindex_result", {})
        return {
            "total_docs": reindex_result.get("total_docs", 0),
            "total_chunks": reindex_result.get("total_chunks", 0),
            "failed": reindex_result.get("failed", 0),
            "active": result.get("active"),
            "target": result.get("target"),
            "alias_swapped": result.get("alias_swapped", False),
            "elapsed_sec": result.get("elapsed_sec"),
        }

    async def reindex_blue_green(
        self,
        alias_name: str | None = None,
        collection_v1: str | None = None,
        collection_v2: str | None = None,
        zone_filter: str | None = None,
    ) -> dict:
        """F1: Zero-downtime blue-green reindex через Qdrant Collection Aliases.

        Flow:
        1. Определить активную коллекцию (knowledge_v1 или knowledge_v2)
        2. Создать новую коллекцию (противоположную)
        3. Заполнить новую коллекцию (поиск продолжается через alias → старую)
        4. Атомарно переключить alias на новую коллекцию (<1 сек)
        5. Удалить старую коллекцию (cleanup)

        Args:
            alias_name: имя alias (default: COLLECTION_ALIAS = "knowledge").
                Для тестов: передать "knowledge_e2e_alias" (НЕ имя существующей коллекции).
            collection_v1: имя первой blue-green коллекции (default: COLLECTION_V1).
            collection_v2: имя второй blue-green коллекции (default: COLLECTION_V2).
            zone_filter: W2.8 — фильтр зоны ("public"/"private");
                None = все файлы (обратная совместимость с admin.py/e2e).

        Returns:
            {active, target, alias_swapped, reindex_result, elapsed_sec}
        """
        alias = alias_name or COLLECTION_ALIAS
        v1 = collection_v1 or COLLECTION_V1
        v2 = collection_v2 or COLLECTION_V2

        t0 = datetime.now(timezone.utc)
        logger.info("reindex_blue_green: начало blue-green reindex (alias=%s)", alias)

        # 1. Определить активную и целевую коллекции
        try:
            active = self._qdrant.get_active_collection(alias_name=alias)
        except Exception:
            active = v1  # fallback: первая коллекция

        # v1 → v2, v2 → v1
        target = v2 if active == v1 else v1
        logger.info("reindex_blue_green: active=%s → target=%s", active, target)

        # 2. Создать новую коллекцию
        self._qdrant.create_collection_named(target, force_recreate=True)

        # 3. Заполнить новую коллекцию
        reindex_result = await self._reindex_into(target, zone_filter=zone_filter)

        # 4. Атомарный swap alias
        self._qdrant.swap_alias(alias, target)
        alias_swapped = True
        logger.info("reindex_blue_green: alias '%s' → '%s' (swap complete)", alias, target)

        # 5. Cleanup старой коллекции
        if active != alias:
            self._qdrant.delete_collection_named(active)
            logger.info("reindex_blue_green: старая коллекция '%s' удалена", active)

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        result = {
            "active": active,
            "target": target,
            "alias_swapped": alias_swapped,
            "reindex_result": reindex_result,
            "elapsed_sec": round(elapsed, 1),
        }
        logger.info("reindex_blue_green: завершено — %s", result)
        return result

    async def _reindex_into(
        self,
        collection_name: str,
        zone_filter: str | None = None,
    ) -> dict:
        """F1: Переиндексировать все документы в заданную коллекцию.

        Args:
            collection_name: имя коллекции (knowledge_v1 или knowledge_v2)
            zone_filter: W2.8 — фильтр зоны ("public"/"private").
                None = все файлы (обратная совместимость).

        Returns:
            {total_docs, total_chunks, failed, elapsed_sec}
        """
        logger.info("[REINDEX] blue_green start collection=%s zone_filter=%s",
                     collection_name, zone_filter)
        t0 = datetime.now(timezone.utc)

        paths = await self._store.reindex_scan()

        # W2.8: зональная фильтрация ДО индексации (zone_filter=None = все файлы)
        if zone_filter is not None:
            zone_paths = []
            for path in paths:
                try:
                    entry = self._store._parse_file(path)
                    if getattr(entry.frontmatter, "zone", ZONE_PRIVATE) == zone_filter:
                        zone_paths.append(path)
                except Exception as e:
                    logger.error("[REINDEX] zone-filter parse error file=%s: %s", path, e)
            paths = zone_paths

        total_docs = len(paths)
        total_chunks = 0
        failed = 0

        for i, path in enumerate(paths):
            try:
                entry = self._store._parse_file(path)
                chunks = self._chunker.chunk(
                    knowledge_id=entry.frontmatter.knowledge_id,
                    content=entry.content,
                )
                if chunks:
                    await self._index_chunks(entry, chunks, collection_name=collection_name)
                    total_chunks += len(chunks)

                if (i + 1) % 100 == 0:
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                    logger.info("[REINDEX] progress i=%d/%d chunks=%d rss=%.0f MB (→ %s)",
                                 i + 1, total_docs, total_chunks, rss_mb, collection_name)
            except Exception as e:
                logger.error("[REINDEX] error file=%s: %s", path, e)
                failed += 1

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        result = {
            "total_docs": total_docs,
            "total_chunks": total_chunks,
            "failed": failed,
            "elapsed_sec": round(elapsed, 1),
        }
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        logger.info("[REINDEX] blue_green done collection=%s docs=%d chunks=%d failed=%d elapsed=%.1fs rss=%.0f MB",
                     collection_name, result["total_docs"], result["total_chunks"],
                     result["failed"], result["elapsed_sec"], rss_mb)
        return result

    async def _reindex_from_ssot(self) -> dict:
        """Legacy: полный переиндекс в текущую коллекцию (delete-all подход)."""
        t0 = datetime.now(timezone.utc)
        paths = await self._store.reindex_scan()
        total_docs = len(paths)
        total_chunks = 0
        failed = 0
        logger.info("[REINDEX] start docs=%d", total_docs)
        for i, path in enumerate(paths):
            try:
                entry = self._store._parse_file(path)
                chunks = self._chunker.chunk(
                    knowledge_id=entry.frontmatter.knowledge_id,
                    content=entry.content,
                )
                if chunks:
                    await self._index_chunks(entry, chunks)
                    total_chunks += len(chunks)

                if (i + 1) % 100 == 0:
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                    logger.info("[REINDEX] progress i=%d/%d chunks=%d rss=%.0f MB",
                                 i + 1, total_docs, total_chunks, rss_mb)
            except Exception as e:
                logger.error("[REINDEX] error file=%s: %s", path, e)
                failed += 1

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        result = {
            "total_docs": total_docs,
            "total_chunks": total_chunks,
            "failed": failed,
            "elapsed_sec": round(elapsed, 1),
        }
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        logger.info("[REINDEX] done docs=%d chunks=%d failed=%d elapsed=%.1fs rss=%.0f MB",
                    total_docs, total_chunks, failed, elapsed, rss_mb)
        return result

    # ── Internal ───────────────────────────────────────────

    async def _worker_loop(self):
        """Основной цикл worker'а: сбор батча → embed → upsert."""
        batch: list[dict] = []

        while self._running:
            try:
                # Собираем батч
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=0.5
                    )
                    batch.append(item)
                except asyncio.TimeoutError:
                    pass  # обработаем что накопилось

                # Добираем до batch_size без блокировки
                while len(batch) < self._batch_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                if not batch:
                    continue

                # Обработка батча
                await self._process_batch(batch)
                batch = []

            except asyncio.CancelledError:
                # Дренируем остатки очереди перед выходом
                if batch:
                    await self._process_batch(batch)
                break
            except Exception:
                logger.exception("Worker loop error")
                # Не роняем worker
                await asyncio.sleep(1)

    async def _process_batch(self, batch: list[dict]):
        """Обработать батч: chunk → embed → upsert."""
        # Собираем все чанки
        all_chunks: list[tuple[dict, list[Chunk]]] = []
        for item in batch:
            entry: KnowledgeEntry = item["entry"]
            chunks = self._chunker.chunk(
                knowledge_id=entry.frontmatter.knowledge_id,
                content=entry.content,
            )
            all_chunks.append((item, chunks))

        # Собираем тексты для батч-embedding
        texts = []
        chunk_map: list[tuple[dict, Chunk]] = []
        for item, chunks in all_chunks:
            for ch in chunks:
                texts.append(ch.content)
                chunk_map.append((item, ch))

        if not texts:
            return

        # Embedding (через run_in_executor — не блокирует event loop)
        try:
            loop = asyncio.get_running_loop()
            vectors = await loop.run_in_executor(
                None, self._embedder.embed_sync, texts
            )
        except Exception as e:
            logger.error("Embedding batch failed: %s", e)
            await self._handle_batch_failure(batch, str(e))
            return

        # Собираем Qdrant points, группируя по зональным коллекциям (W2.9):
        # батч может содержать записи разных зон → upsert по группам
        points_by_collection: dict[str, list] = {}
        for (item, ch), vector in zip(chunk_map, vectors):
            entry: KnowledgeEntry = item["entry"]
            fm = entry.frontmatter
            zone = getattr(fm, "zone", ZONE_PRIVATE)
            collection = collection_for_zone(zone)

            point = build_payload_point(
                point_id=str(uuid.uuid4()),
                vector=vector,
                knowledge_id=fm.knowledge_id,
                chunk_id=ch.chunk_id,
                content=ch.content,
                domain=fm.domain,
                subject=fm.subject,
                project=fm.project,
                tags=fm.tags,
                cross_subjects=fm.cross_subjects,
                section_header=ch.section_header,
                chunk_index=ch.chunk_index,
                updated_at=fm.updated_at.isoformat(),
                parent_knowledge_id=getattr(fm, "parent_knowledge_id", None),
                content_type=getattr(fm, "content_type", None),
                sequence_number=getattr(fm, "sequence_number", None),
                zone=zone,
            )
            points_by_collection.setdefault(collection, []).append(point)

        # Upsert в Qdrant — по группам зон
        try:
            for collection, points in points_by_collection.items():
                await loop.run_in_executor(
                    None,
                    lambda c=collection, pts=points: self._qdrant.upsert_points(
                        pts, collection_name=c
                    ),
                )
            self.stats["processed"] += len(batch)
        except Exception as e:
            logger.error("Qdrant upsert failed: %s", e)
            await self._handle_batch_failure(batch, str(e))
            return

        # Защита от неограниченного роста _completed (до добавления новых)
        if len(self._completed) > self._max_completed_cache:
            logger.debug("_completed cache overflow (%d entries), clearing", len(self._completed))
            self._completed.clear()

        # N1: Добавляем ВСЕ knowledge_id в _completed (sequential wait_for_index)
        for item in batch:
            self._completed.add(item["entry"].frontmatter.knowledge_id)

        # Сигналим ВСЕ зарегистрированные knowledge_id (N3: public API)
        for item in batch:
            kid = item["entry"].frontmatter.knowledge_id
            self._sync.signal_if_registered(kid)

    async def _handle_batch_failure(self, batch: list[dict], error: str):
        """Обработка неудачного батча: retry или DLQ (C3)."""
        for item in batch:
            item["retries"] += 1
            kid = item["entry"].frontmatter.knowledge_id

            if self._dlq.should_retry(item["retries"]):
                delay = self._dlq.backoff_delay(item["retries"])
                logger.warning("Retry %d/%d for %s (delay=%.1fs)",
                               item["retries"], settings.DLQ_MAX_RETRIES, kid, delay)
                await asyncio.sleep(delay)
                await self._queue.put(item)
            else:
                # DLQ (C3)
                self._dlq.record_failure(kid, error, item["retries"])
                self.stats["dlq"] += 1

                # Всё равно сигналим sync-ожидающим
                if item.get("event"):
                    item["event"].set()

        self.stats["failed"] += 1
        self._dlq.check_alert()

    async def _index_chunks(
        self,
        entry: KnowledgeEntry,
        chunks: list[Chunk],
        collection_name: str | None = None,
    ):
        """Индексация чанков (для reindex, без очереди).

        Батчирование по INDEX_BATCH_SIZE (64): каждый батч embed → points →
        upsert, с освобождением памяти между батчами. Без этого весь файл
        (тысячи чанков) одним embed-запросом + все points в RAM → пик ~660 МБ
        на 20 МБ файле (инцидент 2026-08-06, OOM/thrashing при reindex).

        Args:
            entry: KnowledgeEntry с метаданными
            chunks: список чанков для индексации
            collection_name: имя коллекции для blue-green (default: COLLECTION_NAME alias)
        """
        batch_size = getattr(self, "_index_batch_size", 64)
        fm = entry.frontmatter
        loop = asyncio.get_running_loop()
        total = len(chunks)
        indexed = 0

        # W2.4: явный приоритет — переданная коллекция, иначе резолв по зоне
        # (резолв ТОЛЬКО для write-пути)
        if collection_name is None:
            collection_name = collection_for_zone(getattr(fm, "zone", ZONE_PRIVATE))

        zone = getattr(fm, "zone", ZONE_PRIVATE)

        for start in range(0, total, batch_size):
            batch = chunks[start : start + batch_size]
            texts = [ch.content for ch in batch]
            vectors = await loop.run_in_executor(None, self._embedder.embed_sync, texts)

            points = []
            for ch, vector in zip(batch, vectors):
                point = build_payload_point(
                    point_id=str(uuid.uuid4()),
                    vector=vector,
                    knowledge_id=fm.knowledge_id,
                    chunk_id=ch.chunk_id,
                    content=ch.content,
                    domain=fm.domain,
                    subject=fm.subject,
                    project=fm.project,
                    tags=fm.tags,
                    cross_subjects=fm.cross_subjects,
                    section_header=ch.section_header,
                    chunk_index=ch.chunk_index,
                    updated_at=fm.updated_at.isoformat(),
                    parent_knowledge_id=getattr(fm, "parent_knowledge_id", None),
                    content_type=getattr(fm, "content_type", None),
                    sequence_number=getattr(fm, "sequence_number", None),
                    zone=zone,
                )
                points.append(point)

            await loop.run_in_executor(
                None,
                lambda pts=points: self._qdrant.upsert_points(
                    pts, collection_name=collection_name
                ),
            )

            indexed += len(batch)
            del vectors, points, texts, batch
            if indexed % 512 == 0 or indexed == total:
                logger.info(
                    "index: %d/%d chunks (%s)",
                    indexed, total, fm.knowledge_id,
                )
