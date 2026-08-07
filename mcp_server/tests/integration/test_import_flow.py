"""Integration tests: import_content E2E flow + get_entry TOC (#33-#36).

Tests: import_content(book) → N записей в SSOT + git; get_entry(collection_id) → TOC.
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
def tmp_knowledge_root():
    """Временная директория для knowledge/ с git-репозиторием."""
    import git
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "knowledge"
        root.mkdir()
        # Init valid git repo
        git.Repo.init(str(root))
        (root / ".trash").mkdir()
        yield root


@pytest.fixture
def real_store(tmp_knowledge_root):
    """Настоящий MarkdownStore с временной директорией (git-аудит отключён)."""
    from mcp_server.config import settings
    from mcp_server.storage.markdown_store import MarkdownStore

    # Override KNOWLEDGE_ROOT for this fixture
    original_root = settings.KNOWLEDGE_ROOT
    settings.KNOWLEDGE_ROOT = str(tmp_knowledge_root)
    store = MarkdownStore(knowledge_root=tmp_knowledge_root)
    store._repo = None  # disable git
    yield store
    settings.KNOWLEDGE_ROOT = original_root


@pytest.fixture
def mock_pipeline_integ():
    """Mock IndexingPipeline для интеграционных тестов."""
    from mcp_server.models import WriteResult
    pipeline = MagicMock()
    async def _enqueue(entry, wait_for_index=False):
        return WriteResult(knowledge_id=entry.frontmatter.knowledge_id, indexed=True, pending=False)
    pipeline.enqueue = _enqueue
    pipeline.stats = {"processed": 0}
    pipeline._sync = MagicMock()
    pipeline._sync.wait = AsyncMock(return_value=WriteResult(knowledge_id="test", indexed=True, pending=False))
    return pipeline


@pytest.fixture
def mock_token_counter():
    """Mock token counter: ~1 слово = 1 токен."""
    tc = MagicMock()
    tc.count_tokens = lambda text: len(text.split())
    tc.truncate_to_tokens = lambda text, max_tok: " ".join(text.split()[:max_tok])
    return tc


@pytest.fixture(autouse=True)
def setup_registry():
    """Обеспечиваем зарегистрированный BookPreprocessor."""
    registry_reset()
    bp = BookPreprocessor(embedder=None, token_counter=None)
    registry_register(bp)
    yield
    registry_reset()


class TestImportFlow:
    """Интеграционные тесты: import_content → N записей в SSOT."""

    SAMPLE_BOOK = """# Clean Code

## Chapter 1: Introduction
This chapter introduces clean code principles for better software development.

## Chapter 2: Naming
Good naming is essential. Use meaningful and pronounceable names for variables.

## Chapter 3: Functions
Functions should be small. They should do one thing and do it well.
"""

    @pytest.mark.asyncio
    async def test_import_content_creates_entries(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """import_content создаёт N .md записей + root-коллекцию."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content

        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "engineering",
                "subject": "python",
                "title": "Clean Code",
                "tags": ["clean-code", "book"],
            },
            app_state,
        )

        assert "collection_id" in result
        assert result["imported"] >= 2  # at least 3 chapters + intro
        assert result["failed"] == 0
        assert result["partial_success"] is False
        assert result["collection_id"].endswith("-collection")

        # Проверяем что root-коллекция читается
        root = await real_store.read(result["collection_id"])
        assert root is not None
        assert root.frontmatter.content_type == "collection"
        assert root.frontmatter.children is not None
        assert len(root.frontmatter.children) >= 2

    @pytest.mark.asyncio
    async def test_get_entry_returns_toc(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """get_entry(collection_id) возвращает TOC с children[]."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "eng",
                "subject": "code",
                "title": "Test Book",
            },
            app_state,
        )

        collection_id = result["collection_id"]
        entry = await real_store.read(collection_id)
        assert entry is not None
        fm = entry.frontmatter
        assert fm.content_type == "collection"
        assert fm.children is not None

        # Каждый child должен иметь knowledge_id, title, sequence_number
        for child in fm.children:
            assert "knowledge_id" in child
            assert "title" in child
            assert "sequence_number" in child

    @pytest.mark.asyncio
    async def test_child_has_parent_reference(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """Каждый child имеет parent_knowledge_id=<root_id>."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "dom",
                "subject": "sub",
                "title": "Parent Ref Test",
            },
            app_state,
        )

        collection_id = result["collection_id"]
        # Read each child and check parent reference
        root = await real_store.read(collection_id)
        for child in root.frontmatter.children:
            child_entry = await real_store.read(child["knowledge_id"])
            if child_entry:
                assert child_entry.frontmatter.parent_knowledge_id == collection_id

    @pytest.mark.asyncio
    async def test_content_type_unknown_returns_error(self, real_store, mock_pipeline_integ):
        """Неизвестный content_type → ошибка со списком доступных типов."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": "test",
                "content_type": "pdf",
                "domain": "eng",
                "subject": "sub",
            },
            app_state,
        )
        assert "error" in result
        assert "pdf" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_content_returns_error(self, real_store, mock_pipeline_integ):
        """Пустой контент → ошибка валидации."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": "",
                "content_type": "book",
                "domain": "eng",
                "subject": "sub",
            },
            app_state,
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_domain_returns_error(self, real_store, mock_pipeline_integ):
        """Отсутствует domain → ошибка."""
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ

        from mcp_server.tools.content import import_content
        result = await import_content(
            {
                "content": "test",
                "content_type": "book",
                "domain": "",
                "subject": "sub",
            },
            app_state,
        )
        assert "error" in result


# ═══════════════════════════════════════════════════════════════
# Фаза 13.9: ImportProgressTracker integration
# ═══════════════════════════════════════════════════════════════


class TestImportWithProgressTracker:
    """Интеграционные тесты: import_content с ImportProgressTracker."""

    SAMPLE_BOOK = """# Test Book

## Chapter 1: Start
Content of chapter one.

## Chapter 2: Middle
Content of chapter two.

## Chapter 3: End
Content of chapter three.
"""

    @pytest.mark.asyncio
    async def test_import_with_tracker_records_progress(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """import_content с import_id записывает прогресс в tracker."""
        from mcp_server.progress import ImportProgressTracker
        from mcp_server.tools.content import import_content

        tracker = ImportProgressTracker()
        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()
        app_state.import_progress = tracker

        import_id = "test-progress-001"
        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "test",
                "subject": "progress",
                "title": "Progress Test",
                "import_id": import_id,
            },
            app_state,
        )

        assert result["imported"] >= 2
        snap = tracker.get(import_id)
        assert snap is not None
        assert snap["status"] == "done"
        assert snap["imported"] == result["imported"]
        assert snap["total"] >= result["imported"]
        # Должна быть хотя бы одна log-строка с "sections written"
        assert len(snap["messages"]) > 0
        sections_msg = [m for m in snap["messages"] if "sections written" in m.get("text", "")]
        assert len(sections_msg) >= 1, f"Expected 'sections written' in messages: {snap['messages']}"

    @pytest.mark.asyncio
    async def test_import_without_import_id_does_not_crash(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """import_content без import_id НЕ падает (tracker — опциональный)."""
        from mcp_server.tools.content import import_content

        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()

        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "test",
                "subject": "no-progress",
                "title": "No Progress Test",
            },
            app_state,
        )

        assert result["imported"] >= 2
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_import_with_tracker_but_no_import_progress_on_state(
        self, real_store, mock_pipeline_integ, mock_token_counter
    ):
        """import_content с import_id но без tracker на app_state → не падает."""
        from mcp_server.tools.content import import_content

        app_state = MagicMock()
        app_state.store = real_store
        app_state.pipeline = mock_pipeline_integ
        app_state.knowledge_index = MagicMock()
        app_state.knowledge_index.update_section = MagicMock()
        # НЕ устанавливаем app_state.import_progress

        result = await import_content(
            {
                "content": self.SAMPLE_BOOK,
                "content_type": "book",
                "domain": "test",
                "subject": "no-tracker",
                "title": "No Tracker Test",
                "import_id": "no-tracker-001",
            },
            app_state,
        )

        assert result["imported"] >= 2
