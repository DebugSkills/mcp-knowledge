"""Тесты subscriber-ветки auth (W3.3-W3.7, план two-zone-access §2.3).

Покрытие:
- authenticate_key: токен-стор first (subscriber → AuthInfo level/zone/scope/token_id),
  приоритет store > env, active/expires_at → 401, touch_last_used best-effort.
- Префикс-сверка mcp_<level><zone>_: mismatch → warning, доступ по записи;
  env/bootstrap-ключ без префикса → без warning (v1.6).
- SUBSCRIBER_TOOLS: белый список (search_knowledge OK, list_quality_issues → 403),
  zone принудительно "public".
- mask_key: mcp_-ключ → key[:7], обычный → key[:4].
- Rate-limit: subscriber → bucket rate_limiter_subscriber.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from mcp_server.auth import (
    SUBSCRIBER_TOOLS,
    AuthInfo,
    _check_rate_limit_bytes,
    authenticate_key,
    check_tool_permission,
    mask_key,
)
from mcp_server.token_store import TokenRecord, TokenStore

logger = logging.getLogger("mcp_knowledge.auth")


def _rec(
    key: str,
    level: str = "subscriber",
    zone: str = "public",
    active: bool = True,
    expires_at: datetime | None = None,
    scope: list[str] | None = None,
    token_id: str = "tok_test",
) -> TokenRecord:
    from mcp_server.token_store import _hash_key

    return TokenRecord(
        id=token_id,
        key_hash=_hash_key(key),
        level=level,
        zone=zone,
        scope=scope,
        active=active,
        expires_at=expires_at,
        source="manual",
    )


class _FakeLimiter:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.checked_key: str | None = None
        self.checked_count: int | None = None

    async def check(self, key: str, count: int) -> bool:
        self.checked_key = key
        self.checked_count = count
        return self.allow


def _app_state_with(store: TokenStore, **lims) -> SimpleNamespace:
    return SimpleNamespace(token_store=store, **lims)


# ── authenticate_key: токен-стор first ───────────────────────


class TestAuthStoreFirst:
    def test_subscriber_token_from_store(self, tmp_path):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_sa_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(key, level="subscriber", zone="public", token_id="tok_sub1")])
        auth = authenticate_key(key, app_state=_app_state_with(store))
        assert auth.authenticated is True
        assert auth.key_level == "subscriber"
        assert auth.zone == "public"
        assert auth.token_id == "tok_sub1"
        assert auth.scope == set()

    def test_read_token_from_store_carries_scope(self, tmp_path):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_rx_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(key, level="read", zone="both", scope=["k1", "k2"])])
        auth = authenticate_key(key, app_state=_app_state_with(store))
        assert auth.key_level == "read"
        assert auth.zone == "both"
        assert auth.scope == {"k1", "k2"}

    def test_store_beats_env_same_key(self, tmp_path, monkeypatch):
        """Одинаковый ключ в store и env → приоритет store."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_rx_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(key, level="read", zone="private")])
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_READ_KEYS", [key],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_WRITE_KEYS", [],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_IMPORT_KEYS", [],
        )
        auth = authenticate_key(key, app_state=_app_state_with(store))
        assert auth.key_level == "read"
        assert auth.zone == "private"  # зона из записи, не "both" из env

    def test_inactive_token_rejected(self, tmp_path):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_sa_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(key, active=False)])
        auth = authenticate_key(key, app_state=_app_state_with(store))
        assert auth.authenticated is False
        assert auth.key_level == "none"

    def test_expired_token_rejected(self, tmp_path):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_sa_0123456789abcdefghijklmnopqrstuv"
        store.save(
            [_rec(key, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))]
        )
        auth = authenticate_key(key, app_state=_app_state_with(store))
        assert auth.authenticated is False

    def test_unknown_key_falls_back_to_env(self, tmp_path, monkeypatch):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_READ_KEYS", ["env-read-key-123"],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_WRITE_KEYS", [],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_IMPORT_KEYS", [],
        )
        auth = authenticate_key("env-read-key-123", app_state=_app_state_with(store))
        assert auth.authenticated is True
        assert auth.key_level == "read"
        assert auth.zone == "both"

    def test_authenticated_touches_last_used(self, tmp_path):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        key = "mcp_sa_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(key)])
        authenticate_key(key, app_state=_app_state_with(store))
        rec = store.get("tok_test")
        assert rec is not None
        assert rec.last_used_at is not None


# ── Префикс-сверка (v1.6) ────────────────────────────────────


class TestPrefixCheck:
    def test_mismatch_warns_but_grants_by_record(self, tmp_path, caplog):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        # Запись: write/both, но предъявлен ключ с префиксом mcp_ra_ (подсказка подменена)
        presented = "mcp_ra_0123456789abcdefghijklmnopqrstuv"
        store.save([_rec(presented, level="write", zone="both")])
        with caplog.at_level(logging.WARNING, logger="mcp_knowledge.auth"):
            auth = authenticate_key(presented, app_state=_app_state_with(store))
        assert auth.key_level == "write"
        assert auth.zone == "both"  # доступ по записи, не по префиксу
        assert any("prefix" in r.message.lower() for r in caplog.records)

    def test_env_key_without_prefix_no_warning(self, tmp_path, monkeypatch, caplog):
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_READ_KEYS", ["plain-env-key-123"],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_WRITE_KEYS", [],
        )
        monkeypatch.setattr(
            "mcp_server.auth.settings.MCP_IMPORT_KEYS", [],
        )
        with caplog.at_level(logging.WARNING, logger="mcp_knowledge.auth"):
            auth = authenticate_key(
                "plain-env-key-123", app_state=_app_state_with(store),
            )
        assert auth.authenticated is True
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []


# ── SUBSCRIBER_TOOLS + check_tool_permission ─────────────────


class TestSubscriberPermissions:
    def test_whitelist_allowed(self):
        info = AuthInfo(authenticated=True, key_level="subscriber", zone="public")
        assert SUBSCRIBER_TOOLS == {
            "search_knowledge", "search_by_tags", "get_entry", "get_knowledge_map",
            "list_domains", "list_subjects", "list_projects", "list_collections",
            "find_fragment", "analyze_content",
        }
        for tool in sorted(SUBSCRIBER_TOOLS):
            check_tool_permission(info, tool)  # не должно бросать

    def test_quality_tools_forbidden(self):
        info = AuthInfo(authenticated=True, key_level="subscriber", zone="public")
        for tool in ("list_quality_issues", "review_queue", "review_queue_books",
                     "review_duplicate_pairs", "list_audit_log"):
            with pytest.raises(HTTPException) as exc:
                check_tool_permission(info, tool)
            assert exc.value.status_code == 403

    def test_write_tools_forbidden(self):
        info = AuthInfo(authenticated=True, key_level="subscriber", zone="public")
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(info, "write_knowledge")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(info, "import_content")
        assert exc.value.status_code == 403

    def test_resources_and_prompts_forbidden(self):
        info = AuthInfo(authenticated=True, key_level="subscriber", zone="public")
        for tool in ("resources/list", "resources/read", "prompts/list", "prompts/get"):
            with pytest.raises(HTTPException) as exc:
                check_tool_permission(info, tool)
            assert exc.value.status_code == 403

    def test_zone_forced_public(self):
        info = AuthInfo(authenticated=True, key_level="subscriber", zone="private")
        check_tool_permission(info, "search_knowledge")
        assert info.zone == "public"


# ── mask_key ─────────────────────────────────────────────────


class TestMaskKeyMCP:
    def test_mcp_key_uses_7_char_prefix(self):
        masked = mask_key("mcp_sa_0123456789abcdefghijklmnopqrstuv")
        assert masked.startswith("mcp_sa_")
        assert "..." in masked

    def test_short_mcp_key_below_9_still_masked(self):
        assert mask_key("mcp_sa") == "[too-short]"

    def test_plain_key_unchanged(self):
        assert mask_key("my-secret-api-key-42").startswith("my-s")


# ── Rate-limit: subscriber bucket ────────────────────────────


class TestSubscriberRateLimit:
    async def test_uses_subscriber_bucket(self):
        sub_lim = _FakeLimiter(allow=False)
        default_lim = _FakeLimiter(allow=True)
        app = SimpleNamespace(
            state=SimpleNamespace(
                rate_limiter=default_lim,
                rate_limiter_subscriber=sub_lim,
            ),
        )
        auth = AuthInfo(authenticated=True, key_level="subscriber", key_hash="h1")
        result = await _check_rate_limit_bytes(b'{"method":"tools/call"}', auth, app)
        # subscriber-ключ упёрся в subscriber-лимитер
        assert result is not None
        assert result["error"]["code"] == -32003
        assert sub_lim.checked_key == "h1"
        assert sub_lim.checked_count == 1
        assert default_lim.checked_key is None  # общий не тронут

    async def test_subscriber_allowed(self):
        sub_lim = _FakeLimiter(allow=True)
        app = SimpleNamespace(state=SimpleNamespace(rate_limiter_subscriber=sub_lim))
        auth = AuthInfo(authenticated=True, key_level="subscriber", key_hash="h1")
        result = await _check_rate_limit_bytes(b"{}", auth, app)
        assert result is None
