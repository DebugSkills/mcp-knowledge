"""Ф2.0 «Zone-scope токенов MCP»: проводка политики в тулы (C1′).

- read.py: видимость entry (:173) и TOC (:180-182) → zone-in-scope вместо
  is_admin — сервисный ключ (read+both+explicit) видит private, contributor — нет;
- availability.auth_zones ≡ auth_zone.zones_for_auth (R1 parity);
- write-маппинг не сломан: read-ключ (даже zone_explicit) → write/import-тул = 403;
- mcp_handler: ZoneAccessError → MCP_AUTH_FAILED (-32002), не -32603,
  без ERROR-traceback в логе (P1-2/R3).

Носители — реальные типы (AuthInfo, KnowledgeFrontmatter), Qdrant — фейк,
различающий коллекции (паттерн test_subscriber_zone).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from mcp_server.auth import AuthInfo, check_tool_permission
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.schema import COLLECTION_PRIVATE, COLLECTION_PUBLIC
from mcp_server.tools.auth_zone import zones_for_auth
from mcp_server.tools.availability import auth_zones

PRIV_ENTRY = "f20-priv-doc"
PRIV_SECTION = "f20-priv-sec-1"


def _auth(level: str, zone: str = "both", explicit: bool = False) -> AuthInfo:
    return AuthInfo(authenticated=True, key_level=level, zone=zone, zone_explicit=explicit)


def _entry(kid: str, zone: str, content_type: str | None = None) -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid, title=kid, domain="f20", subject="zones",
        zone=zone, **({"content_type": content_type} if content_type else {}),
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n\nКонтент {kid}.")


class FakeQdrantRaw:
    """Raw-Qdrant фейк: scroll по parent_knowledge_id (контракт _build_toc)."""

    def __init__(self, points_by_collection: dict[str, list[dict]]):
        self.points = points_by_collection
        self.scroll_collections: list[str] = []

    def scroll(self, scroll_filter=None, limit=1000, offset=None,
               with_payload=True, with_vectors=False, collection_name=None):
        self.scroll_collections.append(collection_name)
        must = getattr(scroll_filter, "must", None) or []
        where = {}
        for cond in must:
            if hasattr(cond, "key") and hasattr(cond, "match"):
                where[cond.key] = cond.match.value
        pts = [
            SimpleNamespace(payload=p, id=p.get("knowledge_id", "x"))
            for p in self.points.get(collection_name, [])
            if all(p.get(k) == v for k, v in where.items())
        ]
        return pts[:limit], None


def _app_state(entry: KnowledgeEntry, toc_points: dict[str, list[dict]] | None = None):
    async def fake_read(kid):
        return entry if kid == entry.frontmatter.knowledge_id else None

    state = MagicMock()
    state.store.read = fake_read
    state.data_version = 1
    state.qdrant_client = FakeQdrantRaw(toc_points or {})
    return state


# ── read.py :173 — видимость entry по скоупу ──────────────────


async def test_service_key_reads_private_entry():
    """Сервисный ключ (read+both+explicit) ВИДИТ private-запись (positive control:
    совпадение knowledge_id в ответе, не только отсутствие ошибки)."""
    from mcp_server.tools.read import get_entry

    app_state = _app_state(_entry(PRIV_ENTRY, "private"))
    result = await get_entry(
        {"knowledge_id": PRIV_ENTRY, "_auth": _auth("read", "both", explicit=True)},
        app_state,
    )
    assert result.get("knowledge_id") == PRIV_ENTRY
    assert result.get("zone") == "private"
    assert "error" not in result


async def test_admin_reads_private_entry():
    from mcp_server.tools.read import get_entry

    app_state = _app_state(_entry(PRIV_ENTRY, "private"))
    result = await get_entry(
        {"knowledge_id": PRIV_ENTRY, "_auth": _auth("write")}, app_state,
    )
    assert result.get("knowledge_id") == PRIV_ENTRY


async def test_legacy_read_private_entry_not_found():
    """Legacy read (без флага) → private не видит (fail-closed, прежнее поведение)."""
    from mcp_server.tools.read import get_entry

    app_state = _app_state(_entry(PRIV_ENTRY, "private"))
    result = await get_entry(
        {"knowledge_id": PRIV_ENTRY, "_auth": _auth("read", "both", explicit=False)},
        app_state,
    )
    assert "error" in result and "not found" in result["error"].lower()


async def test_contributors_do_not_see_private_entry():
    """Contributor-уровни (import/editor) — не видят private (public-only)."""
    from mcp_server.tools.read import get_entry

    app_state = _app_state(_entry(PRIV_ENTRY, "private"))
    for level in ("import", "editor"):
        result = await get_entry(
            {"knowledge_id": PRIV_ENTRY, "_auth": _auth(level)}, app_state,
        )
        assert "error" in result, f"{level} не должен видеть private"


async def test_private_scope_key_reads_public_entry():
    """read+private explicit: public-запись доступна только если public ∈ скоуп —
    strict-private скоуп public НЕ видит (fail-closed ∩)."""
    from mcp_server.tools.read import get_entry

    app_state = _app_state(_entry("f20-pub-doc", "public"))
    strict = await get_entry(
        {"knowledge_id": "f20-pub-doc", "_auth": _auth("read", "private", explicit=True)},
        app_state,
    )
    assert "error" in strict, "strict-private скоуп не должен видеть public"


# ── read.py :180-182 — TOC коллекции по скоупу ────────────────


async def test_service_key_gets_private_toc():
    """TOC private-коллекции: сервисный ключ получает секции из PRIVATE-коллекции
    (positive control: knowledge_id секции в children)."""
    from mcp_server.tools.read import get_entry

    entry = _entry("f20-priv-book", "private", content_type="collection")
    toc = {
        COLLECTION_PRIVATE: [
            {"knowledge_id": PRIV_SECTION, "chunk_index": 0,
             "section_header": "Секретная секция", "sequence_number": 1,
             "parent_knowledge_id": "f20-priv-book"},
        ],
        COLLECTION_PUBLIC: [],
    }
    app_state = _app_state(entry, toc)
    result = await get_entry(
        {"knowledge_id": "f20-priv-book", "_auth": _auth("read", "both", explicit=True)},
        app_state,
    )
    kids = [c["knowledge_id"] for c in result.get("children", [])]
    assert PRIV_SECTION in kids, "сервисный ключ должен видеть private-TOC"
    assert app_state.qdrant_client.scroll_collections == [COLLECTION_PRIVATE]


async def test_legacy_read_toc_falls_back_to_public():
    """Legacy read на private-коллекции → not found (не доходит до TOC);
    на public-коллекции TOC строится по public-зоне."""
    from mcp_server.tools.read import get_entry

    entry = _entry("f20-pub-book", "public", content_type="collection")
    toc = {
        COLLECTION_PUBLIC: [
            {"knowledge_id": "f20-pub-sec", "chunk_index": 0,
             "section_header": "Публичная секция", "sequence_number": 1,
             "parent_knowledge_id": "f20-pub-book"},
        ],
    }
    app_state = _app_state(entry, toc)
    result = await get_entry(
        {"knowledge_id": "f20-pub-book", "_auth": _auth("read")}, app_state,
    )
    kids = [c["knowledge_id"] for c in result.get("children", [])]
    assert "f20-pub-sec" in kids
    assert app_state.qdrant_client.scroll_collections == [COLLECTION_PUBLIC]


# ── R1: availability ≡ auth_zone (parity) ─────────────────────


def test_availability_parity_with_auth_zone():
    """R1: availability.auth_zones делегирует zones_for_auth — дубля политики нет."""
    carriers = [
        None, {},
        {"level": "write"}, {"level": "subscriber"}, {"level": "read"},
        {"level": "read", "zone": "both", "zone_explicit": True},
        {"level": "read", "zone": "private", "zone_explicit": True},
        {"level": "read", "zone": "public", "zone_explicit": True},
        {"level": "read", "zone": "private"},
        {"level": "editor", "zone": "both", "zone_explicit": True},
        {"level": "import", "zone": "private", "zone_explicit": True},
        _auth("read", "both", explicit=True), _auth("write"), _auth("subscriber"),
    ]
    for auth in carriers:
        assert auth_zones(auth) == zones_for_auth(auth), f"drift on {auth!r}"


def test_availability_source_zone_gate_uses_scope():
    """private Source-метаданные (citation-путь) — по скоупу ключа."""
    from mcp_server.tools.availability import source_accessible

    private_source = {"zone": "private", "status": "published"}
    service = _auth("read", "both", explicit=True)
    legacy = _auth("read")
    assert source_accessible(private_source, service) is True
    assert source_accessible(private_source, legacy) is False


# ── Write-маппинг не сломан ───────────────────────────────────


def test_read_key_cannot_use_write_tools():
    """read-ключ (даже zone_explicit) → write/import-тул = 403 (check_tool_permission)."""
    service = _auth("read", "both", explicit=True)
    for tool in ("write_knowledge", "import_content", "update_entry", "delete_entry"):
        with pytest.raises(HTTPException) as exc_info:
            check_tool_permission(service, tool)
        assert exc_info.value.status_code == 403, tool


def test_read_key_can_use_read_tools():
    service = _auth("read", "both", explicit=True)
    check_tool_permission(service, "get_entry")  # не бросает
    check_tool_permission(service, "search_knowledge")


# ── mcp_handler: ZoneAccessError → -32002, не -32603 (P1-2/R3) ──


def _make_request(auth: AuthInfo, app_state) -> MagicMock:
    req = MagicMock(spec=Request)
    req.state.auth = auth
    req.app.state = app_state
    return req


async def test_zone_access_error_converted_to_auth_failed(caplog):
    """public-only ключ + zone="private" → JSON-RPC MCP_AUTH_FAILED (-32002),
    НЕ -32603; без ERROR-записей в логе (нет traceback-шума в sink)."""
    from mcp_server.mcp_handler import (
        MCP_AUTH_FAILED,
        JSONRPC_INTERNAL_ERROR,
        _handle_tools_call,
    )

    req = _make_request(_auth("read"), MagicMock())
    with caplog.at_level(logging.DEBUG, logger="mcp_knowledge.mcp"):
        resp = await _handle_tools_call(
            {"name": "search_knowledge", "arguments": {"query": "x", "zone": "private"}},
            request_id=1,
            request=req,
        )
    code = resp["error"]["code"]
    assert code == MCP_AUTH_FAILED, f"ожидали -32002, получили {code}"
    assert code != JSONRPC_INTERNAL_ERROR
    assert "Forbidden" in resp["error"]["message"]
    # P1-2: никаких ERROR-записей (иначе traceback-шум в errors-sink)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # отказ замечен warning-строкой (observability без шума)
    assert any("zone access denied" in r.message for r in caplog.records)


async def test_zone_access_allowed_returns_tool_result():
    """Сервисный ключ + zone="private" → ZoneAccessError НЕ бросается (тул исполняется)."""
    from mcp_server.mcp_handler import _handle_tools_call

    app_state = MagicMock()
    # embedder/поиск не важны: Zones резолвятся до qdrant-поиска (search.py:129)
    app_state.embedder.embed_sync.return_value = [0.0]
    req = _make_request(_auth("read", "both", explicit=True), app_state)
    resp = await _handle_tools_call(
        {"name": "search_knowledge", "arguments": {"query": "x", "zone": "private"}},
        request_id=1,
        request=req,
    )
    assert "error" not in resp or resp["error"]["code"] != -32002, resp.get("error")
