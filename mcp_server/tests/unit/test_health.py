"""E1: Health endpoint tests — liveness/readiness split.

Tests:
- /health/live always returns 200 {"status": "alive"}
- /health returns 200 when all backends healthy
- /health returns 503 when qdrant unreachable
- /health returns 503 when embedder not ready
- /health returns 503 when pipeline worker dead
- /health returns 503 when queue overloaded (>90%)
- /health returns 503 when DLQ overflow (>10)
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_server.health import (
    DLQ_OVERFLOW_THRESHOLD,
    QUEUE_UTILIZATION_THRESHOLD,
    set_embedding_manager,
    set_pipeline,
    set_qdrant_client,
)
from mcp_server.health import (
    router as health_router,
)

# ── Minimal test app (avoids importing main.py + qdrant_client) ──


@pytest.fixture(scope="module")
def app():
    """Create a minimal FastAPI app with only the health router."""
    app = FastAPI()
    app.include_router(health_router)
    return app


@pytest.fixture
def client(app):
    """TestClient for the minimal health app."""
    return TestClient(app)


# ── Helpers ──────────────────────────────────────────────────


def _make_mock_embedder(*, is_ready: bool = True, backend_name: str = "fake") -> MagicMock:
    """Create a mock embedding manager."""
    mgr = MagicMock()
    mgr.is_ready = is_ready
    mgr.backend_name = backend_name
    mgr.embed_latency_check = MagicMock(return_value={
        "backend": backend_name,
        "model": "BAAI/bge-m3",
        "latency_ms": 12.5,
    })
    return mgr


def _make_mock_qdrant(*, collection_info_return=None, collection_info_raise=None) -> MagicMock:
    """Create a mock Qdrant client."""
    client = MagicMock()
    if collection_info_raise:
        client.collection_info = MagicMock(side_effect=collection_info_raise)
    else:
        client.collection_info = MagicMock(return_value=collection_info_return or {
            "name": "knowledge",
            "points_count": 42,
            "vectors_count": 42,
        })
    return client


def _make_mock_pipeline(
    *,
    worker_alive: bool = True,
    queue_size: int = 0,
    queue_max: int = 1000,
    dlq_size: int = 0,
) -> MagicMock:
    """Create a mock IndexingPipeline with controllable health state."""
    pipeline = MagicMock()

    # Worker task
    worker_task = MagicMock()
    worker_task.done = MagicMock(return_value=not worker_alive)
    pipeline._worker_task = worker_task

    # Queue
    queue = asyncio.Queue(maxsize=queue_max)
    for i in range(queue_size):
        queue.put_nowait({"dummy": i})
    pipeline._queue = queue

    # DLQ
    dlq = MagicMock()
    dlq.size = dlq_size
    pipeline._dlq = dlq

    # Stats
    pipeline.stats = {"queued": 10, "processed": 8, "failed": 0, "dlq": 0}

    return pipeline


# ── Global reset between tests ────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_health_globals():
    """Reset health module globals between tests to prevent cross-test contamination."""
    set_embedding_manager(None)
    set_qdrant_client(None)
    set_pipeline(None)
    yield


# ── E1.1: Liveness probe ────────────────────────────────────


class TestHealthLive:
    """GET /health/live — always 200."""

    def test_returns_200_alive_status(self, client):
        """Returns {"status": "alive"} regardless of backends."""
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "alive"}

    def test_returns_200_with_degraded_qdrant(self, client):
        """Ignores backend failures — always 200."""
        set_qdrant_client(_make_mock_qdrant(collection_info_raise=ConnectionError("qdrant down")))
        set_embedding_manager(None)
        set_pipeline(None)
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "alive"}


# ── E1.2: Readiness probe — healthy case ────────────────────


class TestHealthReadinessHealthy:
    """GET /health — 200 when all backends healthy."""

    def test_all_healthy_returns_200(self, client):
        """All backends healthy → 200 {"status": "healthy"}."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["checks"]["qdrant"]["ok"] is True
        assert body["checks"]["embedding"]["ok"] is True
        assert body["checks"]["pipeline"]["ok"] is True
        assert body["checks"]["dlq"]["ok"] is True


# ── E1.2: Readiness probe — degraded cases ──────────────────


class TestHealthReadinessDegraded:
    """GET /health — 503 when any backend degraded."""

    def test_qdrant_unreachable_returns_503(self, client):
        """Qdrant raises → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant(collection_info_raise=ConnectionError("no qdrant")))
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["qdrant"]["ok"] is False
        assert "no qdrant" in body["checks"]["qdrant"]["error"]

    def test_qdrant_not_initialized_returns_503(self, client):
        """Qdrant client is None → 503 degraded."""
        set_qdrant_client(None)
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["qdrant"]["ok"] is False

    def test_embedder_not_ready_returns_503(self, client):
        """Embedder.is_ready = False → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder(is_ready=False))
        set_pipeline(_make_mock_pipeline())
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["embedding"]["ok"] is False
        assert resp.json()["checks"]["embedding"]["loaded"] is False

    def test_embedder_not_initialized_returns_503(self, client):
        """Embedder manager is None → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(None)
        set_pipeline(_make_mock_pipeline())
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["embedding"]["ok"] is False

    def test_pipeline_worker_dead_returns_503(self, client):
        """Pipeline worker task done → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline(worker_alive=False))
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["pipeline"]["ok"] is False
        assert resp.json()["checks"]["pipeline"]["worker"] == "dead"

    def test_pipeline_not_initialized_returns_503(self, client):
        """Pipeline is None → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(None)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["pipeline"]["ok"] is False

    def test_queue_overloaded_returns_503(self, client):
        """Queue >90% full → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline(queue_size=95, queue_max=100))
        resp = client.get("/health")
        assert resp.status_code == 503
        pipeline_check = resp.json()["checks"]["pipeline"]
        assert pipeline_check["ok"] is False
        assert pipeline_check["queue_utilization"] > QUEUE_UTILIZATION_THRESHOLD

    def test_queue_below_threshold_ok(self, client):
        """Queue ≤90% → pipeline check ok."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline(queue_size=90, queue_max=100))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["checks"]["pipeline"]["ok"] is True

    def test_dlq_overflow_returns_503(self, client):
        """DLQ size > threshold (10) → 503 degraded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline(dlq_size=DLQ_OVERFLOW_THRESHOLD + 1))
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.json()["checks"]["dlq"]["ok"] is False
        assert resp.json()["checks"]["dlq"]["size"] > DLQ_OVERFLOW_THRESHOLD

    def test_dlq_below_threshold_ok(self, client):
        """DLQ size ≤ threshold → dlq check ok."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline(dlq_size=DLQ_OVERFLOW_THRESHOLD))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["checks"]["dlq"]["ok"] is True


# ── E1.3: Response structure ────────────────────────────────


class TestHealthResponseStructure:
    """Verify response fields are present and well-formed."""

    def test_all_checks_present_when_healthy(self, client):
        """Response includes all 4 check keys: qdrant, embedding, pipeline, dlq."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        body = client.get("/health").json()
        assert set(body["checks"].keys()) == {"qdrant", "embedding", "pipeline", "dlq"}

    def test_version_field_present(self, client):
        """Response includes version field."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        body = client.get("/health").json()
        assert "version" in body
        assert body["version"] == "0.1.0"

    def test_embedding_latency_ms_present_when_healthy(self, client):
        """Embedding check includes latency_ms when loaded."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        embedding = client.get("/health").json()["checks"]["embedding"]
        assert embedding["latency_ms"] == 12.5

    def test_qdrant_points_present_when_connected(self, client):
        """Qdrant check includes points_count when connected."""
        set_qdrant_client(_make_mock_qdrant())
        set_embedding_manager(_make_mock_embedder())
        set_pipeline(_make_mock_pipeline())
        qdrant = client.get("/health").json()["checks"]["qdrant"]
        assert qdrant["points"] == 42
        assert qdrant["vectors"] == 42
