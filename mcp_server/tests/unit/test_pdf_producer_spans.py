"""Ф2b3 (главный acceptance Ф2): продюсер спанов локаторов из PDF.

Trace: code-2026-10-02-bibliography, план §3.2 (строки 122-149) и §5 Ф2
(строка 335). Продюсер = PDFPreprocessor.decompose в сегментном режиме:
Section.meta["locator_spans"] + Section.meta["source_id"].

Проверяется (план, строка 335):
- Golden: программный PDF с маркерами --PAGE-N-- → секции несут ТОЧНЫЕ
  locator_spans (kind=page, реальные номера страниц, включая пропуск пустых);
  чанки из этих секций наследуют спаны (chunker + payload через реальный
  _process_batch); source_id заполнен формата src-<16hex>.
- Fallback-секции → РЕАЛЬНЫЕ номера страниц (не 1): пустая первая страница
  сдвигает локаторы, но не номера.
- Негатив (Л1): нет спанов → ключей НЕТ: (а) PDF без текста → секций нет;
  (б) page-экстрактор не зарегистрирован → легаси-v1 путь, секции без ключей.
- HIT v1 `.txt`-кеша = MISS: яд не влияет, номера страниц реальные.
- 64KB-близнецы → РАЗНЫЕ ключи сегментного кеша (полный sha256, не prefix).

PDF-фикстура — общий хелпер tests/unit/_pdf_fixtures (Ф2b4.E) — без
копирования и без кросс-импорта между тестовыми модулями.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_server.content.locator import (
    Locator,
    LocatorSpan,
    PDFLocatorExtractor,
    full_sha256,
    list_locator_kinds,
    locators_for_chunk,
    register_locator_extractor,
    reset_locator_registry,
    segments_cache_filename,
    spans_from_meta,
)
from mcp_server.content.pdf_preprocessor import PDFPreprocessor
from mcp_server.content.preprocessor import ImportMeta
from mcp_server.content.source import make_source_id
from tests.unit._pdf_fixtures import _build_minimal_pdf

SRC_ID_RE = re.compile(r"^src-[0-9a-f]{16}$")


# ── Фикстуры ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _page_extractor(tmp_path):
    """Детерминированный реестр: page-экстрактор с tmp-кешем сегментов.

    Продюсер разрешает экстрактор через реестр (авторегистрация — Ф2b3.B);
    тест подменяет его экземпляром с изолированным кешем и восстанавливает
    реестр после каждого теста.
    """
    reset_locator_registry()
    register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))
    yield tmp_path / "segments"
    reset_locator_registry()


def _preprocessor(tmp_path: Path) -> PDFPreprocessor:
    """PDFPreprocessor с изолированным v1-кешем (как test_pdf_preprocessor)."""
    pp = PDFPreprocessor()
    pp._cache_dir = str(tmp_path / "v1cache")
    return pp


def _meta(pdf_path: Path, content_sha256: str | None = None) -> ImportMeta:
    return ImportMeta(
        domain="test",
        subject="test",
        title="producer-spans",
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


# ═══════════════════════════════════════════════════════════════
# Golden: секции несут ТОЧНЫЕ locator_spans + source_id
# ═══════════════════════════════════════════════════════════════


class TestGoldenProducerSpans:
    async def test_sections_carry_exact_locator_spans(self, tmp_path):
        """Маркеры --PAGE-N-- ↔ реальные номера в locator_spans секций.

        Пустые страницы 1 и 3 не порождают секций и не сдвигают номера
        (Л1: локаторы к пустым страницам не фабрикуются). Спан покрывает
        тело секции целиком: [0, len(body)) — тело КАК СЕРИАЛИЗУЕТСЯ
        (контракт Ф2b1: offsets относительно нормализованного тела).
        """
        pages = [
            [],  # страница 1 пустая → нет сегмента, нет секции
            ["--PAGE-2--", "body of page two lorem ipsum dolor sit amet"],
            [],  # страница 3 пустая
            ["--PAGE-4--", "body of page four lorem ipsum dolor sit amet"],
        ]
        pdf_path = tmp_path / "golden.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))
        sha = full_sha256(pdf_path)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, sha))

        assert len(sections) == 2
        for sec, page_no in zip(sections, (2, 4)):
            # Маркер страницы в теле подтверждает реальный номер
            assert f"--PAGE-{page_no}--" in sec.body
            assert f"body of page {'two' if page_no == 2 else 'four'}" in sec.body

            spans = sec.meta["locator_spans"]
            assert len(spans) == 1
            assert spans[0]["locator"] == {
                "kind": "page",
                "start": page_no,
                "end": page_no,
                "display": f"с. {page_no}",
            }
            assert spans[0]["offset_start"] == 0
            assert spans[0]["offset_end"] == len(sec.body)

            # source_id от canonical (pdf ≡ original, Л3): src-<16hex>
            assert SRC_ID_RE.fullmatch(sec.meta["source_id"])
            assert sec.meta["source_id"] == make_source_id(sha)

        # Реальные номера, а не seq: 2 и 4 (не 1 и 2)
        assert [s.meta["locator_spans"][0]["locator"]["start"] for s in sections] == [2, 4]

    async def test_spans_roundtrip_via_from_meta(self, tmp_path):
        """meta-спаны парсятся обратно в LocatorSpan с теми же координатами.

        Маркер --PAGE-7-- на ФИЗИЧЕСКОЙ 7-й странице: локатор отражает
        физическую страницу, маркер только подтверждает её.
        """
        pages = [[f"--PAGE-{n}-- short page {n}"] for n in range(1, 8)]
        pdf_path = tmp_path / "rt.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        assert len(sections) == 7
        parsed = spans_from_meta(sections[6].meta)
        assert parsed == [LocatorSpan(Locator.page(7), 0, len(sections[6].body))]

    async def test_content_sha256_param_drives_cache_key_and_source_id(
        self, tmp_path, _page_extractor
    ):
        """content_sha256 (из document_store.put) = ключ сегментного кеша
        и основа source_id — без перевычисления хеша файла."""
        pages = [["--PAGE-1-- one"], ["--PAGE-2-- two"]]
        pdf_path = tmp_path / "param.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))
        sha = "e" * 64

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, sha))

        assert (_page_extractor / segments_cache_filename(sha)).exists()
        for sec in sections:
            assert sec.meta["source_id"] == make_source_id(sha)

    async def test_self_computed_full_sha_when_param_absent(self, tmp_path, _page_extractor):
        """Без параметра продюсер сам берёт полный sha256 файла (pdf ≡ canonical)."""
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- solo"]])
        pdf_path = tmp_path / "solo.pdf"
        pdf_path.write_bytes(pdf_bytes)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        assert sections and sections[0].meta["source_id"] == make_source_id(full_sha256(pdf_bytes))
        assert (_page_extractor / segments_cache_filename(full_sha256(pdf_bytes))).exists()


# ═══════════════════════════════════════════════════════════════
# Чанки из секций получают спаны (chunker → payload)
# ═══════════════════════════════════════════════════════════════


class _QdrantRecorder:
    def __init__(self):
        self.points = []

    def upsert_points(self, pts, collection_name=None):
        self.points.extend(pts)


class TestChunksInheritSpans:
    async def test_chunker_and_payload_carry_spans(self, tmp_path):
        """Схема tools/content.py:514: Section.meta → frontmatter → chunker →
        payload точки (реальный _process_batch) — полныйproducer→citation срез."""
        from mcp_server.indexing.pipeline import IndexingPipeline
        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

        pages = [[f"--PAGE-{n}-- short page {n}"] for n in range(1, 9)]
        pages.append(["--PAGE-9--", "chunkable body of page nine " + "lorem ipsum " * 30])
        pdf_path = tmp_path / "chunks.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))
        sha = full_sha256(pdf_path)

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path, sha))
        assert len(sections) == 9
        sec = next(s for s in sections if "--PAGE-9--" in s.body)
        assert sec.meta["locator_spans"][0]["locator"]["start"] == 9

        # Ровно как _batch_write_sections строит frontmatter из Section.meta
        fm = KnowledgeFrontmatter(
            knowledge_id=sec.meta["knowledge_id"],
            domain=sec.meta["domain"],
            subject=sec.meta["subject"],
            content_type=sec.meta.get("content_type", "book"),
            locator_spans=sec.meta.get("locator_spans"),
            source_id=sec.meta.get("source_id"),
        )
        entry = KnowledgeEntry(frontmatter=fm, content=sec.body)

        with _fallback_chunker()[0]:
            chunker = _fallback_chunker(max_tokens=30, overlap_tokens=10)[1]
            chunks = chunker.chunk(fm.knowledge_id, entry.content, locator_spans=fm.locator_spans)

        assert len(chunks) > 1
        for ch in chunks:
            assert ch.locator_spans == fm.locator_spans
            assert [loc.start for loc in locators_for_chunk(ch)] == [9]

        # Интеграционный срез: payload точки через реальный батч-пайплайн
        qdrant = _QdrantRecorder()
        embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
        with _fallback_chunker()[0]:
            pipe = IndexingPipeline(
                store=SimpleNamespace(),
                qdrant=qdrant,
                embedder=embedder,
                chunker=_fallback_chunker()[1],
            )
            await pipe._process_batch([{"entry": entry, "retries": 0, "event": None}])

        assert qdrant.points
        for pt in qdrant.points:
            assert pt.payload["source_id"] == make_source_id(sha)
            assert pt.payload["locator_kind"] == "page"
            assert pt.payload["locator_start"] == 9
            assert pt.payload["locator_end"] == 9


# ═══════════════════════════════════════════════════════════════
# Fallback-секции → реальные номера страниц (не 1)
# ═══════════════════════════════════════════════════════════════


class TestFallbackRealPageNumbers:
    async def test_first_section_page_is_real_not_one(self, tmp_path):
        """Пустая страница 1: первая секция несёт локатор страницы 2,
        хотя её sequence_number/title = 1 («Страница 1»)."""
        pages = [[], ["--PAGE-2-- real second page body"]]
        pdf_path = tmp_path / "gap.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        assert len(sections) == 1
        assert sections[0].title == "Страница 1"
        loc = sections[0].meta["locator_spans"][0]["locator"]
        assert (loc["start"], loc["end"]) == (2, 2)

    async def test_many_pages_monotonic_real_numbers(self, tmp_path):
        """5 текстовых страниц → локаторы 1..5 по порядку (не «все 1»)."""
        pages = [[f"--PAGE-{n}-- body {n}"] for n in range(1, 6)]
        pdf_path = tmp_path / "five.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        assert [s.meta["locator_spans"][0]["locator"]["start"] for s in sections] == [1, 2, 3, 4, 5]


# ═══════════════════════════════════════════════════════════════
# Негатив (Л1): нет спанов → ключей НЕТ
# ═══════════════════════════════════════════════════════════════


class TestNegativeNoFabrication:
    async def test_pdf_without_text_no_sections(self, tmp_path):
        """Все страницы без текста → нет сегментов, нет секций, нет ключей."""
        pdf_path = tmp_path / "empty.pdf"
        pdf_path.write_bytes(_build_minimal_pdf([[], [], []]))

        sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        assert sections == []

    async def test_extractor_unregistered_v1_path_no_keys(self, tmp_path):
        """Реестр без page-экстрактора → легаси-v1 декомпозиция работает,
        но ключей locator_spans/source_id у секций НЕТ (Л1: не фабриковать)."""
        pages = [["--PAGE-1-- plain text page"], ["--PAGE-2-- another text page"]]
        pdf_path = tmp_path / "noreg.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))

        reset_locator_registry()
        assert "page" not in list_locator_kinds()
        try:
            sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))
        finally:
            register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))

        assert len(sections) == 2  # v1-путь жив (регресс-защита)
        for sec in sections:
            assert "locator_spans" not in sec.meta
            assert "source_id" not in sec.meta


# ═══════════════════════════════════════════════════════════════
# Кеш: HIT v1 = MISS; 64KB-близнецы → разные ключи
# ═══════════════════════════════════════════════════════════════


class TestProducerCache:
    async def test_v1_txt_checkpoint_poison_ignored(self, tmp_path):
        """Яд в v1 `.txt`-кеше не читается: переизвлечение с реальными
        номерами страниц (НЕ «page=1» из яда)."""
        pages = [[f"--PAGE-{n}-- body {n}"] for n in range(1, 4)]
        pdf_path = tmp_path / "poison.pdf"
        pdf_path.write_bytes(_build_minimal_pdf(pages))

        pp = _preprocessor(tmp_path)
        v1_dir = Path(pp._cache_dir)
        v1_dir.mkdir(parents=True, exist_ok=True)
        v1_hash = pp._compute_content_hash(str(pdf_path))
        (v1_dir / f"{v1_hash}.txt").write_text("POISON PAGE 1 ONLY", encoding="utf-8")

        sections = await pp.decompose("", _meta(pdf_path))

        assert [s.meta["locator_spans"][0]["locator"]["start"] for s in sections] == [1, 2, 3]
        assert all("POISON" not in s.body for s in sections)

    async def test_64kb_twins_different_cache_keys(self, tmp_path, _page_extractor):
        """64KB-близнецы (одинаковый размер + идентичные первые 64KB,
        коллидирующие v1 prefix-hash) → РАЗНЫЕ ключи сегментного кеша
        и корректные собственные спаны."""
        filler = [f"FILLER {i:06d} " + "x" * 52 for i in range(1000)]
        twin_a = _build_minimal_pdf([filler, ["--PAGE-2-- TWIN ALPHA tail"], ["--PAGE-3-- alpha-end"]])
        twin_b = _build_minimal_pdf([filler, ["--PAGE-2-- TWIN BRAVO tail"], ["--PAGE-3-- bravo-end"]])

        assert len(twin_a) == len(twin_b)
        assert twin_a[:65536] == twin_b[:65536]
        assert full_sha256(twin_a) != full_sha256(twin_b)

        pa = tmp_path / "twin_a.pdf"
        pb = tmp_path / "twin_b.pdf"
        pa.write_bytes(twin_a)
        pb.write_bytes(twin_b)

        pp = _preprocessor(tmp_path)
        # v1 prefix-hash коллидирует — сегментный режим обязан этого не делать
        assert pp._compute_content_hash(str(pa)) == pp._compute_content_hash(str(pb))

        secs_a = await pp.decompose("", _meta(pa))
        secs_b = await pp.decompose("", _meta(pb))

        sha_a, sha_b = full_sha256(twin_a), full_sha256(twin_b)
        assert (_page_extractor / segments_cache_filename(sha_a)).exists()
        assert (_page_extractor / segments_cache_filename(sha_b)).exists()
        # Собственные данные близнецов не перезаписали друг друга
        alpha = [s for s in secs_a if "ALPHA" in s.body]
        bravo = [s for s in secs_b if "BRAVO" in s.body]
        assert alpha and alpha[0].meta["locator_spans"][0]["locator"]["start"] == 2
        assert bravo and bravo[0].meta["locator_spans"][0]["locator"]["start"] == 2
        assert secs_a[0].meta["source_id"] == make_source_id(sha_a)
        assert secs_b[0].meta["source_id"] == make_source_id(sha_b)
        assert secs_a[0].meta["source_id"] != secs_b[0].meta["source_id"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
