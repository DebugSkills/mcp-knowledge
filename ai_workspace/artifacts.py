"""Artifact-store AI-верстака: готовая работа (deliverable) ≠ знание (Ф3.7, I13).

Трёхслойная модель жизненного цикла (решение оператора): **процесс** (чат/доска — эфемерно),
**артефакт** (готовая статья — здесь, per-user, без поиска, retention 30 дней),
**знание** (KB — только явный `promote` человеком, I13: KB сгенерированным не засоряем).

Ключи: ``ws:artifact:{user}:{id}`` HASH (метаданные + content, TTL = retention),
``ws:artifacts:{user}`` ZSET (score = expires_at, для листинга и prune).
``id = sha256(content)[:16]`` → повторное сохранение того же текста идемпотентно (дедуп).

``promote`` — ЕДИНСТВЕННЫЙ путь из artifact-store в KB и только по явному вызову:
требует явных ``domain``/``subject``/``zone``, тело документа уходит в ``mcp.write_knowledge``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ai_workspace.redis_client import make_ws_redis

__all__ = [
    "RETENTION_SECONDS",
    "ArtifactError",
    "ArtifactNotFound",
    "ArtifactRecord",
    "ArtifactStore",
    "MemoryBackend",
    "RedisBackend",
    "artifact_store",
]

RETENTION_SECONDS = 30 * 24 * 3600
"""Срок жизни артефакта (решение оператора: retention ~месяц)."""


class ArtifactError(RuntimeError):
    """Базовая ошибка artifact-store."""


class ArtifactNotFound(ArtifactError):
    """Артефакт отсутствует или истёк (retention)."""


@dataclass(frozen=True)
class ArtifactRecord:
    """Метаданные артефакта (content — отдельно, ``ArtifactStore.get_content``)."""

    id: str
    user: str
    job_id: str
    type: str
    zone: str
    title: str
    created: str
    expires_at: int
    size: int
    sha256: str
    mode: str = ""
    export_path: str = ""


class ArtifactBackend(Protocol):
    """Минимальный KV+ZSET контракт (Redis в проде, память в тестах)."""

    def hset_ttl(self, key: str, mapping: Mapping[str, str], ttl: int) -> None: ...
    def hgetall(self, key: str) -> dict[str, str] | None: ...
    def delete(self, *keys: str) -> None: ...
    def zadd(self, zkey: str, score: float, member: str) -> None: ...
    def zrange(self, zkey: str, start: int, stop: int) -> list[str]: ...
    def zrem(self, zkey: str, member: str) -> None: ...
    def zremrangebyscore(self, zkey: str, lo: float, hi: float) -> int: ...


class MemoryBackend:
    """In-memory backend с честным TTL по инъектируемым часам (offline-тесты)."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.hash: dict[str, tuple[dict[str, str], float]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def hset_ttl(self, key: str, mapping: Mapping[str, str], ttl: int) -> None:
        self.hash[key] = ({str(k): str(v) for k, v in mapping.items()}, self.clock() + ttl)

    def hgetall(self, key: str) -> dict[str, str] | None:
        item = self.hash.get(key)
        if item is None:
            return None
        data, expires_at = item
        if expires_at <= self.clock():
            self.hash.pop(key, None)
            return None
        return dict(data)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.hash.pop(key, None)
            self.zsets.pop(key, None)

    def zadd(self, zkey: str, score: float, member: str) -> None:
        self.zsets.setdefault(zkey, {})[member] = float(score)

    def zrange(self, zkey: str, start: int, stop: int) -> list[str]:
        members = sorted(self.zsets.get(zkey, {}).items(), key=lambda kv: (kv[1], kv[0]))
        names = [m for m, _ in members]
        return names[start : stop + 1]

    def zrem(self, zkey: str, member: str) -> None:
        self.zsets.get(zkey, {}).pop(member, None)

    def zremrangebyscore(self, zkey: str, lo: float, hi: float) -> int:
        zset = self.zsets.get(zkey, {})
        gone = [m for m, s in zset.items() if lo <= s <= hi]
        for m in gone:
            zset.pop(m, None)
        return len(gone)


class RedisBackend:
    """Backend на ws-redis (HSET+EXPIRE, ZADD; decode_responses=True)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def hset_ttl(self, key: str, mapping: Mapping[str, str], ttl: int) -> None:
        self.client.hset(key, mapping={str(k): str(v) for k, v in mapping.items()})
        self.client.expire(key, ttl)

    def hgetall(self, key: str) -> dict[str, str] | None:
        data = self.client.hgetall(key)
        return data or None

    def delete(self, *keys: str) -> None:
        if keys:
            self.client.delete(*keys)

    def zadd(self, zkey: str, score: float, member: str) -> None:
        self.client.zadd(zkey, {member: float(score)})

    def zrange(self, zkey: str, start: int, stop: int) -> list[str]:
        return [str(m) for m in self.client.zrange(zkey, start, stop)]

    def zrem(self, zkey: str, member: str) -> None:
        self.client.zrem(zkey, member)

    def zremrangebyscore(self, zkey: str, lo: float, hi: float) -> int:
        return int(self.client.zremrangebyscore(zkey, lo, hi))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactStore:
    """Хранилище готовых работ: retention, листинг, экспорт, promote→KB (I13)."""

    def __init__(
        self,
        backend: ArtifactBackend,
        *,
        clock: Callable[[], float] = time.time,
        retention: int = RETENTION_SECONDS,
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.retention = retention

    # ── ключи ────────────────────────────────────────────────────────────

    @staticmethod
    def _key(user: str, artifact_id: str) -> str:
        return f"ws:artifact:{user}:{artifact_id}"

    @staticmethod
    def _index(user: str) -> str:
        return f"ws:artifacts:{user}"

    # ── запись/чтение ────────────────────────────────────────────────────

    def save(
        self,
        content: str,
        *,
        user: str,
        job_id: str = "",
        type: str = "document",
        zone: str = "public",
        title: str = "",
        mode: str = "",
        ttl: int | None = None,
        export_path: str = "",
    ) -> ArtifactRecord:
        """Сохранить артефакт (idempotent по содержимому: id = sha256[:16])."""
        if not content:
            raise ArtifactError("пустой артефакт сохранять нельзя")
        digest = hashlib.sha256(content.encode()).hexdigest()
        artifact_id = digest[:16]
        ttl = self.retention if ttl is None else int(ttl)
        now = _utcnow()
        expires_at = int(self.clock()) + ttl
        record = ArtifactRecord(
            id=artifact_id,
            user=user,
            job_id=job_id,
            type=type,
            zone=zone,
            title=title or f"{type}:{job_id or artifact_id}",
            created=now.isoformat(timespec="milliseconds"),
            expires_at=expires_at,
            size=len(content.encode()),
            sha256=digest,
            mode=mode,
            export_path=export_path,
        )
        self.backend.hset_ttl(
            self._key(user, artifact_id),
            {**{k: str(v) for k, v in record.__dict__.items()}, "content": content},
            ttl,
        )
        self.backend.zadd(self._index(user), expires_at, artifact_id)
        return record

    def get(self, user: str, artifact_id: str) -> ArtifactRecord:
        """Метаданные артефакта; нет/истёк → ``ArtifactNotFound``."""
        data = self.backend.hgetall(self._key(user, artifact_id))
        if not data:
            raise ArtifactNotFound(f"артефакт {artifact_id!r} пользователя {user!r} не найден или истёк")
        return self._to_record(data)

    def get_content(self, user: str, artifact_id: str) -> str:
        """Тело артефакта (Markdown)."""
        data = self.backend.hgetall(self._key(user, artifact_id))
        if not data:
            raise ArtifactNotFound(f"артефакт {artifact_id!r} не найден или истёк")
        return data.get("content", "")

    def list(self, user: str, *, limit: int = 50) -> list[ArtifactRecord]:
        """Артефакты пользователя, свежие первыми (просроченные вычищаются из индекса)."""
        index = self._index(user)
        self.backend.zremrangebyscore(index, 0, self.clock())
        ids = self.backend.zrange(index, 0, max(limit * 2, limit) - 1)
        records: list[ArtifactRecord] = []
        for artifact_id in ids:
            data = self.backend.hgetall(self._key(user, artifact_id))
            if data:
                records.append(self._to_record(data))
        records.sort(key=lambda r: r.created, reverse=True)
        return records[:limit]

    def delete(self, user: str, artifact_id: str) -> None:
        """Удалить артефакт досрочно (обратимо только через повторный save)."""
        self.backend.delete(self._key(user, artifact_id))
        self.backend.zrem(self._index(user), artifact_id)

    def prune(self, user: str | None = None, *, users: list[str] | None = None) -> int:
        """Убрать истёкшие записи индекса (сам HASH гасит TTL Redis)."""
        targets = [user] if user else list(users or [])
        total = 0
        for target in targets:
            total += int(self.backend.zremrangebyscore(self._index(target), 0, self.clock()))
        return total

    # ── экспорт и promote ────────────────────────────────────────────────

    def export(self, user: str, artifact_id: str, dest_dir: str | Path) -> Path:
        """Выгрузить артефакт в файл ``<dest>/<id>-<title>.md`` (deliverable)."""
        record = self.get(user, artifact_id)
        content = self.get_content(user, artifact_id)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in record.title)[:60]
        path = dest / f"{record.id}-{slug}.md"
        path.write_text(content, encoding="utf-8")
        self.backend.hset_ttl(
            self._key(user, artifact_id),
            {"export_path": str(path)},
            self._remaining_ttl(record),
        )
        return path

    def promote(
        self,
        user: str,
        artifact_id: str,
        *,
        kb: Any,
        domain: str,
        subject: str,
        zone: str,
        tags: list[str] | None = None,
        project: str | None = None,
    ) -> Any:
        """Явный (и единственный) путь artifact → KB через ``mcp.write_knowledge`` (I13).

        Требует явных ``domain``/``subject``/``zone`` — никаких дефолтов: промоушен
        в общую базу знаний всегда осознанное действие человека.
        """
        if not domain or not subject or zone not in {"public", "private"}:
            raise ArtifactError("promote требует явных domain/subject и zone ∈ {public, private}")
        record = self.get(user, artifact_id)
        content = self.get_content(user, artifact_id)
        args: dict[str, Any] = {
            "content": content,
            "domain": domain,
            "subject": subject,
            "zone": zone,
            "tags": sorted({*(tags or []), "artifact", record.type}),
        }
        if project:
            args["project"] = project
        return kb.call(tool="mcp.write_knowledge", args=args)

    # ── внутреннее ───────────────────────────────────────────────────────

    def _remaining_ttl(self, record: ArtifactRecord) -> int:
        return max(int(record.expires_at - self.clock()), 1)

    @staticmethod
    def _to_record(data: Mapping[str, str]) -> ArtifactRecord:
        def field(name: str, default: str = "") -> str:
            return str(data.get(name, default))

        return ArtifactRecord(
            id=field("id"),
            user=field("user"),
            job_id=field("job_id"),
            type=field("type"),
            zone=field("zone"),
            title=field("title"),
            created=field("created"),
            expires_at=int(data.get("expires_at") or 0),
            size=int(data.get("size") or 0),
            sha256=field("sha256"),
            mode=field("mode"),
            export_path=field("export_path"),
        )


def artifact_store(*, client: Any | None = None) -> ArtifactStore:
    """ArtifactStore на ws-redis (env ``WS_REDIS_URL``)."""
    return ArtifactStore(RedisBackend(client or make_ws_redis()))
