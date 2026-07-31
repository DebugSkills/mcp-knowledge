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

        # Sync barrier: dict[knowledge_id] = asyncio.Event
        self._sync_events: dict[str, asyncio.Event] = {}

        # DLQ
        self._dlq_dir = Path(settings.DLQ_DIR)
        self._dlq_dir.mkdir(parents=True, exist_ok=True)
        self._max_retries = settings.DLQ_MAX_RETRIES

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
            self._sync_events[entry.frontmatter.knowledge_id] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=30.0)
                result.indexed = True
                result.pending = False
            except asyncio.TimeoutError:
                logger.warning("Sync wait timeout for %s (CPU backend?)",
                               entry.frontmatter.knowledge_id)
                result.pending = True  # всё ещё в процессе
            finally:
                self._sync_events.pop(entry.frontmatter.knowledge_id, None)

        return result

    async def reindex_all(self) -> dict:
        """Полный переиндекс из Markdown SSOT (задача 1.9).

        Обходит все .md в knowledge/, chunking → embed → Qdrant.
        """
        logger.info("reindex_all: начало полного переиндекса")
        t0 = datetime.now(timezone.utc)

        # Очищаем Qdrant
        self._qdrant.delete_all()

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

        # Сигналим sync-ожидающим
        for item in batch:
            if item.get("event"):
                item["event"].set()

    async def _handle_batch_failure(self, batch: list[dict], error: str):
        """Обработка неудачного батча: retry или DLQ."""
        for item in batch:
            item["retries"] += 1
            if item["retries"] < self._max_retries:
                logger.warning("Retry %d/%d for %s",
                               item["retries"], self._max_retries,
                               item["entry"].frontmatter.knowledge_id)
                await self._queue.put(item)
            else:
                # DLQ
                entry: KnowledgeEntry = item["entry"]
                dlq_entry = {
                    "knowledge_id": entry.frontmatter.knowledge_id,
                    "error": error,
                    "retries": item["retries"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                dlq_path = self._dlq_dir / f"{entry.frontmatter.knowledge_id}.json"
                dlq_path.write_text(json.dumps(dlq_entry, ensure_ascii=False, indent=2))
                self.stats["dlq"] += 1
                logger.error("DLQ: %s → %s", entry.frontmatter.knowledge_id, dlq_path)

                # Всё равно сигналим sync-ожидающим
                if item.get("event"):
                    item["event"].set()

        self.stats["failed"] += 1

    async def _index_chunks(self, entry: KnowledgeEntry, chunks: list[Chunk]):
        """Индексация чанков для reindex (без очереди)."""
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
            )
            points.append(point)

        await loop.run_in_executor(None, self._qdrant.upsert_points, points)
