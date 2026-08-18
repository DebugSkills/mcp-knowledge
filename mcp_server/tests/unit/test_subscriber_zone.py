"""W3.18: subscriber-изоляция — зоно-различающие тесты.

Проверяет инвариант №4: subscriber видит ТОЛЬКО public-зону.
Зоно-различающий фейк Qdrant (conftest-фейки игнорируют collection_name).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp_server.storage.schema import COLLECTION_PRIVATE, COLLECTION_PUBLIC


class FakeQdrant:
    """Qdrant-фейк, различающий коллекции по collection_name.

    points: dict[collection_name, list[dict]] — payload-точки.
    Возвращает точки-объекты с .payload/.score (как реальный wrapper).
    """

    def __init__(self, points_by_collection):
        self.points = points_by_collection
        self.search_calls = []

    def _points(self, collection_name):
        return [
            SimpleNamespace(payload=p, score=0.9 - i * 0.01, id=p.get("knowledge_id", "x"))
            for i, p in enumerate(self.points.get(collection_name, []))
        ]

    def _filter(self, point, where):
        for k, v in where.items():
            if point.get(k) != v:
                return False
        return True

    # ── wrapper API (как вызывают тулы) ──────────────────────
    def search(self, vector=None, top_k=10, filters=None, score_threshold=None,
               exclude_content_types=None, exclude_statuses=None, offset=0,
               collection_name=None):
        self.search_calls.append(collection_name)
        return self._points(collection_name)[offset: offset + top_k]

    def search_by_tags(self, tags=None, match_all=True, limit=500,
                       collection_name=None):
        self.search_calls.append(collection_name)
        out = []
        for p in self.points.get(collection_name, []):
            if match_all and set(tags) <= set(p.get("tags", [])) or not match_all and set(tags) & set(p.get("tags", [])):
                out.append(p)
        return [
            SimpleNamespace(payload=p, score=0.8, id=p.get("knowledge_id", "x"))
            for p in out[:limit]
        ]

    def scroll(self, collection_name=None, scroll_filter=None, limit=100, offset=None,
               with_payload=True, with_vectors=False):
        pts = list(self.points.get(collection_name, []))
        if scroll_filter is not None:
            must = getattr(scroll_filter, "must", None) or []
            where = {}
            for cond in must:
                if hasattr(cond, "key") and hasattr(cond, "match"):
                    where[cond.key] = cond.match.value
            pts = [p for p in pts if self._filter(p, where)]
        return self._points(collection_name)[:limit], None

    def scroll_unique_values(self, collection_name=None, field=None, limit=100):
        vals = {p.get(field) for p in self.points.get(collection_name, []) if p.get(field)}
        return sorted(vals), None

    def get_all_knowledge_ids(self, collection_name=None):
        return {p["knowledge_id"] for p in self.points.get(collection_name, [])}


def _make_app_state(points_by_collection, zone="private"):
    from mcp_server.models import KnowledgeFrontmatter

    app_state = MagicMock()
    app_state.qdrant = FakeQdrant(points_by_collection)
    app_state.store = MagicMock()

    async def fake_read(kid):
        entry = MagicMock()
        fm = KnowledgeFrontmatter(
            knowledge_id=kid,
            title=kid,
            domain="test",
            subject="test",
            zone=zone,
        )
        entry.frontmatter = fm
        entry.content = f"# {kid}\n\nКонтент записи {kid}."
        return entry

    app_state.store.read = fake_read
    app_state.embedder = MagicMock()
    app_state.pipeline = MagicMock()
    app_state.knowledge_index = MagicMock()
    return app_state


def _subscriber_auth():
    return MagicMock(key_level="subscriber", zone="public", scope=set())


def _read_auth():
    return MagicMock(key_level="read", zone="both", scope=set())


PUBLIC_POINTS = [
    {"knowledge_id": "pub-1", "domain": "test", "subject": "a", "tags": ["x"], "zone": "public", "content": "Тестовый контент публичной записи один."},
    {"knowledge_id": "pub-2", "domain": "test", "subject": "b", "tags": ["x"], "zone": "public", "content": "Тестовый контент публичной записи два."},
]
PRIVATE_POINTS = [
    {"knowledge_id": "priv-1", "domain": "secret", "subject": "s", "tags": ["y"], "zone": "private", "content": "Секретный контент приватной записи один."},
    {"knowledge_id": "priv-2", "domain": "secret", "subject": "s", "tags": ["y"], "zone": "private", "content": "Секретный контент приватной записи два."},
]


@pytest.fixture
def two_zone_app():
    return _make_app_state({COLLECTION_PUBLIC: PUBLIC_POINTS, COLLECTION_PRIVATE: PRIVATE_POINTS})


@pytest.fixture
def private_only_app():
    return _make_app_state({COLLECTION_PUBLIC: [], COLLECTION_PRIVATE: PRIVATE_POINTS})


# ── 1. subscriber search: только public ─────────────────────


async def test_subscriber_search_public_only(two_zone_app):
    from mcp_server.tools.search import search_knowledge

    params = {"query": "test", "_auth": _subscriber_auth()}
    result = await search_knowledge(params, two_zone_app)
    ids = [r["knowledge_id"] for r in result.get("results", result.get("matches", []))]
    assert "priv-1" not in ids and "priv-2" not in ids
    assert "pub-1" in ids


async def test_subscriber_search_by_tags_public_only(two_zone_app):
    from mcp_server.tools.search import search_by_tags

    params = {"tags": ["x"], "_auth": _subscriber_auth()}
    result = await search_by_tags(params, two_zone_app)
    ids = [r["knowledge_id"] for r in result.get("results", result.get("matches", []))]
    assert "priv-1" not in ids and "priv-2" not in ids
    assert "pub-1" in ids


# ── 2. get_entry на private → not found (fail-closed) ────────


async def test_subscriber_get_entry_private_not_found(private_only_app):
    from mcp_server.tools.read import get_entry

    params = {"knowledge_id": "priv-1", "_auth": _subscriber_auth()}
    result = await get_entry(params, private_only_app)
    assert "error" in result
    assert "not found" in result["error"].lower()


async def test_read_get_entry_private_ok(private_only_app):
    """Команда (read) private-запись видит — регрессия."""
    from mcp_server.tools.read import get_entry

    params = {"knowledge_id": "priv-1", "_auth": _read_auth()}
    result = await get_entry(params, private_only_app)
    assert "error" not in result


# ── 3. find_fragment на private-книгу → Collection not found ─


async def test_subscriber_find_fragment_private_book_not_found(private_only_app):
    from mcp_server.tools.fragments import find_fragment

    params = {"collection_id": "book-priv", "query": "x", "_auth": _subscriber_auth()}
    result = await find_fragment(params, private_only_app)
    assert "error" in result
    assert "Collection not found" in result["error"]


# ── 4. tools/list для subscriber = 10 тулов ──────────────────


def test_tools_list_subscriber_ten_tools():
    import asyncio

    from mcp_server.auth import SUBSCRIBER_TOOLS
    from mcp_server.mcp_handler import _handle_tools_list

    request = MagicMock()
    request.state.auth = _subscriber_auth()
    result = asyncio.run(_handle_tools_list({}, "req-1", request))
    tools = result.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    assert names == SUBSCRIBER_TOOLS
    assert len(names) == 10


# ── 5. resources → 403 для subscriber ────────────────────────


def test_subscriber_resources_list_forbidden():
    import asyncio

    from mcp_server.mcp_handler import _handle_resources_list

    request = MagicMock()
    request.state.auth = _subscriber_auth()
    result = asyncio.run(_handle_resources_list({}, "req-1", request))
    assert "error" in result
    assert "Forbidden" in result["error"].get("message", "")


# ── 6. инъекция _auth в тул ──────────────────────────────────


def test_auth_injection_into_tool_call():
    import asyncio

    from mcp_server.mcp_handler import TOOL_HANDLERS, _handle_tools_call

    captured = {}

    async def fake_search(params, app_state):
        captured["auth"] = params.get("_auth")
        return {"results": []}

    TOOL_HANDLERS["search_knowledge"] = fake_search
    try:
        request = MagicMock()
        request.state.auth = _subscriber_auth()
        request.app = MagicMock()
        asyncio.run(_handle_tools_call({"name": "search_knowledge", "arguments": {"query": "x"}}, "req-1", request))
        assert captured.get("auth") is not None
        assert captured["auth"].key_level == "subscriber"
    finally:
        TOOL_HANDLERS.pop("search_knowledge", None)
