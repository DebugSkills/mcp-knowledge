"""Unit tests: content/locator.py — реестр экстракторов + модели Locator/Segment.

Реестр — 1:1 по образцу content/registry.py:14-47 (register с отказом на
дубликат, get со списком доступных kinds, list_locator_kinds, reset).
"""

from __future__ import annotations

import dataclasses

import pytest
from mcp_server.content.locator import (
    Locator,
    LocatorExtractor,
    Segment,
    get_locator_extractor,
    list_locator_kinds,
    register_locator_extractor,
    reset_locator_registry,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Изоляция глобального реестра между тестами."""
    reset_locator_registry()
    yield
    reset_locator_registry()


class _MockExtractor(LocatorExtractor):
    kind = "mock_kind"

    def extract_segments(
        self,
        source,
        *,
        content_sha256=None,
        cancel_event=None,
    ):
        return []


# ═══════════════════════════════════════════════════════════════
# Модели Locator / Segment
# ═══════════════════════════════════════════════════════════════


class TestLocatorModel:
    def test_page_single_display_ru(self):
        loc = Locator.page(7)
        assert loc.kind == "page"
        assert (loc.start, loc.end) == (7, 7)
        assert loc.display == "с. 7"

    def test_page_range_display_ru_en_dash(self):
        loc = Locator.page(120, 145)
        assert (loc.start, loc.end) == (120, 145)
        assert loc.display == "с. 120–145"  # U+2013, как в плане §3.2

    def test_locator_frozen(self):
        loc = Locator.page(1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            loc.start = 2  # type: ignore[misc]

    def test_segment_to_dict_roundtrip(self):
        seg = Segment(locator=Locator.page(9, 10), text="текст сегмента")
        restored = Segment.from_dict(seg.to_dict())
        assert restored == seg
        assert restored.locator.display == "с. 9–10"


# ═══════════════════════════════════════════════════════════════
# Реестр
# ═══════════════════════════════════════════════════════════════


class TestRegistry:
    def test_register_and_get(self):
        ext = _MockExtractor()
        register_locator_extractor(ext)
        assert get_locator_extractor("mock_kind") is ext

    def test_duplicate_register_raises(self):
        register_locator_extractor(_MockExtractor())
        with pytest.raises(ValueError, match="already registered"):
            register_locator_extractor(_MockExtractor())

    def test_unknown_kind_raises_with_available_list(self):
        ext = _MockExtractor()
        register_locator_extractor(ext)
        with pytest.raises(ValueError) as exc:
            get_locator_extractor("nope")
        assert "nope" in str(exc.value)
        assert "Available kinds" in str(exc.value)
        assert "mock_kind" in str(exc.value)

    def test_list_locator_kinds_sorted(self):
        register_locator_extractor(_MockExtractor())
        assert list_locator_kinds() == ["mock_kind"]

    def test_reset_clears(self):
        register_locator_extractor(_MockExtractor())
        reset_locator_registry()
        with pytest.raises(ValueError):
            get_locator_extractor("mock_kind")
        assert list_locator_kinds() == []

    def test_abc_cannot_instantiate(self):
        with pytest.raises(TypeError):
            LocatorExtractor()  # type: ignore[abstract]

    def test_pdf_extractor_registers_under_page_kind(self, tmp_path):
        from mcp_server.content.locator import PDFLocatorExtractor

        ext = PDFLocatorExtractor(cache_dir=tmp_path)
        register_locator_extractor(ext)
        assert get_locator_extractor("page") is ext
        assert "page" in list_locator_kinds()
