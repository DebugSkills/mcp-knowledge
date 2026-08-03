"""Unit tests: content/splitting.py — hybrid semantic splitting (#34)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from mcp_server.content.splitting import (
    structural_split,
    recursive_split,
    Chunk,
    MAX_CHUNK_TOKENS,
    MIN_SECTIONS,
)


class TestStructuralSplit:
    """Stage 1: structural parsing по #/##/###."""

    def test_book_with_headers(self):
        """Книга с чёткими #/## заголовками → секции."""
        content = """# Title
Intro text.

## Chapter 1
Content of chapter 1.

## Chapter 2
Content of chapter 2.

### Subsection 2.1
Deeper content.
"""
        chunks = structural_split(content)
        assert len(chunks) == 4
        assert chunks[0].title == "Title"
        assert "Intro text" in chunks[0].body
        assert chunks[1].title == "Chapter 1"
        assert chunks[2].title == "Chapter 2"
        assert chunks[3].title == "Subsection 2.1"
        assert chunks[0].sequence_number == 1
        assert chunks[3].sequence_number == 4

    def test_single_header(self):
        """Один заголовок → одна секция."""
        content = "# Only Header\nSome text."
        chunks = structural_split(content)
        assert len(chunks) == 1
        assert chunks[0].title == "Only Header"
        assert chunks[0].sequence_number == 1

    def test_no_headers(self,):
        """Без заголовков → одна секция 'Untitled'."""
        content = "Plain text without any markdown headers."
        chunks = structural_split(content)
        assert len(chunks) == 1
        assert chunks[0].title == "Untitled"
        assert content in chunks[0].body

    def test_empty_content(self):
        """Пустой контент → 0 секций."""
        assert structural_split("") == []
        assert structural_split("   ") == []

    def test_headers_with_levels(self):
        """Заголовки разных уровней."""
        content = """# H1
Text 1.

### H3
Text 3.

## H2
Text 2.
"""
        chunks = structural_split(content)
        assert len(chunks) == 3
        assert chunks[0].title == "H1"
        assert chunks[1].title == "H3"
        assert chunks[2].title == "H2"

    def test_h4_ignored(self):
        """#### не считается заголовком секции (только #/##/###)."""
        content = """## Valid Header
Valid body.

#### Ignored Header
Ignored body.
"""
        chunks = structural_split(content)
        assert len(chunks) == 1
        assert chunks[0].title == "Valid Header"

    def test_preamble_before_first_header(self):
        """P2-1: текст до первого заголовка включается в первую секцию."""
        content = """Copyright 2024. All rights reserved.

# Main Title
Content after title.
"""
        chunks = structural_split(content)
        assert len(chunks) == 1
        assert "Copyright 2024" in chunks[0].body
        assert "Main Title" in chunks[0].body

    def test_preamble_in_multi_section(self):
        """Preamble при нескольких секциях попадает в первую."""
        content = """Abstract: This document describes...

# Chapter 1
Content 1.

# Chapter 2
Content 2.
"""
        chunks = structural_split(content)
        assert len(chunks) == 2
        assert "Abstract" in chunks[0].body
        assert "Chapter 1" in chunks[0].body
        assert "Chapter 2" in chunks[1].body


class TestRecursiveSplit:
    """Stage 3: recursive split — гарантия ≤ max_tokens."""

    def _make_token_counter(self, token_map: dict[str, int]):
        """Mock token counter: текст → число токенов."""
        tc = MagicMock()

        def _count(text: str) -> int:
            return token_map.get(text, len(text.split()))

        def _truncate(text: str, max_tok: int) -> str:
            words = text.split()
            if len(words) <= max_tok:
                return text
            return " ".join(words[:max_tok])

        tc.count_tokens = _count
        tc.truncate_to_tokens = _truncate
        return tc

    def test_under_limit(self):
        """Секция ≤ max_tokens → возвращается без изменений."""
        tc = self._make_token_counter({"short text": 5})
        chunk = Chunk(title="Test", body="short text", sequence_number=1)
        result = recursive_split(chunk, max_tokens=512, token_counter=tc)
        assert len(result) == 1
        assert result[0].body == "short text"

    def test_oversized_splits(self):
        """Секция > max_tokens → разбивается по предложениям."""
        tc = self._make_token_counter({
            "First sentence. Second sentence. Third sentence.": 100,
        })
        # Make each sentence "expensive" in tokens
        def _count_tokens(text: str) -> int:
            return len(text.split())
        tc.count_tokens = _count_tokens

        body = ". ".join(["word"] * 40) + "."
        chunk = Chunk(title="Over", body=body, sequence_number=1)
        result = recursive_split(chunk, max_tokens=20, token_counter=tc)
        assert len(result) >= 2  # must split

    def test_no_token_counter(self):
        """Без token_counter → возвращает как есть."""
        chunk = Chunk(title="Test", body="any text", sequence_number=1)
        result = recursive_split(chunk, max_tokens=512, token_counter=None)
        assert len(result) == 1

    def test_single_sentence_oversized(self):
        """Одно предложение > max_tokens → обрезается."""
        tc = self._make_token_counter({"a" * 1000: 1000})

        chunk = Chunk(title="Big", body="a" * 1000, sequence_number=1)
        result = recursive_split(chunk, max_tokens=512, token_counter=tc)
        assert len(result) >= 1  # at least one chunk

    def test_fallback_char_count_no_tokenizer(self):
        """P1-3: без token_counter — char-count fallback (~4 chars/token).

        Секция > max_tokens*4 символов должна быть разбита.
        """
        # ~2500 символов с точками = 2500/4 ≈ 625 «токенов» > 512
        body = ". ".join(["word"] * 250) + "."
        chunk = Chunk(title="Fallback", body=body, sequence_number=1)
        result = recursive_split(chunk, max_tokens=100, token_counter=None)
        # char-fallback: 100 tokens * 4 = 400 chars max per chunk
        # ~2500 chars → должно быть ≥ 2 секций
        assert len(result) >= 2

    def test_fallback_small_text_no_tokenizer(self):
        """Небольшой текст без token_counter → не разбивается."""
        chunk = Chunk(title="Small", body="Short text.", sequence_number=1)
        result = recursive_split(chunk, max_tokens=512, token_counter=None)
        assert len(result) == 1


class TestSplitSentences:
    """Вспомогательная функция _split_sentences."""

    def test_split_sentences(self):
        from mcp_server.content.splitting import _split_sentences
        text = "First sentence. Second sentence! Third sentence? Yes."
        sents = _split_sentences(text)
        assert len(sents) >= 3

    def test_empty(self):
        from mcp_server.content.splitting import _split_sentences
        assert _split_sentences("") == []
        assert _split_sentences("   ") == []
