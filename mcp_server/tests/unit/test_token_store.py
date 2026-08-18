"""Тесты TokenStore (W3, план two-zone-access §2.3 — ядро токен-стора).

Покрытие: формат токена (единая точка генерации), hash-only хранение,
get_by_key (compare_digest), revoke/set_active, expires_at, touch TTL-кэш,
deactivate_stale (Q9: 90 дней, subscriber-only, env-guard), seed_from_env
(идемпотентность по key_hash), атомарность записи, scope-операции (W6).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from mcp_server.token_store import (
    LEVEL_CODES,
    ZONE_CODES,
    TokenRecord,
    TokenStore,
)

SECRET_RE = re.compile(r"^[A-Za-z0-9]{32}$")
TOKEN_RE = re.compile(r"^mcp_[sriw][abx]_[A-Za-z0-9]{32}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _set_last_used(store: TokenStore, token_id: str, dt: datetime | None) -> None:
    """Обновить last_used_at на диске (через публичный load/save API)."""
    records = store.load()
    for rec in records:
        if rec.id == token_id:
            rec.last_used_at = dt
            break
    store.save(records)


@pytest.fixture()
def store(tmp_path) -> TokenStore:
    return TokenStore(tokens_dir=str(tmp_path / "tokens"))


class TestCreateFormat:
    """Формат токена: mcp_<level_code><zone_code>_<secret32> (план §2.3)."""

    @pytest.mark.parametrize(
        "level,zone,prefix",
        [
            ("subscriber", "public", "mcp_sa_"),
            ("read", "public", "mcp_ra_"),
            ("read", "both", "mcp_rx_"),
            ("import", "private", "mcp_ib_"),
            ("write", "both", "mcp_wx_"),
        ],
    )
    def test_create_prefix_and_secret(self, store, level, zone, prefix):
        token_id, plaintext = store.create(level, zone)
        assert token_id.startswith("tok_")
        assert plaintext.startswith(prefix)
        assert TOKEN_RE.match(plaintext)
        secret = plaintext.split("_", 2)[2]
        assert SECRET_RE.match(secret)
        assert len(secret) == 32

    def test_subscriber_zone_forced_public(self, store):
        """subscriber + private → запись в зоне public (префикс mcp_sa_)."""
        token_id, plaintext = store.create("subscriber", "private")
        assert plaintext.startswith("mcp_sa_")
        assert store.get(token_id).zone == "public"

    def test_unknown_level_or_zone_raises(self, store):
        with pytest.raises(ValueError):
            store.create("admin", "public")
        with pytest.raises(ValueError):
            store.create("read", "galaxy")

    def test_legend_codes(self):
        """Легенда §2.3: s/r/i/w и a/b/x."""
        assert LEVEL_CODES == {
            "subscriber": "s", "read": "r", "import": "i", "write": "w",
        }
        assert ZONE_CODES == {"public": "a", "private": "b", "both": "x"}


class TestGetByKey:
    def test_found_and_not_found(self, store):
        token_id, plaintext = store.create("read", "both")
        record = store.get_by_key(plaintext)
        assert record is not None
        assert record.id == token_id
        assert record.level == "read"
        assert len(record.key_hash) == 64  # sha256 hex, не plaintext
        assert store.get_by_key("mcp_rx_" + "0" * 32) is None
        assert store.get_by_key("") is None

    def test_plaintext_not_stored_on_disk(self, store):
        """Hash-only: в tokens.jsonl нет plaintext-секрета."""
        _, plaintext = store.create("write", "both")
        content = store.store_path.read_text(encoding="utf-8")
        assert plaintext not in content
        assert plaintext.split("_", 2)[2] not in content


class TestRevokeAndActive:
    def test_revoke_and_set_active(self, store):
        token_id, _ = store.create("read", "both")
        assert store.get(token_id).active is True
        assert store.revoke(token_id).active is False
        assert store.get(token_id).active is False
        assert store.set_active(token_id, True).active is True
        assert store.get(token_id).active is True

    def test_unknown_token_returns_none(self, store):
        assert store.revoke("tok_missing") is None
        assert store.set_active("tok_missing", False) is None


class TestExpires:
    def test_expires_at_persists(self, store):
        """Store хранит expires_at; проверку срока делает auth (план §2.3)."""
        expires = _utc_now() + timedelta(days=30)
        token_id, _ = store.create("subscriber", "public", expires_at=expires)
        record = store.get(token_id)
        assert record.expires_at is not None
        assert (record.expires_at - expires).total_seconds() < 1

    def test_is_expiring_soon(self, store):
        """Warning-окно Q9: expires_at ≤ 7 дней."""
        soon, _ = store.create("read", "both", expires_at=_utc_now() + timedelta(days=3))
        far, _ = store.create("read", "both", expires_at=_utc_now() + timedelta(days=30))
        none, _ = store.create("read", "both")
        assert store.is_expiring_soon(soon) is True
        assert store.is_expiring_soon(far) is False
        assert store.is_expiring_soon(none) is False
        assert store.is_expiring_soon("tok_missing") is False


class TestTouchLastUsed:
    def test_touch_ttl_skips_disk_write(self, store):
        """Повторный touch в пределах TTL (5с) — только in-memory, без записи."""
        token_id, _ = store.create("subscriber", "public")
        first = store.touch_last_used(token_id)
        assert first.last_used_at is not None
        disk_after_first = store.store_path.read_text(encoding="utf-8")
        second = store.touch_last_used(token_id)  # < TTL → без дисковой записи
        assert second.last_used_at is not None
        assert store.store_path.read_text(encoding="utf-8") == disk_after_first
        # in-memory индекс обновлён
        assert store.get(token_id).last_used_at == second.last_used_at

    def test_touch_unknown_returns_none(self, store):
        assert store.touch_last_used("tok_missing") is None


class TestDeactivateStale:
    """Q9 (v1.7): авто-деактивация subscriber-токенов после 90 дней."""

    def test_stale_subscriber_deactivated(self, store):
        token_id, _ = store.create("subscriber", "public")
        _set_last_used(store, token_id, _utc_now() - timedelta(days=91))
        assert store.deactivate_stale() == [token_id]
        record = store.get(token_id)
        assert record.active is False
        assert record.note == "deactivated: inactive >90d"

    def test_fresh_subscriber_not_touched(self, store):
        token_id, _ = store.create("subscriber", "public")
        _set_last_used(store, token_id, _utc_now() - timedelta(days=1))
        assert store.deactivate_stale() == []
        assert store.get(token_id).active is True

    def test_no_last_used_falls_back_to_created_at(self, store):
        """База неактивности: last_used_at, если None → created_at."""
        token_id, _ = store.create("subscriber", "public")
        records = store.load()
        for rec in records:
            if rec.id == token_id:
                rec.created_at = _utc_now() - timedelta(days=95)
                break
        store.save(records)
        assert store.deactivate_stale() == [token_id]

    def test_subscriber_only_read_token_not_touched(self, store):
        token_id, _ = store.create("read", "both")
        _set_last_used(store, token_id, _utc_now() - timedelta(days=91))
        assert store.deactivate_stale() == []
        assert store.get(token_id).active is True

    def test_env_source_guard(self, store):
        """source='env' не трогается, даже если level=subscriber (защита Q9)."""
        store.save([TokenRecord(
            id="tok_envtest",
            key_hash="e" * 64,
            level="subscriber",
            zone="public",
            source="env",
            created_at=_utc_now() - timedelta(days=95),
        )])
        assert store.deactivate_stale() == []
        assert store.get("tok_envtest").active is True


class TestSeedFromEnv:
    """Bootstrap R2: env-ключи → store (zone=both, source=env)."""

    def test_seed_creates_env_records(self, store):
        added = store.seed_from_env(
            {"read": ["rk1"], "import": ["ik1"], "write": ["wk1"]},
        )
        assert added == 3
        read_rec = store.get_by_key("rk1")
        assert read_rec.level == "read"
        assert read_rec.zone == "both"
        assert read_rec.source == "env"

    def test_seed_idempotent_by_key_hash(self, store):
        assert store.seed_from_env({"read": ["rk1"], "write": ["wk1"]}) == 2
        assert store.seed_from_env({"read": ["rk1"], "write": ["wk1"]}) == 0
        assert store.seed_from_env({"read": ["rk1"], "import": ["ik1"]}) == 1
        assert len(store.list()) == 3

    def test_seed_skips_empty_and_duplicates(self, store):
        assert store.seed_from_env({"read": ["rk1", "", "rk1", "rk1"]}) == 1
        assert len(store.list()) == 1


class TestAtomicityAndScope:
    def test_file_valid_after_many_writes(self, store):
        """N записей → валидный JSONL, tmp-файлов не остаётся (tmp+os.replace)."""
        for i in range(25):
            store.create("read", "both", note=f"token-{i}")
        lines = store.store_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 25
        for line in lines:
            TokenRecord(**json.loads(line))  # каждая строка парсится
        assert not list(store.store_path.parent.glob("*.tmp"))

    def test_grant_and_revoke_scope(self, store):
        token_id, _ = store.create("read", "private")
        assert store.grant_scope(token_id, ["k1", "k2"]).scope == ["k1", "k2"]
        assert store.grant_scope(token_id, ["k2", "k3"]).scope == ["k1", "k2", "k3"]
        assert store.revoke_scope(token_id).scope is None
        assert store.grant_scope("tok_missing", ["k1"]) is None
        assert store.revoke_scope("tok_missing") is None


class TestIndexTtl:
    """In-memory индекс + TTL-инвалидация (TOKEN_INDEX_TTL_SEC)."""

    def test_ttl_cache_vs_live_reload(self, tmp_path):
        dirpath = str(tmp_path / "t")
        s1 = TokenStore(tokens_dir=dirpath)
        s2_cached = TokenStore(tokens_dir=dirpath, index_ttl_sec=60.0)
        s3_live = TokenStore(tokens_dir=dirpath, index_ttl_sec=0.0)
        _, key1 = s1.create("read", "both")
        assert s2_cached.get_by_key(key1) is not None  # первое чтение → индекс
        _, key2 = s1.create("read", "both")  # внешняя запись после кэша s2
        assert s2_cached.get_by_key(key2) is None  # TTL=60 → кэш не истёк
        assert s3_live.get_by_key(key2) is not None  # TTL=0 → всегда reload
