"""W4.8: integration — set_zone перекладывает запись между коллекциями.

Реальный MarkdownStore в tmp + зоно-различающий Qdrant-фейк:
- запись private → set_zone public → frontmatter обновлён, точка в public-коллекции;
- каскад секций книги;
- promo-чеклист (sensitive-путь → warnings).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp_server.models import WriteRequest
from mcp_server.storage.schema import COLLECTION_PRIVATE, COLLECTION_PUBLIC


@pytest.fixture
def tmp_root():
    import git

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "knowledge"
        root.mkdir(parents=True)
        git.Repo.init(str(root))
        (root / ".trash").mkdir()
        yield root


@pytest.fixture
def store(tmp_root):
    from mcp_server.config import settings
    from mcp_server.storage.markdown_store import MarkdownStore

    original = settings.KNOWLEDGE_ROOT
    settings.KNOWLEDGE_ROOT = str(tmp_root)
    s = MarkdownStore(knowledge_root=tmp_root)
    s._repo = None
    yield s
    settings.KNOWLEDGE_ROOT = original


class FakeQdrant:
    """Две зоны; scroll/delete по collection_name."""

    def __init__(self):
        self.points = {COLLECTION_PUBLIC: [], COLLECTION_PRIVATE: []}

    def scroll(self, collection_name=None, scroll_filter=None, limit=100, offset=None,
               with_payload=True, with_vectors=False):
        pts = list(self.points.get(collection_name, []))
        if scroll_filter is not None:
            must = getattr(scroll_filter, "must", None) or []
            where = {}
            for cond in must:
                if hasattr(cond, "key") and hasattr(cond, "match"):
                    where[cond.key] = cond.match.value
            pts = [p for p in pts if all(p.get(k) == v for k, v in where.items())]
        return [SimpleNamespace(payload=p) for p in pts[:limit]], None

    def delete_by_knowledge_id(self, knowledge_id, collection_name=None):
        self.points[collection_name] = [
            p for p in self.points.get(collection_name, [])
            if p.get("knowledge_id") != knowledge_id
        ]
        return True


def _make_app(store, qdrant):
    from unittest.mock import MagicMock

    app_state = MagicMock()
    app_state.qdrant_client = None
    app_state.qdrant = qdrant
    app_state.store = store
    app_state.pipeline = MagicMock()

    async def fake_enqueue(entry, wait_for_index=False):
        return None

    app_state.pipeline.enqueue = fake_enqueue
    app_state.data_version = 0
    return app_state


async def _write(store, kid, zone, content, domain="e2e-zone"):
    req = WriteRequest(
        content=content,
        domain=domain,
        subject="migration",
        knowledge_id=kid,
        zone=zone,
    )
    return await store.write(req)


async def test_set_zone_moves_entry_between_collections(store):
    from mcp_server.tools.admin import set_zone

    await _write(store, "zone-doc-1", "private", "# zone-doc-1\n\nКонтент.\n")
    qdrant = FakeQdrant()
    qdrant.points[COLLECTION_PRIVATE] = [{"knowledge_id": "zone-doc-1", "zone": "private"}]
    app_state = _make_app(store, qdrant)

    result = await set_zone(
        {"knowledge_id": "zone-doc-1", "zone": "public", "reason": "integration"}, app_state
    )

    assert "error" not in result, result
    assert result["zone"] == "public"
    assert result["sections_moved"] == 0
    # SSOT: frontmatter обновлён
    reread = await store.read("zone-doc-1")
    assert reread.frontmatter.zone == "public"
    # Qdrant: старая зона очищена
    assert qdrant.points[COLLECTION_PRIVATE] == []
    # data_version инкрементирована
    assert app_state.data_version == 1


async def test_set_zone_cascades_book_sections(store):
    from mcp_server.tools.admin import set_zone

    await _write(store, "zone-book", "private", "# zone-book\n\nКнига.\n")
    await _write(store, "zone-book-sec1", "private", "# sec1\n\nСекция.\n")
    # привяжем секцию к книге
    await store.update("zone-book-sec1", metadata={"parent_knowledge_id": "zone-book"})

    qdrant = FakeQdrant()
    qdrant.points[COLLECTION_PRIVATE] = [
        {"knowledge_id": "zone-book", "zone": "private"},
        {"knowledge_id": "zone-book-sec1", "zone": "private", "parent_knowledge_id": "zone-book"},
    ]
    app_state = _make_app(store, qdrant)

    result = await set_zone({"knowledge_id": "zone-book", "zone": "public"}, app_state)

    assert "error" not in result, result
    assert result["sections_moved"] == 1
    child_after = await store.read("zone-book-sec1")
    assert child_after.frontmatter.zone == "public"
    assert qdrant.points[COLLECTION_PRIVATE] == []
    assert app_state.data_version == 1


async def test_set_zone_promo_checklist_flags_sensitive_path(store):
    from mcp_server.tools.admin import set_zone

    # реальный SSOT-путь: domain="PARTNERS" → file_path содержит "partners/"
    await _write(store, "zone-doc-2", "private", "# zone-doc-2\n\nКонтент.\n",
                 domain="PARTNERS")
    entry = await store.read("zone-doc-2")
    assert "partners/" in str(entry.file_path).lower()

    qdrant = FakeQdrant()
    qdrant.points[COLLECTION_PRIVATE] = [{"knowledge_id": "zone-doc-2", "zone": "private"}]
    app_state = _make_app(store, qdrant)

    result = await set_zone({"knowledge_id": "zone-doc-2", "zone": "public"}, app_state)

    assert "error" not in result, result
    promo = result.get("promo", {})
    assert promo.get("ready") is False
    assert any("partners/" in w for w in promo.get("warnings", []))


async def test_set_zone_roundtrip_public_to_private(store):
    from mcp_server.tools.admin import set_zone

    await _write(store, "zone-doc-3", "public", "# zone-doc-3\n\nКонтент.\n")
    qdrant = FakeQdrant()
    qdrant.points[COLLECTION_PUBLIC] = [{"knowledge_id": "zone-doc-3", "zone": "public"}]
    app_state = _make_app(store, qdrant)

    result = await set_zone({"knowledge_id": "zone-doc-3", "zone": "private"}, app_state)

    assert "error" not in result, result
    reread = await store.read("zone-doc-3")
    assert reread.frontmatter.zone == "private"
    assert qdrant.points[COLLECTION_PUBLIC] == []
