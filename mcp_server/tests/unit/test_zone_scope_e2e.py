"""Ф2.0 e2e-сценарий «3 роли × 2 зоны» (C1′) — сквозной auth-контур без Qdrant.

Полный путь: реальный TokenStore (tmp) → реальный authenticate_key (зона и
zone_explicit из SSOT-записи) → реальный _handle_tools_call (проверка прав +
инжекция _auth) → get_entry. Positive control — совпадение knowledge_id/zone
в ответе, не только счётчик/отсутствие ошибки.

Роли:
- admin (write-токен) — видит private+public (control);
- сервисный ключ верстака (read+both, zone_explicit=True, префикс mcp_rx_) —
  ТЕ ЖЕ private-хиты, что у admin; write-тул → 403 (-32002);
- public-only (legacy JSONL-запись без zone_explicit) — 0 private при том,
  что control (admin) их находит;
- contributor (import+public explicit) — не видит private.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import Request
from mcp_server.auth import authenticate_key
from mcp_server.token_store import TokenStore, _hash_key
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter

PRIV_ID = "f20e-priv-doc"
PUB_ID = "f20e-pub-doc"


# ── Fixture: сервисный ключ верстака + роли (не в проде) ──────


def _workstation_fixture(tmp_path):
    """Ключи через РЕАЛЬНЫЙ token-store: create() = осознанный zone-scope.

    index_ttl_sec=0 — legacy-ряд дописан в JSONL напрямую (мимо create()),
    индекс обязан перечитывать диск на каждый lookup.
    """
    store = TokenStore(tokens_dir=str(tmp_path / "tokens"), index_ttl_sec=0.0)
    keys = {}
    # admin
    _, keys["admin"] = store.create(level="write", zone="both", note="f20e admin")
    # Сервисный ключ верстака: read+both+zone_explicit=True → префикс mcp_rx_
    _, keys["service"] = store.create(
        level="read", zone="both", note="f20e workstation service key",
    )
    # contributor: import+public (осознанное создание)
    _, keys["contributor"] = store.create(level="import", zone="public")
    # legacy read-ключ: старый JSONL-ряд БЕЗ zone_explicit (миграции нет)
    legacy_row = {
        "id": "tok_f20e_legacy",
        "key_hash": _hash_key("f20e-legacy-read-key"),
        "level": "read",
        "zone": "both",
        "source": "manual",
    }
    path = store.store_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(legacy_row) + "\n")
    keys["public_only"] = "f20e-legacy-read-key"
    return store, keys


def _auth_source(store: TokenStore) -> SimpleNamespace:
    """app.state-источник токен-стора для authenticate_key.

    authenticate_key делает getattr(app_state, "token_store", None) —
    сам TokenStore прокидывать нельзя, нужна обёртка с атрибутом.
    """
    return SimpleNamespace(token_store=store)


def _app_state() -> MagicMock:
    """app.state с store.read: private+public записи (реальные типы)."""
    entries = {}
    for kid, zone in ((PRIV_ID, "private"), (PUB_ID, "public")):
        fm = KnowledgeFrontmatter(
            knowledge_id=kid, title=kid, domain="f20e", subject="roles", zone=zone,
        )
        entries[kid] = KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n\nКонтент {kid}.")

    async def fake_read(kid):
        return entries.get(kid)

    state = MagicMock()
    state.store.read = fake_read
    return state


def _request_for(auth_info, app_state) -> MagicMock:
    req = MagicMock(spec=Request)
    req.state.auth = auth_info
    req.app.state = app_state
    return req


async def _call_get_entry(key: str, store: TokenStore, app_state, kid: str) -> dict:
    """Полный JSON-RPC путь: authenticate_key → _handle_tools_call(get_entry)."""
    from mcp_server.mcp_handler import _handle_tools_call

    auth_info = authenticate_key(key, app_state=_auth_source(store))
    req = _request_for(auth_info, app_state)
    resp = await _handle_tools_call(
        {"name": "get_entry", "arguments": {"knowledge_id": kid}},
        request_id=1,
        request=req,
    )
    if "error" in resp:
        return resp  # JSON-RPC error (например 403) — как есть
    return json.loads(resp["result"]["content"][0]["text"])


# ── Матрица: роль × зона ──────────────────────────────────────


async def test_three_roles_two_zones_matrix(tmp_path):
    store, keys = _workstation_fixture(tmp_path)
    app_state = _app_state()

    # Positive control: admin находит ОБЕ записи (совпадение knowledge_id)
    admin_priv = await _call_get_entry(keys["admin"], store, app_state, PRIV_ID)
    assert admin_priv.get("knowledge_id") == PRIV_ID and admin_priv.get("zone") == "private"
    admin_pub = await _call_get_entry(keys["admin"], store, app_state, PUB_ID)
    assert admin_pub.get("knowledge_id") == PUB_ID

    # Сервисный ключ верстака: ТЕ ЖЕ private-хит, что у admin (+public)
    assert keys["service"].startswith("mcp_rx_"), "read+both → префикс mcp_rx_"
    svc_priv = await _call_get_entry(keys["service"], store, app_state, PRIV_ID)
    assert svc_priv.get("knowledge_id") == PRIV_ID == admin_priv["knowledge_id"]
    assert svc_priv.get("zone") == "private"
    svc_pub = await _call_get_entry(keys["service"], store, app_state, PUB_ID)
    assert svc_pub.get("knowledge_id") == PUB_ID

    # public-only (legacy): 0 private при том, что control их находит
    legacy_priv = await _call_get_entry(keys["public_only"], store, app_state, PRIV_ID)
    assert "error" in legacy_priv and "not found" in legacy_priv["error"].lower()
    legacy_pub = await _call_get_entry(keys["public_only"], store, app_state, PUB_ID)
    assert legacy_pub.get("knowledge_id") == PUB_ID

    # contributor (import+public): не видит private, видит public
    contrib_priv = await _call_get_entry(keys["contributor"], store, app_state, PRIV_ID)
    assert "error" in contrib_priv and "not found" in contrib_priv["error"].lower()
    contrib_pub = await _call_get_entry(keys["contributor"], store, app_state, PUB_ID)
    assert contrib_pub.get("knowledge_id") == PUB_ID


async def test_service_key_write_tool_forbidden(tmp_path):
    """Сервисный ключ: write-тул → JSON-RPC -32002 (403-семантика), полный путь."""
    from mcp_server.mcp_handler import MCP_AUTH_FAILED, _handle_tools_call

    store, keys = _workstation_fixture(tmp_path)
    auth_info = authenticate_key(keys["service"], app_state=_auth_source(store))
    assert auth_info.zone_explicit is True  # флаг дошёл из SSOT-записи

    req = _request_for(auth_info, _app_state())
    resp = await _handle_tools_call(
        {"name": "write_knowledge",
         "arguments": {"content": "# x", "domain": "d", "subject": "s"}},
        request_id=1,
        request=req,
    )
    assert resp["error"]["code"] == MCP_AUTH_FAILED
