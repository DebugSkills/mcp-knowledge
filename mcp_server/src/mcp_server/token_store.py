"""Token-store — SSOT токенов двухконтурной модели доступа (W3, план §2.3).

Хранилище токенов: {TOKENS_DIR}/tokens.jsonl (JSONL, одна запись на строку).
Паттерн = quality/issues.py: threading.Lock + атомарная запись (tmp + os.replace),
read-modify-write под lock. Стиль аудита: записи не удаляются физически —
revoke/scope меняют запись, файл переписывается целиком под lock.

Гарантии:
- Plaintext-ключи НЕ хранятся: в сторе только key_hash = sha256(plaintext).
  plaintext отдаётся ОДИН раз — возвратом из create().
- Потокобезопасность: threading.Lock сериализует все операции.
- Индекс {key_hash → TokenRecord} в памяти + TTL-инвалидация
  (TOKEN_INDEX_TTL_SEC): перечитывание файла не чаще раза в N сек (hot path).

Формат токена (единая точка генерации — TokenStore.create(), v1.6):
    mcp_<level_code><zone_code>_<secret32>
    level_code: s=subscriber, r=read, i=import, w=write
    zone_code:  a=public, b=private, x=both (subscriber → принудительно a)
    secret: 32 символа base62 (secrets.choice, ≥190 бит энтропии)

Bootstrap (R2): seed_from_env() — env-ключи (config MCP_READ_KEYS /
MCP_IMPORT_KEYS / MCP_WRITE_KEYS) сидятся как level=read|import|write,
zone="both", source="env", идемпотентно по key_hash. Токен-стор — SSOT
для auth; env остаётся fallback на переходный период.

Q9 (авто-деактивация): deactivate_stale() — subscriber-токены, неактивные
> max_inactive_days (90). База неактивности: last_used_at, если None →
created_at (токен без использования деградирует по дате создания).
source="env" не трогается. Возвращает список deactivated id.

Зависимости: только stdlib + Pydantic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger("mcp_knowledge.auth.token_store")

# ── Легенда (план §2.3): коды уровня и зоны в теле токена ─────

LEVEL_CODES: dict[str, str] = {
    "subscriber": "s", "read": "r", "import": "i", "write": "w",
}
ZONE_CODES: dict[str, str] = {"public": "a", "private": "b", "both": "x"}

_SECRET_ALPHABET = string.ascii_letters + string.digits  # base62 (62 символа)
_SECRET_LEN = 32  # log2(62^32) ≈ 190.5 бит энтропии (≥190 по плану)

EXPIRING_SOON_DAYS = 7  # warning-окно Q9: 90 - 7 = 83+ дней

_DEFAULT_TOKENS_DIR = "/app/data/tokens"

_UNSET = object()  # sentinel: «не трогать поле» (update_meta, W5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_key(plaintext: str) -> str:
    """sha256(plaintext) → hex. Plaintext в сторе не хранится."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class TokenRecord(BaseModel):
    """Запись токена доступа (SSOT-строка в tokens.jsonl)."""

    id: str = Field(..., description='"tok_" + token_hex(8)')
    key_hash: str = Field(..., description="sha256(plaintext).hexdigest()")
    level: str = Field(..., description="subscriber | read | import | write")
    zone: str = Field(..., description="public | private | both (subscriber → public)")
    scope: list[str] | None = Field(
        default=None, description="knowledge_id/collection_id grants (W6)",
    )
    active: bool = True
    expires_at: datetime | None = None
    note: str = ""
    source: str = "manual"  # manual | env (bootstrap R2)
    created_at: datetime = Field(default_factory=_now)
    last_used_at: datetime | None = None


class TokenStore:
    """Хранилище токенов: JSONL + in-memory индекс с TTL-инвалидацией."""

    def __init__(
        self,
        tokens_dir: str | None = None,
        index_ttl_sec: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._tokens_dir = tokens_dir
        self._ttl_override = index_ttl_sec
        self._records: list[TokenRecord] | None = None
        self._index_by_hash: dict[str, TokenRecord] = {}
        self._index_by_id: dict[str, TokenRecord] = {}
        self._index_loaded_at = 0.0
        self._last_touch_ts = 0.0

    # ── Пути и конфигурация ──────────────────────────────────

    @property
    def store_dir(self) -> str:
        return self._tokens_dir or self._default_dir()

    @property
    def store_path(self) -> Path:
        return Path(self.store_dir) / "tokens.jsonl"

    @staticmethod
    def _default_dir() -> str:
        try:
            from .config import settings
            return settings.TOKENS_DIR
        except Exception:  # noqa: BLE001
            return _DEFAULT_TOKENS_DIR

    def _ttl_sec(self) -> float:
        if self._ttl_override is not None:
            return self._ttl_override
        try:
            from .config import settings
            return float(settings.TOKEN_INDEX_TTL_SEC)
        except Exception:  # noqa: BLE001
            return 5.0

    def _ensure_dir(self) -> None:
        # makedirs exist_ok=True — паттерн tokenizer.py (локально /app недоступен)
        Path(self.store_dir).mkdir(parents=True, exist_ok=True)

    # ── Индекс (TTL-инвалидация) ─────────────────────────────

    def _index_expired(self) -> bool:
        return self._records is None or (
            time.monotonic() - self._index_loaded_at
        ) >= self._ttl_sec()

    def _refresh_index_locked(self, records: list[TokenRecord]) -> None:
        self._records = records
        self._index_by_hash = {r.key_hash: r for r in records}
        self._index_by_id = {r.id: r for r in records}
        self._index_loaded_at = time.monotonic()

    def _get_index_locked(self) -> dict[str, TokenRecord]:
        if self._index_expired():
            self._refresh_index_locked(self.load())
        return self._index_by_hash

    def _get_by_id_locked(self) -> dict[str, TokenRecord]:
        if self._index_expired():
            self._refresh_index_locked(self.load())
        return self._index_by_id

    # ── Ввод-вывод (атомарный) ───────────────────────────────

    def load(self) -> list[TokenRecord]:
        """Прочитать все записи с диска; битые строки пропускаются (warning)."""
        path = self.store_path
        if not path.exists():
            return []
        records: list[TokenRecord] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(TokenRecord(**json.loads(line)))
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "Пропущена битая строка в %s: %r (%s)", path, line[:80], exc,
                    )
        return records

    def save(self, records: list[TokenRecord]) -> None:
        """Атомарно записать все записи: tmp → os.replace() (под lock)."""
        self._ensure_dir()
        path = self.store_path
        tmp_path = path.with_suffix(".jsonl.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(rec.model_dump(mode="json"), ensure_ascii=False)
                        + "\n" for rec in records)
            os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                os.unlink(tmp_path)
            raise

    # ── Чтение ───────────────────────────────────────────────

    def get_by_key(self, plaintext: str) -> TokenRecord | None:
        """Найти токен по plaintext-ключу (sha256 → lookup, compare_digest).

        Проверки active/expires_at — задача auth (план §2.3: store хранит,
        auth проверяет). Возвращает запись или None.
        """
        if not plaintext:
            return None
        key_hash = _hash_key(plaintext)
        with self._lock:
            index = self._get_index_locked()
            record = index.get(key_hash)
            if record is None:
                return None
            # constant-time сравнение (паттерн auth.py)
            if hmac.compare_digest(record.key_hash, key_hash):
                return record
            return None

    def get(self, token_id: str) -> TokenRecord | None:
        with self._lock:
            record = self._get_by_id_locked().get(token_id)
            return record.model_copy(deep=True) if record else None

    def list(self) -> list[TokenRecord]:
        """Все записи (копии — защита от внешней мутации индекса)."""
        with self._lock:
            if self._index_expired():
                self._refresh_index_locked(self.load())
            records = self._records
            assert records is not None
            return [r.model_copy(deep=True) for r in records]

    # ── Создание (единая точка генерации формата) ────────────

    def create(
        self,
        level: str,
        zone: str,
        scope: list[str] | None = None,
        note: str = "",
        expires_at: datetime | None = None,
    ) -> tuple[str, str]:
        """Создать токен. Возвращает (token_id, plaintext_key) — ОДИН раз.

        Формат: mcp_<level_code><zone_code>_<secret32>.
        subscriber → zone принудительно "public" (префикс mcp_sa_).
        """
        if level not in LEVEL_CODES:
            raise ValueError(
                f"Unknown level: {level!r}. Expected one of {sorted(LEVEL_CODES)}",
            )
        if level == "subscriber":
            zone = "public"  # subscriber живёт только в контуре A (план §2.3)
        if zone not in ZONE_CODES:
            raise ValueError(
                f"Unknown zone: {zone!r}. Expected one of {sorted(ZONE_CODES)}",
            )

        secret = "".join(
            secrets.choice(_SECRET_ALPHABET) for _ in range(_SECRET_LEN)
        )
        plaintext = f"mcp_{LEVEL_CODES[level]}{ZONE_CODES[zone]}_{secret}"

        record = TokenRecord(
            id="tok_" + secrets.token_hex(8),
            key_hash=_hash_key(plaintext),
            level=level,
            zone=zone,
            scope=list(scope) if scope else None,
            active=True,
            expires_at=expires_at,
            note=note,
            source="manual",
            created_at=_now(),
        )
        with self._lock:
            records = self.load()
            records.append(record)
            self.save(records)
            self._refresh_index_locked(records)
        logger.info("Created token %s: level=%s zone=%s", record.id, level, zone)
        return record.id, plaintext

    # ── Управление жизненным циклом ──────────────────────────

    def set_active(self, token_id: str, active: bool) -> TokenRecord | None:
        """Включить/выключить токен (revoke → set_active(False))."""
        with self._lock:
            records = self.load()
            target = None
            for rec in records:
                if rec.id == token_id:
                    rec.active = active
                    target = rec
                    break
            if target is None:
                return None
            self.save(records)
            self._refresh_index_locked(records)
            return target.model_copy(deep=True)

    def revoke(self, token_id: str) -> TokenRecord | None:
        """Отозвать токен: active=False."""
        return self.set_active(token_id, False)

    def update_meta(
        self,
        token_id: str,
        note: str | None = None,
        expires_at=None,
        active: bool | None = None,
    ) -> TokenRecord | None:
        """Обновить note/expires_at/active (W5: PATCH /tokens/{id}).

        expires_at=None — очистить; expires_at=_UNSET — не трогать.
        Паттерн set_active: lock → load → mutate → save → refresh.
        """
        with self._lock:
            records = self.load()
            target = None
            for rec in records:
                if rec.id == token_id:
                    if note is not None:
                        rec.note = note
                    if expires_at is not _UNSET:
                        rec.expires_at = expires_at
                    if active is not None:
                        rec.active = active
                    target = rec
                    break
            if target is None:
                return None
            self.save(records)
            self._refresh_index_locked(records)
            return target.model_copy(deep=True)

    def touch_last_used(self, token_id: str) -> TokenRecord | None:
        """Обновить last_used_at (hot path).

        Дисковая запись — не чаще раза в TTL (TOKEN_INDEX_TTL_SEC):
        повторные touch в пределах окна обновляют только in-memory индекс.
        """
        now = _now()
        with self._lock:
            record = self._get_by_id_locked().get(token_id)
            if record is None:
                return None
            record.last_used_at = now
            if time.monotonic() - self._last_touch_ts < self._ttl_sec():
                return record.model_copy(deep=True)  # TTL-кэш: без записи
            self._last_touch_ts = time.monotonic()
            records = self.load()
            for rec in records:
                if rec.id == token_id:
                    rec.last_used_at = now
                    break
            self.save(records)
            self._refresh_index_locked(records)
            return record.model_copy(deep=True)

    def deactivate_stale(self, max_inactive_days: int = 90) -> list[str]:
        """Q9: авто-деактивация неактивных subscriber-токенов.

        Только level=="subscriber", только active, НЕ source=="env".
        База неактивности: last_used_at, если None → created_at.
        Возвращает список deactivated id.
        """
        now = _now()
        threshold = now - timedelta(days=max_inactive_days)
        deactivated: list[str] = []
        with self._lock:
            records = self.load()
            changed = False
            for rec in records:
                if rec.level != "subscriber" or not rec.active or rec.source == "env":
                    continue
                base = rec.last_used_at or rec.created_at
                if base < threshold:
                    rec.active = False
                    rec.note = "deactivated: inactive >90d"
                    deactivated.append(rec.id)
                    changed = True
            if changed:
                self.save(records)
                self._refresh_index_locked(records)
        if deactivated:
            logger.info(
                "Deactivated %d stale subscriber token(s): %s",
                len(deactivated), deactivated,
            )
        return deactivated

    def is_expiring_soon(self, token_id: str) -> bool:
        """expires_at установлен и ≤ EXPIRING_SOON_DAYS от now (окно Q9)."""
        record = self.get(token_id)
        if record is None or record.expires_at is None:
            return False
        return record.expires_at <= _now() + timedelta(days=EXPIRING_SOON_DAYS)

    # ── Scope (W6 будет использовать) ────────────────────────

    def grant_scope(
        self, token_id: str, knowledge_ids: list[str],
    ) -> TokenRecord | None:
        """Выдать scope-grants (дополняет существующие, без дублей)."""
        with self._lock:
            records = self.load()
            target = None
            for rec in records:
                if rec.id == token_id:
                    merged = list(dict.fromkeys((rec.scope or []) + list(knowledge_ids)))
                    rec.scope = merged
                    target = rec
                    break
            if target is None:
                return None
            self.save(records)
            self._refresh_index_locked(records)
            return target.model_copy(deep=True)

    def revoke_scope(self, token_id: str) -> TokenRecord | None:
        """Снять все scope-grants токена (scope=None → «вся зона»)."""
        with self._lock:
            records = self.load()
            target = None
            for rec in records:
                if rec.id == token_id:
                    rec.scope = None
                    target = rec
                    break
            if target is None:
                return None
            self.save(records)
            self._refresh_index_locked(records)
            return target.model_copy(deep=True)

    # ── Bootstrap (R2): env-ключи → SSOT ─────────────────────

    def seed_from_env(self, env_keys: dict[str, list[str] | str]) -> int:
        """Сид env-ключей в store, идемпотентно по key_hash.

        env_keys: {"read": [...], "import": [...], "write": [...]}
        (config.py MCP_READ_KEYS / MCP_IMPORT_KEYS / MCP_WRITE_KEYS).
        Запись: level=<уровень>, zone="both", source="env".
        Пустые/повторные ключи пропускаются. Возвращает число добавленных.
        """
        added = 0
        with self._lock:
            records = self.load()
            existing_hashes = {rec.key_hash for rec in records}
            created_at = _now()
            for level in ("read", "import", "write"):
                keys = env_keys.get(level) or []
                if isinstance(keys, str):
                    keys = [keys]
                for key in keys:
                    key = (key or "").strip()
                    if not key:
                        continue
                    key_hash = _hash_key(key)
                    if key_hash in existing_hashes:
                        continue  # идемпотентность по key_hash
                    records.append(TokenRecord(
                        id="tok_" + secrets.token_hex(8),
                        key_hash=key_hash,
                        level=level,
                        zone="both",
                        source="env",
                        created_at=created_at,
                    ))
                    existing_hashes.add(key_hash)
                    added += 1
            if added:
                self.save(records)
                self._refresh_index_locked(records)
        if added:
            logger.info("Seeded %d env token(s) into token store", added)
        return added
