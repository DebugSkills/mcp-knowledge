# ruff: noqa: BLE001, S110
"""E2E fixtures: real Qdrant (REST) + Ollama + temp git-root + pipeline.

Фаза 9 (v1.2): session-scoped коллекция knowledge_e2e через monkeypatch,
force_recreate в начале + delete в finally (идемпотентность).
REST-mode QdrantClient (gRPC 6334 не экспонирован).
_OllamaAdapter duck-type под контракт pipeline (embed_sync) + search-tools (encode).

Изоляция: прод-коллекция knowledge/knowledge_root НЕ затрагиваются.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import (
    Request,
)

# ── Module-level env overrides (BEFORE any mcp_server import) ──
os.environ.setdefault("MCP_READ_KEYS", '["e2e-read-key"]')
os.environ.setdefault("MCP_WRITE_KEYS", '["e2e-write-key"]')
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("DLQ_DIR", "/tmp/test-e2e-dlq")

E2E_COLLECTION = "knowledge_e2e"
QDRANT_REST_URL = "http://localhost:6333"
# Ollama-КОНТЕЙНЕР проекта (bridge 127.0.0.1:11435), НЕ host-ollama (11434 —
# чужие проекты; после миграции моделей проекта на host нет). setdefault:
# (1) env-параметризация для запуска против другого инстанса,
# (2) инжект OLLAMA_URL в env ДО импортов mcp_server (см. блок ниже) —
#     тестируемый settings видит тот же URL, что и e2e-фикстуры.
OLLAMA_BASE_URL = os.environ.setdefault("OLLAMA_URL", "http://localhost:11435")
# Текущий embedder проекта (миграция на mxbai-embed-large, 2026-08-19):
# mxbai-embed-large (1024-dim, русская семантика OK; ctx 512 токенов → чанки ≤480c).
# bge-m3 (Ollama F16) отбракован: NaN на реальных текстах.
# e2e-коллекция создаётся с dim из schema (1024).
OLLAMA_MODEL = "mxbai-embed-large"
VECTOR_DIM = 1024


# ═══════════════════════════════════════════════════════════════
# _OllamaAdapter — duck-type под pipeline/tools контракт
# ═══════════════════════════════════════════════════════════════

class _OllamaAdapter:
    """Адаптер OllamaEmbedder под контракт pipeline/tools.

    Pipeline/splitting вызывают embed_sync(texts)→list[list[float]].
    Search-tools вызывают encode(query)→list[float].
    OllamaEmbedder.encode(str)→list[float], encode(list)→list[list[float]].
    """

    def __init__(self, embedder):
        self._embedder = embedder
        self.backend_name = "ollama"
        self.is_ready = True

    @property
    def dim(self) -> int:
        return self._embedder.dim  # 1024 для mxbai-embed-large

    def encode(self, text):
        """search-tools: str→list[float], list→list[list[float]]."""
        return self._embedder.encode(text)

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """pipeline/splitting: всегда list[str]→list[list[float]]."""
        return self._embedder.encode(texts)

    def embed_latency_check(self) -> dict:
        """Фаза 12: health-совместимый latency check."""
        import time
        t0 = time.monotonic()
        self._embedder.encode("health_check_ping")
        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "backend": self.backend_name,
            "model": self._embedder.model if hasattr(self._embedder, "model") else "mxbai-embed-large",
            "latency_ms": round(latency_ms, 2),
        }


# ═══════════════════════════════════════════════════════════════
# Session-scoped fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def _e2e_services_available():
    """Skip-guard: проверяет доступность Qdrant REST + Ollama."""
    import httpx

    try:
        r = httpx.get(f"{QDRANT_REST_URL}/collections", timeout=5.0)
        r.raise_for_status()
    except Exception:
        pytest.skip("E2E requires Qdrant REST at localhost:6333")

    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5.0)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        if not any(m.startswith(OLLAMA_MODEL) for m in models):
            pytest.skip(f"E2E requires Ollama model '{OLLAMA_MODEL}'")
    except Exception:
        pytest.skip(f"E2E requires Ollama at {OLLAMA_BASE_URL}")


@pytest.fixture(scope="session")
def _patched_collection():
    """Monkeypatch зональных коллекций на e2e-имена на всю сессию.

    W2: две зоны — schema.COLLECTION_PUBLIC/PRIVATE (+ blue-green v1/v2).
    Все call-sites идут через collection_for_zone(), читающую модульные
    глобалы в рантайме → патч срабатывает. qdrant_client.COLLECTION_NAME
    импортирован by-value → патчится отдельно. resources.COLLECTION_NAME
    больше НЕ существует (W2-B1e) — не патчим.
    """
    import mcp_server.storage.qdrant_client as qc_mod
    import mcp_server.storage.schema as sch

    orig = {
        "COLLECTION_PUBLIC": sch.COLLECTION_PUBLIC,
        "COLLECTION_PRIVATE": sch.COLLECTION_PRIVATE,
        "PUBLIC_V1": sch.PUBLIC_V1,
        "PUBLIC_V2": sch.PUBLIC_V2,
        "PRIVATE_V1": sch.PRIVATE_V1,
        "PRIVATE_V2": sch.PRIVATE_V2,
        "COLLECTION_NAME": sch.COLLECTION_NAME,
        "qc_COLLECTION_NAME": qc_mod.COLLECTION_NAME,
    }
    sch.COLLECTION_PUBLIC = f"{E2E_COLLECTION}_public"
    sch.COLLECTION_PRIVATE = f"{E2E_COLLECTION}_private"
    sch.PUBLIC_V1 = f"{E2E_COLLECTION}_public_v1"
    sch.PUBLIC_V2 = f"{E2E_COLLECTION}_public_v2"
    sch.PRIVATE_V1 = f"{E2E_COLLECTION}_private_v1"
    sch.PRIVATE_V2 = f"{E2E_COLLECTION}_private_v2"
    sch.COLLECTION_NAME = sch.COLLECTION_PRIVATE
    qc_mod.COLLECTION_NAME = sch.COLLECTION_PRIVATE
    yield
    sch.COLLECTION_PUBLIC = orig["COLLECTION_PUBLIC"]
    sch.COLLECTION_PRIVATE = orig["COLLECTION_PRIVATE"]
    sch.PUBLIC_V1 = orig["PUBLIC_V1"]
    sch.PUBLIC_V2 = orig["PUBLIC_V2"]
    sch.PRIVATE_V1 = orig["PRIVATE_V1"]
    sch.PRIVATE_V2 = orig["PRIVATE_V2"]
    sch.COLLECTION_NAME = orig["COLLECTION_NAME"]
    qc_mod.COLLECTION_NAME = orig["qc_COLLECTION_NAME"]


@pytest.fixture(scope="session")
def real_qdrant(_patched_collection):
    """Session-scoped QdrantClient в REST-mode.

    W2: setup — сброс обеих e2e-зон (delete alias + v1/v2) →
    ensure_zonal_collections() (создаёт knowledge_e2e_public/private + алиасы).
    teardown — удаление обеих зон (finally).
    """
    from mcp_server.storage.qdrant_client import QdrantClient
    from mcp_server.storage.schema import COLLECTION_PRIVATE, COLLECTION_PUBLIC
    from qdrant_client import QdrantClient as QdrantSDKClient

    qc = QdrantClient(url=QDRANT_REST_URL)
    qc._client.close()
    qc._client = QdrantSDKClient(url=QDRANT_REST_URL, prefer_grpc=False)

    try:
        # Полный сброс обеих зон (идемпотентно)
        for coll in (COLLECTION_PUBLIC, COLLECTION_PRIVATE):
            try:
                qc.delete_alias(coll)
            except Exception:
                pass
            try:
                qc.delete_collection_named(coll)
            except Exception:
                pass
        qc.ensure_zonal_collections(force_recreate=True)
        yield qc
    finally:
        try:
            for coll in (COLLECTION_PUBLIC, COLLECTION_PRIVATE):
                try:
                    qc.delete_alias(coll)
                except Exception:
                    pass
                try:
                    qc.delete_collection_named(coll)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            qc.close()
        except Exception:
            pass


@pytest.fixture(scope="session")
def real_embedder():
    """Session-scoped _OllamaAdapter — реальный mxbai-embed-large."""
    from mcp_server.quality.embedder import OllamaEmbedder

    oe = OllamaEmbedder(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL)
    # Verify connectivity
    test_vec = oe.encode("test")
    assert test_vec and len(test_vec) == VECTOR_DIM, f"Ollama returned dim={len(test_vec) if test_vec else 0}"
    adapter = _OllamaAdapter(oe)
    return adapter


@pytest.fixture(scope="session")
def e2e_keys():
    """Тестовые ключи для auth-сценариев."""
    from mcp_server.config import settings

    original_read = list(settings.MCP_READ_KEYS)
    original_write = list(settings.MCP_WRITE_KEYS)
    settings.MCP_READ_KEYS = ["e2e-read-key"]
    settings.MCP_WRITE_KEYS = ["e2e-write-key"]
    yield
    settings.MCP_READ_KEYS = original_read
    settings.MCP_WRITE_KEYS = original_write


# ═══════════════════════════════════════════════════════════════
# Function-scoped fixtures (свежие per-test)
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_git_knowledge_root():
    """Temp knowledge-root с git (аудит включён)."""
    import git as gitpython

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    repo = gitpython.Repo.init(str(root))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "e2e@test.local")
        cw.set_value("user", "name", "E2E Test")
    (root / ".trash").mkdir()
    yield root
    tmp.cleanup()


# ═══════════════════════════════════════════════════════════════
# Ф0: Quality isolation — изолирует quality-пути от прода
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _e2e_quality_isolation(tmp_git_knowledge_root):
    """Изолирует quality-пути e2e от прод-данных.

    Без этого run_quality_scan пишет в прод /app/data/quality/issues.jsonl
    и сканирует прод /app/knowledge. Паттерн: set_store_dir как в unit-тестах.
    Фаза 3: изолирован и audit-стор (scan_completed/fp_rejection/restore —
    иначе e2e S26 и любой скан писали бы в прод audit.jsonl).
    """
    from mcp_server.quality import audit
    from mcp_server.quality.issues import set_store_dir

    quality_e2e_dir = str(tmp_git_knowledge_root / ".quality-e2e")
    set_store_dir(quality_e2e_dir)
    audit.set_store_dir(quality_e2e_dir)
    yield
    # Сброс: fallback на default path (прод /app/data/quality) вне e2e
    set_store_dir(None)
    audit.set_store_dir(None)


@pytest.fixture
def e2e_store(tmp_git_knowledge_root, monkeypatch):
    """MarkdownStore с git-аудитом (git ON, real .git)."""
    from mcp_server.config import settings
    from mcp_server.storage.markdown_store import MarkdownStore

    monkeypatch.setattr(settings, "KNOWLEDGE_ROOT", str(tmp_git_knowledge_root))
    store = MarkdownStore(knowledge_root=tmp_git_knowledge_root)
    return store


@pytest.fixture
def e2e_embedder_for_pipeline(real_embedder):
    """Прокидываем session-scoped real embedder в function scope."""
    return real_embedder


@pytest.fixture
async def e2e_pipeline(e2e_store, real_qdrant, e2e_embedder_for_pipeline):
    """IndexingPipeline с реальными компонентами."""
    from mcp_server.indexing.chunker import MarkdownChunker
    from mcp_server.indexing.pipeline import IndexingPipeline

    pl = IndexingPipeline(
        store=e2e_store,
        qdrant=real_qdrant,
        embedder=e2e_embedder_for_pipeline,
        chunker=MarkdownChunker(),
    )
    await pl.start()
    yield pl
    await pl.stop()


@pytest.fixture
def e2e_knowledge_index(e2e_store, tmp_git_knowledge_root, monkeypatch):
    """KnowledgeIndex с переопределённым KNOWLEDGE_ROOT."""
    from mcp_server.config import settings
    from mcp_server.indexing.knowledge_index import KnowledgeIndex

    monkeypatch.setattr(settings, "KNOWLEDGE_ROOT", str(tmp_git_knowledge_root))
    ki = KnowledgeIndex(store=e2e_store)
    # Override _root (which reads settings.KNOWLEDGE_ROOT at init)
    ki._root = Path(tmp_git_knowledge_root)
    return ki


@pytest.fixture
def e2e_app_state(e2e_store, real_qdrant, e2e_embedder_for_pipeline,
                   e2e_pipeline, e2e_knowledge_index):
    """Composite app.state — мимикрирует FastAPI request.app.state."""
    return SimpleNamespace(
        store=e2e_store,
        qdrant=real_qdrant,
        qdrant_client=real_qdrant,
        embedder=e2e_embedder_for_pipeline,
        pipeline=e2e_pipeline,
        knowledge_index=e2e_knowledge_index,
    )


# ═══════════════════════════════════════════════════════════════
# Фаза 12: HTTP-level E2E fixture (TestClient против real backends)
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
async def e2e_http_app(real_qdrant, real_embedder, e2e_store, e2e_pipeline,
                        e2e_knowledge_index, e2e_keys, tmp_git_knowledge_root):
    """Function-scoped httpx.AsyncClient with health + MCP + metrics.

    Фаза 12 (v1.3 fix): httpx.AsyncClient + ASGITransport вместо TestClient —
    устраняет event-loop mismatch между pytest-asyncio (pipeline worker loop)
    и HTTP-обработчиком. Весь стек теперь в ОДНОМ event loop (pytest-asyncio).

    Собирает минимальный FastAPI app (как в unit test_health.py):
    - health router (liveness + readiness)
    - AuthMiddleware (X-API-Key)
    - POST /mcp → handle_mcp_request (JSON-RPC 2.0)
    - GET /metrics → metrics_endpoint (Prometheus)

    app.state заполняется реальными компонентами (session-scoped Qdrant,
    Ollama embedder, per-test MarkdownStore/IndexingPipeline/KnowledgeIndex).
    Health-глобалы прокидываются через set_*() для /health deep checks.

    Изоляция: knowledge_e2e коллекция (через _patched_collection session fixture).
    Teardown: сброс health-глобалов в None.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from mcp_server.auth import AuthMiddleware
    from mcp_server.health import router as health_router
    from mcp_server.health import set_embedding_manager, set_pipeline, set_qdrant_client
    from mcp_server.mcp_handler import handle_mcp_request
    from mcp_server.metrics import metrics_endpoint
    from mcp_server.rate_limit import TokenBucketLimiter

    # Прокидываем реальные компоненты в health-глобалы
    set_qdrant_client(real_qdrant)
    set_embedding_manager(real_embedder)
    set_pipeline(e2e_pipeline)

    app = FastAPI()
    app.add_middleware(AuthMiddleware, fastapi_app=app)
    app.include_router(health_router)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        return await handle_mcp_request(request)

    @app.get("/metrics")
    async def metrics_route(request: Request):
        return await metrics_endpoint(request)

    # app.state для MCP handler, tools, metrics, auth
    app.state.qdrant = real_qdrant
    app.state.qdrant_client = real_qdrant
    app.state.embedder = real_embedder
    app.state.store = e2e_store
    app.state.pipeline = e2e_pipeline
    app.state.knowledge_index = e2e_knowledge_index
    # Task 1: data_version для TOC-кэш-инвалидации (как main.py:291) — без него
    # _build_toc кэш никогда не инвалидируется (getattr default 0 == 0).
    app.state.data_version = 0

    # Ф0: settings для quality-сканера (KNOWLEDGE_DIR → изолированное хранилище).
    # Без этого run_quality_scan берёт None → fallback на прод /app/knowledge.
    # ВАЖНО: (1) Path, не str — scanner.py:74-77 оборачивает в Path ТОЛЬКО None,
    # str-значение падает на .exists() ('str' object has no attribute 'exists').
    # (2) НЕ подменять settings целиком: тулы (analyze_content и др.) читают
    # остальные поля (ANALYZE_FRAGMENT_CHARS...) через app.state.settings →
    # наследуем ВСЕ поля базового settings, переопределяем только KNOWLEDGE_DIR.
    from mcp_server.config import settings as _base_settings

    _merged = _base_settings.model_dump()
    _merged["KNOWLEDGE_DIR"] = Path(tmp_git_knowledge_root)
    app.state.settings = SimpleNamespace(**_merged)

    # Rate limiter (для S10)
    app.state.rate_limiter_read = TokenBucketLimiter(
        refill_rate=100.0 / 60.0, burst_size=10,
    )
    app.state.rate_limiter_write = TokenBucketLimiter(
        refill_rate=20.0 / 60.0, burst_size=5,
    )
    app.state.rate_limiter = app.state.rate_limiter_read

    # 13.15: фоновый quality scan — lock/task/progress (для S9e health-during-scan)
    import asyncio

    from mcp_server.progress import ImportProgressTracker

    app.state.scan_lock = asyncio.Lock()
    app.state.scan_task = None
    # 13.27: персистентность как в main.py — scan_state.json в tmp рядом с
    # KNOWLEDGE_DIR; recovery-логика (auto-resume) тестируется в S25.
    app.state.scan_progress = ImportProgressTracker(
        persist_path=Path(tmp_git_knowledge_root).parent / "scan_state.json",
    )
    app.state.scan_id = None

    @app.get("/quality/scan/progress")
    async def scan_progress_route():
        """13.15: прогресс фонового quality scan (зеркало /imports/{id}/progress)."""
        entry = app.state.scan_progress.get(app.state.scan_id)
        if entry is None:
            return {"scan_id": app.state.scan_id, "status": "not_found", "messages": []}
        return entry

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Тесты используют e2e_http_app.app.state.* (cleanup, rate limiter)
        client.app = app
        yield client

    # Teardown: сброс health-глобалов
    set_qdrant_client(None)
    set_embedding_manager(None)
    set_pipeline(None)
