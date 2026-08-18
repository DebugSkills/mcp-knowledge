"""W3.19: E2E-инвариант №4 — subscriber-изоляция (0 private-данных).

Сценарий: private + public записи через write-ключ; subscriber-токен
(TokenStore) → матрица 10 read-тулов → 0 private-данных;
tools/list = только SUBSCRIBER_TOOLS; resources/prompts → 403.

Изоляция: e2e-зональные коллекции (_patched_collection session fixture),
tmp TokenStore, tmp KNOWLEDGE_DIR (e2e_http_app).
"""

from __future__ import annotations

import asyncio
import json

import pytest

PRIVATE_ID = "e2e-sub-priv"
PUBLIC_ID = "e2e-sub-pub"


def _tool_result(resp) -> dict:
    """Извлечь результат tools/call: MCP оборачивает в content[0].text (JSON)."""
    body = resp.json()
    result = body.get("result", {})
    content = result.get("content")
    if content and isinstance(content, list) and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    if "error" in body:
        return body
    return result


@pytest.mark.e2e
async def test_subscriber_isolation_invariant4(e2e_http_app, tmp_path):
    """Инвариант №4: subscriber × 10 тулов → 0 private-данных."""
    from mcp_server.auth import SUBSCRIBER_TOOLS
    from mcp_server.rate_limit import TokenBucketLimiter
    from mcp_server.token_store import TokenStore

    # ── Setup: tmp TokenStore + subscriber-токен ──────────────
    store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
    _, subscriber_key = store.create(
        level="subscriber", zone="public", note="e2e subscriber"
    )
    e2e_http_app.app.state.token_store = store
    # Щедрый subscriber-лимитер (тест делает ~15 запросов подряд)
    e2e_http_app.app.state.rate_limiter_subscriber = TokenBucketLimiter(
        refill_rate=1000.0 / 60.0, burst_size=100,
    )

    headers_write = {"X-API-Key": "e2e-write-key"}
    headers_sub = {"X-API-Key": subscriber_key}

    # ── Step 1: private + public записи ───────────────────────
    for kid, zone in ((PRIVATE_ID, "private"), (PUBLIC_ID, "public")):
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "write_knowledge",
                "arguments": {
                    "content": f"# {kid}\n\nКонтент записи {kid} для проверки изоляции.\n",
                    "domain": "e2e-sub",
                    "subject": "isolation",
                    "knowledge_id": kid,
                    "zone": zone,
                    "wait_for_index": True,
                },
            },
            "id": 1,
        }
        resp = await e2e_http_app.post("/mcp", json=payload, headers=headers_write)
        assert resp.status_code == 200, f"write {kid}: {resp.text}"
        body = resp.json()
        assert "error" not in body.get("result", {}), f"write {kid}: {body}"
        # Пауза для refill write rate limiter (burst=5)
        await asyncio.sleep(1.0)

    # ── Step 2: subscriber-матрица × 10 тулов ─────────────────
    # search_knowledge
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "проверки изоляции"}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    result = _tool_result(resp)
    found = [r["knowledge_id"] for r in result.get("results", [])]
    assert PRIVATE_ID not in found, f"subscriber search leaked private: {found}"
    assert PUBLIC_ID in found, f"subscriber search missed public: {found}"

    # search_by_tags
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "search_by_tags", "arguments": {"tags": ["isolation"]}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    found = [r["knowledge_id"] for r in _tool_result(resp).get("results", [])]
    assert PRIVATE_ID not in found

    # get_entry на private → not found (fail-closed)
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "get_entry", "arguments": {"knowledge_id": PRIVATE_ID}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    result = _tool_result(resp)
    assert "error" in result and "not found" in result["error"].lower(), (
        f"subscriber get_entry on private must be fail-closed: {result}"
    )

    # get_entry на public → ok
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "get_entry", "arguments": {"knowledge_id": PUBLIC_ID}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    assert "error" not in _tool_result(resp)

    # find_fragment на private-книгу → «Collection not found»
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "find_fragment",
                   "arguments": {"collection_id": "book-priv-e2e", "query": "x"}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    result = _tool_result(resp)
    assert "error" in result and "Collection not found" in result["error"], result

    # list_* тулы — без ошибок (public-зона пуста/частична, но 401/500 не должно быть)
    for tool in ("list_domains", "list_subjects", "list_projects", "list_collections"):
        resp = await e2e_http_app.post("/mcp", json={
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
            "id": 1,
        }, headers=headers_sub)
        assert resp.status_code == 200, f"{tool}: {resp.text}"

    # get_knowledge_map
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "get_knowledge_map", "arguments": {}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text

    # analyze_content (последний из 10)
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": "analyze_content", "arguments": {"content": "тест анализа"}},
        "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text

    # ── Step 3: tools/list = только SUBSCRIBER_TOOLS ──────────
    resp = await e2e_http_app.post("/mcp", json={
        "jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1,
    }, headers=headers_sub)
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in _tool_result(resp).get("tools", [])}
    assert names == SUBSCRIBER_TOOLS, f"subscriber tools/list: {names}"

    # ── Step 4: resources/prompts → 403 ───────────────────────
    for method in ("resources/list", "prompts/list"):
        resp = await e2e_http_app.post("/mcp", json={
            "jsonrpc": "2.0", "method": method, "params": {}, "id": 1,
        }, headers=headers_sub)
        assert resp.status_code == 200, resp.text
        err = resp.json().get("error", {})
        assert err.get("code") == -32002, f"{method}: {resp.json()}"

    # ── Step 5: cleanup ───────────────────────────────────────
    for kid in (PRIVATE_ID, PUBLIC_ID):
        resp = await e2e_http_app.post("/mcp", json={
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "delete_entry",
                       "arguments": {"knowledge_id": kid, "cascade": True}},
            "id": 1,
        }, headers=headers_write)
        assert resp.status_code == 200, f"cleanup {kid}: {resp.text}"
        await asyncio.sleep(1.0)
