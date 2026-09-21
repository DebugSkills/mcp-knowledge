"""UserStore — учётные записи kb-console (kb-console-roles B2, Ф2).

users.jsonl по пути env CONSOLE_USERS_FILE (default /app/data/console/users.jsonl,
volume ./data/console — паттерн data/tokens). Паттерн = token_store mcp_server:
JSONL + threading.Lock + атомарная запись (tmp + os.replace).

Гарантии:
- Plaintext-пароли НЕ хранятся: pbkdf2_hmac(sha256) stdlib (air-gap, 0
  зависимостей), формат pbkdf2$<iters>$<salt_hex>$<hash_hex>, iters ≥ 100k.
- TTL-кэш верификации {sha256(creds) → (user_id, store_version)} (P2-4/R1):
  Basic шлёт креды каждым запросом — pbkdf2 на каждый запрос = self-DoS.
- Инвалидация при мутациях (P2-1): любая мутация (create/reset-password/
  deactivate/role-change) инкрементирует store_version и чистит кэш —
  сброшенные креды отказывают НЕЖДАЯ TTL. TTL (~5 мин) остаётся для ручных
  правок users.jsonl на диске (reload без рестарта, семантика
  TOKEN_INDEX_TTL_SEC). WORKERS=1 — инвариант проекта, гонок нет.
- Bootstrap: CONSOLE_ADMIN_USER/CONSOLE_ADMIN_PASSWORD → seed admin при
  отсутствии АКТИВНОГО админа (идемпотентно, паттерн seed_from_env).

Роли (спека B2): admin → write-ключ, editor → editor-ключ,
contributor → import-ключ (маппинг — Ф3, здесь только метка роли).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path

logger = logging.getLogger("kb_console.users")

ROLES: tuple[str, ...] = ("admin", "editor", "contributor")
"""Роли консоли (спека B2 §2): admin/editor(добавление+удаление)/contributor(только добавление)."""

DEFAULT_PBKDF2_ITERATIONS = 100_000
"""≥100k по спеке (P2-4): кэш-miss verify ≤ ~150 мс на типичном железе."""

DEFAULT_CACHE_TTL_SEC = 300.0
"""TTL кэша верификации и перечитывания файла (семантика TOKEN_INDEX_TTL_SEC)."""

_DEFAULT_USERS_FILE = "/app/data/console/users.jsonl"

_LOGIN_TOUCH_THROTTLE_SEC = 60.0
"""last_login_at пишем не чаще раза в минуту на юзера (файл не молотим)."""


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def hash_password(password: str, iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> str:
    """Захэшировать пароль: pbkdf2$<iters>$<salt_hex>$<hash_hex>.

    Соль уникальна на вызов; plaintext не возвращается и не логируется.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против pbkdf2$-хэша (constant-time финальное сравнение)."""
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


@dataclass
class UserRecord:
    """Запись учётной записи (SSOT-строка в users.jsonl)."""

    id: str = field(default_factory=lambda: "usr_" + secrets.token_hex(8))
    username: str = ""
    password_hash: str = ""
    role: str = "contributor"  # admin | editor | contributor
    active: bool = True
    created_at: str = field(default_factory=_now_iso)
    last_login_at: str | None = None
    note: str = ""


class UserStore:
    """Хранилище учёток: JSONL + TTL-кэш верификации с версионной инвалидацией."""

    def __init__(
        self,
        users_file: str | None = None,
        cache_ttl_sec: float = DEFAULT_CACHE_TTL_SEC,
        pbkdf2_iterations: int = DEFAULT_PBKDF2_ITERATIONS,
        audit_file: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._users_file = users_file or os.environ.get("CONSOLE_USERS_FILE") or _DEFAULT_USERS_FILE
        self._audit_file = audit_file
        self._cache_ttl = cache_ttl_sec
        self._iterations = pbkdf2_iterations
        self._records: list[UserRecord] | None = None
        self._loaded_at = 0.0
        self._store_version = 0
        # sha256(username \x00 password) → (user_id, version, expires_monotonic)
        self._verify_cache: dict[str, tuple[str, int, float]] = {}
        self._login_touch: dict[str, float] = {}

    # ── Пути ────────────────────────────────────────────────

    @property
    def store_path(self) -> Path:
        return Path(self._users_file)

    @property
    def audit_path(self) -> Path:
        """users_audit.jsonl — рядом с users.jsonl (Ф3.3), либо явный путь."""
        if self._audit_file:
            return Path(self._audit_file)
        return self.store_path.parent / "users_audit.jsonl"

    @property
    def store_version(self) -> int:
        with self._lock:
            return self._store_version

    # ── Загрузка / сохранение (паттерн token_store) ─────────

    def _load(self) -> list[UserRecord]:
        now = time.monotonic()
        with self._lock:
            if self._records is not None and (now - self._loaded_at) < self._cache_ttl:
                return self._records
            records: list[UserRecord] = []
            path = self.store_path
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(UserRecord(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("users.jsonl: пропуск битой строки (len=%d)", len(line))
            self._records = records
            self._loaded_at = now
            return records

    def _save_locked(self, records: list[UserRecord]) -> None:
        """Атомарная запись (tmp + os.replace) — вызывать под self._lock."""
        path = self.store_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _mutate(self) -> None:
        """Общий хвост мутации: версия++, кэш чист, in-memory сброс."""
        self._store_version += 1
        self._verify_cache.clear()
        self._records = None  # следующий доступ перечитает файл

    # ── Аудит (Ф3.3): users_audit.jsonl ─────────────────────

    def _audit(
        self, event: str, target: str, actor: str = "system",
        details: dict | None = None,
    ) -> None:
        """Best-effort append в users_audit.jsonl (ошибки — warning, не бросаем).

        События: user_create/user_reset/role_change/user_deactivate/
        user_activate (из мутаций) + login_ok/login_fail (log_login).
        Отдельный журнал от серверного audit.jsonl (quality-действия).
        """
        record = {
            "ts": _now_iso(),
            "event": event,
            "actor": actor,
            "target": target,
            "details": details or {},
        }
        try:
            path = self.audit_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("users_audit write failed: %s", e)

    def log_login(self, username: str, *, ok: bool) -> None:
        """Зафиксировать исход логина (вызывается middleware)."""
        self._audit("login_ok" if ok else "login_fail", username, actor=username)

    # ── Чтение ──────────────────────────────────────────────

    def list_users(self) -> list[UserRecord]:
        return list(self._load())

    def has_users(self) -> bool:
        """«Стор непуст» — есть хоть одна запись (в т.ч. деактивированная):
        деактивированный админ не должен возвращать консоль в legacy-режим
        общего пароля."""
        return len(self._load()) > 0

    def get(self, username: str) -> UserRecord | None:
        for rec in self._load():
            if rec.username == username:
                return rec
        return None

    def _find_by_id(self, user_id: str, records: list[UserRecord]) -> UserRecord | None:
        for rec in records:
            if rec.id == user_id:
                return rec
        return None

    # ── Мутации ─────────────────────────────────────────────

    def create_user(
        self, username: str, password: str, role: str, note: str = "",
        actor: str = "system",
    ) -> UserRecord:
        if not username or not password:
            raise ValueError("username и password обязательны")
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}. Expected one of {list(ROLES)}")
        with self._lock:
            records = self._load()
            if any(r.username == username for r in records):
                raise ValueError(f"username уже занят: {username!r}")
            rec = UserRecord(
                username=username,
                password_hash=hash_password(password, self._iterations),
                role=role,
                note=note,
            )
            records.append(rec)
            self._save_locked(records)
            self._mutate()
            logger.info("User created: %s (role=%s)", username, role)
            self._audit("user_create", username, actor=actor, details={"role": role})
            return rec

    def set_password(self, username: str, new_password: str, actor: str = "system") -> None:
        if not new_password:
            raise ValueError("пароль не может быть пустым")
        with self._lock:
            records = self._load()
            rec = next((r for r in records if r.username == username), None)
            if rec is None:
                raise KeyError(f"нет такого пользователя: {username!r}")
            rec.password_hash = hash_password(new_password, self._iterations)
            self._save_locked(records)
            self._mutate()
            logger.info("Password reset for user: %s", username)
            self._audit("user_reset", username, actor=actor)

    def set_role(self, username: str, role: str, actor: str = "system") -> None:
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}. Expected one of {list(ROLES)}")
        with self._lock:
            records = self._load()
            rec = next((r for r in records if r.username == username), None)
            if rec is None:
                raise KeyError(f"нет такого пользователя: {username!r}")
            old = rec.role
            rec.role = role
            self._save_locked(records)
            self._mutate()
            logger.info("Role changed: %s → %s", username, role)
            self._audit("role_change", username, actor=actor, details={"old": old, "new": role})

    def set_active(self, username: str, active: bool, actor: str = "system") -> None:
        with self._lock:
            records = self._load()
            rec = next((r for r in records if r.username == username), None)
            if rec is None:
                raise KeyError(f"нет такого пользователя: {username!r}")
            rec.active = active
            self._save_locked(records)
            self._mutate()
            logger.info("User %s: active=%s", username, active)
            self._audit(
                "user_activate" if active else "user_deactivate", username, actor=actor
            )

    # ── Bootstrap (env → seed admin, идемпотентно) ──────────

    def bootstrap_from_env(self, admin_user: str, admin_password: str) -> bool:
        """Seed админа из env, если НЕТ активного админа. True — просеяли."""
        if not admin_user or not admin_password:
            return False
        if any(r.role == "admin" and r.active for r in self._load()):
            return False
        self.create_user(admin_user, admin_password, "admin", note="bootstrap")
        logger.info("Bootstrap: seeded admin user %r from env", admin_user)
        return True

    # ── Верификация (TTL-кэш + версия стора) ────────────────

    @staticmethod
    def _creds_cache_key(username: str, password: str) -> str:
        return hashlib.sha256(f"{username}\x00{password}".encode()).hexdigest()

    def verify(self, username: str, password: str) -> UserRecord | None:
        """Проверить креды. None = отказ (без деталей — не раскрываем, есть ли юзер).

        Синхронный (pbkdf2 CPU-bound): вызывать из event loop ТОЛЬКО через
        run_in_executor (P2-4). Кэш-хит не делает CPU-работы.
        """
        if not username or not password:
            return None
        key = self._creds_cache_key(username, password)
        now = time.monotonic()
        with self._lock:
            hit = self._verify_cache.get(key)
            if hit is not None:
                user_id, version, expires = hit
                if version == self._store_version and expires > now:
                    rec = self._find_by_id(user_id, self._load())
                    if rec is not None and rec.active:
                        return rec
                    return None

        records = self._load()
        rec = next((r for r in records if r.username == username), None)
        if rec is None or not rec.active:
            return None
        if not verify_password(password, rec.password_hash):
            return None

        with self._lock:
            self._verify_cache[key] = (rec.id, self._store_version, now + self._cache_ttl)
        self._touch_last_login(rec)
        return rec

    def _touch_last_login(self, rec: UserRecord) -> None:
        """best-effort last_login_at c троттлингом (не version-мутация)."""
        now = time.monotonic()
        with self._lock:
            if now - self._login_touch.get(rec.id, 0.0) < _LOGIN_TOUCH_THROTTLE_SEC:
                return
            self._login_touch[rec.id] = now
            records = self._load()
            fresh = self._find_by_id(rec.id, records)
            if fresh is None:
                return
            fresh.last_login_at = _now_iso()
            self._save_locked(records)
            # _records уже актуален (тот же список), кэш не трогаем

    # ── Интроспекция для тестов/диагностики ─────────────────

    def cache_size(self) -> int:
        with self._lock:
            return len(self._verify_cache)
