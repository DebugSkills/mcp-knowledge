"""Tests for Fix B: _FallbackTokenizer memory optimization + chunker fallback path.

Fix B addresses:
  - _FallbackTokenizer.encode: don't materialize list(range(n)) (~180 MB for long sections)
  - count_tokens: O(1) fast path without allocations
  - chunker._split_long_section: character-based splitting in fallback mode (no tokenize)
"""

from __future__ import annotations

from unittest.mock import patch

# ── Test 1: _FallbackTokenizer encode returns lightweight object ──


class TestFallbackTokenizerEncode:
    """_FallbackTokenizer.encode MUST NOT materialize a full list."""

    def test_encode_returns_range_not_list(self):
        """encode() returns a range (or similar lightweight object), not a list."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        result = ft.encode("a" * 1000)

        # Must NOT be a list — no materialization
        assert not isinstance(result, list), (
            f"encode() returned {type(result).__name__}, expected lightweight object (not list)"
        )

    def test_encode_len_works(self):
        """len(encode(text)) must work correctly."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        text = "hello world " * 100  # ~1200 chars
        result = ft.encode(text)

        expected_tokens = max(1, (len(text) + 3) // 4)
        assert len(result) == expected_tokens

    def test_encode_empty_returns_empty(self):
        """encode('') returns empty."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        result = ft.encode("")
        assert len(result) == 0

    def test_encode_small_text(self):
        """encode of small text works."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        result = ft.encode("hi")
        assert len(result) == 1  # max(1, (2+3)//4) = max(1, 1) = 1


# ── Test 2: _FallbackTokenizer count_tokens ──


class TestFallbackTokenizerCountTokens:
    """_FallbackTokenizer.count_tokens: O(1) fast path, no allocations."""

    def test_count_tokens_fast_path_exists(self):
        """count_tokens method exists on _FallbackTokenizer."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        assert hasattr(ft, "count_tokens"), "_FallbackTokenizer must have count_tokens method"
        assert callable(ft.count_tokens)

    def test_count_tokens_empty(self):
        """count_tokens('') == 0."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        assert ft.count_tokens("") == 0

    def test_count_tokens_returns_int(self):
        """count_tokens returns an integer."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        result = ft.count_tokens("hello world " * 50)
        assert isinstance(result, int)
        assert result > 0

    def test_count_tokens_consistent_with_encode_len(self):
        """count_tokens(text) == len(encode(text)) for any text."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()
        texts = ["hello", "a" * 100, "русский текст " * 20, ""]
        for text in texts:
            assert ft.count_tokens(text) == len(ft.encode(text)), (
                f"Mismatch for text len={len(text)}: "
                f"count_tokens={ft.count_tokens(text)}, len(encode)={len(ft.encode(text))}"
            )

    def test_count_tokens_no_allocation_for_large_text(self):
        """count_tokens for large text does NOT allocate a list of size n."""
        from mcp_server.embedding.tokenizer import _FallbackTokenizer

        ft = _FallbackTokenizer()

        # Create a large text and measure memory
        large_text = "x" * 1_000_000  # 1 MB of text, ~250k "tokens"

        # count_tokens should not allocate a 250k-element list
        # We check this indirectly: count_tokens should be fast (< 1ms)
        import time
        t0 = time.monotonic()
        result = ft.count_tokens(large_text)
        elapsed = time.monotonic() - t0

        assert result > 0
        # Should be near-instant — no iteration over 250k elements
        assert elapsed < 0.1, f"count_tokens took {elapsed:.4f}s, expected <0.1s"


# ── Test 3: XlmRobertaTokenizer.count_tokens uses fallback fast path ──


class TestXlmRobertaTokenizerCountTokensFallback:
    """When in fallback mode, XlmRobertaTokenizer.count_tokens uses the fast path."""

    def test_count_tokens_fallback_uses_fast_path(self):
        """count_tokens with fallback is O(1) — no list allocation."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )

        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        # count_tokens should return the right value without allocating
        result = tok.count_tokens("hello world " * 50)
        assert result == _FallbackTokenizer().count_tokens("hello world " * 50)
        assert result > 0

    def test_count_tokens_fallback_matches_encode_len(self):
        """count_tokens and len(encode) are consistent for fallback."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )

        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        texts = ["", "a", "hello world " * 100]
        for text in texts:
            assert tok.count_tokens(text) == len(tok.tokenize(text)), (
                f"Mismatch for text len={len(text)}"
            )

    def test_tokenize_fallback_returns_range(self):
        """tokenize() with fallback returns a range, not a list."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )

        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        result = tok.tokenize("hello world " * 100)
        assert not isinstance(result, list), (
            f"tokenize() returned {type(result).__name__}, expected lightweight object"
        )
        # Must support len()
        assert len(result) > 0


# ── Test 4: Chunker fallback path — character-based splitting ──


class TestChunkerFallbackPath:
    """chunker._split_long_section: in fallback mode, uses character-based splitting."""

    def test_fallback_large_section_does_not_crash(self):
        """100 KB section with fallback tokenizer → produces chunks, no OOM."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )
        from mcp_server.indexing.chunker import MarkdownChunker

        # Force fallback tokenizer
        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        # Patch the global tokenizer reference used by chunker
        with patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok):
            chunker = MarkdownChunker(max_tokens=512, overlap_tokens=64)

            # Generate ~20 KB of text (enough for chunking, but fast)
            section = ("This is a test paragraph with multiple sentences. " * 10 + "\n\n") * 40
            assert len(section) >= 20_000, f"Section too short: {len(section)} chars"

            chunks = chunker._split_long_section(
                knowledge_id="test-fallback-kid",
                text=section,
                section_header="Test Section",
                start_index=0,
            )

            # Must produce chunks
            assert len(chunks) > 0, "Should produce at least 1 chunk"
            # Each chunk must have content
            for ch in chunks:
                assert ch.content, "Chunk content must not be empty"
                assert len(ch.content) > 0

    def test_fallback_chunks_within_max_size(self):
        """Fallback chunks are ≤ max_tokens * chars_per_token characters."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )
        from mcp_server.indexing.chunker import MarkdownChunker

        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        with patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok):
            chunker = MarkdownChunker(max_tokens=100, overlap_tokens=10)

            section = "word " * 2000  # ~10 KB
            chunks = chunker._split_long_section(
                knowledge_id="test-size",
                text=section,
                section_header="Size Test",
                start_index=0,
            )

            # Chunks should be roughly ≤ max_tokens * 4 chars
            max_chars = chunker.max_tokens * 4 + 100  # some tolerance
            for ch in chunks:
                # Not an exact science, but should be within reasonable bounds
                assert len(ch.content) <= max_chars, (
                    f"Chunk too large: {len(ch.content)} chars > {max_chars} max"
                )

    def test_fallback_no_tokenize_called(self):
        """_split_long_section in fallback mode does NOT call tokenize()."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )
        from mcp_server.indexing.chunker import MarkdownChunker

        ft = _FallbackTokenizer()
        # Wrap to detect encode() calls
        original_encode = ft.encode
        call_count = [0]

        def _tracking_encode(text, add_special_tokens=False):
            call_count[0] += 1
            return original_encode(text, add_special_tokens)

        ft.encode = _tracking_encode

        tok = XlmRobertaTokenizer()
        tok._tok = ft

        with patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok):
            chunker = MarkdownChunker(max_tokens=100)

            section = "test " * 2000
            chunks = chunker._split_long_section(
                knowledge_id="test-no-tokenize",
                text=section,
                section_header="No Tokenize",
                start_index=0,
            )

            assert len(chunks) > 0
            # _split_long_section in fallback mode should NOT call tokenize → no encode calls
            assert call_count[0] == 0, (
                f"encode() was called {call_count[0]} times in fallback mode, expected 0"
            )

    def test_fallback_short_section_no_extra_work(self):
        """Short section (< max_tokens) in fallback is returned as-is."""
        from mcp_server.embedding.tokenizer import (
            XlmRobertaTokenizer,
            _FallbackTokenizer,
        )
        from mcp_server.indexing.chunker import MarkdownChunker

        tok = XlmRobertaTokenizer()
        tok._tok = _FallbackTokenizer()

        with patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok):
            chunker = MarkdownChunker(max_tokens=512)
            short = "A short paragraph."

            chunks = chunker._split_long_section(
                knowledge_id="test-short",
                text=short,
                section_header="Short",
                start_index=0,
            )

            assert len(chunks) == 1
            assert chunks[0].content == short or short in chunks[0].content
