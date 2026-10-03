"""Ф2b1: спаны локаторов на уровне СЕКЦИЙ + offsets чанкера.

Trace: code-2026-10-02-bibliography, план §3.2 (строки 122-149), фаза Ф2.

Проверяется:
- Round-trip инвариант (§3.2:130): секция со спанами → сериализация .md →
  чтение = идентичные offsets (locator_spans в frontmatter СЕКЦИИ).
- Маппинг (§3.2:147): locators(chunk) = {L : span(L) ∩ [char_start, char_end) ≠ ∅};
  чанк на стыке kind/диапазонов — список БЕЗ схлопывания; overlap-чанк —
  объединение спанов.
- Инвариант C: offsets вычисляются ДО мутаций чанкера — .strip() тела и
  вставка "## header" их НЕ сдвигают (offsets относятся к сериализованному
  телу секции, а не к мутированному чанк-контенту).
- Л1 provenance: секция без спанов → у чанка поля locator_spans НЕТ
  (не пустой список-заглушка).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server.content.locator import (
    Locator,
    LocatorSpan,
    locators_for_chunk,
    locators_for_range,
    spans_to_meta,
)
from mcp_server.models import Chunk, KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore


def _fallback_chunker(max_tokens: int = 512, overlap_tokens: int = 64):
    """Chunker с fallback-токенайзером (точная char-нарезка, без HF-модели)."""
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


# ── A. Round-trip: секция → .md → чтение = те же offsets ────────────────


class TestSectionSpansRoundTrip:
    def test_round_trip_identical_offsets(self, tmp_path: Path):
        """write→read через реальную сериализацию .md: offsets идентичны."""
        spans = [
            LocatorSpan(Locator.page(120, 145), 0, 8412),
            LocatorSpan(Locator("timestamp", 5.0, 9.5, "00:05–00:09"), 8412, 12000),
        ]
        fm = KnowledgeFrontmatter(
            knowledge_id="test-roundtrip-01",
            domain="engineering",
            subject="python",
            content_type="book",
            locator_spans=spans_to_meta(spans),
        )
        # Тело нормализовано (без ведущих/хвостовых whitespace): сериализация
        # _write_file добавляет хвостовой "\n", _parse_text снимает его
        # .strip()-ом — «тело КАК СЕРИАЛИЗУЕТСЯ В .md» = нормализованное тело,
        # относительно него и определены offsets спанов (round-trip стабилен).
        body = "Тело секции. " * 9 + "Тело секции."
        entry = KnowledgeEntry(frontmatter=fm, content=body)

        path = tmp_path / "test-roundtrip-01.md"
        MarkdownStore._write_file(path, entry)
        text = path.read_text(encoding="utf-8")
        parsed = MarkdownStore._parse_text(text)

        assert parsed.frontmatter.locator_spans == spans_to_meta(spans)
        # Сериализованное тело не мутировано (offsets остаются осмысленными)
        assert parsed.content == body

    def test_no_spans_no_field_in_yaml(self, tmp_path: Path):
        """Л1: спанов нет → ключа locator_spans в YAML НЕТ (не пустой список)."""
        fm = KnowledgeFrontmatter(
            knowledge_id="test-nospans-01",
            domain="engineering",
            subject="python",
        )
        entry = KnowledgeEntry(frontmatter=fm, content="Просто текст.")
        path = tmp_path / "test-nospans-01.md"
        MarkdownStore._write_file(path, entry)

        text = path.read_text(encoding="utf-8")
        assert "locator_spans" not in text
        parsed = MarkdownStore._parse_text(text)
        assert parsed.frontmatter.locator_spans is None

    def test_section_meta_carries_spans_to_frontmatter(self):
        """Схема: Section.meta['locator_spans'] → поле frontmatter секции."""
        from mcp_server.content.preprocessor import Section

        spans = [LocatorSpan(Locator.page(7), 0, 100)]
        section = Section(
            title="T",
            body="Тело",
            sequence_number=1,
            meta={"locator_spans": spans_to_meta(spans)},
        )
        fm = KnowledgeFrontmatter(
            knowledge_id="test-meta-carry-01",
            domain="d",
            subject="s",
            locator_spans=section.meta.get("locator_spans"),
        )
        assert fm.locator_spans == spans_to_meta(spans)


# ── D. Intersection-маппинг: locators(chunk) ────────────────────────────


class TestLocatorsForRange:
    def test_chunk_inside_single_page(self):
        """Чанк внутри страницы → один локатор."""
        spans = [LocatorSpan(Locator.page(5), 0, 100)]
        result = locators_for_range(spans, 10, 20)
        assert result == [Locator.page(5)]

    def test_kind_boundary_no_collapse(self):
        """Чанк на стыке двух kind → список ОБЕИХ локаторов, без схлопывания."""
        spans = [
            LocatorSpan(Locator.page(5), 0, 100),
            LocatorSpan(Locator("timestamp", 5.0, 9.5, "00:05–00:09"), 90, 200),
        ]
        result = locators_for_range(spans, 80, 120)
        assert len(result) == 2
        kinds = {loc.kind for loc in result}
        assert kinds == {"page", "timestamp"}

    def test_adjacent_ranges_no_collapse(self):
        """Чанк на стыке двух диапазонов одного kind → 2 элемента, не мержатся."""
        spans = [
            LocatorSpan(Locator.page(5), 0, 100),
            LocatorSpan(Locator.page(6), 100, 200),
        ]
        result = locators_for_range(spans, 50, 150)
        assert result == [Locator.page(5), Locator.page(6)]

    def test_overlap_chunk_union(self):
        """Overlap-чанк (диапазон покрывает несколько спанов) → объединение."""
        spans = [
            LocatorSpan(Locator.page(1), 0, 100),
            LocatorSpan(Locator.page(2), 100, 200),
            LocatorSpan(Locator("timestamp", 1.0, 2.0, "00:01–00:02"), 150, 300),
        ]
        result = locators_for_range(spans, 0, 250)
        assert len(result) == 3
        assert {loc.kind for loc in result} == {"page", "timestamp"}

    def test_zero_intersection_touching_boundary_excluded(self):
        """Касание границы (полуоткрытые интервалы) — НЕ пересечение."""
        spans = [LocatorSpan(Locator.page(5), 0, 100)]
        assert locators_for_range(spans, 100, 110) == []
        assert locators_for_range(spans, 99, 100) == [Locator.page(5)]
        assert locators_for_range(spans, 200, 300) == []

    def test_duplicate_locator_deduplicated(self):
        """Формула плана задаёт МНОЖЕСТВО локаторов: дубли схлопываются."""
        spans = [
            LocatorSpan(Locator.page(5), 0, 50),
            LocatorSpan(Locator.page(5), 60, 120),
        ]
        result = locators_for_range(spans, 0, 120)
        assert result == [Locator.page(5)]


class TestLocatorsForChunk:
    def test_duck_typing_chunk_fields(self):
        """Chunk с char-диапазоном и спанами → пересечённые локаторы."""
        spans = [
            LocatorSpan(Locator.page(5), 0, 100),
            LocatorSpan(Locator.page(6), 100, 200),
        ]
        chunk = Chunk(
            chunk_id="kid#0",
            knowledge_id="kid",
            content="текст",
            chunk_index=0,
            token_count=1,
            char_start=50,
            char_end=150,
            locator_spans=spans_to_meta(spans),
        )
        result = locators_for_chunk(chunk)
        assert result == [Locator.page(5), Locator.page(6)]

    def test_missing_fields_empty_not_fabricated(self):
        """Нет полей (спанов/границ) → [] — ничего не фабрикуется."""
        chunk = Chunk(
            chunk_id="kid#0",
            knowledge_id="kid",
            content="текст",
            chunk_index=0,
            token_count=1,
        )
        assert locators_for_chunk(chunk) == []

    def test_none_char_offsets_empty(self):
        """Границы неизвестны (decode не восстановил текст) → [] (Л1)."""
        chunk = Chunk(
            chunk_id="kid#0",
            knowledge_id="kid",
            content="текст",
            chunk_index=0,
            token_count=1,
            char_start=None,
            char_end=None,
            locator_spans=[{"locator": {"kind": "page", "start": 1, "end": 1,
                                        "display": "с. 1"},
                            "offset_start": 0, "offset_end": 10}],
        )
        assert locators_for_chunk(chunk) == []


# ── C. Offsets чанкера ДО мутаций (.strip / "## header") ────────────────


class TestChunkerOffsetsBeforeMutations:
    def test_offsets_survive_strip_leading_whitespace(self):
        """Ведущие пробелы тела: char-границы указывают на stripped-тело
        в координатах ИСХОДНОГО content (offsets не сдвигаются strip'ом)."""
        with _fallback_chunker()[0] as _:
            from mcp_server.indexing.chunker import MarkdownChunker

            chunker = MarkdownChunker(max_tokens=512)
            content = "## Alpha\n\n\n   Первое тело секции здесь.\n"
            chunks = chunker.chunk("kid-strip", content)

            assert len(chunks) == 1
            ch = chunks[0]
            assert ch.content == "Первое тело секции здесь."
            # Инвариант: срез исходного content по границам = телу чанка
            assert content[ch.char_start:ch.char_end] == ch.content
            # Граница указывает на первый не-whitespace символ (не 0-сдвиг)
            assert content[ch.char_start] == "П"

    def test_offsets_survive_header_insertion(self):
        """Длинная секция: вставка "## header" в чанк-контент НЕ сдвигает
        offsets — char-границы указывают на chunk_text-часть тела без префикса."""
        with _fallback_chunker(max_tokens=25, overlap_tokens=10)[0] as _:
            from mcp_server.indexing.chunker import MarkdownChunker

            chunker = MarkdownChunker(max_tokens=25, overlap_tokens=10)
            body = "слово " * 300  # длинная секция → нарезка с overlap
            content = f"## Doc\n\n{body}"
            chunks = chunker.chunk("kid-hdr", content, section_header="Doc")

            assert len(chunks) > 1
            prefix = "## Doc\n\n"
            for ch in chunks:
                assert ch.content.startswith(prefix)
                # Инвариант C: offsets НЕ включают вставленный header —
                # срез по границам = chunk-контент БЕЗ префикса
                assert content[ch.char_start:ch.char_end] == ch.content[len(prefix):]

    def test_fallback_long_section_exact_char_offsets(self):
        """Fallback-нарезка по символам: границы точные и монотонные,
        перекрытие чанков (overlap) отражено в диапазонах."""
        with _fallback_chunker(max_tokens=30, overlap_tokens=10)[0] as _:
            from mcp_server.indexing.chunker import MarkdownChunker

            chunker = MarkdownChunker(max_tokens=30, overlap_tokens=10)
            content = "ab " * 500
            chunks = chunker.chunk("kid-fb", content)

            assert len(chunks) > 1
            prev_start = -1
            for ch in chunks:
                assert ch.char_start is not None and ch.char_end is not None
                assert 0 <= ch.char_start < ch.char_end <= len(content)
                assert ch.char_start > prev_start  # монотонность
                prev_start = ch.char_start
            # Overlap: соседние чанки перекрываются (это и порождает
            # «объединение спанов» у маппинга)
            assert any(
                chunks[i + 1].char_start < chunks[i].char_end
                for i in range(len(chunks) - 1)
            )

    def test_no_header_no_spans_fields_absent(self):
        """Л1: спаны не переданы → у чанков поля locator_spans НЕТ (None),
        model_dump(exclude_none=True) не содержит ключа-заглушки."""
        with _fallback_chunker()[0] as _:
            from mcp_server.indexing.chunker import MarkdownChunker

            chunker = MarkdownChunker(max_tokens=512)
            chunks = chunker.chunk("kid-neg", "## T\n\nТело без спанов.")
            assert len(chunks) == 1
            ch = chunks[0]
            assert ch.locator_spans is None
            assert "locator_spans" not in ch.model_dump(exclude_none=True)
            # char-границы при этом вычислены (это не фабрикация, а координаты)
            assert ch.char_start is not None and ch.char_end is not None

    def test_chunks_inherit_section_spans_and_map(self):
        """Чанки наследуют спаны СЕКЦИИ; маппинг различает их по диапазонам:
        чанки в зоне page=1 → один локатор; чанк через границу → оба."""
        spans = [
            LocatorSpan(Locator.page(1), 0, 60),
            LocatorSpan(Locator.page(2), 60, 120),
        ]
        with _fallback_chunker(max_tokens=15, overlap_tokens=5)[0] as _:
            from mcp_server.indexing.chunker import MarkdownChunker

            chunker = MarkdownChunker(max_tokens=15, overlap_tokens=5)
            content = "xy " * 40  # ~120 chars, граница спанов в середине
            chunks = chunker.chunk(
                "kid-spans", content, locator_spans=spans_to_meta(spans)
            )

            assert len(chunks) > 1
            for ch in chunks:
                assert ch.locator_spans == spans_to_meta(spans)
            # Есть чанки ровно с одним локатором (внутри страницы)
            single = [c for c in chunks if len(locators_for_chunk(c)) == 1]
            assert single, "должен быть чанк внутри одной страницы"
            # Есть чанк на границе → оба локатора без схлопывания
            boundary = [
                c for c in chunks
                if c.char_start < 60 < c.char_end
            ]
            assert boundary, "должен быть чанк, пересекающий границу спанов"
            for c in boundary:
                result = locators_for_chunk(c)
                assert [loc.display for loc in result] == ["с. 1", "с. 2"]


if __name__ == "__main__":
    sys_exit = pytest.main([__file__, "-q"])
    raise SystemExit(sys_exit)
