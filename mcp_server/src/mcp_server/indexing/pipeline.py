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
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings
from ..embedding.manager import EmbeddingManager
from ..models import Chunk, KnowledgeEntry, WriteResult
from ..storage.markdown_store import MarkdownStore
from ..storage.qdrant_client import QdrantClient
from ..storage.schema import build_payload_point
from .chunker import MarkdownChunker
from .sync_barrier import SyncBarrier
from .dlq import DeadLetterQueue

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
        self._worker_task: Optional[asyncio.Task] = None

        # C2: Sync barrier (выделен в sync_barrier.py)
        self._sync = SyncBarrier()

        # C3: DLQ (выделен в dlq.py)
        self._dlq = DeadLetterQueue()

        # Статистика
        self.stats = {"queued": 0, "processed": 0, "failed": 0, "dlq": 0}

    # ── Public API ─────────────────────────────────────────

    async def start(self):
        """Запустить worker-корутину."""
        if self._running:
            return
        self._running = True
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
            self._sync.register(entry.frontmatter.knowledge_id)
            # Используем тот же event что и создали
            self._sync._events[entry.frontmatter.knowledge_id] = event
            result = await self._sync.wait(entry.frontmatter.knowledge_id, timeout=30.0)

        return result

    async def reindex_all(self) -> dict:
        """Полный переиндекс из Markdown SSOT (задача 1.9).

        Обходит все .md в knowledge/, chunking → embed → Qdrant.
        """
        logger.info("reindex_all: начало полного переиндекса")
        t0 = datetime.now(timezone.utc)

        # Очищаем Qdrant
        self._qdrant.delete_all()

        return await self._reindex_from_ssot()

    async def reindex_blue_green(self) -> dict:
        """F1: Zero-downtime blue-green reindex через Qdrant Collection Aliases.

        Flow:
        1. Определить активную коллекцию (knowledge_v1 или knowledge_v2)
        2. Создать новую коллекцию (противоположную)
        3. Заполнить новую коллекцию (поиск продолжается через alias → старую)
        4. Атомарно переключить alias на новую коллекцию (<1 сек)
        5. Удалить старую коллекцию (cleanup)

        Returns:
            {active, target, alias_swapped, reindex_result, elapsed_sec}
        """
        from ..storage.schema import COLLECTION_V1, COLLECTION_V2

        t0 = datetime.now(timezone.utc)
        logger.info("reindex_blue_green: начало blue-green reindex")

        # 1. Определить активную и целевую коллекции
        try:
            active = self._qdrant.get_active_collection()
        except Exception:
            active = COLLECTION_V1  # fallback: первая коллекция

        # v1 → v2, v2 → v1
        target = COLLECTION_V2 if active == COLLECTION_V1 else COLLECTION_V1
        logger.info("reindex_blue_green: active=%s → target=%s", active, target)

        # 2. Создать новую коллекцию
        self._qdrant.create_collection_named(target, force_recreate=True)

        # 3. Заполнить новую коллекцию
        reindex_result = await self._reindex_into(target)

        # 4. Атомарный swap alias
        self._qdrant.swap_alias(COLLECTION_ALIAS, target)
        alias_swapped = True
        logger.info("reindex_blue_green: alias 'knowledge' → '%s' (swap complete)", target)

        # 5. Cleanup старой коллекции
        if active != COLLECTION_ALIAS:
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

    async def _reindex_into(self, collection_name: str) -> dict:
        """F1: Переиндексировать все документы в заданную коллекцию.

        Args:
            collection_name: имя коллекции (knowledge_v1 или knowledge_v2)

        Returns:
            {total_docs, total_chunks, failed, elapsed_sec}
        """
        logger.info("_reindex_into: переиндекс в '%s'", collection_name)
        t0 = datetime.now(timezone.utc)

        paths = await self._store.reindex_scan()
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
                    logger.info("reindex: %d/%d документов, %d чанков (→ %s)",
                                 i + 1, total_docs, total_chunks, collection_name)
            except Exception as e:
                logger.error("reindex error for %s: %s", path, e)
                failed += 1

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        result = {
            "total_docs": total_docs,
            "total_chunks": total_chunks,
            "failed": failed,
            "elapsed_sec": round(elapsed, 1),
        }
        logger.info("_reindex_into '%s': завершено — %s", collection_name, result)
        return result

    async def _reindex_from_ssot(self) -> dict:
        """Legacy: полный переиндекс в текущую коллекцию (delete-all подход)."""
        t0 = datetime.now(timezone.utc)
        paths = await self._store.reindex_scan()
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
                    await self._index_chunks(entry, chunks)
                    total_chunks += len(chunks)

                if (i + 1) % 100 == 0:
                    logger.info("reindex: %d/%d документов, %d чанков",
                                 i + 1, total_docs, total_chunks)
            except Exception as e:
                logger.error("reindex error for %s: %s", path, e)
                failed += 1

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        result = {
            "total_docs": total_docs,
            "total_chunks": total_chunks,
            "failed": failed,
            "elapsed_sec": round(elapsed, 1),
        }
        logger.info("reindex_all: завершено — %s", result)
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
            except Exception as e:
                logger.error("Worker loop error: %s", e, exc_info=True)
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

        # Собираем Qdrant points
        points = []
        for (item, ch), vector in zip(chunk_map, vectors):
            entry: KnowledgeEntry = item["entry"]
            fm = entry.frontmatter

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
            )
            points.append(point)

        # Upsert в Qdrant
        try:
            await loop.run_in_executor(None, self._qdrant.upsert_points, points)
            self.stats["processed"] += len(batch)
        except Exception as e:
            logger.error("Qdrant upsert failed: %s", e)
            await self._handle_batch_failure(batch, str(e))
            return

        # Сигналим sync-ожидающим (C2)
        kids = [item["entry"].frontmatter.knowledge_id for item in batch if item.get("event")]
        if kids:
            self._sync.signal_batch(kids)

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

        Args:
            entry: KnowledgeEntry с метаданными
            chunks: список чанков для индексации
            collection_name: имя коллекции для blue-green (default: COLLECTION_NAME alias)
        """
        texts = [ch.content for ch in chunks]
        loop = asyncio.get_running_loop()
        vectors = await loop.run_in_executor(None, self._embedder.embed_sync, texts)

        points = []
        fm = entry.frontmatter
        for ch, vector in zip(chunks, vectors):
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
            )
            points.append(point)

        await loop.run_in_executor(
            None,
            lambda: self._qdrant.upsert_points(points, collection_name=collection_name),
        )
