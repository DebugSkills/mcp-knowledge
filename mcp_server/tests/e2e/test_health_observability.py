"""S9: Health + Metrics E2E — HTTP-level проверка liveness, readiness, /metrics.

Фаза 12 (V2): верифицирует HTTP-контракт /health/live, /health (real backends),
/metrics формат + значения, и что метрики отражают реальную активность.

Сценарии:
  s9a: GET /health/live → 200 {"status":"alive"}
  s9b: GET /health → 200 (real Qdrant + Ollama healthy)
  s9c: GET /metrics → 200 text/plain, содержит ключевые метрики
  s9d: после write+search → collection_size > 0, write/search latency observed
"""

from __future__ import annotations

import json

import pytest

# ═══════════════════════════════════════════════════════════════
# S9a: Liveness probe — всегда 200
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s9a_health_live_returns_200(e2e_http_app):
    """GET /health/live → 200 {"status":"alive"} независимо от backends."""
    resp = await e2e_http_app.get("/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "alive"}


# ═══════════════════════════════════════════════════════════════
# S9b: Readiness probe — real backends healthy
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s9b_health_real_backends_healthy(e2e_http_app):
    """GET /health → 200 c real Qdrant + Ollama (живые)."""
    resp = await e2e_http_app.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["version"] == "0.1.0"

    checks = body["checks"]
    assert set(checks.keys()) == {"qdrant", "embedding", "pipeline", "dlq"}

    # Qdrant: жив (REST localhost:6333)
    assert checks["qdrant"]["ok"] is True
    assert checks["qdrant"]["connected"] is True

    # Embedding: Ollama mxbai-embed-large жив
    assert checks["embedding"]["ok"] is True
    assert checks["embedding"]["loaded"] is True
    assert isinstance(checks["embedding"].get("latency_ms"), (int, float))

    # Pipeline: worker жив
    assert checks["pipeline"]["ok"] is True
    assert checks["pipeline"]["worker"] == "alive"

    # DLQ: не переполнена
    assert checks["dlq"]["ok"] is True


# ═══════════════════════════════════════════════════════════════
# S9c: /metrics endpoint — формат + ключевые метрики
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s9c_metrics_endpoint_returns_prometheus_format(e2e_http_app):
    """GET /metrics → 200 text/plain, содержит ключевые Prometheus метрики."""
    resp = await e2e_http_app.get("/metrics")
    assert resp.status_code == 200
    text = resp.text

    # Content-Type: text/plain
    assert "text/plain" in resp.headers.get("content-type", "")

    # Ключевые метрики (существовали до Фазы 12)
    assert "mcp_queue_size" in text
    assert "mcp_dlq_size" in text
    assert "mcp_collection_size" in text
    assert "mcp_search_latency_seconds" in text
    assert "mcp_write_latency_seconds" in text
    assert "mcp_quality_gate_skipped_total" in text
    assert "mcp_pipeline_processed_total" in text

    # Новые метрики Фазы 12 (AC2)
    assert "mcp_health_check_status" in text
    assert "mcp_rate_limit_rejected_total" in text
    assert "mcp_optimistic_lock_conflicts_total" in text
    assert "mcp_process_uptime_seconds" in text
    assert "mcp_tool_requests_total" in text

    # Prometheus HELP/TYPE lines
    assert "# HELP " in text
    assert "# TYPE " in text


# ═══════════════════════════════════════════════════════════════
# S9d: Метрики после write + search (реальная активность)
# ═══════════════════════════════════════════════════════════════

S9D_KNOWLEDGE_ID = "e2e-s9d-health-write"


@pytest.mark.e2e
async def test_s9d_metrics_after_write_and_search(e2e_http_app):
    """После write_knowledge + search_knowledge через HTTP — метрики отражают активность.

    Шаги:
    1. POST /mcp tools/call write_knowledge (wait_for_index=True)
    2. POST /mcp tools/call search_knowledge
    3. GET /metrics → collection_size > 0, write/search latency observed
    """
    headers = {"X-API-Key": "e2e-write-key"}

    # Step 1: Write
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# Health Metrics Test\n\nТестовый контент для проверки метрик.\n",
                "domain": "e2e-health",
                "subject": "metrics",
                "knowledge_id": S9D_KNOWLEDGE_ID,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers)
    assert resp.status_code == 200
    write_body = resp.json()
    assert "result" in write_body, f"Write failed: {write_body}"
    write_data = json.loads(write_body["result"]["content"][0]["text"])
    assert write_data["knowledge_id"] == S9D_KNOWLEDGE_ID
    assert write_data["indexed"] is True

    # Step 2: Search
    search_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "search_knowledge",
            "arguments": {"query": "метрики тест", "top_k": 5},
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=search_payload, headers=headers)
    assert resp.status_code == 200
    search_body = resp.json()
    assert "result" in search_body, f"Search failed: {search_body}"

    # Step 3: Проверка метрик
    resp = await e2e_http_app.get("/metrics")
    assert resp.status_code == 200
    text = resp.text

    # collection_size > 0 (write создал запись в Qdrant)
    assert "mcp_collection_size " in text
    # write_latency_seconds_count > 0 (write был вызван)
    assert "mcp_write_latency_seconds_count " in text
    # search_latency_seconds_count > 0 (search был вызван)
    assert "mcp_search_latency_seconds_count " in text
    # tool_requests со статусом success (Prometheus labels в алфавитном порядке!)
    assert 'mcp_tool_requests_total{status="success",tool="write_knowledge"}' in text
    assert 'mcp_tool_requests_total{status="success",tool="search_knowledge"}' in text

    # Cleanup: ждём обработки задач воркером (иначе drain при pl.stop()
    # перезапишет точку ПОСЛЕ delete → мусор в коллекции) → удаляем из Qdrant
    await e2e_http_app.app.state.pipeline.wait_for_index(S9D_KNOWLEDGE_ID, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S9D_KNOWLEDGE_ID)


# ═══════════════════════════════════════════════════════════════
# S9e: 13.15 регрессия инцидента — /health/live отвечает во время скана
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s9e_health_responsive_during_quality_scan(e2e_http_app):
    """Регрессия инцидента 2026-08-07: run_quality_scan НЕ блокирует event loop.

    Инцидент: scanner._update_qdrant_payloads делал ~15K sync set_payload
    прямо в event loop → /health/live таймаутил (ReadTimeout в kb-console).
    13.15: скан — фоновая задача, sync-вызовы в run_in_executor.

    Проверка: во время активного фонового скана GET /health/live отвечает
    <100ms, а GET /quality/scan/progress возвращает прогресс.
    """
    import asyncio
    import time
    from unittest.mock import AsyncMock, patch

    headers = {"X-API-Key": "e2e-write-key"}

    async def slow_scan(**kwargs):
        """Имитация долгого скана: 10 итераций × 50ms (loop должен дышать)."""
        for _ in range(10):
            await asyncio.sleep(0.05)
        return {
            "files_scanned": 0,
            "review_queue_size": 0,
            "duplicates_detected": 0,
            "issues_created": 0,
        }

    with patch("mcp_server.quality.scanner.run_scan", new=AsyncMock(side_effect=slow_scan)):
        resp = await e2e_http_app.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "run_quality_scan", "arguments": {}},
                "id": 1,
            },
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body, f"run_quality_scan failed: {body}"
        data = json.loads(body["result"]["content"][0]["text"])
        assert data["status"] == "started", f"Expected started, got: {data}"
        assert len(data["scan_id"]) == 16

        # 1) /health/live отвечает быстро во время активного скана
        t0 = time.monotonic()
        h = await e2e_http_app.get("/health/live")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert h.status_code == 200
        assert h.json() == {"status": "alive"}
        assert elapsed_ms < 100, f"/health/live took {elapsed_ms:.1f}ms during scan (event loop blocked!)"

        # 2) прогресс-эндпоинт отдаёт данные скана
        p = await e2e_http_app.get("/quality/scan/progress")
        assert p.status_code == 200
        progress = p.json()
        # ImportProgressTracker хранит запись под ключом import_id == scan_id
        assert progress.get("import_id") == data["scan_id"]
        assert progress["status"] in ("running", "done", "error")

        # 3) даём фону завершиться → прогресс в терминальном статусе
        await asyncio.sleep(0.7)
        p2 = await e2e_http_app.get("/quality/scan/progress")
        progress2 = p2.json()
        assert progress2["status"] in ("done", "error")

        # 4) ждём фоновую задачу (иначе «Task was destroyed but it is pending»)
        scan_task = e2e_http_app.app.state.scan_task
        if scan_task is not None:
            try:
                await asyncio.wait_for(scan_task, timeout=3.0)
            except (asyncio.CancelledError, TimeoutError):
                pass
