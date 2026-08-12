"""P1 regression gate: concurrent GET /imports → 0 RuntimeError (Content-Length).

Task A фикс (AuthMiddleware → pure ASGI) устраняет root-cause BaseHTTPMiddleware
стриминговой гонки. Этот тест — регрессионный гейт: 50 параллельных GET /imports
против минимального FastAPI app с AuthMiddleware, все должны вернуть 200 без
RuntimeError "Response content longer than Content-Length".

Фаза code-2026-08-10-305.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport
from mcp_server.auth import AuthMiddleware


@pytest.fixture
def concurrent_app():
    """Минимальный FastAPI app с AuthMiddleware + GET /imports + GET /metrics."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, fastapi_app=app)

    # Mock auth state: GET-запросы без ключа пропускаются middleware,
    # но /imports endpoint требует defence-in-depth auth → установим bypass.
    @app.get("/imports")
    async def list_imports(request: Request):
        """GET /imports — lean payload (без log)."""
        auth = getattr(request.state, "auth", None)
        if auth is None or not getattr(auth, "authenticated", False):
            # В тесте устанавливаем фиктивный auth через scope state
            pass
        queue = getattr(request.app.state, "import_queue", [])
        sanitized = [{k: v for k, v in rec.items() if not k.startswith("_")}
                     for rec in queue]
        return sanitized

    # Pre-populate mock queue (10 записей — realistic payload)
    app.state.import_queue = [
        {
            "import_id": f"test-{i:04d}",
            "name": f"book_{i}.pdf",
            "status": "done" if i < 8 else "running",
            "phase": "done" if i < 8 else "indexing",
            "imported": 42,
            "total": 42,
            "error": None,
            "created_at": "2026-08-10T18:00:00+03:00",
            "finished_at": "2026-08-10T18:05:00+03:00",
        }
        for i in range(10)
    ]

    return app


@pytest.mark.asyncio
async def test_concurrent_get_imports_no_content_length_error(concurrent_app):
    """50 параллельных GET /imports — все 200, без RuntimeError.

    Ключевой regression-гейт Task A (AuthMiddleware → pure ASGI):
    BaseHTTPMiddleware при конкурентной нагрузке вызывал Content-Length mismatch.
    Pure ASGI middleware не имеет этого дефекта.
    """
    N = 50
    transport = ASGITransport(app=concurrent_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def fetch_one(i: int) -> tuple[int, int]:
            try:
                resp = await client.get("/imports", timeout=10.0)
                return i, resp.status_code
            except Exception:  # noqa: BLE001 — сетевой сбой → код -1
                return i, -1

        tasks = [fetch_one(i) for i in range(N)]
        results = await asyncio.gather(*tasks)

    # Анализ результатов
    failures = [(i, status) for i, status in results if status != 200]
    error_count = sum(1 for _, status in results if status == -1)

    if failures:
        failure_details = "\n".join(
            f"  request {i}: HTTP {status}" for i, status in failures
        )
        pytest.fail(
            f"Concurrent load test: {len(failures)}/{N} requests failed "
            f"(errors={error_count}):\n{failure_details}"
        )

    assert len(results) == N, f"Expected {N} results, got {len(results)}"
    all_200 = [r for _, r in results if r == 200]
    assert len(all_200) == N, f"Only {len(all_200)}/{N} returned 200"
