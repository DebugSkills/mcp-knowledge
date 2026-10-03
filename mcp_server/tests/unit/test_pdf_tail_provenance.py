"""P2 (tail-provenance): хвостовые секции PDF получают locator_spans + source_id.

trace_id: code-2026-10-02-bibliography.
Spec (SSOT): `.tmp/2026-10-04-bibliography-tail-provenance-fix-spec.md`.

Проблема: `splitting.py` (split + strip + " ".join) схлопывает пробельные
серии в чанках большого tail (>4000 симв.) → `body.find(chunk, cursor)` = -1
→ секции «Последний раздел…» остаются без locator_spans/source_id (18/80).
Фикс: `locate_chunk` — fast-path verbatim → нормализованный поиск с картой
позиций (`normalize_with_map`), fail-safe Л1 (короткий/немонотонный/отсутствующий
чанк → None → ключей нет).

Тесты:
- RED-контроль (heading-путь принуждён patch'ем `_detect_headings` — иначе
  `_fallback_per_page` даст ложно-зелёный результат, V2-1): у КАЖДОЙ
  хвостовой секции есть locator_spans + source_id, страницы монотонны в [1,N].
- Юнит `locate_chunk`: verbatim / whitespace-мутация / дубликат-боилерплейт
  при монотонном курсоре / отсутствующий и короткий (<MIN_NORM) чанк.
- Юнит `normalize_with_map`: свойство карты + сентинел.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from unit._pdf_fixtures import _build_minimal_pdf

from mcp_server.content.locator import (
    PDFLocatorExtractor,
    register_locator_extractor,
    reset_locator_registry,
)
from mcp_server.content.pdf_preprocessor import (
    MIN_NORM,
    PDFPreprocessor,
    locate_chunk,
    normalize_with_map,
)
from mcp_server.content.preprocessor import ImportMeta

HEADING_A = "FIRSTSECTION"
HEADING_B = "SECONDSECTION"
TAIL_PAGES = 12          # страниц хвоста (после второй heading)
SENTENCES_PER_PAGE = 6
TOTAL_PAGES = 1 + TAIL_PAGES


@pytest.fixture(autouse=True)
def _page_extractor(tmp_path):
    """Сегментный режим: page-экстрактор с изолированным tmp-кешем."""
    reset_locator_registry()
    register_locator_extractor(PDFLocatorExtractor(cache_dir=tmp_path / "segments"))
    yield
    reset_locator_registry()


def _preprocessor(tmp_path) -> PDFPreprocessor:
    pp = PDFPreprocessor()
    pp._cache_dir = str(tmp_path / "v1cache")
    return pp


def _meta(pdf_path) -> ImportMeta:
    return ImportMeta(
        domain="test",
        subject="test",
        title="tail-provenance",
        source_path=str(pdf_path),
    )


def _build_tail_pdf() -> bytes:
    """PDF: стр.1 = 2 «заголовка» + интро; стр.2..13 = длинный хвост.

    Хвост > 4000 симв., предложения разделены `\n` (внутри страницы) и `\n\n`
    (между страницами) → `_split_sentences` + `" ".join` схлопывают их в
    пробелы → чанки невербатимны → `body.find` их не находит (RED на старом
    коде).
    """
    pages = [[HEADING_A, "intro sentence one.", "intro sentence two.", HEADING_B]]
    for p in range(TAIL_PAGES):
        pages.append([
            f"Tail sentence {p}_{s} about the QUIC transport protocol specification details."
            for s in range(SENTENCES_PER_PAGE)
        ])
    return _build_minimal_pdf(pages)


async def _fake_headings(self, source_path):
    """Принудительный heading-путь: 2 заголовка → _sections_by_headings.

    Позиции в tuple игнорируются _sections_by_headings (ищет по тексту),
    важны только тексты — они присутствуют на стр.1 full_text.
    """
    return [(0, HEADING_A), (0, HEADING_B)]


# ═══════════════════════════════════════════════════════════════
# RED-контроль: heading-путь → каждая хвостовая секция несёт спаны
# ═══════════════════════════════════════════════════════════════


class TestTailProvenanceHeadingPath:
    async def test_every_tail_section_has_spans_and_source_id(self, tmp_path):
        """У КАЖДОЙ хвостовой секции есть locator_spans + source_id, страницы
        монотонны и в [1, TOTAL_PAGES]. На старом коде (body.find) падает —
        хвостовые чанки нелокализуемы (RED)."""
        pdf_path = tmp_path / "tail.pdf"
        pdf_path.write_bytes(_build_tail_pdf())

        with patch.object(PDFPreprocessor, "_detect_headings", new=_fake_headings):
            sections = await _preprocessor(tmp_path).decompose("", _meta(pdf_path))

        tail = [s for s in sections if s.title.startswith("Последний раздел")]
        assert tail, "ожидались хвостовые секции (heading-путь не сработал)"
        assert len(tail) >= 2, f"хвост не нарезан на чанки: {len(tail)} секций"

        prev_last_page = 0
        for s in tail:
            assert "locator_spans" in s.meta, f"{s.title}: нет locator_spans (RED)"
            assert "source_id" in s.meta, f"{s.title}: нет source_id"
            spans = s.meta["locator_spans"]
            assert spans, f"{s.title}: locator_spans пуст"
            for sp in spans:
                loc = sp["locator"]
                assert loc["kind"] == "page"
                assert 1 <= loc["start"] <= loc["end"] <= TOTAL_PAGES
            first_page = min(sp["locator"]["start"] for sp in spans)
            last_page = max(sp["locator"]["end"] for sp in spans)
            assert first_page >= prev_last_page, (
                f"{s.title}: страницы не монотонны ({first_page} < {prev_last_page})"
            )
            prev_last_page = last_page


# ═══════════════════════════════════════════════════════════════
# Юнит locate_chunk
# ═══════════════════════════════════════════════════════════════


class TestLocateChunk:
    def test_verbatim_exact_range(self):
        body = "first sentence. second sentence. third sentence."
        norm_body, map_body = normalize_with_map(body)
        chunk = "second sentence."
        expected = body.find(chunk)
        assert locate_chunk(body, chunk, 0, norm_body, map_body, 0, 0) == (
            expected,
            expected + len(chunk),
        )

    def test_whitespace_mutation_same_range(self):
        """`\n`-разделители → чанк после `" ".join` — невербатимный, но тот
        же диапазон через нормализованный поиск."""
        body = "first sentence.\nsecond sentence.\nthird sentence."
        chunk = "first sentence. second sentence."  # как splitting.py: " ".join
        norm_body, map_body = normalize_with_map(body)
        r = locate_chunk(body, chunk, 0, norm_body, map_body, 0, 0)
        assert r == (0, len("first sentence.\nsecond sentence."))
        s, e = r
        assert re.sub(r"\s+", " ", body[s:e]).strip() == re.sub(r"\s+", " ", chunk).strip()

    def test_duplicate_boilerplate_monotonic_cursor(self):
        """Одинаковая фраза на двух «страницах»: монотонный курсор находит
        РАЗНЫЕ корректные вхождения."""
        body = (
            "Page one header text here.\n"
            "unique alpha sentence.\n"
            "shared boilerplate sentence.\n"
            "Page two header text here.\n"
            "unique beta sentence.\n"
            "shared boilerplate sentence."
        )
        norm_body, map_body = normalize_with_map(body)
        chunk1 = (
            "Page one header text here. unique alpha sentence. shared boilerplate sentence."
        )
        r1 = locate_chunk(body, chunk1, 0, norm_body, map_body, 0, 0)
        assert r1 is not None
        s1, e1 = r1
        assert s1 == 0
        assert "unique alpha" in body[s1:e1]

        cursor_norm = len(re.sub(r"\s+", " ", body[:e1]))
        chunk2 = (
            "Page two header text here. unique beta sentence. shared boilerplate sentence."
        )
        r2 = locate_chunk(body, chunk2, e1, norm_body, map_body, cursor_norm, e1)
        assert r2 is not None
        s2, e2 = r2
        assert s2 > e1, "второе вхождение обязано быть ПОСЛЕ первого"
        assert "unique beta" in body[s2:e2]

    def test_missing_chunk_none(self):
        body = "the quick brown fox jumps over the lazy dog."
        norm_body, map_body = normalize_with_map(body)
        assert (
            locate_chunk(body, "completely absent phrase", 0, norm_body, map_body, 0, 0)
            is None
        )

    def test_short_norm_chunk_none(self):
        """Невербатимный чанк с collapse < MIN_NORM → None (fail-safe Л1)."""
        body = "the quick brown fox jumps over the lazy dog."
        norm_body, map_body = normalize_with_map(body)
        short = "quick\nfox"  # collapse "quick fox" (len 9 < MIN_NORM), невербатимно
        assert locate_chunk(body, short, 0, norm_body, map_body, 0, 0) is None
        assert MIN_NORM == 24


# ═══════════════════════════════════════════════════════════════
# Юнит normalize_with_map
# ═══════════════════════════════════════════════════════════════


class TestNormalizeWithMap:
    def test_map_property_and_sentinel(self):
        s = "abc  def\tghi\n"
        norm, pos_map = normalize_with_map(s)
        assert norm == "abc def ghi "
        assert len(pos_map) == len(norm) + 1
        for i, ch in enumerate(norm):
            src = s[pos_map[i]]
            if ch == " ":
                assert src.isspace()
            else:
                assert src == ch
        assert pos_map[-1] == len(s)

    def test_no_whitespace_identity_map(self):
        s = "no-whitespace-here"
        norm, pos_map = normalize_with_map(s)
        assert norm == s
        assert pos_map == list(range(len(s))) + [len(s)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
