"""Ф2.0 «Zone-scope токенов MCP» (C1′, trace arch-2026-10-05-ai-workspace).

Носитель: TokenRecord.zone_explicit (create → True; legacy/env → False) +
AuthInfo.zone_explicit. Политика (auth_zone) тестируется здесь на границе
носителя: create/seed/parse/AuthInfo-прокидка. Резолв-матрица — в test_zone_policy.py.

RED на старом коде: поля zone_explicit не существует / env-ключи получают zone="both".
"""

from __future__ import annotations

import json

from mcp_server.auth import AuthInfo, authenticate_key
from mcp_server.token_store import TokenStore, _hash_key


# ── TokenRecord.zone_explicit (R5: legacy-парсинг) ────────────


class TestTokenRecordZoneExplicit:
    def test_create_sets_zone_explicit_true(self, tmp_path):
        """create() — осознанное создание: зона ключа Honour-ится политикой."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        token_id, plaintext = store.create(level="read", zone="both")
        record = store.get(token_id)
        assert record is not None
        assert record.zone_explicit is True
        assert record.zone == "both"
        assert record.level == "read"

    def test_old_jsonl_row_without_flag_parses_as_legacy(self, tmp_path):
        """R5: старый JSONL-ряд без zone_explicit парсится (missing-поле ок)
        и трактуется как legacy → public-only при чтении."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        legacy_row = {
            "id": "tok_legacy01",
            "key_hash": "ab" * 32,
            "level": "read",
            "zone": "both",
            "source": "manual",
        }
        path = store.store_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy_row) + "\n", encoding="utf-8")

        records = store.load()
        assert len(records) == 1
        assert records[0].zone_explicit is False  # default → legacy-public

    def test_service_workstation_key_prefix(self, tmp_path):
        """Сервисный ключ верстака: read+both+zone_explicit → префикс mcp_rx_."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        _, plaintext = store.create(level="read", zone="both")
        assert plaintext.startswith("mcp_rx_")


# ── seed_from_env: env-ключи не получают эскалацию зон (P0-3) ──


class TestSeedFromEnvZones:
    def test_env_read_import_seed_public_write_both(self, tmp_path):
        """read/import → zone=public (+zone_explicit=False); write не сужается (both)."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        added = store.seed_from_env({
            "read": ["env-read-key-1"],
            "import": ["env-import-key-1"],
            "write": ["env-write-key-1"],
        })
        assert added == 3
        by_level = {r.level: r for r in store.list()}
        assert by_level["read"].zone == "public"
        assert by_level["read"].zone_explicit is False
        assert by_level["import"].zone == "public"
        assert by_level["import"].zone_explicit is False
        assert by_level["write"].zone == "both"
        assert by_level["write"].zone_explicit is False


# ── AuthInfo: прокидка zone_explicit из стора; env-fallback → public ──


class TestAuthInfoZoneExplicit:
    def test_authinfo_carries_zone_explicit(self):
        info = AuthInfo(authenticated=True, key_level="read", zone="both",
                        zone_explicit=True)
        assert info.zone_explicit is True
        # default — legacy (False): старые конструкторы не меняют семантику
        assert AuthInfo().zone_explicit is False

    def test_store_record_auth_carries_flag(self, tmp_path):
        """Полный путь: create(read, both) → authenticate_key → zone_explicit=True."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        _, plaintext = store.create(level="read", zone="both")
        state = type("S", (), {"token_store": store})()
        info = authenticate_key(plaintext, app_state=state)
        assert info.authenticated is True
        assert info.key_level == "read"
        assert info.zone == "both"
        assert info.zone_explicit is True

    def test_legacy_store_record_auth_flag_false(self, tmp_path):
        """Legacy-запись (без флага) → AuthInfo.zone_explicit=False → public-only."""
        store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
        legacy_row = {
            "id": "tok_legacy02",
            "key_hash": _hash_key("legacy-plaintext-key"),
            "level": "read",
            "zone": "both",
            "source": "manual",
        }
        path = store.store_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy_row) + "\n", encoding="utf-8")

        state = type("S", (), {"token_store": store})()
        info = authenticate_key("legacy-plaintext-key", app_state=state)
        assert info.authenticated is True
        assert info.zone == "both"  # зона в носителе есть
        assert info.zone_explicit is False  # но политика её НЕ honour-ит

    def test_env_fallback_read_import_zone_public(self, monkeypatch):
        """P0-3: env-fallback read/import → зона public (не эскалация both);
        write не сужается."""
        from mcp_server.config import settings

        monkeypatch.setattr(settings, "MCP_READ_KEYS", ["env-read-only"])
        monkeypatch.setattr(settings, "MCP_IMPORT_KEYS", ["env-import-only"])
        monkeypatch.setattr(settings, "MCP_WRITE_KEYS", ["env-write-master"])

        info = authenticate_key("env-read-only", app_state=None)
        assert (info.key_level, info.zone, info.zone_explicit) == ("read", "public", False)

        info = authenticate_key("env-import-only", app_state=None)
        assert (info.key_level, info.zone, info.zone_explicit) == ("import", "public", False)

        info = authenticate_key("env-write-master", app_state=None)
        assert (info.key_level, info.zone, info.zone_explicit) == ("write", "both", False)
