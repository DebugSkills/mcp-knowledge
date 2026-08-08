"""S13-S16: MCP protocol E2E — ping, notifications/initialized, unknown notification.

Фаза 13.21 Фаза 1: MCP spec compliance tests:
- S13: ping → {"result": {}} (пустой объект, не null)
- S14: notifications/initialized → HTTP 204 (no body)
- S15: notifications/initialized without id → accepted (no -32600)
- S16: unknown notification → HTTP 204 (no -32601 error per JSON-RPC 2.0 §4.1)
- Regression: tools/call without id → -32600 (fix не сломал валидацию обычных методов)
"""

from __future__ import annotations

import json

import pytest


@pytest.mark.e2e
async def test_s13_ping_returns_empty_result(e2e_http_app):
    """S13: ping → {"result": {}} — пустой объект, не null."""
    payload = {
        "jsonrpc": "2.0",
        "method": "ping",
        "id": 1,
    }
    headers = {"X-API-Key": "e2e-read-key"}
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert "result" in body, f"Expected 'result', got keys: {list(body.keys())}"
    assert body["result"] == {}, f"Expected empty object {{}}, got {body['result']}"
    assert body["id"] == 1


@pytest.mark.e2e
async def test_s14_notifications_initialized_returns_204(e2e_http_app):
    """S14: notifications/initialized → HTTP 204 (no body, notification response)."""
    # Notification: no id field (JSON-RPC notification semantics)
    payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    headers = {"X-API-Key": "e2e-read-key"}
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}"
    # 204 No Content — body should be empty
    assert resp.content == b"" or resp.content == b"null" or resp.content == b'""', \
        f"Expected empty body for 204, got: {resp.content[:100]}"


@pytest.mark.e2e
async def test_s15_notifications_initialized_accepted_without_id(e2e_http_app):
    """S15: notifications/initialized без id → accepted (не -32600 Invalid Request)."""
    # P0 fix: _validate_jsonrpc должен разрешить absent id для notifications/*
    payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    # Отправляем БЕЗ id — валидация не должна ругаться на missing id
    assert "id" not in payload
    
    headers = {"X-API-Key": "e2e-read-key"}
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    # Должен быть 204 (notification accepted), а не 200 с ошибкой -32600
    assert resp.status_code == 204, \
        f"Expected 204 for notification without id, got {resp.status_code}: {resp.content[:200]}"


@pytest.mark.e2e
async def test_s16_unknown_notification_returns_204(e2e_http_app):
    """S16: notifications/unknown → 204 (P2-9: JSON-RPC spec §4.1 compliance)."""
    # Unknown notification method — сервер НЕ должен отвечать ошибкой
    payload = {
        "jsonrpc": "2.0",
        "method": "notifications/unknown",
    }
    headers = {"X-API-Key": "e2e-read-key"}
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    assert resp.status_code == 204, \
        f"Expected 204 for unknown notification, got {resp.status_code}: {resp.content[:200]}"


@pytest.mark.e2e
async def test_s17_tools_call_without_id_still_rejected(e2e_http_app):
    """S17 regression: tools/call без id → -32600 (fix не сломал валидацию)."""
    # Обычный метод (не notification) без id должен всё ещё возвращать ошибку
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
    }
    headers = {"X-API-Key": "e2e-read-key"}
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    # Должен быть 200 с ошибкой -32600 (id отсутствует для не-notification метода)
    body = resp.json()
    assert "error" in body, f"Expected error for tools/call without id, got: {body}"
    assert body["error"]["code"] == -32600, \
        f"Expected -32600, got {body['error']['code']}: {body['error']['message']}"


@pytest.mark.e2e
async def test_s18_ping_accepted_without_auth(e2e_http_app):
    """S18: ping должен быть в UNAUTHENTICATED_METHODS — без API-ключа OK."""
    payload = {
        "jsonrpc": "2.0",
        "method": "ping",
        "id": 1,
    }
    # БЕЗ X-API-Key заголовка
    resp = await e2e_http_app.post("/mcp", json=payload)

    assert resp.status_code == 200, f"Expected 200 for unauthenticated ping, got {resp.status_code}"
    body = resp.json()
    assert body["result"] == {}
    assert body["id"] == 1


@pytest.mark.e2e
async def test_s19_notifications_initialized_accepted_without_auth(e2e_http_app):
    """S19: notifications/initialized без auth — OK (в UNAUTHENTICATED_METHODS)."""
    payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    # БЕЗ X-API-Key
    resp = await e2e_http_app.post("/mcp", json=payload)

    assert resp.status_code == 204, \
        f"Expected 204 for unauthenticated notification, got {resp.status_code}: {resp.content[:200]}"
