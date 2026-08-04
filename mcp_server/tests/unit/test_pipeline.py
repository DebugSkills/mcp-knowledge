"""Unit tests for IndexingPipeline — wait_for_index, _completed tracking, SyncBarrier API.

Task 6.3 Cleanup Cycle: N1 _completed set, N3 SyncBarrier public API, N5 double wait fix.
AC6: concurrent racing, AC10/N4: sequential racing, AC12: overflow protection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.indexing.sync_barrier import SyncBarrier

# ── SyncBarrier Public API (N3) ──────────────────────────────────


class TestSyncBarrierPublicAPI:
    """N3: is_registered() + signal_if_registered() — публичный API."""

    def test_is_registered_true(self):
        """is_registered() возвращает True для зарегистрированного knowledge_id."""
        sb = SyncBarrier()
        sb.register("test-kid")
        assert sb.is_registered("test-kid") is True

    def test_is_registered_false(self):
        """is_registered() возвращает False для незарегистрированного knowledge_id."""
        sb = SyncBarrier()
        assert sb.is_registered("nonexistent") is False

    def test_signal_if_registered_true(self):
        """signal_if_registered() возвращает True и устанавливает event."""
        sb = SyncBarrier()
        event = sb.register("test-kid")

        result = sb.signal_if_registered("test-kid")
        assert result is True
        assert event.is_set()

    def test_signal_if_registered_false(self):
        """signal_if_registered() возвращает False для незарегистрированного kid."""
        sb = SyncBarrier()
        result = sb.signal_if_registered("nonexistent")
        assert result is False

    def test_signal_if_registered_removes_event(self):
        """signal_if_registered() удаляет event (pop), is_registered → False после."""
        sb = SyncBarrier()
        sb.register("test-kid")
        sb.signal_if_registered("test-kid")
        assert sb.is_registered("test-kid") is False


# ── Pipeline._completed Tracking (N1) ─────────────────────────────


@pytest.fixture
def pipeline():
    """Создать IndexingPipeline с мок-зависимостями."""
    from mcp_server.indexing.pipeline import IndexingPipeline

    store = MagicMock()
    store.reindex_scan = AsyncMock(return_value=[])

    qdrant = MagicMock()
    qdrant.upsert_points = MagicMock()
    qdrant.delete_all = MagicMock()

    embedder = MagicMock()
    embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

    # Mock chunker — изолируем от реального MarkdownChunker (требует transformers/tokenizer)
    from mcp_server.models import Chunk
    chunker = MagicMock()
    chunker.chunk = MagicMock(return_value=[
        Chunk(chunk_id="c1", knowledge_id="test-sync-kid", content="test chunk",
              section_header="# Test", chunk_index=0, token_count=5)
    ])

    p = IndexingPipeline(store=store, qdrant=qdrant, embedder=embedder, chunker=chunker)
    return p


@pytest.fixture
def sample_entry():
    from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
    fm = KnowledgeFrontmatter(
        knowledge_id="test-sync-kid",
        domain="test",
        subject="demo",
        project="test-project",
        tags=["test"],
        version=1,
    )
    return KnowledgeEntry(frontmatter=fm, content="# Test")


class TestPipelineCompletedTracking:
    """N1: _completed set tracking — sequential + concurrent + overflow."""

    @pytest.mark.asyncio
    async def test_wait_for_index_sequential_already_completed(self, pipeline, sample_entry):
        """N4/AC10: knowledge_id уже в _completed → indexed=True немедленно."""
        kid = sample_entry.frontmatter.knowledge_id
        pipeline._completed.add(kid)

        result = await pipeline.wait_for_index(kid, timeout=0.01)

        assert result.indexed is True
        assert result.pending is False
        assert result.knowledge_id == kid

    @pytest.mark.asyncio
    async def test_wait_for_index_concurrent_self_register(self, pipeline):
        """Concurrent случай: kid не в _completed → self-register + wait."""
        kid = "new-concurrent-kid"

        # kid не зарегистрирован и не в _completed
        assert kid not in pipeline._completed
        assert not pipeline._sync.is_registered(kid)

        # wait_for_index должен self-register и ждать (event не будет signal'd → timeout)
        result = await pipeline.wait_for_index(kid, timeout=0.01)

        # После таймаута — pending=True
        assert result.indexed is False
        assert result.pending is True

    @pytest.mark.asyncio
    async def test_wait_for_index_concurrent_signalled(self, pipeline):
        """Concurrent: wait_for_index + parallel signal → indexed=True."""
        kid = "concurrent-signalled"

        async def _signal_after_delay():
            await asyncio.sleep(0.05)
            pipeline._sync.signal_if_registered(kid)

        # Запускаем waiter и signal параллельно
        waiter = asyncio.create_task(pipeline.wait_for_index(kid, timeout=5.0))
        signaller = asyncio.create_task(_signal_after_delay())

        result = await waiter
        await signaller

        assert result.indexed is True
        assert result.pending is False

    @pytest.mark.asyncio
    async def test_start_clears_completed(self, pipeline):
        """start() очищает _completed."""
        pipeline._completed.add("some-kid")
        pipeline._completed.add("other-kid")
        assert len(pipeline._completed) == 2

        pipeline._worker_task = MagicMock()  # предотвращаем реальный запуск
        await pipeline.start()
        pipeline._worker_task = None  # cleanup

        assert len(pipeline._completed) == 0

    def test_completed_overflow_protection(self, pipeline):
        """_completed очищается при превышении _max_completed_cache."""
        pipeline._max_completed_cache = 10
        for i in range(9):
            pipeline._completed.add(f"kid-{i}")

        assert len(pipeline._completed) == 9

        # Добавляем 11-й — должно быть ≤ 10 (not yet overflowed)
        # Но при добавлении 10-го, len становится 10, что НЕ > 10
        # При добавлении 11-го, len становится 11 > 10 → clear
        pipeline._completed.add("kid-9")
        assert len(pipeline._completed) == 10  # ещё не переполнен

    @pytest.mark.asyncio
    async def test_process_batch_adds_to_completed(self, pipeline, sample_entry):
        """_process_batch() добавляет все knowledge_id в _completed."""
        kid = sample_entry.frontmatter.knowledge_id

        # Мокируем embedder для быстрого возврата
        pipeline._embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

        batch = [{"entry": sample_entry, "retries": 0, "event": None}]
        await pipeline._process_batch(batch)

        # После обработки батча kid должен быть в _completed
        assert kid in pipeline._completed

    @pytest.mark.asyncio
    async def test_process_batch_signals_registered(self, pipeline, sample_entry):
        """_process_batch() сигналит всех зарегистрированных через signal_if_registered."""
        kid = sample_entry.frontmatter.knowledge_id

        # Регистрируем kid до обработки батча
        pipeline._sync.register(kid)

        # Мокируем embedder
        pipeline._embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

        batch = [{"entry": sample_entry, "retries": 0, "event": None}]
        await pipeline._process_batch(batch)

        # После обработки: kid должен быть больше не зарегистрирован (сигнал удалил event)
        assert not pipeline._sync.is_registered(kid)
        assert kid in pipeline._completed


# ── Sequential Racing Test (AC10 / N4) ─────────────────────────────


class TestSequentialRacingAC10:
    """AC10/N4: enqueue items → drain очереди → wait_for_index → indexed=True немедленно."""

    @pytest.mark.asyncio
    async def test_sequential_enqueue_drain_wait(self, pipeline, sample_entry):
        """enqueue с wait_for_index=False, запустить worker, дождаться drain, wait_for_index → indexed=True."""
        kid = sample_entry.frontmatter.knowledge_id

        # Создаём второго entry для батча
        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
        fm2 = KnowledgeFrontmatter(
            knowledge_id="kid-2",
            domain="test",
            subject="demo",
            project="test-project",
            tags=["test"],
            version=1,
        )
        entry2 = KnowledgeEntry(frontmatter=fm2, content="# Second")

        # Мокируем embedder
        pipeline._embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

        # Устанавливаем маленький batch_size чтобы батч обработался быстро
        pipeline._batch_size = 2

        # Запускаем worker
        await pipeline.start()

        # Enqueue без wait_for_index
        await pipeline.enqueue(sample_entry, wait_for_index=False)
        await pipeline.enqueue(entry2, wait_for_index=False)

        # Даём время на обработку батча
        await asyncio.sleep(0.3)

        # Останавливаем worker
        await pipeline.stop()

        # Теперь wait_for_index должен вернуть indexed=True немедленно (через _completed)
        import time
        t0 = time.monotonic()
        result = await pipeline.wait_for_index(kid, timeout=10.0)
        elapsed = time.monotonic() - t0

        assert result.indexed is True
        assert result.pending is False
        # Должен вернуться мгновенно (< 1s), не ждать 10s timeout
        assert elapsed < 1.0, f"wait_for_index took {elapsed:.2f}s, expected <1s"


class TestPipelineOverflow:
    """AC12: _completed overflow protection."""

    @pytest.mark.asyncio
    async def test_overflow_clears_cache(self, pipeline, sample_entry):
        """Добавление > _max_completed_cache записей очищает кэш, новые entries выживают."""
        pipeline._max_completed_cache = 3

        pipeline._embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

        kid = sample_entry.frontmatter.knowledge_id

        # Добавляем 4 записи в _completed (уже > max_completed_cache)
        pipeline._completed.add("kid-a")
        pipeline._completed.add("kid-b")
        pipeline._completed.add("kid-c")
        pipeline._completed.add("kid-d")
        assert len(pipeline._completed) == 4  # > max_completed_cache (3)

        # Обрабатываем батч — overflow BEFORE add → clear старых, затем add batch
        batch = [{"entry": sample_entry, "retries": 0, "event": None}]
        await pipeline._process_batch(batch)

        # После overflow: старые очищены, batch-entries добавлены
        assert kid in pipeline._completed
        assert "kid-a" not in pipeline._completed
        assert len(pipeline._completed) == 1  # только batch entry
