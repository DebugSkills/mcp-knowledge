"""Unit-тесты страниц и компонентов kb-console.

Тестируемые компоненты:
  - render_header (components/header.py) — навигационный хедер
  - _sanitize_title (core/utils.py) — санитизация названия
  - update_entry helper (core/mcp_client.py) — вызов тула rename
"""

from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import MagicMock, patch

import httpx
import pytest

from kb_console.core.mcp_client import MCPClient
from kb_console.pages.books import _find_section_child

# ── Sanitize title (чистая функция, импортируется из реализации) ──


class TestSanitizeTitle:
    """Тесты функции санитизации названия книги."""

    def test_strips_whitespace(self):
        """Пробелы по краям убираются."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("  My Book  ") == "My Book"

    def test_strips_hash_prefix(self):
        """Ведущий # убирается."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("# My Book") == "My Book"

    def test_strips_multiple_hashes(self):
        """Множественные ведущие # убираются."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("## My Book") == "My Book"

    def test_handles_hash_and_whitespace(self):
        """Комбинация пробелов и # — полная санитизация."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("  #  My Book  ") == "My Book"

    def test_empty_after_sanitize(self):
        """Пустая строка после санитизации остаётся пустой."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("  #  ") == ""

    def test_preserves_inner_hashes(self):
        """# внутри названия сохраняются."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("# C# Programming") == "C# Programming"

    def test_already_clean(self):
        """Чистое название не портится."""
        from kb_console.core.utils import _sanitize_title
        assert _sanitize_title("Python Basics") == "Python Basics"


# ── MCPClient.update_entry helper ──


@pytest.fixture
def update_entry_transport():
    """Транспорт, перехватывающий tools_call для update_entry."""

    captured_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        method = body.get("method", "")
        rid = body.get("id", 1)

        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            captured_calls.append({"tool": tool_name, "args": dict(args)})

            inner = {"knowledge_id": args.get("knowledge_id", ""), "ok": True}
            wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    transport = httpx.MockTransport(handler)
    transport.captured_calls = captured_calls
    return transport


@pytest.fixture
def update_client(update_entry_transport):
    """MCPClient с транспортом, захватывающим вызовы update_entry."""
    c = httpx.AsyncClient(transport=update_entry_transport, base_url="http://test")
    client = MCPClient(base_url="http://test", client=c)
    client._captured = update_entry_transport.captured_calls
    return client


@pytest.mark.asyncio
async def test_update_entry_calls_tools_call(update_client):
    """update_entry должен вызывать tools_call с knowledge_id и content."""
    result = await update_client.update_entry(
        "eng-test-collection",
        content="# New Title\n\nКоллекция импортированных секций. Оглавление — в frontmatter.children.",
    )
    assert result["ok"] is True
    assert len(update_client._captured) == 1
    call = update_client._captured[0]
    assert call["tool"] == "update_entry"
    assert call["args"]["knowledge_id"] == "eng-test-collection"
    assert "New Title" in call["args"]["content"]


@pytest.mark.asyncio
async def test_update_entry_preserves_content(update_client):
    """update_entry должен передавать content как есть (санитизация — на вызывающей стороне)."""
    await update_client.update_entry("kid-1", content="# My Book\n\nBody.")
    call = update_client._captured[0]
    assert call["args"]["content"] == "# My Book\n\nBody."


# ── Smoke: ROUTES registry (требует импорта после реализации) ──


def test_routes_registry_exists():
    """ROUTES должен существовать в pages/__init__.py после реализации."""
    from kb_console.pages import ROUTES
    assert isinstance(ROUTES, list)
    assert len(ROUTES) == 5
    paths = {r[0] for r in ROUTES}
    assert paths == {"/status", "/books", "/import", "/search", "/quality"}
    labels = {r[1] for r in ROUTES}
    assert labels == {"Статус", "Книги", "Импорт", "Поиск", "Качество"}


def test_routes_builders_are_callable():
    """Каждый builder в ROUTES должен быть callable."""
    from kb_console.pages import ROUTES
    for path, label, builder in ROUTES:
        assert callable(builder), f"Builder for {path} ({label}) is not callable"


# ── Import: title_input pre-fill logic ──

# Примечание: тестирование UI-логики (handle_upload → title_input.value)
# требует запущенного NiceGUI-сервера. Эти тесты проверяют только
# чистые функции (sanitize) и MCPClient-интеграцию.
# Визуальная проверка UI — через Playwright (Фаза F).


# ── Header component: smoke test ──

def test_header_module_imports():
    """Модуль components.header должен импортироваться и содержать render_header."""
    from kb_console.components.header import render_header
    assert callable(render_header)


def test_progress_panel_module_imports():
    """Модуль components.progress_panel должен импортироваться и содержать build_scan_progress."""
    from kb_console.components.progress_panel import (
        _LEVEL_COLORS,
        SCAN_POLL_INTERVAL,
        build_scan_progress,
    )
    assert callable(build_scan_progress)
    assert isinstance(SCAN_POLL_INTERVAL, float)
    assert isinstance(_LEVEL_COLORS, dict)
    assert "info" in _LEVEL_COLORS
    assert "warning" in _LEVEL_COLORS
    assert "error" in _LEVEL_COLORS


def test_components_init_exports():
    """components/__init__.py должен экспортировать render_header и build_scan_progress."""
    from kb_console.components import build_scan_progress, render_header
    assert callable(render_header)
    assert callable(build_scan_progress)


# ── _find_section_child (чистая функция, unit-тестируема) ──


class TestFindSectionChild:
    """Тесты функции поиска child в TOC по knowledge_id (Фаза 13.13)."""

    CHILDREN: ClassVar[list[dict]] = [
        {"knowledge_id": "sec-1", "title": "Section 1", "sequence_number": 0},
        {"knowledge_id": "sec-2", "title": "Section 2", "sequence_number": 1},
        {"knowledge_id": "sec-3", "title": "Section 3", "sequence_number": 2},
    ]

    def test_found(self):
        """Возвращает child с matching knowledge_id."""
        result = _find_section_child(self.CHILDREN, "sec-2")
        assert result is not None
        assert result["knowledge_id"] == "sec-2"
        assert result["title"] == "Section 2"

    def test_not_found(self):
        """Возвращает None, если section_id отсутствует в children."""
        result = _find_section_child(self.CHILDREN, "sec-nonexistent")
        assert result is None

    def test_none_section_id(self):
        """section_id=None → None (backward-compat: Books-страница без фрагмента)."""
        result = _find_section_child(self.CHILDREN, None)
        assert result is None

    def test_empty_children(self):
        """Пустой список → None."""
        result = _find_section_child([], "sec-1")
        assert result is None


# ── Quality page (Фаза 13.14) ──


class TestQualityPageImports:
    """Проверка импортов страницы качества."""

    def test_quality_module_imports(self):
        """Модуль pages.quality должен импортироваться."""
        from kb_console.pages import quality
        assert hasattr(quality, "build_quality")
        assert callable(quality.build_quality)

    def test_quality_in_routes(self):
        """/quality должен быть в ROUTES."""
        from kb_console.pages import ROUTES
        quality_routes = [r for r in ROUTES if r[0] == "/quality"]
        assert len(quality_routes) == 1
        assert quality_routes[0][1] == "Качество"
        assert callable(quality_routes[0][2])


class TestMCPClientQualityMethods:
    """Тесты новых методов MCPClient для quality tools (Фаза 13.14)."""

    @pytest.fixture
    def quality_transport(self):
        """Транспорт для quality-вызовов."""
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            method = body.get("method", "")
            rid = body.get("id", 1)

            if method == "tools/call":
                params = body.get("params", {})
                tool_name = params.get("name", "")
                args = params.get("arguments", {})
                captured.append({"tool": tool_name, "args": dict(args)})

                inner = {"ok": True, "tool": tool_name}
                wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

        transport = httpx.MockTransport(handler)
        transport.captured = captured
        return transport

    @pytest.fixture
    def quality_client(self, quality_transport):
        """MCPClient для quality-тестов."""
        c = httpx.AsyncClient(transport=quality_transport, base_url="http://test")
        client = MCPClient(base_url="http://test", client=c)
        client._captured = quality_transport.captured
        return client

    @pytest.mark.asyncio
    async def test_review_queue_books_call(self, quality_client):
        """review_queue_books должен вызывать tools/call с review_queue_books."""
        result = await quality_client.review_queue_books(domain="eng", limit=20)
        assert result["ok"] is True
        assert len(quality_client._captured) == 1
        call = quality_client._captured[0]
        assert call["tool"] == "review_queue_books"
        assert call["args"]["domain"] == "eng"
        assert call["args"]["limit"] == 20

    @pytest.mark.asyncio
    async def test_resolve_quality_issue_call(self, quality_client):
        """resolve_quality_issue с knowledge_id + cascade."""
        result = await quality_client.resolve_quality_issue(
            action="deprecate", knowledge_id="book-x", cascade=True, reason="test",
        )
        assert result["ok"] is True
        call = quality_client._captured[0]
        assert call["tool"] == "resolve_quality_issue"
        assert call["args"]["knowledge_id"] == "book-x"
        assert call["args"]["cascade"] is True
        assert call["args"]["action"] == "deprecate"

    @pytest.mark.asyncio
    async def test_delete_entry_cascade_call(self, quality_client):
        """delete_entry с cascade=True."""
        result = await quality_client.delete_entry("book-x", cascade=True)
        assert result["ok"] is True
        call = quality_client._captured[0]
        assert call["tool"] == "delete_entry"
        assert call["args"]["knowledge_id"] == "book-x"
        assert call["args"]["cascade"] is True

    @pytest.mark.asyncio
    async def test_run_quality_scan_call(self, quality_client):
        """run_quality_scan вызывает tools/call."""
        result = await quality_client.run_quality_scan()
        assert result["ok"] is True
        call = quality_client._captured[0]
        assert call["tool"] == "run_quality_scan"

    @pytest.mark.asyncio
    async def test_list_quality_issues_call(self, quality_client):
        """list_quality_issues должен вызывать tools/call с status/limit."""
        result = await quality_client.list_quality_issues(status="open", limit=50)
        assert result["ok"] is True
        call = quality_client._captured[0]
        assert call["tool"] == "list_quality_issues"
        assert call["args"]["status"] == "open"
        assert call["args"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_list_quality_issues_passes_types(self, quality_client):
        """list_quality_issues передаёт types при указании."""
        result = await quality_client.list_quality_issues(
            types=["duplicate", "broken_link"], status="open", limit=20,
        )
        assert result["ok"] is True
        call = quality_client._captured[0]
        assert call["tool"] == "list_quality_issues"
        assert call["args"]["types"] == ["duplicate", "broken_link"]
        assert call["args"]["limit"] == 20


class TestRenderIssuesPanel:
    """Тесты рендера панели issues (Фаза 1)."""

    def test_render_issues_with_data(self):
        """При наличии issues рендерятся type-chip и кнопки resolve/ignore."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        data = {
            "issues": [
                {
                    "issue_id": "iss-1",
                    "type": "duplicate",
                    "knowledge_id": "kid-1",
                    "detail": "Duplicate content",
                    "detected_at": "2026-08-12T00:00:00+00:00",
                    "status": "open",
                    "severity": "high",
                }
            ],
            "total": 1,
        }
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issues(data, refresh_fn)
        assert mock_ui.chip.called  # type-chip
        assert mock_ui.button.called  # resolve/ignore buttons

    def test_render_issues_empty(self):
        """При отсутствии issues — нет кнопок, есть строка 'Issues не найдены'."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issues({"issues": [], "total": 0}, refresh_fn)
        assert not mock_ui.button.called
        label_texts = [c.args[0] for c in mock_ui.label.call_args_list if c.args]
        assert any("Issues не найдены" in str(t) for t in label_texts)


# ── _read_uploaded_file (extracted to core/utils.py, Phase 13.24) ──


class TestReadUploadedFile:
    """Тесты _read_uploaded_file — чистая функция чтения файла (Фаза 1, 13.24)."""

    def test_valid_md(self):
        """Корректный .md файл в UTF-8 — возвращает (content, None)."""
        from kb_console.core.utils import _read_uploaded_file
        content, error = _read_uploaded_file("test.md", b"# Hello")
        assert error is None
        assert content == "# Hello"

    def test_unsupported_ext(self):
        """Неподдерживаемое расширение — возвращает (None, error_msg)."""
        from kb_console.core.utils import _read_uploaded_file
        content, error = _read_uploaded_file("test.docx", b"%PDF")
        assert content is None
        assert error is not None
        assert "Неподдерживаемый" in error

    def test_pdf_returns_binary_marker(self):
        """PDF (поддерживается с 13.21 Ф3) — возвращает PDF_BINARY_MARKER, не decode."""
        from kb_console.core.utils import PDF_BINARY_MARKER, _read_uploaded_file
        content, error = _read_uploaded_file("test.pdf", b"%PDF-1.7 binary")
        assert content == PDF_BINARY_MARKER
        assert error is None

    def test_too_large(self):
        """Превышение MAX_FILE_SIZE — возвращает (None, error_msg)."""
        from kb_console.core.utils import MAX_FILE_SIZE, _read_uploaded_file
        big = b"x" * (MAX_FILE_SIZE + 1)
        content, error = _read_uploaded_file("test.md", big)
        assert content is None
        assert error is not None
        assert "слишком большой" in error

    def test_encoding_fallback_windows1251(self):
        """Файл в windows-1251 (не UTF-8) — fallback успешен."""
        from kb_console.core.utils import _read_uploaded_file
        # "Привет" в windows-1251 (кириллица)
        text_cp1251 = "Привет, мир!".encode("windows-1251")
        content, error = _read_uploaded_file("test.txt", text_cp1251)
        assert error is None
        assert "Привет" in content


# ── Replace dialog (новый компонент, Фаза 2, 13.24) ──


class TestReplaceDialogImports:
    """Проверка импортов нового компонента replace_dialog (Фаза 2, 13.24)."""

    def test_replace_dialog_module_imports(self):
        """Модуль components.replace_dialog должен импортироваться,
        show_replace_dialog должна быть callable."""
        from kb_console.components.replace_dialog import show_replace_dialog
        assert callable(show_replace_dialog)

    def test_replace_dialog_in_components_init(self):
        """components/__init__.py должен экспортировать show_replace_dialog."""
        from kb_console.components import show_replace_dialog
        assert callable(show_replace_dialog)


# ── render_import_progress (чистая функция рендера, extracted to core/utils.py, Phase 13.24+critic P1-2) ──


class TestRenderImportProgress:
    """Unit-тесты render_import_progress — чистая функция рендера прогресса импорта.

    Использует mock-объекты (MagicMock + patch) для проверки вызовов ui.*
    без необходимости запущенного NiceGUI-сервера (page-context не нужен —
    render_import_progress не зависит от глобального состояния NiceGUI, только
    от переданного container и snapshot).

    Проверяемые аспекты:
      1. container.clear() вызывается перед рендером
      2. snapshot с phase="done" рендерит финальный статус (label + linear_progress)
      3. snapshot с error рендерит сообщения об ошибках
    """

    def test_clears_container(self):
        """render_import_progress должен очищать контейнер перед отрисовкой."""
        from kb_console.core.utils import render_import_progress
        container = MagicMock()
        snapshot = {"imported": 3, "total": 10, "failed": 0, "status": "running", "messages": []}
        with patch("kb_console.core.utils.ui"):
            render_import_progress(snapshot, container)
        container.clear.assert_called_once()

    def test_renders_done_status(self):
        """Snapshot с status='done' → рендерит финальный прогресс (label + linear_progress)."""
        from kb_console.core.utils import render_import_progress
        container = MagicMock()
        snapshot = {"imported": 10, "total": 10, "failed": 0, "status": "done", "messages": []}
        with patch("kb_console.core.utils.ui") as mock_ui:
            render_import_progress(snapshot, container)
        assert mock_ui.label.called
        assert mock_ui.linear_progress.called
        # Проверяем, что linear_progress вызван с value=1.0 (100% при done)
        mock_ui.linear_progress.assert_called_once_with(value=1.0)

    def test_renders_error_with_messages(self):
        """Snapshot с status='error' + messages → рендерит ошибку и лог-сообщения."""
        from kb_console.core.utils import render_import_progress
        container = MagicMock()
        snapshot = {
            "imported": 2,
            "total": 10,
            "failed": 1,
            "status": "error",
            "messages": [
                {"t": "12:00:01", "text": "Import failed", "level": "error"},
            ],
        }
        with patch("kb_console.core.utils.ui") as mock_ui:
            render_import_progress(snapshot, container)
        assert mock_ui.label.called
        assert mock_ui.linear_progress.called
        # Проверяем, что среди label-вызовов есть текст ошибки
        label_texts = [c.args[0] for c in mock_ui.label.call_args_list if c.args]
        assert any("Import failed" in str(t) for t in label_texts)
        # Проверяем linear_progress с value=0.2 (2/10 при error)
        mock_ui.linear_progress.assert_called_once_with(value=0.2)


class TestBulkIgnoreButton:
    """Bulk-кнопка «Игнорировать все <тип>» в панели issues (P0 B1)."""

    def test_bulk_button_renders_for_types(self):
        """При наличии issues с типами рендерятся bulk-кнопки."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        data = {
            "issues": [
                {
                    "issue_id": "iss-1",
                    "type": "duplicate",
                    "knowledge_id": "kid-1",
                    "detail": "Dup",
                    "detected_at": "2026-08-12T00:00:00+00:00",
                    "status": "open",
                    "severity": "warn",
                },
                {
                    "issue_id": "iss-2",
                    "type": "orphaned",
                    "knowledge_id": "kid-2",
                    "detail": "Missing child",
                    "detected_at": "2026-08-12T00:00:00+00:00",
                    "status": "open",
                    "severity": "warn",
                },
            ],
            "total": 2,
        }
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issues(data, refresh_fn)
        # Ищем button-вызовы с текстом «Игнорировать все»
        btn_texts = [
            " ".join(str(a) for a in c.args)
            for c in mock_ui.button.call_args_list
        ]
        assert any("Игнорировать все duplicate" in t for t in btn_texts)
        assert any("Игнорировать все orphaned" in t for t in btn_texts)

    def test_no_bulk_button_when_no_issues(self):
        """Без issues — bulk-кнопок нет."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issues({"issues": [], "total": 0}, refresh_fn)
        btn_texts = [
            " ".join(str(a) for a in c.args)
            for c in mock_ui.button.call_args_list
        ]
        assert not any("Игнорировать все" in t for t in btn_texts)


class TestDedupButtons:
    """Фаза 1 dedup: 📦-кнопка и checkbox на dup-карточках."""

    def _dup_issue(self):
        return {
            "issue_id": "iss-dup-1",
            "type": "duplicate",
            "knowledge_id": "kid-source",
            "detail": "Possible duplicate of kid-target (same subject=devops, cosine=0.974)",
            "detected_at": "2026-08-13T00:00:00+00:00",
            "status": "open",
            "severity": "warn",
        }

    def test_deprecate_button_on_duplicate_card(self):
        """Duplicate-карточка содержит 📦 (deprecate) и checkbox."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issue_card(self._dup_issue(), refresh_fn)
        texts = [" ".join(str(a) for a in c.args) for c in mock_ui.button.call_args_list]
        assert any("📦" in t for t in texts)
        # checkbox для пакетного выбора рендерится
        assert mock_ui.checkbox.call_count == 1

    def test_no_deprecate_button_on_non_duplicate(self):
        """Non-duplicate-карточка НЕ содержит 📦 и checkbox."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        issue = self._dup_issue()
        issue["type"] = "missing_field"
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issue_card(issue, refresh_fn)
        texts = [" ".join(str(a) for a in c.args) for c in mock_ui.button.call_args_list]
        assert not any("📦" in t for t in texts)
        assert mock_ui.checkbox.call_count == 0

    def test_bulk_hide_button_renders(self):
        """Кнопка «Пакетно скрыть выбранные» — при наличии duplicate."""
        from kb_console.pages import quality
        refresh_fn = MagicMock()
        data = {"issues": [self._dup_issue()], "total": 1}
        with patch("kb_console.pages.quality.ui") as mock_ui:
            quality._render_issues(data, refresh_fn)
        texts = [" ".join(str(a) for a in c.args) for c in mock_ui.button.call_args_list]
        assert any("Пакетно скрыть выбранные" in t for t in texts)

    def test_set_selected(self):
        """_set_selected добавляет/убирает issue_id в сессию."""
        from kb_console.pages import quality
        quality._selected_issues.clear()
        quality._set_selected("iss-1", True)
        quality._set_selected("iss-2", True)
        quality._set_selected("iss-1", False)
        assert quality._selected_issues == {"iss-2"}
        quality._selected_issues.clear()
