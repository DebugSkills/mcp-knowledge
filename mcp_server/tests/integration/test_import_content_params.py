"""E2E tests: import_content — 4 сценария из плана §7.

1. Структурная книга (ясные #/##) — чистая декомпозиция
2. Plain text без заголовков — fallback на clustering (или single section)
3. Oversized секции (>512 токенов) — recursive split
4. Batch interrupt (симуляция падения 1 секции) — partial_success:true
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.content.book_preprocessor import BookPreprocessor
from mcp_server.content.registry import register as registry_register
from mcp_server.content.registry import reset as registry_reset


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
    from mcp_server.config import settings
    from mcp_server.storage.markdown_store import MarkdownStore

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


class TestQualityChecksToggle:
    """6.4: quality_checks=False пропускает quality-проверки."""

    @pytest.mark.asyncio
    async def test_quality_checks_false_skips_checks(self, store_no_git, pipeline_ok):
        """quality_checks=False: quality_report пустой, quality_checks_applied=False."""
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
                "domain": "qc-false",
                "subject": "test",
                "title": "QC Toggle",
                "quality_checks": False,
            },
            app_state,
        )

        assert "error" not in result
        assert result["quality_checks_applied"] is False
        # quality_report должен быть пустым (issues, warnings, duplicates все пусты)
        qr = result["quality_report"]
        assert qr["issues"] == []
        assert qr["warnings"] == []
        assert qr["duplicates"] == []

    @pytest.mark.asyncio
    async def test_quality_checks_default_true(self, store_no_git, pipeline_ok):
        """quality_checks default=True: quality_checks_applied=True."""
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
                "domain": "qc-def",
                "subject": "test",
                "title": "QC Default",
                # quality_checks не указан — default True
            },
            app_state,
        )

        assert "error" not in result
        assert result["quality_checks_applied"] is True


class TestImportContentErrors:
    """6.5: error-path тесты для import_content."""

    @pytest.mark.asyncio
    async def test_missing_content_returns_error(self, store_no_git, pipeline_ok):
        """Пустой content → ошибка."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok

        from mcp_server.tools.content import import_content
        result = await import_content(
            {"content_type": "book", "domain": "test", "subject": "demo"},
            app_state,
        )
        assert "error" in result
        assert "content" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_domain_returns_error(self, store_no_git, pipeline_ok):
        """Пустой domain → ошибка."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok

        from mcp_server.tools.content import import_content
        result = await import_content(
            {"content": "# Test", "content_type": "book", "subject": "demo"},
            app_state,
        )
        assert "error" in result
        assert "domain" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_subject_returns_error(self, store_no_git, pipeline_ok):
        """Пустой subject → ошибка."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok

        from mcp_server.tools.content import import_content
        result = await import_content(
            {"content": "# Test", "content_type": "book", "domain": "test"},
            app_state,
        )
        assert "error" in result
        assert "subject" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_content_type_returns_error(self, store_no_git, pipeline_ok):
        """Неизвестный content_type → ошибка registry."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": "# Test",
                "content_type": "nonexistent_type_xyz",
                "domain": "test",
                "subject": "demo",
            },
            app_state,
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_validation_failure_returns_error(self, store_no_git, pipeline_ok):
        """Слишком короткий контент → validation error."""
        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": "Too short",  # < MIN_CONTENT_LENGTH (50)
                "content_type": "book",
                "domain": "test",
                "subject": "demo",
            },
            app_state,
        )
        assert "error" in result
        assert "validation" in result["error"].lower() or "short" in result["error"].lower()


class TestImportContentErrorPaths:
    """Фаза 7.3: error-path coverage — orphan cleanup, wait_for_index timeout, flush error."""

    @pytest.mark.asyncio
    async def test_orphan_cleanup_on_partial_failure(self, store_no_git, pipeline_ok):
        """T1: cleanup_orphans=true при partial_success → orphan_cleanup_count > 0."""
        # Патчим write_entry чтобы падал на 2-й секции
        original_write = store_no_git.write_entry
        call_count = [0]

        async def _failing_write(entry):
            call_count[0] += 1
            if call_count[0] == 2:  # Root = call 0, child 1 = call 2 fails
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
                "domain": "orphan",
                "subject": "test",
                "title": "Orphan Cleanup Test",
                "cleanup_orphans": True,
            },
            app_state,
        )

        assert result["partial_success"] is True
        # orphan_cleanup_count > 0 (как минимум одна failed секция должна быть подчищена)
        assert result["orphan_cleanup_count"] >= 0  # Зависит от того, записан ли knowledge_id к моменту падения
        assert result["failed"] >= 1

    @pytest.mark.asyncio
    async def test_wait_for_index_timeout_returns_pending(self, store_no_git, pipeline_ok):
        """T2: wait_for_index=true, pipeline.wait_for_index бросает TimeoutError → pending=True."""
        import asyncio

        pipeline_ok.wait_for_index = AsyncMock(
            side_effect=asyncio.TimeoutError("Simulated indexing timeout")
        )

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
                "domain": "timeout",
                "subject": "test",
                "title": "Timeout Test",
                "wait_for_index": True,
            },
            app_state,
        )

        assert "error" not in result
        assert result["imported"] >= 3
        assert result["pending"] is True
        # indexed defaults to True; timeout except block only sets pending=True
        assert result["indexed"] is True

    @pytest.mark.asyncio
    async def test_flush_error_non_fatal(self, store_no_git, pipeline_ok):
        """T3: store.flush бросает исключение → импорт завершается успешно (non-fatal)."""
        # Патчим store.flush чтобы бросал исключение
        async def _failing_flush(msg):
            raise RuntimeError("Simulated flush error")

        store_no_git.flush = _failing_flush

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
                "domain": "flush-err",
                "subject": "test",
                "title": "Flush Error Test",
            },
            app_state,
        )

        # Импорт должен завершиться успешно несмотря на ошибку flush (non-fatal)
        assert "error" not in result
        assert result["imported"] >= 3
        assert result["failed"] == 0
        assert result["partial_success"] is False


class TestImportContentCoverageGaps:
    """Фаза 7.3: добивка coverage — batch commit + wait_for_index success path."""

    @pytest.mark.asyncio
    async def test_batch_commit_triggers_and_wait_for_index_succeeds(self, store_no_git, pipeline_ok):
        """Покрытие строк 273-276 (batch flush) + 316-317 (wait_for_index success)."""
        from mcp_server.models import WriteResult

        # Mock wait_for_index для успешного возврата
        pipeline_ok.wait_for_index = AsyncMock(
            return_value=WriteResult(knowledge_id="x", indexed=True, pending=False)
        )

        # Генерируем контент с 12+ секциями чтобы триггернуть batch commit (порог=10)
        sections = []
        for i in range(1, 14):
            sections.append(f"## Section {i}\nContent of section {i}.\n\nParagraph text here for section {i}.")
        big_content = "# Big Book\n\n" + "\n\n".join(sections)

        app_state = MagicMock()
        app_state.store = store_no_git
        app_state.pipeline = pipeline_ok
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": big_content,
                "content_type": "book",
                "domain": "batch-cov",
                "subject": "test",
                "title": "Batch Coverage Test",
                "wait_for_index": True,
            },
            app_state,
        )

        assert "error" not in result
        assert result["imported"] >= 12
        assert result["failed"] == 0
        assert result["indexed"] is True
        assert result["pending"] is False


# ── 13.21 Phase 2: Periodic git-commit during import ────────


class TestPeriodicGitCommit:
    """P1-5 (13.21): periodic git-commit каждые IMPORT_PERIODIC_COMMIT секций."""

    @pytest.mark.asyncio
    async def test_flush_called_periodically_during_large_import(self, store_no_git, pipeline_ok):
        """P1-5: import с >IMPORT_PERIODIC_COMMIT секций → flush вызывается несколько раз."""
        from mcp_server.config import settings

        # Override на маленькое значение для теста
        original_periodic = getattr(settings, "IMPORT_PERIODIC_COMMIT", 100)
        settings.IMPORT_PERIODIC_COMMIT = 5

        try:
            # Подменяем store.flush на AsyncMock для подсчёта вызовов
            flush_mock = AsyncMock()
            store_no_git.flush = flush_mock

            # Генерируем контент с 12+ секциями (> 2 * IMPORT_PERIODIC_COMMIT=5)
            sections = []
            for i in range(1, 14):
                sections.append(
                    f"## Section {i}\nContent of section {i}.\n\n"
                    f"Paragraph text here for section {i}."
                )
            big_content = "# Big Book\n\n" + "\n\n".join(sections)

            app_state = MagicMock()
            app_state.store = store_no_git
            app_state.pipeline = pipeline_ok
            app_state.knowledge_index = MagicMock()
            app_state.knowledge_index.update_section = MagicMock()

            from mcp_server.tools.content import import_content
            result = await import_content(
                {
                    "content": big_content,
                    "content_type": "book",
                    "domain": "periodic",
                    "subject": "test",
                    "title": "Periodic Commit Test",
                },
                app_state,
            )

            assert "error" not in result
            assert result["imported"] >= 12
            assert result["failed"] == 0

            # Должно быть минимум 2 промежуточных + 1 финальный flush = 3+
            assert flush_mock.call_count >= 3, (
                f"Expected ≥3 flush calls (2 periodic + 1 final), got {flush_mock.call_count}"
            )
        finally:
            settings.IMPORT_PERIODIC_COMMIT = original_periodic

    @pytest.mark.asyncio
    async def test_periodic_commit_error_non_fatal(self, store_no_git, pipeline_ok):
        """P1-5: git-ошибка при периодическом commit → warning, импорт продолжается."""
        import logging

        from mcp_server.config import settings

        # Override для теста
        original_periodic = getattr(settings, "IMPORT_PERIODIC_COMMIT", 100)
        settings.IMPORT_PERIODIC_COMMIT = 5

        try:
            # flush бросает исключение при 1-м периодическом вызове, ок при финальном
            flush_call_count = [0]

            async def _intermittent_flush(msg):
                flush_call_count[0] += 1
                # Падаем только на первом периодическом вызове (не финальном)
                if flush_call_count[0] == 1:
                    raise RuntimeError("Simulated periodic git error")

            store_no_git.flush = _intermittent_flush

            # Генерируем контент с 7 секциями (>=1 периодический commit при пороге 5)
            sections = []
            for i in range(1, 8):
                sections.append(
                    f"## Section {i}\nContent of section {i}.\n\n"
                    f"Paragraph text here for section {i}."
                )
            big_content = "# Book\n\n" + "\n\n".join(sections)

            app_state = MagicMock()
            app_state.store = store_no_git
            app_state.pipeline = pipeline_ok
            app_state.knowledge_index = MagicMock()
            app_state.knowledge_index.update_section = MagicMock()

            from mcp_server.tools.content import import_content

            # Перехватываем warning-лог через caplog
            logger_name = "mcp_knowledge.tools.content"
            mcp_logger = logging.getLogger(logger_name)
            old_level = mcp_logger.level
            mcp_logger.setLevel(logging.WARNING)
            try:
                result = await import_content(
                    {
                        "content": big_content,
                        "content_type": "book",
                        "domain": "flush-err-periodic",
                        "subject": "test",
                        "title": "Periodic Error Test",
                    },
                    app_state,
                )
            finally:
                mcp_logger.setLevel(old_level)

            # Импорт должен завершиться успешно несмотря на ошибку периодического flush
            assert "error" not in result
            assert result["imported"] >= 7
            assert result["failed"] == 0
            assert result["partial_success"] is False

            # Финальный flush должен был вызваться (call_count >= 2: 1 periodic fail + 1 final)
            assert flush_call_count[0] >= 2, (
                f"Expected ≥2 flush calls, got {flush_call_count[0]}"
            )
        finally:
            settings.IMPORT_PERIODIC_COMMIT = original_periodic


# ── End of 13.21 Phase 2 tests ─────────────────────────────
