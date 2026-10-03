"""Content-addressed blob-store + rebuildable SQLite/WAL-реестр (Фаза 1, bibliography).

Контракт (план §3.3):
- Каталог `data/documents/ab/cd/<sha256-64>` — shard = hex-символы 0-1 и 2-3.
- `put(stream|bytes)` → полный sha256 при записи → `tmp/<uuid>.part` → `os.replace` → fsync.
- Дедуп: существующий sha256 → no-op (без перезаписи provenance).
- Квота `DOCUMENTS_STORE_MAX_GB` — проверка ДО записи (Σ физических sha256).
- Реестр `registry.db` (SQLite WAL): `blobs(sha256 PK, role, size, mime,
  original_filename, created_at, derived_from, tool, tool_version)`.
  **Schema-запрет** zone/status/ref_count/refs — инвариант (проверяется тестом).
- Rebuildable: rescan FS (sha256/size) + Source SSOT (mime/filename/provenance).
- `VACUUM INTO` для бэкапа.

Async-инвариант: все методы СИНХРОННЫЕ (блокирующие I/O) — вызывающий код
оборачивает в `run_in_executor` (single-worker кодовая база, `config.WORKERS=1`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("mcp_knowledge.document_store")

# Файлы, которые НЕ являются блобами (служебные) — исключаются из скана FS.
_RESERVED_NAMES = {"registry.db", "registry.db-wal", "registry.db-shm"}

# Guard-значения для blob-без-Source при rebuild (план §3.3).
GUARD_MIME = "application/pdf"


class QuotaExceededError(Exception):
    """Квота стора превышена — отказ ДО записи (original живёт, canonical отброшен)."""

    def __init__(self, requested: int, current: int, limit: int):
        self.requested = requested
        self.current = current
        self.limit = limit
        super().__init__(
            f"Document store quota exceeded: {current} + {requested} > {limit} bytes"
        )


class InvalidSha256Error(ValueError):
    """Невалидный sha256 (не 64 hex-символа) — защита oracle-имени (§7 R7)."""


def _validate_sha256(sha256: str) -> str:
    """Проверить sha256: ровно 64 hex-символа (lowercase)."""
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise InvalidSha256Error(f"invalid sha256 length: {len(sha256) if isinstance(sha256, str) else 'n/a'}")
    if not all(c in "0123456789abcdef" for c in sha256):
        raise InvalidSha256Error("sha256 contains non-hex characters")
    return sha256


@dataclass
class BlobInfo:
    """Строка реестра (одна на физический sha256)."""

    sha256: str
    role: str = "original"
    size: int = 0
    mime: str | None = None
    original_filename: str | None = None
    created_at: str | None = None
    derived_from: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    params_hash: str | None = None


@dataclass
class PutResult:
    """Результат put: полный sha256 + флаг «уже существовал» (дедуп)."""

    sha256: str
    size: int
    role: str
    deduplicated: bool = False
    existed: bool = False

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "role": self.role,
            "deduplicated": self.deduplicated,
            "existed": self.existed,
        }


class DocumentStore:
    """Content-addressed blob-store с SQLite/WAL-реестром (rebuildable)."""

    def __init__(self, documents_dir: str | Path, max_gb: int = 10):
        self._root = Path(documents_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._tmp = self._root / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._root / "registry.db"
        self._max_bytes = int(max_gb) * 1024 ** 3
        self._init_registry()

    # ── Registry ────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Новое соединение на операцию (thread-safe, single-worker + executor)."""
        conn = sqlite3.connect(str(self._registry_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_registry(self) -> None:
        """Создать таблицы + включить WAL (идемпотентно)."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256           TEXT PRIMARY KEY,
                    role             TEXT NOT NULL DEFAULT 'original',
                    size             INTEGER NOT NULL DEFAULT 0,
                    mime             TEXT,
                    original_filename TEXT,
                    created_at       TEXT,
                    derived_from     TEXT,
                    tool             TEXT,
                    tool_version     TEXT,
                    params_hash      TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS canonicalization_jobs (
                    sha256     TEXT PRIMARY KEY,
                    format     TEXT,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    error      TEXT,
                    attempts   INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

    # ── Put / Get / Exists ──────────────────────────────────

    def put(
        self,
        data: bytes,
        mime: str | None = None,
        filename: str | None = None,
        role: str = "original",
        derived_from: str | None = None,
        tool: str | None = None,
        tool_version: str | None = None,
        params_hash: str | None = None,
    ) -> PutResult:
        """Записать blob (байты) content-addressed.

        Дедуп: существующий sha256 → no-op (возвращает существующий, provenance не трогается).
        Квота: проверка ДО записи (Σ физических sha256 + новый, если не дедуп).
        """
        sha256 = hashlib.sha256(data).hexdigest()
        return self._put_bytes(sha256, data, mime=mime, filename=filename,
                               role=role, derived_from=derived_from,
                               tool=tool, tool_version=tool_version,
                               params_hash=params_hash)

    def put_file(
        self,
        path: str | Path,
        mime: str | None = None,
        filename: str | None = None,
        role: str = "original",
        derived_from: str | None = None,
        tool: str | None = None,
        tool_version: str | None = None,
        params_hash: str | None = None,
    ) -> PutResult:
        """Записать blob из файла (стриминг, полный sha256 при чтении)."""
        path = Path(path)
        sha = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
                size += len(chunk)
        sha256 = sha.hexdigest()

        # Дедуп/квота ДО перечтения
        existing = self.info(sha256)
        if existing is not None:
            return PutResult(sha256=sha256, size=existing.size, role=existing.role,
                             deduplicated=True, existed=True)

        self._quota_check(size, sha256)
        dest = self._shard_path(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp, "wb") as out:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
            self._fsync_dir(dest.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

        self._upsert_blob(sha256, size, mime, filename or path.name, role,
                          derived_from, tool, tool_version, params_hash)
        return PutResult(sha256=sha256, size=size, role=role,
                         deduplicated=False, existed=False)

    def _put_bytes(self, sha256: str, data: bytes, **kw) -> PutResult:
        """Внутренний put для уже вычисленного sha256 (bytes в памяти)."""
        size = len(data)
        existing = self.info(sha256)
        if existing is not None:
            return PutResult(sha256=sha256, size=existing.size, role=existing.role,
                             deduplicated=True, existed=True)

        self._quota_check(size, sha256)
        dest = self._shard_path(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
            self._fsync_dir(dest.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

        self._upsert_blob(
            sha256, size,
            kw.get("mime"), kw.get("filename"), kw.get("role", "original"),
            kw.get("derived_from"), kw.get("tool"), kw.get("tool_version"),
            kw.get("params_hash"),
        )
        return PutResult(sha256=sha256, size=size, role=kw.get("role", "original"),
                         deduplicated=False, existed=False)

    def get(self, sha256: str) -> bytes | None:
        """Прочитать blob по полному sha256 (None если отсутствует)."""
        _validate_sha256(sha256)
        path = self._shard_path(sha256)
        if not path.exists():
            return None
        return path.read_bytes()

    def exists(self, sha256: str) -> bool:
        """Blob существует физически (реестр + fs)."""
        try:
            _validate_sha256(sha256)
        except InvalidSha256Error:
            return False
        return self._shard_path(sha256).exists()

    def open_blob(self, sha256: str):
        """Открыть blob для потоковой отдачи (binary file-object; None если нет).

        Ф4a (GET /documents): стриминг без чтения всего blob в память.
        Вызывающий код ЗАКРЫВАЕТ file-object (async-итератор роута).
        """
        _validate_sha256(sha256)
        path = self._shard_path(sha256)
        if not path.exists():
            return None
        return path.open("rb")

    def blob_size(self, sha256: str) -> int | None:
        """Размер blob по FS (None если отсутствует) — Content-Length/Range."""
        _validate_sha256(sha256)
        try:
            return self._shard_path(sha256).stat().st_size
        except FileNotFoundError:
            return None

    def blob_mtime(self, sha256: str) -> float | None:
        """mtime блоба (epoch seconds; None если файла нет) — GC grace-возраст.

        Фактический источник времени блоба — FS mtime (наиболее правдивый:
        не зависит от rebuild-перезаписи реестра). Синхронный — звать из executor.
        """
        _validate_sha256(sha256)
        try:
            return self._shard_path(sha256).stat().st_mtime
        except FileNotFoundError:
            return None

    def verify(self, sha256: str) -> bool:
        """Перечитать blob и сверить sha256 (integrity)."""
        try:
            data = self.get(sha256)
        except InvalidSha256Error:
            return False
        if data is None:
            return False
        return hashlib.sha256(data).hexdigest() == sha256

    def delete_blob_direct(self, sha256: str) -> bool:
        """Удалить blob (файл + строка реестра) — GC orphan-sweep (Ф5b3).

        Минимальный API удаления (raw rm запрещён): validate → unlink shard →
        DELETE строки реестра. Идемпотентно: нет файла → False (строку всё
        равно чистим). Синхронный — звать из executor.
        """
        _validate_sha256(sha256)
        path = self._shard_path(sha256)
        existed = path.exists()
        try:
            if existed:
                path.unlink()
        finally:
            with self._connect() as conn:
                conn.execute("DELETE FROM blobs WHERE sha256 = ?", (sha256,))
        return existed

    def info(self, sha256: str) -> BlobInfo | None:
        """Метаданные блоба из реестра (None если нет строки)."""
        try:
            _validate_sha256(sha256)
        except InvalidSha256Error:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM blobs WHERE sha256 = ?", (sha256,)
            ).fetchone()
        if row is None:
            return None
        return BlobInfo(
            sha256=row["sha256"], role=row["role"], size=row["size"],
            mime=row["mime"], original_filename=row["original_filename"],
            created_at=row["created_at"], derived_from=row["derived_from"],
            tool=row["tool"], tool_version=row["tool_version"],
            params_hash=row["params_hash"],
        )

    def list_blobs(self) -> list[BlobInfo]:
        """Все строки реестра."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM blobs ORDER BY sha256").fetchall()
        return [
            BlobInfo(
                sha256=r["sha256"], role=r["role"], size=r["size"],
                mime=r["mime"], original_filename=r["original_filename"],
                created_at=r["created_at"], derived_from=r["derived_from"],
                tool=r["tool"], tool_version=r["tool_version"],
                params_hash=r["params_hash"],
            )
            for r in rows
        ]

    # ── Quota / size ────────────────────────────────────────

    def total_bytes(self) -> int:
        """Σ size по физическим sha256 (дедуп уже учтён в реестре)."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(size), 0) AS total FROM blobs").fetchone()
        return int(row["total"])

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM blobs").fetchone()
        return int(row["c"])

    @property
    def max_bytes(self) -> int:
        """Квота стора в байтах (max_gb × 1024³) — для статистики documents_stats."""
        return self._max_bytes

    def _quota_check(self, extra_bytes: int, sha256: str) -> None:
        """Отказ ДО записи при превышении квоты (existing sha не считается дважды)."""
        current = self.total_bytes()
        if current + extra_bytes > self._max_bytes:
            # Ф3c3 (G7): counter отказов по квоте. Lazy-import — storage не зависит
            # от observability на уровне модулей; fail-safe: сбой метрики не
            # меняет поведение квоты (отказ всё равно бросается).
            try:
                from ..metrics import documents_quota_exceeded

                documents_quota_exceeded.inc()
            except Exception:
                pass
            raise QuotaExceededError(extra_bytes, current, self._max_bytes)

    # ── Rebuild ─────────────────────────────────────────────

    def rebuild(self, sources: Iterable[dict] | None = None) -> dict:
        """Перестроить реестр из FS + Source SSOT.

        FS = истина по физическим блобам (sha256 из имени файла, size из stat).
        SSOT = метаданные (mime/filename/role/provenance) из frontmatter Source.
        Blob в FS без Source → guard-строка (mime=application/pdf, filename=<sha256>).
        Blob в SSOT без FS → не попадает в реестр (реестр = физическая правда).

        Returns:
            {blobs, with_source_meta, orphans, elapsed_sec}
        """
        import time as _time
        t0 = _time.monotonic()

        # 1. Скан FS
        fs_blobs: dict[str, int] = {}
        for path in self._iter_blob_files():
            fs_blobs[path.name] = path.stat().st_size

        # 2. Метаданные из SSOT
        meta: dict[str, dict] = {}
        for src in (sources or []):
            blobs = src.get("blobs") or {}
            original = blobs.get("original") or {}
            canonical = blobs.get("canonical") or {}
            orig_sha = original.get("sha256") if isinstance(original, dict) else None
            if orig_sha:
                meta[orig_sha] = {
                    "role": "original",
                    "mime": original.get("mime"),
                    "original_filename": original.get("original_filename"),
                    "derived_from": None,
                    "tool": None,
                    "tool_version": None,
                    "params_hash": None,
                }
            canon_sha = canonical.get("sha256") if isinstance(canonical, dict) else None
            # P2-2 (critic, зонд B): PDF canonical≡original — НЕ перетирать original-строку
            # (иначе role→canonical, filename теряется). Отдельная строка только для distinct sha.
            if canon_sha and canon_sha != orig_sha:
                meta[canon_sha] = {
                    "role": "canonical",
                    "mime": canonical.get("mime") or "application/pdf",
                    "original_filename": canonical.get("original_filename"),
                    "derived_from": canonical.get("derived_from"),
                    "tool": canonical.get("tool"),
                    "tool_version": canonical.get("tool_version"),
                    "params_hash": canonical.get("params_hash"),
                }
            for derived in (blobs.get("derived") or []):
                if isinstance(derived, dict) and derived.get("sha256"):
                    meta[derived["sha256"]] = {
                        "role": derived.get("role", "derived"),
                        "mime": derived.get("mime"),
                        "original_filename": derived.get("original_filename"),
                        "derived_from": derived.get("derived_from"),
                        "tool": derived.get("tool"),
                        "tool_version": derived.get("tool_version"),
                        "params_hash": derived.get("params_hash"),
                    }

        # 3. Перезапись реестра
        now = datetime.now(timezone.utc).isoformat()
        with_source_meta = 0
        orphans = 0
        with self._connect() as conn:
            conn.execute("DELETE FROM blobs")
            for sha256, size in sorted(fs_blobs.items()):
                m = meta.get(sha256)
                if m is not None:
                    with_source_meta += 1
                    conn.execute(
                        "INSERT INTO blobs (sha256, role, size, mime, original_filename, "
                        "created_at, derived_from, tool, tool_version, params_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sha256, m["role"], size, m["mime"], m["original_filename"],
                         now, m["derived_from"], m["tool"], m["tool_version"], m["params_hash"]),
                    )
                else:
                    orphans += 1
                    conn.execute(
                        "INSERT INTO blobs (sha256, role, size, mime, original_filename, "
                        "created_at) VALUES (?, 'original', ?, ?, ?, ?)",
                        (sha256, size, GUARD_MIME, sha256, now),
                    )

        elapsed = _time.monotonic() - t0
        return {
            "blobs": len(fs_blobs),
            "with_source_meta": with_source_meta,
            "orphans": orphans,
            "elapsed_sec": round(elapsed, 3),
        }

    # ── Backup / maintenance ────────────────────────────────

    def vacuum_into(self, dest: str | Path) -> Path:
        """VACUUM INTO — консистентный бэкап реестра (паттерн console-state)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("VACUUM INTO ?", (str(dest),))
        return dest

    # ── Canonicalization jobs (operational, НЕ SSOT) ────────

    def upsert_job(self, sha256: str, format: str, status: str,
                   error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO canonicalization_jobs (sha256, format, status, error, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sha256) DO UPDATE SET status=excluded.status, "
                "error=excluded.error, updated_at=excluded.updated_at",
                (sha256, format, status, error, now, now),
            )

    def job_status(self, sha256: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM canonicalization_jobs WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return dict(row) if row else None

    def prune_jobs(self, retention_days: int) -> int:
        """Ретеншн завершённых canonicalization-jobs (Ф3c2c).

        Удаляет done/failed джобы старше retention_days от ЗАВЕРШЕНИЯ:
        cutoff-поле COALESCE(updated_at, created_at) — upsert_job пишет
        datetime.now(timezone.utc).isoformat() (fixed-offset +00:00), ISO-строки
        лексикографически хронологичны → строковое сравнение валидно.
        running/pending НЕ удаляются (активные джобы бессрочно живы).
        Возвращает число удалённых строк (rowcount).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM canonicalization_jobs "
                "WHERE status IN ('done', 'failed') "
                "AND COALESCE(updated_at, created_at) < ?",
                (cutoff,),
            )
            return cur.rowcount

    def job_counts(self) -> dict[str, int]:
        """Счётчики canonicalization_jobs по статусам (метрики Ф3c3).

        Returns:
            {status: count} — только ненулевые статусы (done/failed/running/pending).
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM canonicalization_jobs GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["c"]) for row in rows}

    # ── Internal ────────────────────────────────────────────

    def _shard_path(self, sha256: str) -> Path:
        _validate_sha256(sha256)
        return self._root / sha256[0:2] / sha256[2:4] / sha256

    def _iter_blob_files(self) -> Iterable[Path]:
        """Все физические блобы (исключая tmp/, registry.db и shard-каталоги)."""
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            if path.name in _RESERVED_NAMES:
                continue
            if self._tmp in path.parents:
                continue
            yield path

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """fsync каталога после rename — гарантия долговечности (durability)."""
        try:
            fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            logger.debug("fsync dir failed (non-fatal): %s", directory)

    def _upsert_blob(self, sha256: str, size: int, mime: str | None,
                     filename: str | None, role: str, derived_from: str | None,
                     tool: str | None, tool_version: str | None,
                     params_hash: str | None) -> None:
        """INSERT (не REPLACE) — дедуп гарантирует отсутствие строки заранее."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blobs (sha256, role, size, mime, original_filename, "
                "created_at, derived_from, tool, tool_version, params_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha256, role, size, mime, filename, now, derived_from, tool, tool_version, params_hash),
            )
