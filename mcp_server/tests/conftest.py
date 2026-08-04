"""Shared test fixtures for MCP Knowledge Server.

Provides mock components for unit-testing all 11 MCP tools, auth, and JSON-RPC handler
without requiring Qdrant, embedding models, or filesystem access.

Uses module-level env var overrides to prevent pydantic-settings from parsing
production .env List[str] fields as JSON (which fails for comma-separated values).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# ── Module-level env overrides (BEFORE any mcp_server import) ──
# Prevent pydantic-settings from failing to parse List[str] fields
# from .env as JSON. Override with valid default values before config.Settings() loads.
os.environ.setdefault("MCP_READ_KEYS", '["test-read-key"]')
os.environ.setdefault("MCP_WRITE_KEYS", '["test-write-key"]')
os.environ.setdefault("QDRANT_URL", "http://localhost:6334")
os.environ.setdefault("KNOWLEDGE_DIR", "/tmp/test-knowledge")
os.environ.setdefault("DLQ_DIR", "/tmp/test-dlq")
os.environ.setdefault("QUALITY_DIR", "/tmp/test-quality")

from mcp_server.models import (
    KnowledgeEntry,
    KnowledgeFrontmatter,
    VersionConflictError,
    WriteRequest,
)

# ── Mock factories ────────────────────────────────────────────


def _make_entry(
    knowledge_id: str = "ru-test-entry",
    domain: str = "engineering",
    subject: str = "testing",
) -> KnowledgeEntry:
    """Create a minimal KnowledgeEntry for tests."""
    fm = KnowledgeFrontmatter(
        knowledge_id=knowledge_id,
        domain=domain,
        subject=subject,
        project="test-project",
        tags=["test", "mock"],
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    return KnowledgeEntry(
        frontmatter=fm,
        content="# Test Entry\n\nTest content.",
    )


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def sample_entry() -> KnowledgeEntry:
    """A minimal valid KnowledgeEntry."""
    return _make_entry()


@pytest.fixture
def mock_qdrant() -> MagicMock:
    """Mock QdrantClient — search, scroll, upsert, delete."""
    client = MagicMock()

    # search() returns scored points
    def _fake_search(
        vector=None,
        top_k: int = 5,
        filters: dict | None = None,
        score_threshold: float = 0.0,
    ):
        point = MagicMock()
        point.id = 1
        point.score = 0.95
        point.payload = {
            "knowledge_id": "ru-test-entry",
            "chunk_id": "chunk-1",
            "content": "Test content",
            "section_header": "# Test Entry",
            "domain": "engineering",
            "subject": "testing",
            "tags": ["test", "mock"],
        }
        return [point]

    client.search = _fake_search

    # search_by_tags()
    def _fake_search_by_tags(tags, match_all: bool = True, limit: int = 500):
        point = MagicMock()
        point.id = 2
        point.score = 1.0
        point.payload = {
            "knowledge_id": "ru-test-entry",
            "chunk_id": "chunk-1",
            "content": "Tagged content",
            "domain": "engineering",
            "subject": "testing",
            "tags": tags,
        }
        return [point]

    client.search_by_tags = _fake_search_by_tags

    # scroll_unique_values()
    def _fake_scroll_unique_values(field, domain_filter=None, subject_filter=None,
                                   cursor=None, limit=100, max_scan=100):
        if field == "domain":
            return (["engineering", "devops"], None, 2)
        elif field == "subject":
            return (["testing", "python"], None, 2)
        elif field == "project":
            return (["test-project"], None, 1)
        return ([], None, 0)

    client.scroll_unique_values = _fake_scroll_unique_values

    # collection_info()
    client.collection_info = MagicMock(return_value={
        "name": "knowledge",
        "points_count": 42,
        "vectors_count": 42,
    })

    # delete_by_knowledge_id()
    client.delete_by_knowledge_id = MagicMock()

    # get_all_knowledge_ids()
    client.get_all_knowledge_ids = MagicMock(return_value={"ru-test-entry"})

    # close()
    client.close = MagicMock()

    return client


@pytest.fixture
def mock_embedder() -> MagicMock:
    """Mock EmbeddingManager — returns a fixed vector."""
    mgr = MagicMock()
    mgr.encode = MagicMock(return_value=[0.1] * 1024)
    mgr.is_ready = True
    mgr.backend_name = "mock"
    return mgr


@pytest.fixture
def mock_store(sample_entry: KnowledgeEntry) -> MagicMock:
    """Mock MarkdownStore — CRUD operations."""
    store = MagicMock()

    async def _read(knowledge_id):
        if knowledge_id == sample_entry.frontmatter.knowledge_id:
            return sample_entry
        return None

    store.read = _read

    async def _write(req: WriteRequest) -> KnowledgeEntry:
        return sample_entry

    store.write = _write

    async def _update(knowledge_id, content=None, metadata=None, expected_version=None):
        # F2: support expected_version + VersionConflictError
        if expected_version is not None and expected_version != sample_entry.frontmatter.version:
            raise VersionConflictError(knowledge_id, expected_version, sample_entry.frontmatter.version)
        if knowledge_id == sample_entry.frontmatter.knowledge_id:
            return sample_entry
        return None

    store.update = _update

    async def _delete(knowledge_id) -> bool:
        return knowledge_id == sample_entry.frontmatter.knowledge_id

    store.delete = _delete

    return store


@pytest.fixture
def mock_pipeline() -> MagicMock:
    """Mock IndexingPipeline."""
    pipeline = MagicMock()

    async def _enqueue(entry, wait_for_index=False):
        pass  # no-op

    pipeline.enqueue = _enqueue

    async def _reindex_all():
        return {"total_docs": 1, "total_chunks": 3, "failed": 0}

    pipeline.reindex_all = _reindex_all

    async def _reindex_blue_green():
        return {
            "active": "knowledge_v1",
            "target": "knowledge_v2",
            "alias_swapped": True,
            "reindex_result": {"total_docs": 1, "total_chunks": 3, "failed": 0},
            "elapsed_sec": 1.5,
        }

    pipeline.reindex_blue_green = _reindex_blue_green

    return pipeline


@pytest.fixture
def mock_knowledge_index() -> MagicMock:
    """Mock KnowledgeIndex."""
    kidx = MagicMock()

    kidx.get_map = MagicMock(return_value={
        "sections": {
            "engineering": {
                "total_files": 1,
                "tags": ["test", "mock"],
                "files": [{"knowledge_id": "ru-test-entry"}],
            }
        },
        "total_entries": 1,
    })

    kidx.update_section = MagicMock()
    kidx.rebuild_all = MagicMock(return_value={
        "sections": {"engineering": {}},
        "root": {"total_entries": 3},
    })

    return kidx


@pytest.fixture
def app_state(
    mock_qdrant: MagicMock,
    mock_embedder: MagicMock,
    mock_store: MagicMock,
    mock_pipeline: MagicMock,
    mock_knowledge_index: MagicMock,
) -> MagicMock:
    """Composite app.state mock — mimics FastAPI request.app.state."""
    state = MagicMock()
    state.qdrant = mock_qdrant
    state.embedder = mock_embedder
    state.store = mock_store
    state.pipeline = mock_pipeline
    state.knowledge_index = mock_knowledge_index
    return state
