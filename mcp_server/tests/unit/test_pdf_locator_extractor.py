"""Unit tests: content/locator.py — PDFLocatorExtractor (kind="page", 1-based).

Покрытие по acceptance Ф2 (план, строка 335):
- Golden: программный PDF с маркерами --PAGE-N-- → сегменты с ТОЧНЫМИ
  номерами страниц (off-by-one ловится: номер парсится из самого текста).
- Негатив: страницы без текста → пустой список (Л1: локаторы не выдуманы);
  мусорные байты → ошибка пробрасывается, кеш не пишется.
- Кеш: MISS → extract → HIT; v1 `.txt`-checkpoint физически не ищется
  (HIT v1 = MISS → переизвлечение, НЕ «page=1»).
- 64KB-близнецы (одинаковый размер, идентичные первые 64KB) → РАЗНЫЕ ключи.

Фикстура-PDF: _build_minimal_pdf — общий хелпер tests/unit/_pdf_fixtures
(Ф2b4.E, вынесен из этого модуля): ручной билдер байтов с программно
вычисляемыми byte-offsets и корректным xref — без reportlab / pikepdf /
weasyprint и без сети (air-gap).
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from tests.unit._pdf_fixtures import _build_minimal_pdf

from mcp_server.content.locator import (
    Locator,
    PDFLocatorExtractor,
    Segment,
    full_sha256,
)

V1_PREFIX_WINDOW = 65536  # окно v1 prefix-hash из _compute_content_hash


# ═══════════════════════════════════════════════════════════════
# Golden: точные номера страниц
# ═══════════════════════════════════════════════════════════════


class TestGoldenPageNumbers:
    def test_markers_match_locator_numbers(self, tmp_path):
        """Маркер --PAGE-N-- в тексте страницы ↔ locator.start == N (1-based)."""
        pages = [
            [f"--PAGE-{num}--", f"body of page {num} lorem ipsum dolor sit amet"]
            for num in range(1, 6)
        ]
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        segments = ext.extract_segments(_build_minimal_pdf(pages))

        assert len(segments) == 5
        for seg in segments:
            m = re.search(r"--PAGE-(\d+)--", seg.text)
            assert m is not None, f"маркер потерян в сегменте: {seg.locator}"
            n = int(m.group(1))
            assert seg.locator.kind == "page"
            assert seg.locator.start == n
            assert seg.locator.end == n
            assert seg.locator.display == f"с. {n}"
            assert f"body of page {n}" in seg.text
        assert [s.locator.start for s in segments] == [1, 2, 3, 4, 5]

    def test_page_boundaries_preserved_no_join(self, tmp_path):
        """Текст каждой страницы — только её страницы (v1 join потерял бы границы)."""
        pages = [[f"--PAGE-{num}-- page {num} content"] for num in range(1, 4)]
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        segments = ext.extract_segments(_build_minimal_pdf(pages))

        assert len(segments) == 3
        for i, seg in enumerate(segments):
            for j in range(1, 4):
                if j == i + 1:
                    assert f"--PAGE-{j}--" in seg.text
                else:
                    assert f"--PAGE-{j}--" not in seg.text

    def test_str_path_source(self, tmp_path):
        """Контракт bytes|str: путь к файлу даёт тот же результат."""
        pdf_path = tmp_path / "doc.pdf"
        pdf_path.write_bytes(
            _build_minimal_pdf([["--PAGE-1-- one"], ["--PAGE-2-- two"]])
        )
        cache_dir = tmp_path / "cache"
        ext = PDFLocatorExtractor(cache_dir=cache_dir)

        segments = ext.extract_segments(str(pdf_path))

        assert [s.locator.start for s in segments] == [1, 2]
        assert (cache_dir / f"{full_sha256(str(pdf_path))}.segments.v2.json").exists()


# ═══════════════════════════════════════════════════════════════
# Негатив: Л1 — нет спанов → нет полей
# ═══════════════════════════════════════════════════════════════


class TestNegativeNoFabrication:
    def test_pages_without_text_yield_empty_list(self, tmp_path):
        """Пустые content streams → [] : локаторы к пустым страницам НЕ выдуманы."""
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        segments = ext.extract_segments(_build_minimal_pdf([[], [], []]))
        assert segments == []

    def test_partially_empty_pages_skipped(self, tmp_path):
        """Страница без текста пропускается, нумерация остальных не сдвигается."""
        pages = [["--PAGE-1-- has text"], [], ["--PAGE-3-- has text too"]]
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        segments = ext.extract_segments(_build_minimal_pdf(pages))
        assert [s.locator.start for s in segments] == [1, 3]

    def test_invalid_bytes_raise_no_cache_written(self, tmp_path):
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        with pytest.raises(Exception):  # noqa: B017 — парсер ошибок не фабрикует сегменты
            ext.extract_segments(b"definitely not a pdf")
        assert list(tmp_path.iterdir()) == []

    def test_cancel_event_aborts_before_first_page(self, tmp_path):
        ev = asyncio.Event()
        ev.set()
        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        with pytest.raises(asyncio.CancelledError):
            ext.extract_segments(
                _build_minimal_pdf([["--PAGE-1-- x"]]), cancel_event=ev
            )


# ═══════════════════════════════════════════════════════════════
# Сегментный кеш v2
# ═══════════════════════════════════════════════════════════════


class TestSegmentsCache:
    def test_miss_extract_then_hit(self, tmp_path):
        """MISS → извлечение + запись v2; подмена файла → второй вызов читает кеш."""
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- alpha"], ["--PAGE-2-- beta"]])
        sha = "a" * 64
        ext = PDFLocatorExtractor(cache_dir=tmp_path)

        segments = ext.extract_segments(pdf_bytes, content_sha256=sha)
        assert [s.locator.start for s in segments] == [1, 2]
        cache_file = tmp_path / f"{sha}.segments.v2.json"
        assert cache_file.exists()

        # Подменяем кеш сентинелом: HIT обязан вернуть именно его
        sentinel = [Segment(locator=Locator.page(999), text="SENTINEL")]
        cache_file.write_text(
            json.dumps(
                {"version": 2, "kind": "page", "segments": [s.to_dict() for s in sentinel]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert ext.extract_segments(pdf_bytes, content_sha256=sha) == sentinel

    def test_v1_txt_cache_physically_not_searched(self, tmp_path):
        """HIT v1 = MISS: `.txt`-checkpoint игнорируется, извлечение повторяется."""
        pdf_bytes = _build_minimal_pdf(
            [["--PAGE-1-- one"], ["--PAGE-2-- two"], ["--PAGE-3-- three"]]
        )
        sha = "b" * 64
        v1_file = tmp_path / f"{sha}.txt"
        v1_file.write_text("POISON PAGE 1 ONLY", encoding="utf-8")
        ext = PDFLocatorExtractor(cache_dir=tmp_path)

        segments = ext.extract_segments(pdf_bytes, content_sha256=sha)

        # Не «page=1» из яда, а полное переизвлечение с точными номерами
        assert [s.locator.start for s in segments] == [1, 2, 3]
        assert all("POISON" not in s.text for s in segments)
        assert (tmp_path / f"{sha}.segments.v2.json").exists()
        assert v1_file.read_text(encoding="utf-8") == "POISON PAGE 1 ONLY"

    def test_corrupt_v2_cache_is_miss(self, tmp_path):
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- x"]])
        sha = "c" * 64
        cache_file = tmp_path / f"{sha}.segments.v2.json"
        cache_file.write_text("{ not json", encoding="utf-8")
        ext = PDFLocatorExtractor(cache_dir=tmp_path)

        segments = ext.extract_segments(pdf_bytes, content_sha256=sha)
        assert [s.locator.start for s in segments] == [1]

    def test_wrong_version_cache_is_miss(self, tmp_path):
        pdf_bytes = _build_minimal_pdf([["--PAGE-1-- x"]])
        sha = "d" * 64
        cache_file = tmp_path / f"{sha}.segments.v2.json"
        cache_file.write_text(
            json.dumps({"version": 1, "kind": "page", "segments": []}), encoding="utf-8"
        )
        ext = PDFLocatorExtractor(cache_dir=tmp_path)

        segments = ext.extract_segments(pdf_bytes, content_sha256=sha)
        assert [s.locator.start for s in segments] == [1]

    def test_64kb_twins_different_cache_keys(self, tmp_path):
        """Близнецы (одинаковый размер + идентичные первые 64KB) → разные ключи.

        Регрессия на слабость v1 prefix-hash: _compute_content_hash
        (первые 64KB + размер) на такой паре коллидирует — полный sha256 нет.
        """
        # Страница 1 (>64KB) у близнецов байт-в-байт одинакова и выносит
        # все объекты страницы 2+ за границу 65536.
        filler = [f"FILLER {i:06d} " + "x" * 52 for i in range(1000)]
        twin_a = _build_minimal_pdf(
            [filler, ["--PAGE-2-- TWIN ALPHA tail"], ["--PAGE-3-- alpha-end"]]
        )
        twin_b = _build_minimal_pdf(
            [filler, ["--PAGE-2-- TWIN BRAVO tail"], ["--PAGE-3-- bravo-end"]]
        )

        # Sanity фикстуры: одинаковый размер, общие первые 64KB, разные хвосты
        assert len(twin_a) == len(twin_b)
        assert twin_a[:V1_PREFIX_WINDOW] == twin_b[:V1_PREFIX_WINDOW]
        assert twin_a != twin_b
        assert full_sha256(twin_a) != full_sha256(twin_b)

        # Демонстрация уязвимости v1 (почему ключ обязан быть полным):
        from mcp_server.content.pdf_preprocessor import PDFPreprocessor

        fa = tmp_path / "twin_a.pdf"
        fb = tmp_path / "twin_b.pdf"
        fa.write_bytes(twin_a)
        fb.write_bytes(twin_b)
        pp = PDFPreprocessor()
        assert pp._compute_content_hash(str(fa)) == pp._compute_content_hash(str(fb))

        # Оба извлечения кладут РАЗНЫЕ кеш-файлы рядом, без перезатирания
        ext = PDFLocatorExtractor(cache_dir=tmp_path / "cache")
        segs_a = ext.extract_segments(twin_a)
        segs_b = ext.extract_segments(twin_b)
        cache = tmp_path / "cache"
        assert (cache / f"{full_sha256(twin_a)}.segments.v2.json").exists()
        assert (cache / f"{full_sha256(twin_b)}.segments.v2.json").exists()
        assert any("ALPHA" in s.text for s in segs_a)
        assert any("BRAVO" in s.text for s in segs_b)
        # Общая >64KB страница 1 сохранила одинаковый текст у обоих
        assert segs_a[0].text == segs_b[0].text
