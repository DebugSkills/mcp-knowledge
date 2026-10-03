"""Тесты is_indexable (bibliography Ф1): Source ∉ индексация/реконсиляция/выдача + mutation."""

from __future__ import annotations

from unittest.mock import MagicMock

from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.models import (
    INDEX_EXCLUDED_CONTENT_TYPES,
    KnowledgeEntry,
    KnowledgeFrontmatter,
    is_indexable,
)


def _entry(knowledge_id: str, content_type: str | None) -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=knowledge_id, domain="library", subject="bibliography",
        content_type=content_type,
    )
    return KnowledgeEntry(frontmatter=fm, content="body")


# ── Предикат ───────────────────────────────────────────────

def test_excluded_content_types():
    assert INDEX_EXCLUDED_CONTENT_TYPES == frozenset({"source"})


def test_is_indexable_predicate():
    assert is_indexable(_entry("src-0000000000000000", "source")) is False
    assert is_indexable(_entry("book-1", "pdf")) is True
    assert is_indexable(_entry("coll-1", "collection")) is True
    assert is_indexable(_entry("note-1", None)) is True
    # frontmatter напрямую (duck-typing)
    assert is_indexable(_entry("src-x", "source").frontmatter) is False


# ── N1: enqueue skip (mutation-чувствительно) ──────────────

async def test_enqueue_skips_source():
    pipeline = IndexingPipeline(store=MagicMock(), qdrant=MagicMock(), embedder=MagicMock())
    result = await pipeline.enqueue(_entry("src-0000000000000000", "source"))
    assert result.indexed is True and result.pending is False
    assert pipeline.stats["queued"] == 0  # НЕ попала в очередь
    assert "src-0000000000000000" in pipeline._completed


async def test_process_batch_choke_point_skips_source():
    """N1: choke point _process_batch не чанкует Source (снятие проверки → chunker вызван)."""
    pipeline = IndexingPipeline(store=MagicMock(), qdrant=MagicMock(), embedder=MagicMock())
    chunker = MagicMock()
    chunker.chunk = MagicMock(return_value=[])
    pipeline._chunker = chunker
    item = {"entry": _entry("src-0000000000000000", "source"), "retries": 0, "event": None}
    await pipeline._process_batch([item])
    chunker.chunk.assert_not_called()
    assert "src-0000000000000000" in pipeline._completed


# ── R1/R2: защитные исключения в выдаче ───────────────────

def _fake_embedder():
    mgr = MagicMock()
    mgr.embed_sync = MagicMock(return_value=[0.1] * 8)
    return mgr


async def test_search_knowledge_excludes_source():
    from mcp_server.tools.search import search_knowledge

    qdrant = MagicMock()
    captured = {}

    def _search(vector, top_k, filters, score_threshold, exclude_content_types,
                exclude_statuses, offset, collection_name):
        captured["exclude_content_types"] = exclude_content_types
        return []

    qdrant.search = _search
    app_state = MagicMock()
    app_state.qdrant = qdrant
    app_state.embedder = _fake_embedder()
    await search_knowledge({"query": "x", "top_k": 3, "_auth": {"level": "read"}}, app_state)
    assert captured["exclude_content_types"] == ["collection", "source"]


async def test_search_knowledge_explicit_source_not_excluded():
    from mcp_server.tools.search import search_knowledge

    qdrant = MagicMock()
    captured = {}

    def _search(vector, top_k, filters, score_threshold, exclude_content_types,
                exclude_statuses, offset, collection_name):
        captured["exclude_content_types"] = exclude_content_types
        return []

    qdrant.search = _search
    app_state = MagicMock()
    app_state.qdrant = qdrant
    app_state.embedder = _fake_embedder()
    await search_knowledge({"query": "x", "content_type": "source", "_auth": {"level": "read"}}, app_state)
    assert captured["exclude_content_types"] is None  # явный запрос — осознанный доступ


async def test_search_by_tags_excludes_source():
    from mcp_server.tools.search import search_by_tags

    qdrant = MagicMock()
    captured = {}

    def _search_by_tags(tags, match_all, limit, collection_name, exclude_content_types,
                        exclude_statuses=None):
        captured["exclude_content_types"] = exclude_content_types
        captured["exclude_statuses"] = exclude_statuses
        return []

    qdrant.search_by_tags = _search_by_tags
    app_state = MagicMock()
    app_state.qdrant = qdrant
    await search_by_tags({"tags": ["x"], "_auth": {"level": "read"}}, app_state)
    assert captured["exclude_content_types"] == ["collection", "source"]
    # fix2b P2-3: тег-поиск по умолчанию исключает deprecated (паритет search_knowledge)
    assert captured["exclude_statuses"] == ["deprecated"]


# ── W1/W2/W3: mutation-тесты на КАЖДЫЙ write-контур (critic P2-4) ──

def _pipeline_with_source() -> tuple:
    """Pipeline + MagicMock chunker; store._parse_file возвращает Source-запись."""
    pipeline = IndexingPipeline(store=MagicMock(), qdrant=MagicMock(), embedder=MagicMock())
    source_entry = _entry("src-0000000000000000", "source")
    pipeline._store._parse_file = MagicMock(return_value=source_entry)
    chunker = MagicMock()
    chunker.chunk = MagicMock(return_value=[])
    pipeline._chunker = chunker
    return pipeline, chunker


async def test_w1_index_missing_skips_source():
    """W1: index_missing не чанкует Source (снятие предиката → chunker вызван)."""
    pipeline, chunker = _pipeline_with_source()
    result = await pipeline.index_missing(["/fake/src.md"])
    chunker.chunk.assert_not_called()
    assert result["total_docs"] == 1  # учтена, но не проиндексирована


async def test_w2_reindex_into_skips_source():
    """W2: _reindex_into не чанкует Source."""
    pipeline, chunker = _pipeline_with_source()
    async def _scan():
        return ["/fake/src.md"]
    pipeline._store.reindex_scan = _scan
    await pipeline._reindex_into("knowledge_private_v1")
    chunker.chunk.assert_not_called()


async def test_w3_reconcile_skips_source():
    """W3: reconcile Source → skipped, НЕ доиндексируется (нет цикла)."""
    from unittest.mock import AsyncMock

    from mcp_server.indexing.reconcile import reconcile

    store = MagicMock()
    source_entry = _entry("src-0000000000000000", "source")
    async def _scan():
        return ["/fake/src.md"]
    store.reindex_scan = _scan
    store._parse_file = MagicMock(return_value=source_entry)

    qdrant = MagicMock()
    qdrant.get_knowledge_updated_at = MagicMock(return_value={})

    pipeline = MagicMock()
    pipeline.index_missing = AsyncMock(return_value={"total_docs": 0})
    pipeline.reindex_all = AsyncMock(return_value={"total_docs": 0})

    kidx = MagicMock()
    kidx.rebuild_all = MagicMock(return_value={})

    result = await reconcile(store, qdrant, pipeline, kidx, skip_orphan_detection=True)
    assert result["skipped"] >= 1
    pipeline.index_missing.assert_not_called()
    pipeline.reindex_all.assert_not_called()
