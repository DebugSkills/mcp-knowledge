"""E2E tests: import_content replace_collection_id — атомарная замена книги.

8 сценариев:
  R1: replace success — старая книга в .trash/, новая жива
  R2: replace_collection_id not found — error до декомпозиции
  R3: replace_collection_id not a collection — error
  R4: replace + import failure — старая книга ЦЕЛА
  R5: replace + partial success (без force) — replaced=False
  R6: replace + partial success + replace_on_partial=True — replaced=True
  R7: self-replace guard — collection_id == replace_collection_id
  R8: replace + progress tracker — set_phase "replacing"
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.content.book_preprocessor import BookPreprocessor
from mcp_server.content.registry import register as registry_register
from mcp_server.content.registry import reset as registry_reset
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

# ── Fixtures (переиспользуем паттерны test_import_content_params.py) ──

@pytest.fixture
def tmp_root():
    import git
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "knowledge"
        root.mkdir(parents=True)
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


@pytest.fixture
def qdrant_mock():
    """Qdrant mock для delete_entry(cascade): scroll + delete_by_knowledge_id."""
    q = MagicMock()
    q.delete_by_knowledge_id = MagicMock()
    q.scroll = MagicMock(return_value=([], None))
    return q


def _make_app_state(store, pipeline, qdrant_mock):
    """Собрать app_state для replace-тестов (единый паттерн)."""
    app_state = MagicMock()
    app_state.store = store
    app_state.pipeline = pipeline
    app_state.knowledge_index = MagicMock()
    app_state.knowledge_index.update_section = MagicMock()
    app_state.qdrant = qdrant_mock
    app_state.qdrant_client = None  # чтобы _get_qdrant не создавал auto-mock
    app_state.data_version = 0
    return app_state


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

ALT_BOOK = """# Advanced Python Patterns

## Chapter 1: Decorators Deep Dive
Decorators are a powerful feature in Python for metaprogramming.

## Chapter 2: Context Managers
Context managers simplify resource management with the with statement.

## Chapter 3: Generators and Iterators
Generators provide lazy evaluation and memory-efficient iteration.

## Chapter 4: Descriptors
Descriptors are the mechanism behind properties, methods, and static methods.
"""


# ═══════════════════════════════════════════════════════════
# R1: Replace success — старая книга удалена, новая жива
# ═══════════════════════════════════════════════════════════

class TestR1ReplaceSuccess:
    @pytest.mark.asyncio
    async def test_replace_success_old_deleted_new_alive(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """Импорт A → импорт B с replace_collection_id=A → A в .trash/, B жива."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "test1",
                "title": "Book A",
                "tags": ["async", "python"],
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        assert result_a["imported"] >= 3
        collection_id_a = result_a["collection_id"]
        assert collection_id_a.endswith("-collection")

        # Verify A exists in store
        root_a = await store_no_git.read(collection_id_a)
        assert root_a is not None, "Book A should exist after import"

        # Шаг 2: настроим qdrant.scroll чтобы возвращать дочерние точки A
        # (для cascade_deleted >= 3)
        child_points = []
        for child_ref in root_a.frontmatter.children:
            pt = MagicMock()
            pt.payload = {"knowledge_id": child_ref["knowledge_id"]}
            child_points.append(pt)

        qdrant_mock.scroll = MagicMock(return_value=(child_points, None))

        # Шаг 3: импорт книги B с replace_collection_id=A
        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_b = await import_content(
            {
                "content": ALT_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "test2",
                "title": "Book B",
                "tags": ["patterns"],
                "replace_collection_id": collection_id_a,
            },
            app_state_b,
        )

        assert "error" not in result_b, f"Import B failed: {result_b}"
        assert result_b["imported"] >= 4
        assert result_b["replaced"] is True, f"Expected replaced=True, got: {result_b}"
        assert result_b["replaced_collection_id"] == collection_id_a
        assert result_b["cascade_deleted"] >= 3, (
            f"Expected cascade_deleted >= 3, got {result_b.get('cascade_deleted')}"
        )

        # Verify: A удалена (store.read → None)
        root_a_after = await store_no_git.read(collection_id_a)
        assert root_a_after is None, (
            f"Book A should be in .trash/, but store.read returned {root_a_after}"
        )

        # Verify: B жива
        collection_id_b = result_b["collection_id"]
        root_b = await store_no_git.read(collection_id_b)
        assert root_b is not None, "Book B should exist after replace import"
        assert root_b.frontmatter.content_type == "collection"


# ═══════════════════════════════════════════════════════════
# R2: replace_collection_id not found — error до декомпозиции
# ═══════════════════════════════════════════════════════════

class TestR2ReplaceNotFound:
    @pytest.mark.asyncio
    async def test_replace_not_found_error_before_decomposition(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """replace_collection_id="nonexistent" → error «not found»."""
        from mcp_server.tools.content import import_content

        app_state = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "nf",
                "title": "NotFound Test",
                "replace_collection_id": "nonexistent-collection-id",
            },
            app_state,
        )

        assert "error" in result, f"Expected error, got: {result}"
        assert "not found" in result["error"].lower(), (
            f"Error should contain 'not found', got: {result['error']}"
        )

        # Verify: декомпозиция не запускалась — никакие файлы не записаны
        # (проверяем отсутствие root-записи для этого domain/subject)
        collection_id = result.get("collection_id", "")
        assert not collection_id, (
            f"No collection_id should be returned on error, got: {collection_id}"
        )


# ═══════════════════════════════════════════════════════════
# R3: replace_collection_id not a collection — error
# ═══════════════════════════════════════════════════════════

class TestR3ReplaceNotCollection:
    @pytest.mark.asyncio
    async def test_replace_not_collection_error(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """replace_collection_id = обычная запись (не collection) → error."""
        from mcp_server.tools.content import import_content

        # Создаём обычную запись (content_type != "collection")
        regular_fm = KnowledgeFrontmatter(
            knowledge_id="replace-regular-section",
            domain="replace",
            subject="regular",
            content_type="book",
        )
        regular_entry = KnowledgeEntry(
            frontmatter=regular_fm,
            content="# Regular Entry\n\nNot a collection.",
        )
        await store_no_git.write_entry(regular_entry)

        app_state = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "nc",
                "title": "NotCollection Test",
                "replace_collection_id": "replace-regular-section",
            },
            app_state,
        )

        assert "error" in result, f"Expected error, got: {result}"
        assert "not a collection" in result["error"].lower(), (
            f"Error should contain 'not a collection', got: {result['error']}"
        )


# ═══════════════════════════════════════════════════════════
# R4: replace + import failure — старая книга ЦЕЛА
# ═══════════════════════════════════════════════════════════

class TestR4ReplaceImportFailure:
    @pytest.mark.asyncio
    async def test_replace_import_failure_old_intact(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """Невалидный контент → error, старая книга ЦЕЛА."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "fail1",
                "title": "Book A Fail",
                "tags": ["test"],
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        # Шаг 2: импорт с невалидным контентом (слишком короткий)
        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_b = await import_content(
            {
                "content": "Too short",
                "content_type": "book",
                "domain": "replace",
                "subject": "fail2",
                "title": "Fail Import",
                "replace_collection_id": collection_id_a,
            },
            app_state_b,
        )

        assert "error" in result_b, f"Expected validation error, got: {result_b}"

        # Verify: старая книга A ЦЕЛА
        root_a = await store_no_git.read(collection_id_a)
        assert root_a is not None, (
            "Book A should still exist after failed replace import"
        )

        # Verify: replaced не True (возвращается до replace-блока из-за validation error)
        assert result_b.get("replaced") is not True


# ═══════════════════════════════════════════════════════════
# R5: replace + partial success (без force) — replaced=False
# ═══════════════════════════════════════════════════════════

class TestR5ReplacePartialNoForce:
    @pytest.mark.asyncio
    async def test_replace_partial_success_no_force(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """Патч write_entry падает на 2-й секции → failed>0, replaced=False."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "part1",
                "title": "Book A Partial",
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        # Шаг 2: патчим write_entry для падения на 2-й секции
        original_write = store_no_git.write_entry
        call_count = [0]

        async def _failing_write(entry):
            call_count[0] += 1
            # Root = call 1, child 1 = call 2 — ok, child 2 = call 3 — fail
            if call_count[0] == 3:
                raise RuntimeError("Simulated write failure")
            return await original_write(entry)

        store_no_git.write_entry = _failing_write

        # Шаг 3: импорт B с replace_collection_id=A (NO force)
        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_b = await import_content(
            {
                "content": ALT_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "part2",
                "title": "Book B Partial NoForce",
                "replace_collection_id": collection_id_a,
            },
            app_state_b,
        )

        # partial_success ожидаем (часть секций записана)
        assert result_b["partial_success"] is True
        assert result_b["failed"] >= 1
        assert result_b["imported"] >= 1  # хотя бы одна секция записана

        # replace НЕ должен был сработать (failed > 0, replace_on_partial=False)
        assert result_b["replaced"] is False, (
            f"Expected replaced=False (partial without force), got: {result_b}"
        )
        assert result_b["replace_skipped_reason"] == "import_partial", (
            f"Expected replace_skipped_reason='import_partial', "
            f"got: {result_b.get('replace_skipped_reason')}"
        )

        # Verify: старая книга A ЦЕЛА
        root_a = await store_no_git.read(collection_id_a)
        assert root_a is not None, "Book A should still exist after partial replace without force"


# ═══════════════════════════════════════════════════════════
# R6: replace + partial success + replace_on_partial=True
# ═══════════════════════════════════════════════════════════

class TestR6ReplacePartialForced:
    @pytest.mark.asyncio
    async def test_replace_partial_success_forced(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """replace_on_partial=True → replaced=True даже при failed>0."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "force1",
                "title": "Book A Force",
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        # Шаг 2: патчим write_entry для падения на 2-й секции
        original_write = store_no_git.write_entry
        call_count = [0]

        async def _failing_write(entry):
            call_count[0] += 1
            if call_count[0] == 3:
                raise RuntimeError("Simulated write failure")
            return await original_write(entry)

        store_no_git.write_entry = _failing_write

        # Шаг 3: импорт B с replace_collection_id=A (WITH replace_on_partial=True)
        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_b = await import_content(
            {
                "content": ALT_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "force2",
                "title": "Book B Force",
                "replace_collection_id": collection_id_a,
                "replace_on_partial": True,
            },
            app_state_b,
        )

        assert result_b["partial_success"] is True
        assert result_b["failed"] >= 1

        # replace ДОЛЖЕН сработать (replace_on_partial=True, imported > 0)
        assert result_b["replaced"] is True, (
            f"Expected replaced=True (partial with force), got: {result_b}"
        )
        assert result_b["replaced_collection_id"] == collection_id_a

        # Verify: старая книга A удалена
        root_a = await store_no_git.read(collection_id_a)
        assert root_a is None, "Book A should be deleted after forced partial replace"


# ═══════════════════════════════════════════════════════════
# R7: self-replace guard — collection_id == replace_collection_id
# ═══════════════════════════════════════════════════════════

class TestR7SelfReplaceGuard:
    @pytest.mark.asyncio
    async def test_self_replace_guard_rejects(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """replace_collection_id == новый collection_id → error «Self-replace»."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A с конкретным названием
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "self",
                "title": "Same Title",
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        # Шаг 2: импорт B с ТЕМ ЖЕ domain/subject/title → collection_id совпадёт
        # и replace_collection_id = A → это self-replace
        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_b = await import_content(
            {
                "content": ALT_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "self",
                "title": "Same Title",
                "replace_collection_id": collection_id_a,
            },
            app_state_b,
        )

        assert "error" in result_b, f"Expected self-replace error, got: {result_b}"
        assert "self-replace" in result_b["error"].lower(), (
            f"Error should contain 'self-replace', got: {result_b['error']}"
        )


# ═══════════════════════════════════════════════════════════
# R8: replace + progress tracker — set_phase "replacing"
# ═══════════════════════════════════════════════════════════

class TestR8ReplaceProgressTracker:
    @pytest.mark.asyncio
    async def test_replace_sets_progress_phase_replacing(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """Tracker записывает set_phase 'replacing' при замене."""
        from mcp_server.tools.content import import_content

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "track1",
                "title": "Book A Tracker",
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        # Шаг 2: настраиваем tracker mock
        tracker = MagicMock()
        tracker.start = MagicMock()
        tracker.set_phase = MagicMock()
        tracker.log = MagicMock()
        tracker.section_done = MagicMock()
        tracker.section_failed = MagicMock()
        tracker.done = MagicMock()
        tracker.error = MagicMock()

        app_state_b = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        app_state_b.import_progress = tracker

        result_b = await import_content(
            {
                "content": ALT_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "track2",
                "title": "Book B Tracker",
                "replace_collection_id": collection_id_a,
                "import_id": "test-import-id-r8",
            },
            app_state_b,
        )

        assert "error" not in result_b, f"Import B failed: {result_b}"
        assert result_b["replaced"] is True

        # Проверяем что set_phase вызывался с "replacing"
        phase_calls = [
            call[0][1] for call in tracker.set_phase.call_args_list
            if len(call[0]) >= 2
        ]
        assert "replacing" in phase_calls, (
            f"Expected set_phase('replacing') call, got phases: {phase_calls}"
        )


# ═══════════════════════════════════════════════════════════
# R9: delete_entry(cascade) с обёрткой QdrantClient — регрессия
# ═══════════════════════════════════════════════════════════
# Реальный баг (найден в smoke 13.22): crud.delete_entry(cascade=True) вызывал
# qdrant_raw.scroll(collection_name="knowledge", ...), а обёртка QdrantClient
# (storage/qdrant_client.py) хардкодила коллекцию и НЕ принимала collection_name
# kwarg → TypeError → except → cascade_deleted=0 (дети НЕ удалялись никогда).
# W2: обёртка теперь принимает collection_name (зональный контракт через
# collection_for_zone + _require_collection) — scroll в cascade передаёт её явно.

class _WrapperStyleQdrant:
    """Имитация обёртки QdrantClient: scroll принимает collection_name (W2).

    Признак обёртки — атрибут _client. Сигнатура повторяет обёртку W2:
    незнакомые kwargs → TypeError (как на реальной обёртке) → тест FAIL.
    """

    def __init__(self, child_ids: list[str]) -> None:
        self._client = object()  # признак обёртки (фикс _get_qdrant)
        self._child_ids = child_ids
        self.delete_by_knowledge_id = MagicMock()

    def scroll(  # сигнатура обёртки W2: + collection_name
        self,
        scroll_filter=None,
        limit: int = 100,
        offset: object = None,
        with_payload: list[str] | bool = True,
        with_vectors: bool = False,
        collection_name: str | None = None,
    ) -> tuple[list, object]:
        if offset is None:
            points = [MagicMock(payload={"knowledge_id": cid}) for cid in self._child_ids]
            return points, None
        return [], None


class TestR9CascadeScrollWrapper:
    @pytest.mark.asyncio
    async def test_cascade_delete_works_with_wrapper_style_client(
        self, store_no_git, pipeline_ok, qdrant_mock
    ):
        """delete_entry(cascade=True) находит и удаляет детей при обёртке Qdrant.

        Регрессия: до фикса cascade_deleted=0 (TypeError collection_name).
        """
        from mcp_server.tools.content import import_content
        from mcp_server.tools.crud import delete_entry

        # Шаг 1: импорт книги A (root + дети в store)
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "wrapper",
                "title": "Wrapper Book",
            },
            app_state_a,
        )
        assert "error" not in result_a, f"Import A failed: {result_a}"
        collection_id_a = result_a["collection_id"]

        root_a = await store_no_git.read(collection_id_a)
        assert root_a is not None
        child_ids = [c["knowledge_id"] for c in root_a.frontmatter.children]
        assert len(child_ids) >= 3

        # Шаг 2: delete_entry с обёрткой-стилем клиентом
        wrapper = _WrapperStyleQdrant(child_ids)
        app_state_del = _make_app_state(store_no_git, pipeline_ok, wrapper)

        res = await delete_entry(
            {"knowledge_id": collection_id_a, "cascade": True}, app_state_del
        )

        assert res.get("deleted") is True, f"delete_entry failed: {res}"
        assert res.get("cascade_deleted") == len(child_ids), (
            f"Expected cascade_deleted={len(child_ids)}, got {res.get('cascade_deleted')} "
            f"(scroll с collection_name kwarg сломал каскад)"
        )
        # Дети реально удалены из SSOT (→ .trash)
        for cid in child_ids:
            assert await store_no_git.read(cid) is None, (
                f"Child {cid} should be deleted (→ .trash/)"
            )
        # delete_by_knowledge_id вызывался для каждого ребёнка + root (N+1)
        assert wrapper.delete_by_knowledge_id.call_count == len(child_ids) + 1


# ═══════════════════════════════════════════════════════════
# R10: batch-delete — каскад делает ОДИН git-flush, не N+1
# ═══════════════════════════════════════════════════════════
# Фаза 13.22 P1: до delete_many каскад на книгу с N секциями делал N+1 git-коммитов
# (store.delete → flush на каждый) — блокировал event loop. Теперь: 1 flush на
# delete_many + 1 flush на root delete = 2, независимо от N.

class TestR10BatchDeleteSingleFlush:
    @pytest.mark.asyncio
    async def test_cascade_makes_single_batch_flush(
        self, store_no_git, pipeline_ok, qdrant_mock, monkeypatch
    ):
        """delete_entry(cascade=True) с delete_many: flush-вызовы = 2 (root + batch)."""
        from mcp_server.tools.content import import_content
        from mcp_server.tools.crud import delete_entry

        # Шаг 1: импорт книги A
        app_state_a = _make_app_state(store_no_git, pipeline_ok, qdrant_mock)
        result_a = await import_content(
            {
                "content": STRUCTURED_BOOK,
                "content_type": "book",
                "domain": "replace",
                "subject": "batch",
                "title": "Batch Book",
            },
            app_state_a,
        )
        assert "error" not in result_a
        collection_id_a = result_a["collection_id"]
        root_a = await store_no_git.read(collection_id_a)
        child_ids = [c["knowledge_id"] for c in root_a.frontmatter.children]
        assert len(child_ids) >= 3

        # Шаг 2: шпион на flush — считаем вызовы и сообщения
        flush_messages: list[str] = []
        orig_flush = store_no_git.flush

        async def _spy_flush(message: str = ""):
            flush_messages.append(message or "")
            return await orig_flush(message)

        monkeypatch.setattr(store_no_git, "flush", _spy_flush)
        flush_messages.clear()

        # Шаг 3: cascade delete
        wrapper = _WrapperStyleQdrant(child_ids)
        app_state_del = _make_app_state(store_no_git, pipeline_ok, wrapper)
        res = await delete_entry(
            {"knowledge_id": collection_id_a, "cascade": True}, app_state_del
        )

        assert res.get("deleted") is True
        assert res.get("cascade_deleted") == len(child_ids)

        # Ровно 2 flush: root-удаление + batch-удаление (НЕ N+1)
        assert len(flush_messages) == 2, (
            f"Expected 2 flushes (root + batch), got {len(flush_messages)}: {flush_messages}"
        )
        assert any("cascade delete" in m for m in flush_messages), (
            f"Expected batch flush message, got: {flush_messages}"
        )
