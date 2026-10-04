"""Ф2b2: payload локаторов в точках Qdrant + проводка спанов в чанкер.

Trace: code-2026-10-02-bibliography, план §3.2:148 (payload чанка) и §5 Ф2.

Проверяется:
- Чанк со спанами → payload точки содержит source_id/locator_kind/
  locator_start/locator_end с корректными значениями (kind=page).
- Чанк без спанов → в payload НЕТ локаторных ключей (Л1: «нет спанов →
  нет полей»; не null-заглушки) — в т.ч. через реальный _process_batch.
- Локаторные поля ОТСУТСТВУЮТ в PAYLOAD_INDEXES/PAYLOAD_SCHEMA (факт №16:
  без Qdrant-индексов → миграция коллекций не нужна).
- _format_point отдаёт локаторные поля; без спанов — не отдаёт.

Тестируется через реальные функции проекта (MarkdownChunker, locator_payload,
build_payload_point, IndexingPipeline._process_batch, _format_point) —
без копий логики.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_server.content.locator import Locator, LocatorSpan, spans_to_meta
from mcp_server.indexing.pipeline import IndexingPipeline, locator_payload
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.schema import (
    PAYLOAD_INDEXES,
    PAYLOAD_SCHEMA,
    build_payload_point,
)
from mcp_server.tools.search import _format_point

LOCATOR_KEYS = ("source_id", "locator_kind", "locator_start", "locator_end")
SOURCE_ID = "src-3f2a9c01b74d25e6"


def _fallback_chunker(max_tokens: int = 512, overlap_tokens: int = 64):
    """Chunker с fallback-токенайзером (как test_locator_spans, без HF-модели)."""
    from mcp_server.embedding.tokenizer import (
        _FallbackTokenizer,
        XlmRobertaTokenizer,
    )
    from mcp_server.indexing.chunker import MarkdownChunker

    tok = XlmRobertaTokenizer()
    tok._tok = _FallbackTokenizer()
    return patch(
        "mcp_server.indexing.chunker.xlmr_tokenizer", tok
    ), MarkdownChunker(max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _fm(spans=None, source_id=SOURCE_ID) -> KnowledgeFrontmatter:
    return KnowledgeFrontmatter(
        knowledge_id="test-f2b2-payload",
        domain="library",
        subject="bibliography",
        content_type="book",
        locator_spans=spans_to_meta(spans) if spans else None,
        source_id=source_id,
    )


def _build_point(fm: KnowledgeFrontmatter, ch) -> dict:
    """Payload точки через реальный build_payload_point + locator_payload."""
    point = build_payload_point(
        point_id="00000000-0000-0000-0000-000000000000",
        vector=[0.0, 1.0],
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
        parent_knowledge_id=fm.parent_knowledge_id,
        content_type=fm.content_type,
        sequence_number=fm.sequence_number,
        zone=fm.zone,
        **locator_payload(fm, ch),
    )
    return point.payload


# ── A/B. Payload точки: со спанами / без спанов ─────────────────────────


class TestPointPayload:
    def test_chunk_with_spans_point_has_four_locator_fields(self):
        """Чанк, пересекающий спаны двух страниц → точка содержит 4 поля
        с покрывающим диапазоном [min(start), max(end)] (kind=page)."""
        spans = [
            LocatorSpan(Locator.page(7), 0, 60),
            LocatorSpan(Locator.page(8), 60, 120),
        ]
        with _fallback_chunker()[0] as _:
            chunks = _fallback_chunker()[1].chunk(
                "test-f2b2-payload", "ab " * 40, locator_spans=spans_to_meta(spans)
            )
        assert len(chunks) == 1
        payload = _build_point(_fm(spans), chunks[0])

        assert payload["source_id"] == SOURCE_ID
        assert payload["locator_kind"] == "page"
        assert payload["locator_start"] == 7
        assert payload["locator_end"] == 8

    def test_chunk_inside_single_span_exact_locator(self):
        """Чанк внутри одного спана → точный локатор этой страницы (7..7)."""
        spans = [
            LocatorSpan(Locator.page(7), 0, 60),
            LocatorSpan(Locator.page(8), 60, 120),
        ]
        with _fallback_chunker()[0] as _:
            chunks = _fallback_chunker()[1].chunk(
                "test-f2b2-single", "ab " * 10, locator_spans=spans_to_meta(spans)
            )
        payload = _build_point(_fm(spans), chunks[0])

        assert payload["locator_kind"] == "page"
        assert payload["locator_start"] == 7
        assert payload["locator_end"] == 7

    def test_chunk_without_spans_no_locator_keys(self):
        """Л1: спанов нет → в payload НЕТ ни одного локаторного ключа."""
        with _fallback_chunker()[0] as _:
            chunks = _fallback_chunker()[1].chunk("test-f2b2-neg", "Просто текст.")
        fm = _fm(spans=None, source_id=None)
        payload = _build_point(fm, chunks[0])

        for key in LOCATOR_KEYS:
            assert key not in payload, f"ключ {key} не должен писаться без спанов"

    def test_no_spans_dominates_source_id(self):
        """Л1: спанов нет, но source_id задан → локаторная группа всё равно
        отсутствует (source_id не пишется без пересечённых спанов)."""
        with _fallback_chunker()[0] as _:
            chunks = _fallback_chunker()[1].chunk("test-f2b2-dom", "Текст без спанов.")
        fm = _fm(spans=None, source_id=SOURCE_ID)
        payload = _build_point(fm, chunks[0])

        for key in LOCATOR_KEYS:
            assert key not in payload

    def test_spans_without_source_id_no_source_key(self):
        """Спаны есть, source_id неизвестен → locator_* пишутся,
        ключа source_id НЕТ (не null-заглушка: не фабрикуем)."""
        spans = [LocatorSpan(Locator.page(7), 0, 60)]
        with _fallback_chunker()[0] as _:
            chunks = _fallback_chunker()[1].chunk(
                "test-f2b2-nosrc", "ab " * 10, locator_spans=spans_to_meta(spans)
            )
        payload = _build_point(_fm(spans, source_id=None), chunks[0])

        assert "source_id" not in payload
        assert payload["locator_kind"] == "page"
        assert payload["locator_start"] == 7
        assert payload["locator_end"] == 7


# ── Проводка через реальный батч-путь пайплайна ─────────────────────────


class _QdrantRecorder:
    def __init__(self):
        self.points = []
        self.deletes = []

    def delete_by_knowledge_id(self, knowledge_id, collection_name=None):
        """P2-3: delete-before-upsert — фиксируем и эмулируем удаление."""
        self.deletes.append((knowledge_id, collection_name))
        self.points = [p for p in self.points if p.payload.get("knowledge_id") != knowledge_id]

    def upsert_points(self, pts, collection_name=None):
        self.points.extend(pts)


class TestProcessBatchWiring:
    def test_batch_point_carries_locator_fields(self):
        """entry.frontmatter.locator_spans → chunker → точка в Qdrant:
        реальные _process_batch (embedder/qdrant — заглушки ввода-вывода)."""
        spans = [LocatorSpan(Locator.page(7), 0, 60)]
        fm = _fm(spans)
        entry = KnowledgeEntry(frontmatter=fm, content="ab " * 10)

        qdrant = _QdrantRecorder()
        embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
        with _fallback_chunker()[0]:
            pipe = IndexingPipeline(
                store=SimpleNamespace(),
                qdrant=qdrant,
                embedder=embedder,
                chunker=_fallback_chunker()[1],
            )
            asyncio.run(
                pipe._process_batch([{"entry": entry, "retries": 0, "event": None}])
            )

        assert len(qdrant.points) == 1
        payload = qdrant.points[0].payload
        assert payload["source_id"] == SOURCE_ID
        assert payload["locator_kind"] == "page"
        assert payload["locator_start"] == 7
        assert payload["locator_end"] == 7

    def test_batch_point_without_spans_no_locator_keys(self):
        """Запись без спанов через _process_batch → в точке нет локаторных
        ключей (проводка не фабрикует)."""
        fm = _fm(spans=None, source_id=None)
        entry = KnowledgeEntry(frontmatter=fm, content="Просто текст.")

        qdrant = _QdrantRecorder()
        embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
        with _fallback_chunker()[0]:
            pipe = IndexingPipeline(
                store=SimpleNamespace(),
                qdrant=qdrant,
                embedder=embedder,
                chunker=_fallback_chunker()[1],
            )
            asyncio.run(
                pipe._process_batch([{"entry": entry, "retries": 0, "event": None}])
            )

        payload = qdrant.points[0].payload
        for key in LOCATOR_KEYS:
            assert key not in payload


# ── C. PAYLOAD_INDEXES не тронут (миграция коллекций не нужна) ──────────


class TestPayloadIndexesUntouched:
    def test_locator_fields_not_in_payload_indexes(self):
        indexed = {name for name, _ in PAYLOAD_INDEXES}
        for key in LOCATOR_KEYS:
            assert key not in indexed, f"{key} не должен индексироваться (факт №16)"
            assert key not in PAYLOAD_SCHEMA, f"{key} не должен быть в PAYLOAD_SCHEMA"


# ── D. _format_point: выдача локаторных полей ────────────────────────────


class TestFormatPoint:
    def _point_stub(self):
        return SimpleNamespace(id="pid-1", score=0.87654)

    def test_format_point_emits_locator_fields(self):
        payload = {
            "knowledge_id": "kid",
            "chunk_id": "kid#0",
            "content": "текст",
            "score": 0.9,
            "source_id": SOURCE_ID,
            "locator_kind": "page",
            "locator_start": 7,
            "locator_end": 8,
        }
        formatted = _format_point(self._point_stub(), payload)

        assert formatted["source_id"] == SOURCE_ID
        assert formatted["locator_kind"] == "page"
        assert formatted["locator_start"] == 7
        assert formatted["locator_end"] == 8

    def test_format_point_without_spans_no_fields(self):
        """Локаторных ключей в payload нет → в выдаче их нет (не None)."""
        payload = {
            "knowledge_id": "kid",
            "chunk_id": "kid#0",
            "content": "текст",
        }
        formatted = _format_point(self._point_stub(), payload)

        for key in LOCATOR_KEYS:
            assert key not in formatted


if __name__ == "__main__":
    sys_exit = pytest.main([__file__, "-q"])
    raise SystemExit(sys_exit)
