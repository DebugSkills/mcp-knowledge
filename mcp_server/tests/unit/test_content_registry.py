"""Unit tests: content/registry.py — content_type → preprocessor lookup."""

from __future__ import annotations

import pytest
from mcp_server.content.preprocessor import (
    ContentPreprocessor,
    ImportMeta,
    Section,
    ValidationResult,
)
from mcp_server.content.registry import get, list_types, register, reset


class _MockPreprocessor(ContentPreprocessor):
    content_type = "mock_type"

    def validate(self, content: str, metadata: ImportMeta) -> ValidationResult:
        return ValidationResult(valid=True)

    def decompose(self, content: str, metadata: ImportMeta) -> list[Section]:
        return []


class TestRegistry:
    """Registry operations."""

    def test_register_and_get(self):
        reset()
        pp = _MockPreprocessor()
        register(pp)
        assert get("mock_type") is pp

    def test_unknown_type_raises_valueerror(self):
        reset()
        with pytest.raises(ValueError) as exc:
            get("unknown")
        assert "unknown" in str(exc.value)
        assert "Available" in str(exc.value)

    def test_list_types(self):
        reset()
        pp = _MockPreprocessor()
        register(pp)
        types = list_types()
        assert "mock_type" in types

    def test_duplicate_register_raises(self):
        reset()
        pp1 = _MockPreprocessor()
        register(pp1)
        pp2 = _MockPreprocessor()
        with pytest.raises(ValueError, match="already registered"):
            register(pp2)

    def test_reset_clears(self):
        reset()
        pp = _MockPreprocessor()
        register(pp)
        reset()
        with pytest.raises(ValueError):
            get("mock_type")

    def test_book_is_registered(self):
        """BookPreprocessor должен быть зарегистрирован при импорте content пакета."""
        reset()
        # Re-import triggers registration
        from mcp_server.content.book_preprocessor import BookPreprocessor
        from mcp_server.content.registry import register as reg

        reg(BookPreprocessor(embedder=None, token_counter=None))
        assert "book" in list_types()
        prep = get("book")
        assert prep.content_type == "book"

    def test_error_has_available_list(self):
        reset()
        pp = _MockPreprocessor()
        register(pp)
        with pytest.raises(ValueError) as exc:
            get("pdf")
        assert "mock_type" in str(exc.value)
