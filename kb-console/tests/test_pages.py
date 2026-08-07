"""Unit-тесты страниц и компонентов kb-console.

Тестируемые компоненты:
  - render_header (components/header.py) — навигационный хедер
  - _sanitize_title (core/utils.py) — санитизация названия
  - update_entry helper (core/mcp_client.py) — вызов тула rename
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb_console.core.mcp_client import MCPClient

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
    assert len(ROUTES) == 4
    paths = {r[0] for r in ROUTES}
    assert paths == {"/status", "/books", "/import", "/search"}
    labels = {r[1] for r in ROUTES}
    assert labels == {"Статус", "Книги", "Импорт", "Поиск"}


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


def test_components_init_exports():
    """components/__init__.py должен экспортировать render_header."""
    from kb_console.components import render_header
    assert callable(render_header)
