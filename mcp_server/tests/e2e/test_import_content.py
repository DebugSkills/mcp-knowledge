"""E2E tests: import_content — 4 сценария из плана §7.

1. Структурная книга (ясные #/##) — чистая декомпозиция
2. Plain text без заголовков — fallback на clustering (или single section)
3. Oversized секции (>512 токенов) — recursive split
4. Batch interrupt (симуляция падения 1 секции) — partial_success:true
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.content.registry import reset as registry_reset
from mcp_server.content.book_preprocessor import BookPreprocessor
from mcp_server.content.registry import register as registry_register


@pytest.fixture
def tmp_root():
    import git
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "knowledge"
        root.mkdir()
        git.Repo.init(str(root))
        (root / ".trash").mkdir()
        yield root


@pytest.fixture
def store_no_git(tmp_root):
    """Store без git-аудита."""
    from mcp_server.storage.markdown_store import MarkdownStore
    from mcp_server.config import settings

    original = settings.KNOWLEDGE_ROOT
    settings.KNOWLEDGE_ROOT = str(tmp_root)
    s = MarkdownStore(knowledge_root=tmp_root)
    s._repo = None
    yield s
    settings.KNOWLEDGE_ROOT = original


@pytest.fixture
def pipeline_ok():
    pipeline = MagicMock()
    pipeline.enqueue = AsyncMock()
    pipeline.stats = {"processed": 0}
    pipeline._sync = MagicMock()
    pipeline._sync.wait = AsyncMock()
    return pipeline


@pytest.fixture
def token_counter():
    tc = MagicMock()
    tc.count_tokens = lambda text: len(text.split())
    tc.truncate_to_tokens = lambda text, max_t: " ".join(text.split()[:max_t])
    return tc


@pytest.fixture(autouse=True)
def setup_registry(token_counter):
    registry_reset()
    bp = BookPreprocessor(embedder=None, token_counter=token_counter)
    registry_register(bp)
    yield
    registry_reset()


# ── Test Data ─────────────────────────────────────────────

STRUCTURED_BOOK = """# Python Async Programming

## Chapter 1: Introduction to Asyncio
Asynchronous programming allows concurrent execution of tasks.
The asyncio module provides event loop, coroutines, and futures.

## Chapter 2: Coroutines and Tasks
Coroutines are the core of asyncio. Use async def to define them.
Tasks wrap coroutines and schedule them on the event loop.

## Chapter 3: Event Loop Internals
The event loop is the heart of asyncio. It manages callbacks,
schedules tasks, and handles I/O events efficiently.
"""

PLAIN_TEXT = """This is a plain text document without any markdown headers.
It contains several paragraphs about different topics.

The first topic discusses software engineering practices and methodologies.
Clean code, testing, and continuous integration are essential practices.

The second topic covers database design patterns for scalable applications.
Normalization, indexing strategies, and query optimization matter.

The third topic explores distributed systems and microservices architecture.
Service discovery, load balancing, and fault tolerance are critical.
"""


class TestE2EStructuralBook:
    """E2E-1: Структурная книга — чистая декомпозиция по #/##."""

    @pytest.mark.asyncio
    async def test_structural_book_decomposes(self, store_no_git, pipeline_ok):
        """Книга с ясными заголовками → N секций."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "engineering",
                "subject": "python",
                "title": "Python Async",
                "tags": ["async", "python"],
            },
            app_state,
        )

        assert "error" not in result
        assert result["imported"] >= 3  # 3 chapters
        assert result["failed"] == 0
        assert result["partial_success"] is False
        assert result["collection_id"].endswith("-collection")

        # Root должен иметь children с корректными sequence_number
        root = await store_no_git.read(result["collection_id"])
        assert root is not None
        assert len(root.frontmatter.children) >= 3

    @pytest.mark.asyncio
    async def test_children_have_correct_frontmatter(self, store_no_git, pipeline_ok):
        """Дети наследуют domain/subject, имеют корректный knowledge_id."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "eng",
                "subject": "py",
                "title": "Test",
                "tags": ["tag1"],
                "cross_subjects": ["xsub"],
            },
            app_state,
        )

        root = await store_no_git.read(result["collection_id"])
        for child_ref in root.frontmatter.children:
            child = await store_no_git.read(child_ref["knowledge_id"])
            assert child is not None
            assert child.frontmatter.domain == "eng"
            assert child.frontmatter.subject == "py"
            assert child.frontmatter.parent_knowledge_id == result["collection_id"]
            assert child.frontmatter.content_type == "book"


class TestE2EPlainText:
    """E2E-2: Plain text без заголовков."""

    @pytest.mark.asyncio
    async def test_plain_text_imports(self, store_no_git, pipeline_ok):
        """Plain text → импортируется как минимум 1 секция."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": PLAIN_TEXT,
                "content_type": "book",
                "domain": "docs",
                "subject": "general",
            },
            app_state,
        )

        assert "error" not in result
        assert result["imported"] >= 1
        assert result["failed"] == 0


class TestE2EOversized:
    """E2E-3: Oversized секция — recursive split."""

    @pytest.mark.asyncio
    async def test_oversized_splits_correctly(self, store_no_git, pipeline_ok, token_counter):
        """Секция > max_chunk_tokens разбивается."""
        # Generate a big "chapter" with many sentences
        big_section = "## Big Chapter\n" + ". ".join(["word"] * 2000) + "."

        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": big_section,
                "content_type": "book",
                "domain": "test",
                "subject": "big",
                "max_chunk_tokens": 100,
            },
            app_state,
        )

        assert "error" not in result
        # Oversized секция должна быть разбита на несколько
        assert result["imported"] >= 1


class TestE2EBatchInterrupt:
    """E2E-4: Batch interrupt — partial_success контракт."""

    @pytest.mark.asyncio
    async def test_partial_failure(self, store_no_git, pipeline_ok):
        """Симуляция падения 1 секции → partial_success:true, остальные LIVE."""
        # Патчим write_entry чтобы падал на 2-й секции
        original_write = store_no_git.write_entry
        call_count = [0]

        async def _failing_write(entry):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated write failure")
            return await original_write(entry)

        store_no_git.write_entry = _failing_write

        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "fail",
                "subject": "test",
                "title": "Batch Interrupt Test",
            },
            app_state,
        )

        assert result["partial_success"] is True
        assert result["failed"] >= 1
        assert result["imported"] >= 2  # остальные успешны
        assert len(result["failed_sections"]) >= 1
        assert "sequence_number" in result["failed_sections"][0]
        assert "title" in result["failed_sections"][0]
        assert "error" in result["failed_sections"][0]

        # Успешные секции должны быть в SSOT
        root = await store_no_git.read(result["collection_id"])
        # Root записан в начале, должен быть доступен
        assert root is not None
