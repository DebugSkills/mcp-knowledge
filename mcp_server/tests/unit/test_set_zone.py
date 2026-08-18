"""W4.7: unit-тесты set_zone (перекладывание записи между зонами)."""

from unittest.mock import MagicMock


from mcp_server.models import KnowledgeFrontmatter
from mcp_server.storage.schema import COLLECTION_PRIVATE, COLLECTION_PUBLIC


class FakeQdrant:
    """Зоно-различающий фейк (как test_subscriber_zone)."""

    def __init__(self, points_by_collection):
        self.points = points_by_collection

    def _payloads(self, collection_name):
        return list(self.points.get(collection_name, []))

    def scroll(self, collection_name=None, scroll_filter=None, limit=100, offset=None,
               with_payload=True, with_vectors=False):
        pts = self._payloads(collection_name)
        if scroll_filter is not None:
            must = getattr(scroll_filter, "must", None) or []
            where = {}
            for cond in must:
                if hasattr(cond, "key") and hasattr(cond, "match"):
                    where[cond.key] = cond.match.value
            pts = [p for p in pts if self._filter(p, where)]
        from types import SimpleNamespace

        return [SimpleNamespace(payload=p) for p in pts[:limit]], None

    def _filter(self, point, where):
        return all(point.get(k) == v for k, v in where.items())

    def delete_by_knowledge_id(self, knowledge_id, collection_name=None):
        self.points[collection_name] = [
            p for p in self.points.get(collection_name, [])
            if p.get("knowledge_id") != knowledge_id
        ]
        return True


def _make_app_state(store, qdrant):
    app_state = MagicMock()
    app_state.qdrant_client = None  # _get_qdrant предпочитает qdrant_client
    app_state.qdrant = qdrant
    app_state.store = store
    app_state.pipeline = MagicMock()

    async def fake_enqueue(entry, wait_for_index=False):
        return None

    app_state.pipeline.enqueue = fake_enqueue
    app_state.data_version = 0
    return app_state


def _make_store(entries: dict) -> MagicMock:
    """Фейк store: entries[kid] -> KnowledgeEntry; update пишет в entries[].fm.zone."""
    store = MagicMock()

    async def read(kid):
        return entries.get(kid)

    async def update(kid, metadata=None):
        if kid in entries:
            if metadata and "zone" in metadata:
                entries[kid].frontmatter.zone = metadata["zone"]
        return entries.get(kid)

    async def write_entry(entry):
        return None

    async def flush(message):
        return None

    store.read = read
    store.update = update
    store.write_entry = write_entry
    store.flush = flush
    return store


def _entry(kid: str, zone: str, parent: str | None = None) -> MagicMock:
    e = MagicMock()
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        title=kid,
        domain="test",
        subject="test",
        zone=zone,
    )
    e.frontmatter = fm
    e.content = f"# {kid}\n\nКонтент {kid}."
    e.file_path = f"engineering/{kid}.md"
    return e


# ── базовые кейсы ────────────────────────────────────────────


async def test_set_zone_private_to_public_moves_collection():
    from mcp_server.tools.admin import set_zone

    entry = _entry("doc-1", "private")
    entries = {"doc-1": entry}
    qdrant = FakeQdrant({
        COLLECTION_PRIVATE: [{"knowledge_id": "doc-1", "zone": "private"}],
        COLLECTION_PUBLIC: [],
    })
    app_state = _make_app_state(_make_store(entries), qdrant)

    result = await set_zone({"knowledge_id": "doc-1", "zone": "public", "reason": "test"}, app_state)

    assert "error" not in result, result
    assert result.get("zone") == "public"
    assert entry.frontmatter.zone == "public"
    # старая зона очищена
    assert qdrant.points[COLLECTION_PRIVATE] == []
    # data_version инкрементирована (TOC-кэш)
    assert app_state.data_version == 1


async def test_set_zone_invalid_zone():
    from mcp_server.tools.admin import set_zone

    entry = _entry("doc-1", "private")
    entries = {"doc-1": entry}
    app_state = _make_app_state(_make_store(entries), FakeQdrant({}))
    result = await set_zone({"knowledge_id": "doc-1", "zone": "invalid"}, app_state)
    assert "error" in result


async def test_set_zone_missing_entry():
    from mcp_server.tools.admin import set_zone

    app_state = _make_app_state(_make_store({}), FakeQdrant({}))
    result = await set_zone({"knowledge_id": "no-such", "zone": "public"}, app_state)
    assert "error" in result


async def test_set_zone_idempotent_same_zone():
    from mcp_server.tools.admin import set_zone

    entry = _entry("doc-1", "public")
    entries = {"doc-1": entry}
    qdrant = FakeQdrant({
        COLLECTION_PUBLIC: [{"knowledge_id": "doc-1", "zone": "public"}],
        COLLECTION_PRIVATE: [],
    })
    app_state = _make_app_state(_make_store(entries), qdrant)
    result = await set_zone({"knowledge_id": "doc-1", "zone": "public"}, app_state)
    assert "error" not in result
    # no-op: ничего не перекладывалось
    assert qdrant.points[COLLECTION_PUBLIC] == [{"knowledge_id": "doc-1", "zone": "public"}]


# ── каскад секций ────────────────────────────────────────────


async def test_set_zone_cascades_child_sections():
    from mcp_server.tools.admin import set_zone

    root = _entry("book-1", "private")
    child = _entry("book-1-sec1", "private", parent="book-1")
    entries = {"book-1": root, "book-1-sec1": child}
    qdrant = FakeQdrant({
        COLLECTION_PRIVATE: [
            {"knowledge_id": "book-1", "zone": "private"},
            {"knowledge_id": "book-1-sec1", "zone": "private", "parent_knowledge_id": "book-1"},
        ],
        COLLECTION_PUBLIC: [],
    })
    app_state = _make_app_state(_make_store(entries), qdrant)

    result = await set_zone({"knowledge_id": "book-1", "zone": "public"}, app_state)

    assert "error" not in result, result
    assert child.frontmatter.zone == "public"
    assert qdrant.points[COLLECTION_PRIVATE] == []
    assert qdrant.points[COLLECTION_PUBLIC] == []
    assert app_state.data_version == 1


# ── promo-чеклист ────────────────────────────────────────────


async def test_set_zone_promo_checklist_warns_on_sensitive_path():
    from mcp_server.tools.admin import set_zone

    entry = _entry("doc-1", "private")
    entry.file_path = "PARTNERS/partner-1.md"  # sensitive-путь
    entries = {"doc-1": entry}
    qdrant = FakeQdrant({
        COLLECTION_PRIVATE: [{"knowledge_id": "doc-1", "zone": "private"}],
        COLLECTION_PUBLIC: [],
    })
    app_state = _make_app_state(_make_store(entries), qdrant)

    result = await set_zone({"knowledge_id": "doc-1", "zone": "public"}, app_state)

    assert "error" not in result
    promo = result.get("promo", {})
    assert promo.get("ready") is False
    assert promo.get("warnings")
