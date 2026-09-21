"""kb-console-roles Ф1.6 (B2, P1-1/Q4): серверный replace-гейт в import_content.

Гейт стоит сразу после извлечения replace_collection_id (content.py, после
:956-957), ДО pdf-ветки (:969) и early-validation (:1006) — единая точка
всех путей (book: submit_import; pdf: _bg_import). Механика: params["_auth"]
(mcp_handler инжектит всегда); level ∉ {editor, write} + replace задан →
error-dict (формат content.py:1008-1027). `_auth` отсутствует (internal
CLI/тесты) → allow. R6: оба content_type (book/pdf) ловятся гейтом.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from mcp_server.auth import AuthInfo
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.content import import_content

pytestmark = pytest.mark.asyncio

_BOOK_CONTENT = (
    "# Replacement Gate Book\n\n"
    "## Chapter 1\n\n"
    "This is the first chapter with enough body text to be valid content.\n\n"
    "## Chapter 2\n\n"
    "This is the second chapter with its own distinct body text."
)


@pytest.fixture
def real_store(tmp_path) -> MarkdownStore:
    return MarkdownStore(knowledge_root=tmp_path / "knowledge")


async def _make_book(store: MarkdownStore, knowledge_id: str) -> None:
    fm = KnowledgeFrontmatter(
        knowledge_id=knowledge_id,
        domain="engineering",
        subject="testing",
        content_type="collection",
        zone="private",
        tags=["test"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content="# Book\n\nBody."))


def _params(**overrides) -> dict:
    params = {
        "content": _BOOK_CONTENT,
        "content_type": "book",
        "domain": "engineering",
        "subject": "testing",
        "title": "Replacement Gate Book",
        "replace_collection_id": "eng-testing-replace-target",
        "quality_checks": False,
    }
    params.update(overrides)
    return params


def _auth(level: str) -> AuthInfo:
    return AuthInfo(authenticated=True, key_level=level)


class TestReplaceGateForbidden:
    async def test_import_level_replace_denied(self, app_state, real_store):
        """Основной кейс: contributor (import) + replace → error-dict, книга цела."""
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(_params(_auth=_auth("import")), app_state)
        assert "error" in result
        assert "not allowed for key level 'import'" in result["error"]
        assert "editor or write key" in result["error"]
        # книга НЕ удалена и НЕ заменена
        assert await real_store.read("eng-testing-replace-target") is not None

    @pytest.mark.parametrize("level", ["read", "subscriber"])
    async def test_read_subscriber_replace_denied(self, app_state, real_store, level):
        """Defense-in-depth: даже если тул пропустит, replace запрещён."""
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(_params(_auth=_auth(level)), app_state)
        assert "error" in result
        assert "not allowed for key level" in result["error"]

    async def test_pdf_import_replace_denied_before_queue(self, app_state, real_store):
        """R6: pdf-ветка ловится гейтом ДО submit_import (нет import_id в ответе)."""
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(
            _params(
                _auth=_auth("import"),
                content_type="pdf",
                content="",
                pdf_path="/tmp/nonexistent.pdf",
            ),
            app_state,
        )
        assert "error" in result
        assert "not allowed for key level 'import'" in result["error"]
        assert "import_id" not in result


class TestReplaceGateAllowed:
    async def test_editor_level_replace_allowed(self, app_state, real_store):
        """editor + replace → гейт пропускает, импорт завершается успешно."""
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(_params(_auth=_auth("editor")), app_state)
        assert "error" not in result, result.get("error")
        root = await real_store.read(result["collection_id"])
        assert root is not None

    async def test_write_level_replace_allowed(self, app_state, real_store):
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(_params(_auth=_auth("write")), app_state)
        assert "error" not in result, result.get("error")

    async def test_no_auth_internal_allowed(self, app_state, real_store):
        """`_auth` отсутствует (CLI/тесты/внутренние вызовы) → allow."""
        await _make_book(real_store, "eng-testing-replace-target")
        app_state.store = real_store

        result = await import_content(_params(), app_state)
        assert "error" not in result, result.get("error")

    async def test_import_level_without_replace_allowed(self, app_state, real_store):
        """import без replace_collection_id гейтом не блокируется (обычный импорт)."""
        app_state.store = real_store

        result = await import_content(
            _params(_auth=_auth("import"), replace_collection_id=""), app_state,
        )
        assert "error" not in result, result.get("error")
