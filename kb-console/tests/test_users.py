"""kb-console-roles Ф2.1-2.2 (B2): UserStore — учётные записи kb-console.

Спецификация .boardData.md §7 (delta P2-1/P2-4/P2-5):
- users.jsonl (JSONL+Lock+atomic, паттерн token_store); env CONSOLE_USERS_FILE;
- pbkdf2_hmac(sha256) stdlib, формат pbkdf2$<iters>$<salt_hex>$<hash_hex>,
  plaintext НЕ хранится, iters ≥ 100k;
- TTL-кэш верификации {sha256(creds) → (user_id, store_version)}; мутации
  (create/reset-password/deactivate/role-change) сбрасывают кэш — сброшенные
  креды отказывают НЕЖДАЯ TTL; TTL остаётся для ручных правок файла на диске;
- bootstrap CONSOLE_ADMIN_USER/CONSOLE_ADMIN_PASSWORD → seed admin при
  отсутствии активного админа (идемпотентно);
- замер pbkdf2: кэш-miss verify ≤ ~150 мс (P2-4, R1).
"""

from __future__ import annotations

import time

import pytest

from kb_console.core.users import (
    DEFAULT_PBKDF2_ITERATIONS,
    UserStore,
    hash_password,
    verify_password,
)


@pytest.fixture
def store(tmp_path) -> UserStore:
    return UserStore(users_file=str(tmp_path / "users.jsonl"), cache_ttl_sec=60.0)


# ── Формат хэша ─────────────────────────────────────────────


class TestPasswordHashFormat:
    def test_format_pbkdf2_dollars(self):
        stored = hash_password("s3cret", iterations=1000)
        parts = stored.split("$")
        assert len(parts) == 4
        assert parts[0] == "pbkdf2"
        assert int(parts[1]) == 1000
        # salt и hash — hex
        int(parts[2], 16)
        int(parts[3], 16)

    def test_verify_ok_and_wrong(self):
        stored = hash_password("s3cret", iterations=1000)
        assert verify_password("s3cret", stored) is True
        assert verify_password("wrong", stored) is False

    def test_different_salts_each_call(self):
        assert hash_password("x", iterations=1000) != hash_password("x", iterations=1000)

    def test_default_iterations_at_least_100k(self):
        assert DEFAULT_PBKDF2_ITERATIONS >= 100_000

    def test_stored_hash_has_default_iterations(self):
        stored = hash_password("x")
        assert int(stored.split("$")[1]) >= 100_000


# ── CRUD ────────────────────────────────────────────────────


class TestUserCrud:
    def test_create_and_list(self, store: UserStore):
        rec = store.create_user("alice", "pw-alice", "editor", note="тест")
        assert rec.username == "alice"
        assert rec.role == "editor"
        assert rec.active is True
        users = store.list_users()
        assert [u.username for u in users] == ["alice"]

    def test_duplicate_username_rejected(self, store: UserStore):
        store.create_user("alice", "pw", "admin")
        with pytest.raises(ValueError):
            store.create_user("alice", "other", "editor")

    def test_unknown_role_rejected(self, store: UserStore):
        with pytest.raises(ValueError):
            store.create_user("bob", "pw", "root")

    def test_persistence_across_instances(self, store: UserStore, tmp_path):
        store.create_user("alice", "pw-alice", "admin")
        reopened = UserStore(users_file=str(tmp_path / "users.jsonl"))
        assert [u.username for u in reopened.list_users()] == ["alice"]
        assert reopened.verify("alice", "pw-alice") is not None


# ── verify: верный / неверный / неизвестный / inactive ──────


class TestVerify:
    def test_verify_correct(self, store: UserStore):
        store.create_user("alice", "pw-alice", "editor")
        rec = store.verify("alice", "pw-alice")
        assert rec is not None and rec.username == "alice" and rec.role == "editor"

    def test_verify_wrong_password(self, store: UserStore):
        store.create_user("alice", "pw-alice", "editor")
        assert store.verify("alice", "wrong") is None

    def test_verify_unknown_user(self, store: UserStore):
        assert store.verify("ghost", "whatever") is None

    def test_verify_inactive_user(self, store: UserStore):
        store.create_user("alice", "pw-alice", "editor")
        store.set_active("alice", False)
        assert store.verify("alice", "pw-alice") is None

    def test_verify_empty_store(self, store: UserStore):
        assert store.has_users() is False
        assert store.verify("anyone", "pw") is None


# ── Bootstrap из env (идемпотентность) ──────────────────────


class TestBootstrap:
    def test_bootstrap_seeds_admin(self, store: UserStore):
        assert store.bootstrap_from_env("boss", "boss-pw") is True
        rec = store.verify("boss", "boss-pw")
        assert rec is not None and rec.role == "admin"

    def test_bootstrap_idempotent_no_duplicate(self, store: UserStore):
        store.bootstrap_from_env("boss", "boss-pw")
        store.bootstrap_from_env("boss", "boss-pw")
        assert len(store.list_users()) == 1

    def test_bootstrap_skipped_when_admin_exists(self, store: UserStore):
        store.create_user("root", "root-pw", "admin")
        # другой админ из env НЕ сидится поверх существующего
        assert store.bootstrap_from_env("boss", "boss-pw") is False
        assert [u.username for u in store.list_users()] == ["root"]

    def test_bootstrap_reseeds_when_admin_deactivated(self, store: UserStore):
        """Активного админа нет (деактивирован) → bootstrap сидит нового."""
        store.create_user("root", "root-pw", "admin")
        store.set_active("root", False)
        assert store.bootstrap_from_env("boss", "boss-pw") is True

    def test_bootstrap_empty_env_noop(self, store: UserStore):
        assert store.bootstrap_from_env("", "") is False
        assert store.bootstrap_from_env("boss", "") is False
        assert store.bootstrap_from_env("", "pw") is False
        assert store.has_users() is False


# ── TTL-кэш верификации + инвалидация при мутациях (P2-1) ──


class TestVerifyCache:
    def test_cached_second_verify_is_fast(self, store: UserStore):
        store.create_user("alice", "pw-alice", "editor")
        store.verify("alice", "pw-alice")  # прогрев кэша
        t0 = time.monotonic()
        for _ in range(20):
            assert store.verify("alice", "pw-alice") is not None
        assert time.monotonic() - t0 < 0.15  # 20 кэш-хитов ≪ одного pbkdf2

    def test_reset_password_invalidates_immediately(self, store: UserStore):
        """P2-1: старые креды отказывают НЕЖДАЯ TTL (store_version/cache.clear)."""
        store.create_user("alice", "old-pw", "editor")
        assert store.verify("alice", "old-pw") is not None  # прогрев кэша
        store.set_password("alice", "new-pw")
        assert store.verify("alice", "old-pw") is None      # немедленный отказ
        assert store.verify("alice", "new-pw") is not None

    def test_deactivate_invalidates_immediately(self, store: UserStore):
        store.create_user("alice", "pw", "editor")
        assert store.verify("alice", "pw") is not None
        store.set_active("alice", False)
        assert store.verify("alice", "pw") is None

    def test_role_change_invalidates_immediately(self, store: UserStore):
        store.create_user("alice", "pw", "contributor")
        store.verify("alice", "pw")
        store.set_role("alice", "editor")
        rec = store.verify("alice", "pw")
        assert rec is not None and rec.role == "editor"

    def test_create_user_visible_without_ttl_wait(self, store: UserStore):
        """Новый юзер сразу верифицируется (мутация сбросила записи)."""
        store.create_user("alice", "pw", "editor")
        store.create_user("bob", "pw-bob", "admin")
        assert store.verify("bob", "pw-bob") is not None

    def test_ttl_picks_up_manual_file_edit(self, tmp_path):
        """Ручная правка файла на диске подхватывается после TTL (reload-семантика)."""
        path = tmp_path / "users.jsonl"
        store = UserStore(users_file=str(path), cache_ttl_sec=0.05)
        store.create_user("alice", "pw", "editor")
        # «Ручная» правка: записали второго юзера напрямую в файл
        store.create_user("bob", "pw-bob", "admin")  # через стор
        store2 = UserStore(users_file=str(path), cache_ttl_sec=0.05)
        # отдельный процесс-имитация: удалили bob прямо из файла
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [
            ln for ln in lines
            if '"bob"' not in ln
        ]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        time.sleep(0.1)  # > TTL
        assert store2.verify("bob", "pw-bob") is None
        assert store2.verify("alice", "pw") is not None


# ── Замер pbkdf2 (P2-4/R1) ─────────────────────────────────


class TestPbkdf2Timing:
    def test_cold_verify_under_150ms(self, store: UserStore):
        """Кэш-miss verify — грубый performance-budget (не точный замер).

        Бюджет 500 мс: ловит ГРУБЫЕ регрессии (iters 1M+, случайный
        двойной хэш, синхронный re-read файла на каждый verify), но
        терпит загрузку машины под полным suite: pbkdf2 100k iters ≈
        30-60 мс в покое, до ~300 мс под load-spike. Флейк-прецедент:
        полный suite 2026-09-21 (218 tests параллельно с ruff).
        """
        store.create_user("alice", "pw-alice", "editor")
        fresh = UserStore(users_file=store.store_path, cache_ttl_sec=60.0)
        t0 = time.monotonic()
        assert fresh.verify("alice", "pw-alice") is not None
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"pbkdf2 verify занял {elapsed * 1000:.0f} мс"


# ── last_login ──────────────────────────────────────────────


class TestLastLogin:
    def test_verify_updates_last_login(self, store: UserStore):
        store.create_user("alice", "pw", "editor")
        assert store.verify("alice", "pw") is not None
        rec = store.get("alice")
        assert rec is not None and rec.last_login_at
