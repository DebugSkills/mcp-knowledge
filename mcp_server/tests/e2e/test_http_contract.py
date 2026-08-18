"""S10-S12: HTTP-level E2E — rate-limit 429, optimistic-lock 409, failure-injection 503.

Фаза 12 (V2): верифицирует HTTP-контракт для:
- S10: Rate-limit через AuthMiddleware → 429 + MCP_RATE_LIMITED (-32003)
- S11: Optimistic-lock через MCP handler → MCP_CONFLICT (-32005)
- S12: Failure-injection → /health 503 + health_check_status=0

Фаза 12 (v1.3 fix): все тесты async — httpx.AsyncClient + ASGITransport
в одном event loop с pytest-asyncio/pipeline worker. Устраняет event-loop mismatch.

Изоляция: per-test monkeypatch (function-scoped), teardown через fixture reset.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from mcp_server.health import (
    DLQ_OVERFLOW_THRESHOLD,
    set_embedding_manager,
    set_pipeline,
    set_qdrant_client,
)
from mcp_server.rate_limit import TokenBucketLimiter
from mcp_server.storage.schema import ZONE_PRIVATE, collection_for_zone

# ═══════════════════════════════════════════════════════════════
# S10: Rate-limit 429 — POST /mcp с burst=0 → MCP_RATE_LIMITED
# ═══════════════════════════════════════════════════════════════


@pytest.mark.e2e
async def test_s10a_rate_limit_write_key_returns_429(e2e_http_app):
    """S10a: burst write-key POST /mcp → HTTP 429 + JSON-RPC -32003.

    Детерминизм: заменяем rate_limiter_write на TokenBucketLimiter(burst_size=0)
    — все запросы немедленно отклоняются.
    """
    # Заменяем rate limiter на «всегда deny»
    e2e_http_app.app.state.rate_limiter_write = TokenBucketLimiter(
        refill_rate=0.0, burst_size=0,
    )

    headers = {"X-API-Key": "e2e-write-key"}
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {},
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)

    assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert "error" in body
    assert body["error"]["code"] == -32003, f"Expected -32003, got {body['error']['code']}"
    assert "Rate limit exceeded" in body["error"]["message"]


@pytest.mark.e2e
async def test_s10b_batch_request_consumes_multiple_tokens(e2e_http_app):
    """S10b: batch [A,B,C] тратит 3 токена.

    Устанавливаем лимитер с burst_size=5. Одиночный batch из 3 методов
    тратит 3 токена → второй batch из 3 методов должен пройти
    (осталось 2 + пополнение), а третий — fail.
    """
    from mcp_server.rate_limit import TokenBucketLimiter

    # Лимитер с burst_size=5 (refill_rate=0 — без пополнения, чисто burst)
    limiter = TokenBucketLimiter(refill_rate=0.0, burst_size=5)
    e2e_http_app.app.state.rate_limiter_write = limiter

    headers = {"X-API-Key": "e2e-write-key"}

    # Batch из 3 методов (tools/list × 3)
    batch = [
        {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": i}
        for i in range(3)
    ]

    # Первый batch (3 токена из 5) — OK
    resp = await e2e_http_app.post("/mcp", json=batch, headers=headers)
    assert resp.status_code == 200, f"First batch should pass, got {resp.status_code}"

    # Второй batch (ещё 3 токена, осталось 2 + refill=0) — должен упасть
    resp = await e2e_http_app.post("/mcp", json=batch, headers=headers)
    assert resp.status_code == 429, f"Second batch should be rate-limited, got {resp.status_code}"
    body = resp.json()
    assert body["error"]["code"] == -32003


@pytest.mark.e2e
async def test_s10c_rate_limit_rejected_metric_incremented(e2e_http_app):
    """S10c: после 429 → mcp_rate_limit_rejected_total > 0."""
    # Deny-all limiter
    e2e_http_app.app.state.rate_limiter_write = TokenBucketLimiter(
        refill_rate=0.0, burst_size=0,
    )

    headers = {"X-API-Key": "e2e-write-key"}
    payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}

    # Делаем запрос → 429
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)
    assert resp.status_code == 429

    # Проверяем метрику
    resp = await e2e_http_app.get("/metrics")
    text = resp.text
    # Метрика должна содержать rate_limit_rejected_total с key_level="write"
    assert 'mcp_rate_limit_rejected_total{key_level="write"} ' in text


# ═══════════════════════════════════════════════════════════════
# S11: Optimistic-lock conflict → MCP_CONFLICT -32005
# ═══════════════════════════════════════════════════════════════

S11_KNOWLEDGE_ID = "e2e-s11-lock"


@pytest.mark.e2e
async def test_s11a_optimistic_lock_conflict_returns_mcp_conflict(e2e_http_app):
    """S11a: write → update(v1) → update(v1 again) через HTTP /mcp → -32005.

    Шаги:
    1. write_knowledge → создаём запись (v1)
    2. update_entry(v1) → успех (v2)
    3. update_entry(v1) снова → конфликт (v1 уже не актуален)
    """
    headers = {"X-API-Key": "e2e-write-key"}

    # Step 1: Write
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# Optimistic Lock Test\n\nТестовый контент.\n",
                "domain": "e2e-lock",
                "subject": "testing",
                "knowledge_id": S11_KNOWLEDGE_ID,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers)
    assert resp.status_code == 200
    write_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert write_data["knowledge_id"] == S11_KNOWLEDGE_ID
    assert write_data["indexed"] is True

    # Step 2: Update v1 → success (v2)
    update_payload_v1 = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "update_entry",
            "arguments": {
                "knowledge_id": S11_KNOWLEDGE_ID,
                "content": "# Updated to v2\n\nОбновлённый контент.\n",
                "version": 1,
                "wait_for_index": True,
            },
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=update_payload_v1, headers=headers)
    assert resp.status_code == 200
    update_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert update_data["version"] == 2, f"Expected v2, got {update_data}"

    # Step 3: Update v1 again → conflict (v2 уже)
    resp = await e2e_http_app.post("/mcp", json=update_payload_v1, headers=headers)
    assert resp.status_code == 200  # JSON-RPC error приходит с HTTP 200
    body = resp.json()
    assert "error" in body, f"Expected JSON-RPC error, got {body}"
    assert body["error"]["code"] == -32005, f"Expected -32005, got {body['error']['code']}"
    assert "Version conflict" in body["error"]["message"]
    # data содержит expected/current версии
    assert body["error"]["data"] is not None
    assert body["error"]["data"]["expected_version"] == 1

    # Cleanup: ждём обработки update-задач воркером (иначе drain при pl.stop()
    # перезапишет точку ПОСЛЕ delete → мусор в коллекции) → удаляем из Qdrant
    await e2e_http_app.app.state.pipeline.wait_for_index(S11_KNOWLEDGE_ID, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S11_KNOWLEDGE_ID, collection_name=collection_for_zone(ZONE_PRIVATE))


@pytest.mark.e2e
async def test_s11b_optimistic_lock_conflicts_metric_incremented(e2e_http_app):
    """S11b: после conflict → mcp_optimistic_lock_conflicts_total > 0.

    Повторяет конфликт из s11a (write → update(v1) → update(v1) → conflict)
    и проверяет метрику.
    """
    headers = {"X-API-Key": "e2e-write-key"}

    s11b_kid = f"{S11_KNOWLEDGE_ID}-b"

    # Write
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# Lock Metric Test\n\n.\n",
                "domain": "e2e-lock",
                "subject": "metrics",
                "knowledge_id": s11b_kid,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers)
    assert resp.status_code == 200

    # Update(v1) → success
    update_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "update_entry",
            "arguments": {
                "knowledge_id": s11b_kid,
                "content": "# Updated\n",
                "version": 1,
                "wait_for_index": True,
            },
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=update_payload, headers=headers)
    assert resp.status_code == 200
    # Update(v1) again → conflict
    resp = await e2e_http_app.post("/mcp", json=update_payload, headers=headers)
    body = resp.json()
    assert body.get("error", {}).get("code") == -32005

    # Проверяем метрику
    resp = await e2e_http_app.get("/metrics")
    text = resp.text
    assert "mcp_optimistic_lock_conflicts_total " in text

    # Cleanup: ждём обработки update-задач воркером (иначе drain при pl.stop()
    # перезапишет точку ПОСЛЕ delete → мусор в коллекции) → удаляем из Qdrant
    await e2e_http_app.app.state.pipeline.wait_for_index(s11b_kid, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(s11b_kid, collection_name=collection_for_zone(ZONE_PRIVATE))


# ═══════════════════════════════════════════════════════════════
# S12: Failure-injection → /health 503 + health_check_status=0
# ═══════════════════════════════════════════════════════════════


@pytest.mark.e2e
async def test_s12a_qdrant_unreachable_returns_503(e2e_http_app):
    """S12a: monkeypatch qdrant collection_info → raise → /health 503.

    Использует set_qdrant_client() для замены health-глобала на mock.
    Не трогает session-scoped real_qdrant.
    """
    # Создаём mock с падающим collection_info
    mock_qdrant = MagicMock()
    mock_qdrant.collection_info = MagicMock(
        side_effect=ConnectionError("qdrant down"),
    )
    set_qdrant_client(mock_qdrant)

    resp = await e2e_http_app.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    qdrant_check = body["checks"]["qdrant"]
    assert qdrant_check["ok"] is False
    assert qdrant_check["connected"] is False
    assert "qdrant down" in qdrant_check.get("error", "")

    # Не должен ронять остальные проверки (pipeline/embedding — живые)
    assert body["checks"]["pipeline"]["ok"] is True
    assert body["checks"]["embedding"]["ok"] is True


@pytest.mark.e2e
async def test_s12b_embedder_not_ready_returns_503(e2e_http_app):
    """S12b: embedder.is_ready=False → /health 503.

    Заменяем _embedding_manager на mock с is_ready=False.
    """
    mock_embedder = MagicMock()
    mock_embedder.is_ready = False
    mock_embedder.backend_name = "mock"
    set_embedding_manager(mock_embedder)

    resp = await e2e_http_app.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    embedding_check = body["checks"]["embedding"]
    assert embedding_check["ok"] is False
    assert embedding_check["loaded"] is False


@pytest.mark.e2e
async def test_s12c_dlq_overflow_returns_503(e2e_http_app):
    """S12c: inject 11 DLQ entries → /health 503, checks.dlq.ok=False.

    Заменяем _pipeline на mock с _dlq.size > DLQ_OVERFLOW_THRESHOLD (10).
    """
    mock_dlq = MagicMock()
    mock_dlq.size = DLQ_OVERFLOW_THRESHOLD + 1  # 11

    mock_pipeline = MagicMock()
    mock_pipeline._dlq = mock_dlq
    # pipeline._worker_task: нужно чтобы worker_alive был True
    worker_task = MagicMock()
    worker_task.done = MagicMock(return_value=False)
    mock_pipeline._worker_task = worker_task
    # queue (чтобы pipeline check прошёл)
    import asyncio
    mock_pipeline._queue = asyncio.Queue(maxsize=1000)
    mock_pipeline.stats = {}

    set_pipeline(mock_pipeline)

    resp = await e2e_http_app.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    dlq_check = body["checks"]["dlq"]
    assert dlq_check["ok"] is False
    assert dlq_check["size"] > DLQ_OVERFLOW_THRESHOLD


@pytest.mark.e2e
async def test_s12d_health_check_status_metric_zero_when_down(e2e_http_app):
    """S12d: metric check — health_check_status{component="qdrant"}=0 при down.

    Вызываем /health с упавшим qdrant → проверяем /metrics на 0.
    """
    mock_qdrant = MagicMock()
    mock_qdrant.collection_info = MagicMock(
        side_effect=ConnectionError("qdrant down"),
    )
    set_qdrant_client(mock_qdrant)

    # Триггерим health check (обновляет метрики через update_health_metrics)
    resp = await e2e_http_app.get("/health")
    assert resp.status_code == 503

    # Проверяем метрики
    resp = await e2e_http_app.get("/metrics")
    text = resp.text
    # health_check_status для qdrant должен быть 0
    assert 'mcp_health_check_status{component="qdrant"} 0.0' in text
    # pipeline должен быть 1 (живой — не трогали)
    assert 'mcp_health_check_status{component="pipeline"} 1.0' in text
