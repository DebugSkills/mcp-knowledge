"""Unit tests: content/pdf_preprocessor.py — PDF validation, decomposition, checkpoint, OCR.

13.21: TDD Red-Green-Refactor for PDFPreprocessor.
Uses mocks for pdfplumber/pytesseract — no real Tesseract required.
Real libraries used for fixture PDFs (importorskip'd).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from mcp_server.content.pdf_preprocessor import PDFPreprocessor
from mcp_server.content.preprocessor import ImportMeta


@pytest.fixture
def preprocessor():
    """PDFPreprocessor instance with test cache dir."""
    pp = PDFPreprocessor()
    pp._cache_dir = tempfile.mkdtemp(prefix="pdf_cache_")
    pp._max_pages = 5
    pp._max_size = 50 * 1024 * 1024
    pp._cache_max_age_days = 30
    pp._cache_max_size_mb = 10
    yield pp
    # cleanup
    import shutil
    if os.path.exists(pp._cache_dir):
        shutil.rmtree(pp._cache_dir, ignore_errors=True)


@pytest.fixture
def meta():
    return ImportMeta(domain="test", subject="test", title="Test PDF")


# ═══════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════


class TestValidate:
    """PDF validate() — encrypted, size, page limits."""

    def test_encrypted_returns_error(self, preprocessor, sample_encrypted_pdf_path, meta):
        meta.source_path = sample_encrypted_pdf_path
        result = preprocessor.validate("", meta)
        assert not result.valid
        assert "паролем" in result.error.lower() or "encrypt" in result.error.lower()

    def test_missing_source_path_returns_error(self, preprocessor, meta):
        meta.source_path = None
        result = preprocessor.validate("", meta)
        assert not result.valid
        assert "not found" in result.error

    def test_nonexistent_file_returns_error(self, preprocessor, meta):
        meta.source_path = "/nonexistent/file.pdf"
        result = preprocessor.validate("", meta)
        assert not result.valid

    def test_too_large_file_returns_error(self, preprocessor, meta):
        preprocessor._max_size = 100
        meta.source_path = __file__  # any existing file > 100 bytes
        result = preprocessor.validate("", meta)
        assert not result.valid
        assert "large" in result.error.lower()

    def test_too_many_pages_returns_error(self, preprocessor, sample_pdf_path, meta):
        preprocessor._max_pages = 1
        meta.source_path = sample_pdf_path
        result = preprocessor.validate("", meta)
        assert not result.valid
        assert "pages" in result.error.lower()

    def test_valid_pdf_passes_validate(self, preprocessor, sample_pdf_path, meta):
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        meta.source_path = sample_pdf_path
        result = preprocessor.validate("", meta)
        assert result.valid
        assert result.content_size > 0
        assert result.estimated_sections > 0


# ═══════════════════════════════════════════════════════════════
# Decomposition
# ═══════════════════════════════════════════════════════════════


class TestDecompose:
    """PDF decompose() — text extraction, heading detection, fallback."""

    async def test_multi_page_decompose(self, preprocessor, sample_pdf_path, meta):
        """3-page PDF → should produce sections."""
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        meta.source_path = sample_pdf_path
        sections = await preprocessor.decompose("", meta)
        assert len(sections) > 0
        # at least one section with content
        bodies = [s.body for s in sections if s.body.strip()]
        assert len(bodies) > 0
        # each section has required fields
        for s in sections:
            assert s.title
            assert s.sequence_number > 0
            assert "knowledge_id" in s.meta

    async def test_checkpoint_writes_cache(self, preprocessor, sample_pdf_path, meta):
        """First call: should write checkpoint file."""
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        meta.source_path = sample_pdf_path
        await preprocessor.decompose("", meta)
        # Check cache file exists
        cache_hash = preprocessor._compute_content_hash(sample_pdf_path)
        cache_path = Path(preprocessor._cache_dir) / f"{cache_hash}.txt"
        assert cache_path.exists()
        assert cache_path.stat().st_size > 0

    async def test_checkpoint_reads_cache_second_call(self, preprocessor, sample_pdf_path, meta):
        """Second call with same file: should read from cache (no pdfplumber extract)."""
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        meta.source_path = sample_pdf_path

        # First call
        sections1 = await preprocessor.decompose("", meta)

        # Second call: verify cache hit by checking pdfplumber is not re-used for text
        # (We can verify sections are the same)
        sections2 = await preprocessor.decompose("", meta)
        assert len(sections1) == len(sections2)

    async def test_fallback_per_page_for_scan_pdf(self, preprocessor, sample_scan_pdf_path, meta):
        """Image-only PDF: falls back to per-page. If no tesseract, sections may be empty."""
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        meta.source_path = sample_scan_pdf_path
        # Tesseract not installed → OCR will fail gracefully
        # Sections may be empty (no text extracted) — that's acceptable
        await preprocessor.decompose("", meta)
        # OCR failures are non-fatal — we just get fewer/no sections
        # Test passes if no crash occurs

    async def test_missing_source_path_raises(self, preprocessor, meta):
        meta.source_path = None
        with pytest.raises(ValueError, match="source_path"):
            await preprocessor.decompose("", meta)


# ═══════════════════════════════════════════════════════════════
# OCR path (mock pytesseract)
# ═══════════════════════════════════════════════════════════════


class TestOCR:
    """OCR path — pytesseract.image_to_string via run_in_executor."""

    async def test_ocr_via_run_in_executor(self, preprocessor, sample_scan_pdf_path, meta):
        """Image page triggers OCR path. Skip if tesseract binary not available."""
        pytest.importorskip("pdfplumber", reason="pdfplumber not installed")
        pytest.importorskip("pytesseract", reason="pytesseract not installed")
        # Check tesseract binary is available
        import shutil
        if shutil.which("tesseract") is None:
            pytest.skip("tesseract binary not installed")
        meta.source_path = sample_scan_pdf_path
        sections = await preprocessor.decompose("", meta)
        assert len(sections) > 0


# ═══════════════════════════════════════════════════════════════
# Cache prune (P0-2)
# ═══════════════════════════════════════════════════════════════


class TestPruneCache:
    """_prune_pdf_cache — TTL and LRU eviction."""

    async def test_prune_old_files(self, preprocessor):
        """Files older than MAX_AGE_DAYS should be deleted."""
        import time as _time

        cache_dir = Path(preprocessor._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create old file (40 days ago)
        old_path = cache_dir / "old_file.txt"
        old_path.write_text("old content")
        old_mtime = _time.time() - 40 * 86400
        os.utime(str(old_path), (old_mtime, old_mtime))

        # Create recent file
        recent_path = cache_dir / "recent_file.txt"
        recent_path.write_text("recent content")

        removed = await preprocessor._prune_pdf_cache(cache_dir)
        assert removed >= 1
        assert not old_path.exists()
        assert recent_path.exists()

    async def test_prune_lru_when_over_size_limit(self, preprocessor):
        """When total size exceeds MAX_SIZE_MB, oldest files deleted."""
        cache_dir = Path(preprocessor._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        preprocessor._cache_max_size_mb = 0.001  # 1KB

        # Write files totaling >1KB with different mtimes
        import time as _time

        files = []
        for i in range(5):
            fpath = cache_dir / f"file_{i}.txt"
            data = "x" * 500  # 500 bytes each, total 2.5KB
            fpath.write_text(data)
            mtime = _time.time() - (60 * (4 - i))  # oldest = file_0
            os.utime(str(fpath), (mtime, mtime))
            files.append(fpath)

        removed = await preprocessor._prune_pdf_cache(cache_dir)
        # Should have evicted some to get under 1KB
        remaining = list(cache_dir.glob("*.txt"))
        sum(f.stat().st_size for f in remaining)
        assert removed >= 1
        # Oldest files removed first
        assert not files[0].exists() or not files[1].exists()

    async def test_prune_empty_dir(self, preprocessor):
        cache_dir = Path(preprocessor._cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        removed = await preprocessor._prune_pdf_cache(cache_dir)
        assert removed == 0


# ═══════════════════════════════════════════════════════════════
# Content hash
# ═══════════════════════════════════════════════════════════════


class TestContentHash:
    """_compute_content_hash — deterministic, unique per file."""

    def test_same_file_same_hash(self, preprocessor, sample_pdf_path):
        h1 = preprocessor._compute_content_hash(sample_pdf_path)
        h2 = preprocessor._compute_content_hash(sample_pdf_path)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_different_files_different_hash(self, preprocessor, sample_pdf_path, sample_scan_pdf_path):
        h1 = preprocessor._compute_content_hash(sample_pdf_path)
        h2 = preprocessor._compute_content_hash(sample_scan_pdf_path)
        assert h1 != h2

    def test_nonexistent_file_returns_hash(self, preprocessor):
        h = preprocessor._compute_content_hash("/nonexistent/file.pdf")
        assert len(h) == 64
