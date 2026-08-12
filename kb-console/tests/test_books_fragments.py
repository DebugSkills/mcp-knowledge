"""Unit-тесты фрагментных операций kb-console (Фаза 13.23).

Тестируемые компоненты:
  - MCPClient helpers: add_fragment/update_fragment/delete_fragment/find_fragment
  - Чистые функции: _find_section_child, workaround fallback логика
"""

from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest

from kb_console.core.mcp_client import MCPClient
from kb_console.pages.books import _find_section_child

# ── Transport fixtures for fragment helpers ────────────────

@pytest.fixture
def fragment_transport():
    """Транспорт, перехватывающий tools_call для fragment-тулов."""
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

            # Default responses for fragment tools
            if tool_name == "add_fragment":
                inner = {"fragment_id": "eng-test-new-section", "collection_id": args.get("collection_id"), "sequence_number": 4, "indexed": True}
            elif tool_name == "update_fragment":
                # Check for version conflict
                version = args.get("version")
                if version is not None and version != 2:
                    inner = {"message": f"Version conflict: expected v{version}, actual v2", "fragment_id": args.get("fragment_id"), "expected_version": version, "current_version": 2, "conflict": True}
                else:
                    inner = {"fragment_id": args.get("fragment_id"), "version": 3, "updated_at": "2026-01-01T00:00:00Z", "indexed": True}
            elif tool_name == "delete_fragment":
                inner = {"fragment_id": args.get("fragment_id"), "deleted": True}
            elif tool_name == "find_fragment":
                inner = {"collection_id": args.get("collection_id"), "query": args.get("query"), "fragments": [{"fragment_id": "sec-1", "title": "Found", "score": 0.9, "snippet": "text..."}], "total": 1}
            else:
                inner = {"ok": True}

            wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    transport = httpx.MockTransport(handler)
    transport.captured_calls = captured_calls
    return transport


@pytest.fixture
def fragment_client(fragment_transport):
    """MCPClient с транспортом для fragment-вызовов."""
    c = httpx.AsyncClient(transport=fragment_transport, base_url="http://test")
    client = MCPClient(base_url="http://test", client=c)
    client._captured = fragment_transport.captured_calls
    return client


# ── MCPClient helpers ──────────────────────────────────────

@pytest.mark.asyncio
async def test_add_fragment_calls_tools_call(fragment_client):
    """add_fragment должен вызывать tools_call с правильными параметрами."""
    result = await fragment_client.add_fragment(
        "eng-testing-book-collection", "New Section", "Some content",
        tags=["extra"],
    )
    assert result["fragment_id"] == "eng-test-new-section"
    assert result["sequence_number"] == 4
    assert len(fragment_client._captured) == 1
    call = fragment_client._captured[0]
    assert call["tool"] == "add_fragment"
    assert call["args"]["collection_id"] == "eng-testing-book-collection"
    assert call["args"]["title"] == "New Section"
    assert call["args"]["content"] == "Some content"
    assert "extra" in call["args"]["tags"]


@pytest.mark.asyncio
async def test_add_fragment_without_tags(fragment_client):
    """add_fragment без tags — tags не передаётся."""
    await fragment_client.add_fragment("coll", "T", "C")
    call = fragment_client._captured[0]
    assert "tags" not in call["args"] or not call["args"]["tags"]


@pytest.mark.asyncio
async def test_update_fragment_with_all_params(fragment_client):
    """update_fragment со всеми параметрами — content, title, version."""
    result = await fragment_client.update_fragment(
        "sec-1", content="new content", title="New Title", version=2,
    )
    assert result["version"] == 3
    call = fragment_client._captured[0]
    assert call["tool"] == "update_fragment"
    assert call["args"]["fragment_id"] == "sec-1"
    assert call["args"]["content"] == "new content"
    assert call["args"]["title"] == "New Title"
    assert call["args"]["version"] == 2


@pytest.mark.asyncio
async def test_update_fragment_version_conflict(fragment_client):
    """Version mismatch → conflict:true в ответе."""
    result = await fragment_client.update_fragment(
        "sec-1", content="new", version=1,  # server has v2
    )
    assert result["conflict"] is True
    assert result["current_version"] == 2


@pytest.mark.asyncio
async def test_update_fragment_null_params_not_sent(fragment_client):
    """update_fragment без content/title/version — только fragment_id."""
    await fragment_client.update_fragment("sec-1")
    call = fragment_client._captured[0]
    assert call["args"] == {"fragment_id": "sec-1"}


@pytest.mark.asyncio
async def test_delete_fragment_calls_tools_call(fragment_client):
    """delete_fragment вызывает tools_call с fragment_id."""
    result = await fragment_client.delete_fragment("sec-1")
    assert result["deleted"] is True
    call = fragment_client._captured[0]
    assert call["tool"] == "delete_fragment"
    assert call["args"]["fragment_id"] == "sec-1"


@pytest.mark.asyncio
async def test_find_fragment_calls_tools_call(fragment_client):
    """find_fragment обёртка над search_knowledge."""
    result = await fragment_client.find_fragment(
        "eng-testing-book-collection", "test query", limit=10,
    )
    assert result["total"] == 1
    assert result["fragments"][0]["fragment_id"] == "sec-1"
    call = fragment_client._captured[0]
    assert call["tool"] == "find_fragment"
    assert call["args"]["collection_id"] == "eng-testing-book-collection"
    assert call["args"]["query"] == "test query"
    assert call["args"]["limit"] == 10


@pytest.mark.asyncio
async def test_find_fragment_limit_capped_at_50(fragment_client):
    """limit > 50 → capped at 50."""
    await fragment_client.find_fragment("coll", "q", limit=100)
    call = fragment_client._captured[0]
    assert call["args"]["limit"] == 50


# ── _find_section_child (NH-iter3-7: workaround removed, fallback logic) ──

class TestWorkaroundRemoved:
    """NH-iter2-8: workaround удалён; NH-iter3-7: fallback сохранён."""

    CHILDREN: ClassVar[list[dict]] = [
        {"knowledge_id": "sec-1", "title": "A", "sequence_number": 1},
        {"knowledge_id": "sec-2", "title": "B", "sequence_number": 2},
    ]

    def test_find_section_child_returns_correct(self):
        """_find_section_child находит секцию по knowledge_id."""
        result = _find_section_child(self.CHILDREN, "sec-2")
        assert result is not None
        assert result["knowledge_id"] == "sec-2"

    def test_find_section_child_not_found(self):
        """Секция не найдена в children → None (fallback: TOC + notify)."""
        result = _find_section_child(self.CHILDREN, "sec-missing")
        assert result is None
        # After workaround removal, this triggers the else-branch
        # which calls _render_toc_page(0) + ui.notify(...)
        # This test confirms the function correctly returns None,
        # which triggers the fallback in render_book_detail.

    def test_find_section_child_none_id(self):
        """section_id=None → None."""
        result = _find_section_child(self.CHILDREN, None)
        assert result is None

    def test_no_direct_fetch_workaround(self):
        """После NH-iter2-8, прямой fetch не должен вызываться.
        Проверяем что _find_section_child — чистая функция без side-effects."""
        import inspect
        source = inspect.getsource(_find_section_child)
        # Чистая функция: нет await, нет client.* вызовов
        assert "await" not in source
        assert "client" not in source
        assert "get_entry" not in source


# ── Per-call timeout (Фаза 13.16: update_fragment должен иметь per-call timeout 60s) ──

@pytest.mark.asyncio
async def test_update_fragment_has_per_call_timeout():
    """update_fragment должен использовать timeout=60.0 (как update_entry)."""
    # Проверяем сигнатуру метода
    import inspect
    source = inspect.getsource(MCPClient.update_fragment)
    # Должен быть вызов tools_call или _call с timeout=60.0
    assert "timeout=60.0" in source or "timeout = 60.0" in source, (
        "update_fragment должен использовать per-call timeout 60s"
    )


# ── Connected flow: stateful MockTransport ─────────────────

@pytest.fixture
def stateful_transport():
    """Stateful MockTransport: разные ответы в зависимости от call order.

    Вызовы:
      0: add_fragment → fragment_id + sequence_number + indexed
      1: get_entry (collection) → children=[4 шт., включая frag1, frag2]
      2: update_fragment (frag1, version=1) → version=2 success
      3: update_fragment (frag1, version=1) → conflict (already v2)
      4: delete_fragment (frag1) → deleted=true
    """
    calls = []
    call_index = [0]  # mutable counter

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        rid = body.get("id", 1)

        if body.get("method") != "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

        params = body.get("params", {})
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        calls.append({"tool": tool_name, "args": dict(args), "call_index": call_index[0]})

        idx = call_index[0]
        call_index[0] += 1

        if idx == 0:  # add_fragment
            inner = {
                "fragment_id": "e2e-frag-lifecycle-001",
                "collection_id": "test-book-collection",
                "sequence_number": 4,
                "indexed": True,
            }
        elif idx == 1:  # get_entry (коллекция с детьми)
            inner = {
                "knowledge_id": "test-book-collection",
                "content_type": "collection",
                "title": "Test Book Collection",
                "domain": "e2e",
                "subject": "test",
                "children": [
                    {"knowledge_id": "orig-sec-1", "title": "Original 1", "sequence_number": 1},
                    {"knowledge_id": "orig-sec-2", "title": "Original 2", "sequence_number": 2},
                    {"knowledge_id": "orig-sec-3", "title": "Original 3", "sequence_number": 3},
                    {"knowledge_id": "e2e-frag-lifecycle-001", "title": "Connected Flow Fragment", "sequence_number": 4},
                ],
            }
        elif idx == 2:  # update_fragment (успешно, v1→v2)
            inner = {
                "fragment_id": "e2e-frag-lifecycle-001",
                "version": 2,
                "updated_at": "2026-08-12T15:30:00Z",
                "indexed": True,
            }
        elif idx == 3:  # update_fragment повторно с v1 → conflict
            inner = {
                "message": "Version conflict: expected v1, actual v2",
                "fragment_id": "e2e-frag-lifecycle-001",
                "expected_version": 1,
                "current_version": 2,
                "conflict": True,
            }
        elif idx == 4:  # delete_fragment (успешно)
            inner = {
                "fragment_id": "e2e-frag-lifecycle-001",
                "deleted": True,
            }
        else:
            inner = {"ok": True}

        wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

    transport = httpx.MockTransport(handler)
    transport.calls = calls
    return transport


@pytest.fixture
def lifecycle_client(stateful_transport):
    """MCPClient для connected flow тестов."""
    c = httpx.AsyncClient(transport=stateful_transport, base_url="http://test")
    client = MCPClient(base_url="http://test", client=c)
    client._calls_tracker = stateful_transport.calls
    return client


@pytest.mark.asyncio
async def test_fragment_lifecycle_connected_flow(lifecycle_client):
    """Connected flow: add_fragment → get_entry → update_fragment → conflict → delete_fragment.

    Проверяет что хелперы передают правильные аргументы и unwrap-ят результаты.
    """
    # Step 1: add_fragment — проверяем передачу аргументов + unwrap результата
    result = await lifecycle_client.add_fragment(
        collection_id="test-book-collection",
        title="Connected Flow Fragment",
        content="This is a stateful test content.",
        tags=["flow-test"],
    )
    assert result["fragment_id"] == "e2e-frag-lifecycle-001"
    assert result["collection_id"] == "test-book-collection"
    assert result["sequence_number"] == 4
    assert result["indexed"] is True

    # Step 2: get_entry — проверяем что новый фрагмент виден в children
    entry = await lifecycle_client.get_entry("test-book-collection")
    assert entry["content_type"] == "collection"
    children = entry["children"]
    assert len(children) == 4
    child_ids = {c["knowledge_id"] for c in children}
    assert "e2e-frag-lifecycle-001" in child_ids
    assert children[-1]["sequence_number"] == 4

    # Step 3: update_fragment — проверяем version upgrade + unwrap
    update_result = await lifecycle_client.update_fragment(
        fragment_id="e2e-frag-lifecycle-001",
        content="Updated content for stateful test.",
        version=1,
    )
    assert update_result["version"] == 2
    assert update_result["indexed"] is True
    assert "conflict" not in update_result or update_result.get("conflict") is not True

    # Step 4: повторный update_fragment с тем же version=1 → conflict
    conflict_result = await lifecycle_client.update_fragment(
        fragment_id="e2e-frag-lifecycle-001",
        content="Should conflict.",
        version=1,
    )
    assert conflict_result["conflict"] is True
    assert conflict_result["current_version"] == 2
    assert conflict_result["expected_version"] == 1

    # Step 5: delete_fragment — проверяем unwrap deleted=true
    delete_result = await lifecycle_client.delete_fragment("e2e-frag-lifecycle-001")
    assert delete_result["deleted"] is True
    assert delete_result["fragment_id"] == "e2e-frag-lifecycle-001"

    # Проверяем что все 5 вызовов сделаны с правильными tool_name
    expected_sequence = [
        ("add_fragment", {"collection_id": "test-book-collection", "title": "Connected Flow Fragment", "content": "This is a stateful test content.", "tags": ["flow-test"]}),
        ("get_entry", {"knowledge_id": "test-book-collection"}),
        ("update_fragment", {"fragment_id": "e2e-frag-lifecycle-001", "content": "Updated content for stateful test.", "version": 1}),
        ("update_fragment", {"fragment_id": "e2e-frag-lifecycle-001", "content": "Should conflict.", "version": 1}),
        ("delete_fragment", {"fragment_id": "e2e-frag-lifecycle-001"}),
    ]
    assert len(lifecycle_client._calls_tracker) == 5, (
        f"Expected 5 calls, got {len(lifecycle_client._calls_tracker)}: {lifecycle_client._calls_tracker}"
    )
    for i, (expected_tool, expected_args) in enumerate(expected_sequence):
        actual = lifecycle_client._calls_tracker[i]
        assert actual["tool"] == expected_tool, (
            f"Call {i}: expected tool={expected_tool}, got {actual['tool']}"
        )
        assert actual["args"] == expected_args, (
            f"Call {i}: expected args={expected_args}, got {actual['args']}"
        )
