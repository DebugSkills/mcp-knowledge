"""Unit tests for MarkdownStore — flush() and _git_commit() deprecation.

Task 6.2 Cleanup Cycle: flush() публичный метод, _git_commit() DeprecationWarning-обёртка.
P2-2: N2 фикс — внутренние вызовы (write/update/delete) используют self.flush().
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import pytest


@pytest.fixture
def tmp_git_root():
    """Временный git-репозиторий для тестов flush()."""
    import git
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "knowledge"
        root.mkdir()
        git.Repo.init(str(root))
        (root / ".trash").mkdir()
        yield root


class TestFlushPublic:
    """Тест: flush() — публичный метод с git commit."""

    def test_flush_exists_and_is_callable(self, tmp_git_root):
        """flush() — публичный метод, не приватный."""
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)
        assert hasattr(store, "flush")
        assert callable(store.flush)

        # Проверяем что НЕ приватный (без _ префикса)
        from inspect import iscoroutinefunction
        assert iscoroutinefunction(store.flush)

    @pytest.mark.asyncio
    async def test_flush_does_not_emit_deprecation_warning(self, tmp_git_root):
        """flush() не генерирует DeprecationWarning."""
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await store.flush("test commit")
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) == 0, (
                f"flush() should not emit DeprecationWarning, got: {deprecation_warnings}"
            )

    @pytest.mark.asyncio
    async def test_flush_with_no_repo_is_noop(self, tmp_git_root):
        """flush() с _repo=None не падает и не делает git-операций."""
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)
        store._repo = None

        # Не должно падать
        await store.flush("should be noop")

    @pytest.mark.asyncio
    async def test_flush_with_git_audit_disabled(self, tmp_git_root):
        """flush() с GIT_AUDIT=False — no-op."""
        from mcp_server.config import settings
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)
        original = settings.GIT_AUDIT
        settings.GIT_AUDIT = False
        try:
            await store.flush("should be noop")
        finally:
            settings.GIT_AUDIT = original


class TestGitCommitDeprecation:
    """Тест: _git_commit() генерирует DeprecationWarning."""

    @pytest.mark.asyncio
    async def test_git_commit_emits_deprecation_warning(self, tmp_git_root):
        """_git_commit() генерирует DeprecationWarning."""
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        with pytest.warns(DeprecationWarning, match="_git_commit.*deprecated"):
            await store._git_commit("test")

    @pytest.mark.asyncio
    async def test_git_commit_still_functions(self, tmp_git_root):
        """_git_commit() по-прежнему работает (делегирует flush())."""
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        # Подавляем DeprecationWarning и проверяем что выполняется
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await store._git_commit("test via deprecated")
            # Не падает — функция работает


class TestInternalCallersNoWarning:
    """N2: write(), update(), delete() вызывают self.flush() (не _git_commit)."""

    @pytest.mark.asyncio
    async def test_write_calls_flush_not_git_commit(self, tmp_git_root):
        """write() использует self.flush(), не self._git_commit()."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        # Перехватываем flush чтобы верифицировать вызов
        flush_called = []

        async def _fake_flush(message):
            flush_called.append(message)

        store.flush = _fake_flush

        req = WriteRequest(
            domain="test",
            subject="demo",
            content="# Test\nTest content.",
        )
        await store.write(req)

        assert len(flush_called) == 1
        assert "add:" in flush_called[0]

    @pytest.mark.asyncio
    async def test_update_calls_flush_not_git_commit(self, tmp_git_root):
        """update() использует self.flush(), не self._git_commit()."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        # Сначала создаём запись
        req = WriteRequest(domain="test", subject="demo", content="# Hello")
        entry = await store.write(req)

        # Перехватываем flush
        flush_called = []

        async def _fake_flush(message):
            flush_called.append(message)

        store.flush = _fake_flush

        await store.update(entry.frontmatter.knowledge_id, content="# Updated")

        assert len(flush_called) == 1
        assert "update:" in flush_called[0]

    @pytest.mark.asyncio
    async def test_delete_calls_flush_not_git_commit(self, tmp_git_root):
        """delete() использует self.flush(), не self._git_commit()."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        # Создаём запись
        req = WriteRequest(domain="test", subject="demo", content="# ToDelete")
        entry = await store.write(req)

        # Перехватываем flush
        flush_called = []

        async def _fake_flush(message):
            flush_called.append(message)

        store.flush = _fake_flush

        await store.delete(entry.frontmatter.knowledge_id)

        assert len(flush_called) == 1
        assert "delete:" in flush_called[0]


class TestPytestDeprecationWarningStrict:
    """AC11: pytest -W error::DeprecationWarning не падает на внутренних вызовах.

    Проверяет что вызов store.flush() через write/update/delete не генерирует
    DeprecationWarning (т.к. внутренние вызовы обновлены на self.flush(), N2 fix).
    """

    @pytest.mark.asyncio
    async def test_write_no_deprecation_warning(self, tmp_git_root):
        """write() → self.flush() → НЕ генерирует DeprecationWarning."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            req = WriteRequest(domain="test", subject="demo", content="# NoWarn")
            await store.write(req)

    @pytest.mark.asyncio
    async def test_update_no_deprecation_warning(self, tmp_git_root):
        """update() → self.flush() → НЕ генерирует DeprecationWarning."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)
        req = WriteRequest(domain="test", subject="demo", content="# Hello")
        entry = await store.write(req)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            await store.update(entry.frontmatter.knowledge_id, content="# Updated")

    @pytest.mark.asyncio
    async def test_delete_no_deprecation_warning(self, tmp_git_root):
        """delete() → self.flush() → НЕ генерирует DeprecationWarning."""
        from mcp_server.models import WriteRequest
        from mcp_server.storage.markdown_store import MarkdownStore

        store = MarkdownStore(knowledge_root=tmp_git_root)
        req = WriteRequest(domain="test", subject="demo", content="# ToDelete")
        entry = await store.write(req)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            await store.delete(entry.frontmatter.knowledge_id)
