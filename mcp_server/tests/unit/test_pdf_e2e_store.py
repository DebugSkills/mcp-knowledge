"""E2E Ф2b4 (trace code-2026-10-02-bibliography): закрытие acceptance Ф2.

Полный срез через РЕАЛЬНЫЙ blob-store (§3.3, план строки 150-157):
``document_store.put`` → sha256 canonical → продюсер спанов
(``PDFPreprocessor.decompose`` — реальный путь декомпозиции, НЕ копия
логики) → ``Section.meta`` (locator_spans + source_id) → frontmatter
(зеркалит ``_batch_write_sections``, tools/content.py:509-522) → реальный
``_process_batch`` → payload точки → ``_format_point``.

Проверяется (план, строка 335):
- Golden: программный PDF с маркерами --PAGE-N-- → точные локаторы
  секций/чанков, round-trip через store; fallback-секции с реальными
  номерами страниц (пустые страницы не сдвигают нумерацию).
- Негатив (Л1): без спанов полей НЕТ — ни у секции, ни у чанка, ни в
  payload, ни в выдаче _format_point.
- HIT v1 ``.txt`` → переизвлечение (не «page=1» из яда).
- 64KB-близнецы → РАЗНЫЕ ключи (уровень интеграции: store.put даёт
  разные sha256 → разные сегментные кеши и source_id).
- ``content_type:"pdf"`` (Ф2b4.D): PDF-секции несут "pdf", не "book";
  ``is_indexable`` не сломан.

Внешний I/O (embedder/qdrant) замокан заглушками ввода-вывода — как в
test_source_locator_payload; blob-store и декомпозиция — реальные.

Запуск: из cwd ``mcp_server`` (импорт tests.unit.*).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_server.content.locator import (
    PDFLocatorExtractor,
    full_sha256,
    list_locator_kinds,
    register_locator_extractor,
    reset_locator_registry,
    segments_cache_filename,
)
from mcp_server.content.pdf_preprocessor import PDFPreprocessor
from mcp_server.content.preprocessor import ImportMeta
from mcp_server.content.source import make_source_id
from mcp_server.models import (
    INDEX_EXCLUDED_CONTENT_TYPES,
    KnowledgeEntry,
    KnowledgeFrontmatter,
    is_indexable,
)
from mcp_server.storage.document_store import DocumentStore
from mcp_server.tools.search import _format_point
from tests.unit._pdf_fixtures import _build_minimal_pdf

SRC_ID_RE = re.compile(r"^src-[0-9a-f]{16}$")
LOCATOR_KEYS = ("source_id", "locator_kind", "locator_start", "locator_end")
E2E_COLLECTION_ID = "e2e-pdf-collection"


# ── Фикстуры/хелперы ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _page_extractor(tmp_path):
    """Детерминированный реестр: page-экстрактор с tmp-кешем сегментов."""
    reset_locator_registry()
    register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))
    yield tmp_path / "segments"
    reset_locator_registry()


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    """Реальный blob-store на temp DATA_ROOT (паттерн test_document_store)."""
    return DocumentStore(tmp_path / "documents", max_gb=10)


def _preprocessor(tmp_path: Path) -> PDFPreprocessor:
    """PDFPreprocessor с изолированным v1-кешем."""
    pp = PDFPreprocessor()
    pp._cache_dir = str(tmp_path / "v1cache")
    return pp


def _meta(pdf_path: Path, content_sha256: str | None = None) -> ImportMeta:
    return ImportMeta(
        domain="test",
        subject="test",
        title="e2e-store",
        source_path=str(pdf_path),
        content_sha256=content_sha256,
    )


def _fallback_chunker(max_tokens: int = 512, overlap_tokens: int = 64):
    """Chunker с fallback-токенайзером (без HF-модели/сети — air-gap)."""
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


def _fm_from_section(section, zone: str = "private") -> KnowledgeFrontmatter:
    """Frontmatter ровно как _batch_write_sections (tools/content.py:509-522).

    Включая реальное выражение ``meta.get("content_type", "book")`` —
    верифицирует Ф2b4.D end-to-end: PDF-секции обязаны нести "pdf" в meta,
    дефолт "book" для них не срабатывает.
    """
    meta = section.meta
    return KnowledgeFrontmatter(
        knowledge_id=meta["knowledge_id"],
        domain=meta["domain"],
        subject=meta["subject"],
        project=meta.get("project"),
        content_type=meta.get("content_type", "book"),
        parent_knowledge_id=E2E_COLLECTION_ID,
        sequence_number=section.sequence_number,
        tags=section.tags,
        cross_subjects=meta.get("cross_subjects", []),
        zone=zone,
        locator_spans=meta.get("locator_spans"),
        source_id=meta.get("source_id"),
    )


class _QdrantRecorder:
    """Заглушка ввода-вывода Qdrant (как test_source_locator_payload)."""

    def __init__(self):
        self.points = []

    def upsert_points(self, pts, collection_name=None):
        self.points.extend(pts)


async def _index_sections(sections, max_tokens: int = 512) -> list:
    """Секции → fm → реальные chunker+_process_batch → записанные точки."""
    from mcp_server.indexing.pipeline import IndexingPipeline

    qdrant = _QdrantRecorder()
    embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
    with _fallback_chunker(max_tokens=max_tokens)[0]:
        pipe = IndexingPipeline(
            store=SimpleNamespace(),
            qdrant=qdrant,
            embedder=embedder,
            chunker=_fallback_chunker(max_tokens=max_tokens)[1],
        )
        batch = []
        for sec in sections:
            entry = KnowledgeEntry(frontmatter=_fm_from_section(sec), content=sec.body)
            batch.append({"entry": entry, "retries": 0, "event": None})
        await pipe._process_batch(batch)
    return qdrant.points


def _point_stub(pid: str = "e2e-point-1"):
    return SimpleNamespace(id=pid, score=0.91234)


# ═══════════════════════════════════════════════════════════════
# Golden: put → sha → decompose → точные локаторы + source_id
# ═══════════════════════════════════════════════════════════════


class TestGoldenE2EThroughStore:
    async def test_put_get_roundtrip_feeds_producer(self, tmp_path, store):
        """Blob round-trip: put(pdf) → get(sha) → байты совпадают →
        декомпозиция ИМЕННО восстановленных из store байтов."""
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- one"], ["--PAGE-2-- two"]])
        r = store.put(pdf_bytes, mime="application/pdf", filename="golden.pdf")

        assert store.get(r.sha256) == pdf_bytes
        # sharded-путь blob на месте (§3.3: ab/cd/<sha>)
        assert (store._root / r.sha256[0:2] / r.sha256[2:4] / r.sha256).exists()

        # producer читает байты, ВОССТАНОВЛЕННЫЕ из store (round-trip)
        restored = tmp_path / "restored.pdf"
        restored.write_bytes(store.get(r.sha256))
        sections = await _preprocessor(tmp_path).decompose("", _meta(restored, r.sha256))

        assert [s.meta["locator_spans"][0]["locator"]["start"] for s in sections] == [1, 2]

    async def test_sections_carry_exact_locators_and_source_id(self, tmp_path, store):
        """Маркеры --PAGE-N-- (с пропусками пустых страниц) ↔ точные
        локаторы; source_id = make_source_id(sha от put) — canonical ≡
        original (Л3), сегментный кеш ключуется sha из store."""
        pages = [
            [],  # пустая: нет сегмента/секции, номер НЕ сдвигается
            ["--PAGE-2--", "body of page two lorem ipsum dolor sit amet"],
            [],
            ["--PAGE-4--", "body of page four lorem ipsum dolor sit amet"],
            ["--PAGE-5--", "body of page five lorem ipsum dolor sit amet"],
        ]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="gaps.pdf")
        pdf_path = tmp_path / "gaps.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))

        assert len(sections) == 3
        for sec, page_no in zip(sections, (2, 4, 5)):
            assert f"--PAGE-{page_no}--" in sec.body
            spans = sec.meta["locator_spans"]
            assert len(spans) == 1
            assert spans[0]["locator"] == {
                "kind": "page", "start": page_no, "end": page_no,
                "display": f"с. {page_no}",
            }
            assert spans[0]["offset_start"] == 0
            assert spans[0]["offset_end"] == len(sec.body)
            assert SRC_ID_RE.fullmatch(sec.meta["source_id"])
            assert sec.meta["source_id"] == make_source_id(r.sha256)

        # сегментный кеш ключуется sha от document_store.put
        assert (tmp_path / "segments" / segments_cache_filename(r.sha256)).exists()

    async def test_all_section_points_carry_locator_payload(self, tmp_path, store):
        """Каждая секция → реальный батч-пайплайн → точка с payload
        source_id/locator_kind/locator_start/locator_end своей страницы."""
        pages = [[f"--PAGE-{n}-- body of page {n} lorem ipsum"] for n in range(1, 6)]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="payload.pdf")
        pdf_path = tmp_path / "payload.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        assert len(sections) == 5

        points = await _index_sections(sections)
        assert len(points) == 5

        by_page = {}
        for pt in points:
            payload = pt.payload
            assert payload["source_id"] == make_source_id(r.sha256)
            assert payload["locator_kind"] == "page"
            by_page[payload["locator_start"]] = payload["locator_end"]

        assert by_page == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}

    async def test_format_point_emits_locator_fields(self, tmp_path, store):
        """_format_point отдаёт локаторные поля точки, записанной через
        реальный батч-путь из store-провенанса."""
        # Маркер --PAGE-7-- на ФИЗИЧЕСКОЙ 7-й странице: локатор отражает
        # физическую страницу, маркер только подтверждает её.
        pages = [[f"--PAGE-{n}-- page {n} body"] for n in range(1, 8)]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="fmt.pdf")
        pdf_path = tmp_path / "fmt.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        assert len(sections) == 7
        points = await _index_sections(sections)

        formatted = _format_point(_point_stub(points[6].id), points[6].payload)
        assert formatted["source_id"] == make_source_id(r.sha256)
        assert formatted["locator_kind"] == "page"
        assert formatted["locator_start"] == 7
        assert formatted["locator_end"] == 7

    async def test_multichunk_section_points_keep_page_locator(self, tmp_path, store):
        """Длинная страница → несколько чанков → КАЖДЫЙ чанк-точка несёт
        локатор своей (одной) страницы — спаны переживают chunking."""
        long_body = "chunkable body of page two " + "lorem ipsum dolor sit amet " * 40
        pages = [
            ["--PAGE-1-- short opener"],
            ["--PAGE-2--", long_body],
        ]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="chunks.pdf")
        pdf_path = tmp_path / "chunks.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        assert len(sections) == 2

        # Страница секции — из её спанов (не из вхождения слова в чанке:
        # поздние чанки длинного тела слова из начала могут не содержать)
        page_by_kid = {
            s.meta["knowledge_id"]:
                s.meta["locator_spans"][0]["locator"]["start"]
            for s in sections
        }

        points = await _index_sections(sections, max_tokens=60)
        assert len(points) > 2  # страница 2 реально нарезана на чанки

        for pt in points:
            payload = pt.payload
            expected_page = page_by_kid[payload["knowledge_id"]]
            assert payload["locator_kind"] == "page"
            assert payload["locator_start"] == expected_page
            assert payload["locator_end"] == expected_page
            assert payload["source_id"] == make_source_id(r.sha256)
        # Длинная страница дала более одной чанк-точки с локатором 2
        page2_points = [pt for pt in points if pt.payload["locator_start"] == 2]
        assert len(page2_points) > 1

    async def test_fallback_section_real_page_not_sequence(self, tmp_path, store):
        """Acceptance (строка 335): fallback-секции несут РЕАЛЬНЫЕ номера —
        пустая первая страница сдвигает локатор (страница 2), но не
        sequence_number/title («Страница 1»)."""
        pages = [[], ["--PAGE-2-- real second page body"]]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="gap.pdf")
        pdf_path = tmp_path / "gap.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))

        assert len(sections) == 1
        first = sections[0]
        assert first.title == "Страница 1"  # seq-based title
        loc = first.meta["locator_spans"][0]["locator"]
        assert (loc["start"], loc["end"]) == (2, 2)  # реальная страница

        points = await _index_sections(first and sections)
        assert len(points) == 1
        assert points[0].payload["locator_start"] == 2

    async def test_store_dedup_same_pdf_same_source_id(self, tmp_path, store):
        """Повторный put тех же байтов → один blob (дедуп), sha стабилен →
        source_id/кеш-ключ идемпотентны (повторный ingest = no-op для Ф2)."""
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- idempotent body"]])
        r1 = store.put(pdf_bytes, mime="application/pdf", filename="a.pdf")
        r2 = store.put(pdf_bytes, mime="application/pdf", filename="b.pdf")

        assert r1.sha256 == r2.sha256
        assert r2.deduplicated is True
        assert store.count() == 1
        assert make_source_id(r1.sha256) == make_source_id(r2.sha256)


# ═══════════════════════════════════════════════════════════════
# Негатив (Л1): без спанов полей НЕТ — секция/чанк/payload/выдача
# ═══════════════════════════════════════════════════════════════


class TestNegativeE2E:
    async def test_pdf_without_text_no_sections(self, tmp_path, store):
        """Все страницы без текста → нет сегментов → нет секций (ключам
        неоткуда взяться — пустой результат, не fabricated-заглушки)."""
        pdf_bytes = _build_minimal_pdf([[], [], []])
        r = store.put(pdf_bytes, mime="application/pdf", filename="empty.pdf")
        pdf_path = tmp_path / "empty.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))

        assert sections == []

    async def test_v1_path_no_keys_in_section_chunk_payload(self, tmp_path, store):
        """Реестр без page-экстрактора → легаси-v1 декомпозиция: секции
        есть, ключей НЕТ ни в meta, ни в чанках, ни в payload, ни в
        _format_point (Л1: «нет спанов → нет полей» на каждом уровне)."""
        pages = [["--PAGE-1-- plain text page body"], ["--PAGE-2-- another plain page"]]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="noreg.pdf")
        pdf_path = tmp_path / "noreg.pdf"
        pdf_path.write_bytes(pdf_bytes)

        reset_locator_registry()
        assert "page" not in list_locator_kinds()
        try:
            sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        finally:
            register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))

        assert len(sections) == 2  # v1-путь жив (регресс-защита)
        for sec in sections:
            assert "locator_spans" not in sec.meta
            assert "source_id" not in sec.meta

        points = await _index_sections(sections)
        assert points
        for pt in points:
            for key in LOCATOR_KEYS:
                assert key not in pt.payload, f"{key} не должен писаться без спанов"
            formatted = _format_point(_point_stub(pt.id), pt.payload)
            for key in LOCATOR_KEYS:
                assert key not in formatted


# ═══════════════════════════════════════════════════════════════
# Кеш: HIT v1 = MISS; 64KB-близнецы → разные ключи (интеграция)
# ═══════════════════════════════════════════════════════════════


class TestCacheE2E:
    async def test_v1_txt_poison_reextracted_not_page1(self, tmp_path, store):
        """Яд в v1 ``.txt``-checkpoint (по prefix-hash) не читается:
        переизвлечение с реальными номерами страниц, НЕ «page=1» из яда."""
        pages = [[f"--PAGE-{n}-- poison-free body {n}"] for n in range(1, 4)]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="poison.pdf")
        pdf_path = tmp_path / "poison.pdf"
        pdf_path.write_bytes(pdf_bytes)

        pp = _preprocessor(tmp_path)
        v1_dir = Path(pp._cache_dir)
        v1_dir.mkdir(parents=True, exist_ok=True)
        v1_hash = pp._compute_content_hash(str(pdf_path))
        (v1_dir / f"{v1_hash}.txt").write_text("POISON PAGE 1 ONLY", encoding="utf-8")

        sections = await pp.decompose("", _meta(pdf_path, r.sha256))

        assert [s.meta["locator_spans"][0]["locator"]["start"] for s in sections] == [1, 2, 3]
        assert all("POISON" not in s.body for s in sections)
        # сегментный кеш v2 записан под ПОЛНЫМ sha от store.put
        assert (tmp_path / "segments" / segments_cache_filename(r.sha256)).exists()

    async def test_64kb_twins_store_different_shas_cache_and_source(self, tmp_path, store):
        """64KB-близнецы (одинаковый размер + идентичные первые 64KB,
        коллидирующие v1 prefix-hash) → store.put даёт РАЗНЫЕ sha256 →
        разные сегментные кеши и source_id; данные не перезаписали друг
        друга."""
        filler = [f"FILLER {i:06d} " + "x" * 52 for i in range(1000)]
        twin_a = _build_minimal_pdf(
            [filler, ["--PAGE-2-- TWIN ALPHA tail"], ["--PAGE-3-- alpha-end"]]
        )
        twin_b = _build_minimal_pdf(
            [filler, ["--PAGE-2-- TWIN BRAVO tail"], ["--PAGE-3-- bravo-end"]]
        )
        # Sanity: близнецы по v1-хешу, разные по полному
        pa = tmp_path / "twin_a.pdf"
        pb = tmp_path / "twin_b.pdf"
        pa.write_bytes(twin_a)
        pb.write_bytes(twin_b)
        pp = _preprocessor(tmp_path)
        assert pp._compute_content_hash(str(pa)) == pp._compute_content_hash(str(pb))
        assert full_sha256(twin_a) != full_sha256(twin_b)

        ra = store.put(twin_a, mime="application/pdf", filename="a.pdf")
        rb = store.put(twin_b, mime="application/pdf", filename="b.pdf")
        assert ra.sha256 != rb.sha256
        assert store.count() == 2  # дедуп НЕ схлопнул близнецов

        secs_a = await pp.decompose("", _meta(pa, ra.sha256))
        secs_b = await pp.decompose("", _meta(pb, rb.sha256))

        segments_dir = tmp_path / "segments"
        assert (segments_dir / segments_cache_filename(ra.sha256)).exists()
        assert (segments_dir / segments_cache_filename(rb.sha256)).exists()

        alpha = [s for s in secs_a if "ALPHA" in s.body]
        bravo = [s for s in secs_b if "BRAVO" in s.body]
        assert alpha and alpha[0].meta["locator_spans"][0]["locator"]["start"] == 2
        assert bravo and bravo[0].meta["locator_spans"][0]["locator"]["start"] == 2

        src_a = make_source_id(ra.sha256)
        src_b = make_source_id(rb.sha256)
        assert src_a != src_b
        assert secs_a[0].meta["source_id"] == src_a
        assert secs_b[0].meta["source_id"] == src_b


# ═══════════════════════════════════════════════════════════════
# Ф2b4.D: content_type="pdf" (не "book")
# ═══════════════════════════════════════════════════════════════


class TestContentTypePdf:
    async def test_sections_frontmatter_and_payload_are_pdf(self, tmp_path, store):
        """PDF-секции несут content_type="pdf" в meta → frontmatter
        (реальное выражение _batch_write_sections) → payload точки →
        выдача _format_point. Дефолт "book" НЕ срабатывает."""
        pages = [["--PAGE-1-- typed body of pdf import"]]
        pdf_bytes = _build_minimal_pdf(pages)
        r = store.put(pdf_bytes, mime="application/pdf", filename="typed.pdf")
        pdf_path = tmp_path / "typed.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        assert sections

        for sec in sections:
            assert sec.meta["content_type"] == "pdf"
            fm = _fm_from_section(sec)
            assert fm.content_type == "pdf"  # meta.get(..., "book") не дал "book"
            assert is_indexable(fm) is True

            entry = KnowledgeEntry(frontmatter=fm, content=sec.body)
            assert is_indexable(entry) is True

        points = await _index_sections(sections)
        for pt in points:
            assert pt.payload["content_type"] == "pdf"
            formatted = _format_point(_point_stub(pt.id), pt.payload)
            assert formatted["content_type"] == "pdf"

    async def test_v1_path_sections_also_pdf(self, tmp_path, store):
        """content_type="pdf" — контракт препроцессора, НЕ спанов: легаси-v1
        секции (без локаторов) тоже "pdf", и остаются индексируемыми."""
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- v1 typed page"]])
        r = store.put(pdf_bytes, mime="application/pdf", filename="v1typed.pdf")
        pdf_path = tmp_path / "v1typed.pdf"
        pdf_path.write_bytes(pdf_bytes)

        reset_locator_registry()
        try:
            sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, r.sha256))
        finally:
            register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))

        assert sections
        for sec in sections:
            assert sec.meta["content_type"] == "pdf"
            assert is_indexable(_fm_from_section(sec)) is True

    def test_index_exclusions_untouched_by_pdf(self):
        """Исключения индексации не расширены: только "source" (Ф1),
        "pdf" индексируем — W1-W3/R1-R2/N1 не сломаны."""
        assert INDEX_EXCLUDED_CONTENT_TYPES == frozenset({"source"})
        assert is_indexable(SimpleNamespace(content_type="pdf")) is True
        assert is_indexable(SimpleNamespace(content_type=None)) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
