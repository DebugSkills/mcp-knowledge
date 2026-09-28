"""SQLite-стор заявок на доступ (036, план §2; ПДн 152-ФЗ §4).

Дизайн-решения оператора (Q1=B, план 036 §2):
- **SQLite** (JSONL отклонён); файл `data/console/access_requests.db`,
  права 0600 / каталог 0700 (паттерн storage_secret);
- `journal_mode=DELETE` — один файл, нет `-wal`/`-shm`, tar-дружелюбно
  (фиксируется PRAGMA при каждом открытии — R10);
- `auto_vacuum=INCREMENTAL` + `PRAGMA incremental_vacuum` после
  retention-удалений (ПДн-гигиена файла, §2.6);
- миграции через `PRAGMA user_version` (0→1 идемпотентный DDL; >1 →
  fail-fast RuntimeError, без порчи — §2.3);
- per-call соединение + busy_timeout=5000 + module-RLock вокруг write
  (однопроцессный uvicorn, WORKERS=1 — §2.4); короткие транзакции;
- dupe_hash = sha256(norm(fio)|norm(phone)), окно 10 мин → 409 (R6);
- ПДн НЕ логируются: в исключениях только `type(exc).__name__`/id/ip/статус.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_SCHEMA_VERSION = 1
_DDL = """
CREATE TABLE IF NOT EXISTS access_requests (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  fio TEXT NOT NULL,
  department TEXT NOT NULL,
  phone TEXT NOT NULL,
  email TEXT NOT NULL,
  work_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  decision_note TEXT NOT NULL DEFAULT '',
  decided_at TEXT,
  decided_by TEXT,
  dupe_hash TEXT NOT NULL,
  consent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ar_status ON access_requests(status);
CREATE INDEX IF NOT EXISTS idx_ar_created ON access_requests(created_at);
CREATE INDEX IF NOT EXISTS idx_ar_dupe ON access_requests(dupe_hash, created_at);
CREATE TABLE IF NOT EXISTS access_request_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_are_req ON access_request_events(request_id, ts);
"""

STATUSES = ("new", "in_progress", "access_granted", "rejected")
TERMINAL_STATUSES = ("access_granted", "rejected")

_WRITE_LOCK = threading.RLock()
"""Детерминизм тестов/серийных write в одном процессе (§2.4)."""


class AccessRequestError(Exception):
    """Доменные ошибки стора (код → HTTP на уровне endpoint)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AccessRequest:
    id: str
    created_at: str
    fio: str
    department: str
    phone: str
    email: str
    work_summary: str
    status: str
    decision_note: str
    decided_at: str | None
    decided_by: str | None
    dupe_hash: str
    consent_at: str


def _now(clock: Callable[[], datetime]) -> datetime:
    return clock()


def normalize_for_dupe(value: str) -> str:
    """Нормализация для dupe-хэша: нижний регистр, схлопнутые пробелы."""
    return re.sub(r"\s+", " ", value.strip().lower())


def compute_dupe_hash(fio: str, phone: str) -> str:
    return hashlib.sha256(
        f"{normalize_for_dupe(fio)}|{normalize_for_dupe(phone)}".encode()
    ).hexdigest()


def _row_to_request(row: sqlite3.Row) -> AccessRequest:
    return AccessRequest(
        id=row["id"],
        created_at=row["created_at"],
        fio=row["fio"],
        department=row["department"],
        phone=row["phone"],
        email=row["email"],
        work_summary=row["work_summary"],
        status=row["status"],
        decision_note=row["decision_note"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        dupe_hash=row["dupe_hash"],
        consent_at=row["consent_at"],
    )


class AccessRequestStore:
    """Заявки на доступ: SQLite, per-call conn, миграции, prune+cap.

    clock инъекцируется (тесты retention/dupe-окна без sleep).
    """

    def __init__(
        self,
        db_path: str,
        *,
        cap: int = 500,
        retention_days: int = 180,
        dupe_window_sec: int = 600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = db_path
        self._cap = cap
        self._retention_days = retention_days
        self._dupe_window = dupe_window_sec
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── соединение/миграции ──

    def _connect(self) -> sqlite3.Connection:
        first_creation = not os.path.exists(self._path)
        directory = os.path.dirname(self._path) or "."
        if first_creation:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=DELETE")  # §2.1/R10: при каждом открытии
        if first_creation:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass  # права的最佳-усилие (tmp-маунты); тест ловит на реальном fs
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            conn.close()
            raise RuntimeError(
                "access_requests.db создана новой версией kb-console — "
                "обновите консоль или используйте прежний файл"
            )
        if version == _SCHEMA_VERSION:
            return
        # auto_vacuum задаётся до первой таблицы (§2.6)
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.executescript(_DDL)
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        conn.commit()

    def migrate(self) -> None:
        """Идемпотентная инициализация схемы; версия > поддерживаемой —
        fail-fast при каждом вызове (не только первом: БД могли подменить)."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
        finally:
            conn.close()

    # ── API ──

    def append(
        self,
        *,
        fio: str,
        department: str,
        phone: str,
        email: str,
        work_summary: str,
    ) -> AccessRequest:
        """Сохранить заявку. Порядок (план §3): prune → cap → dupe → INSERT."""
        self.migrate()
        now = _now(self._clock)
        created = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        dupe = compute_dupe_hash(fio, phone)
        rid = "req_" + hashlib.sha256(
            (created + os.urandom(8).hex()).encode("utf-8")
        ).hexdigest()[:8]
        with _WRITE_LOCK:
            conn = self._connect()
            try:
                self._prune_locked(conn, now)
                total = conn.execute("SELECT count(*) FROM access_requests").fetchone()[0]
                if total >= self._cap:
                    raise AccessRequestError("cap_exceeded", "store is full")
                cutoff = (now - timedelta(seconds=self._dupe_window)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f"
                )[:-3] + "Z"
                dup = conn.execute(
                    "SELECT id FROM access_requests WHERE dupe_hash=? AND created_at>=?",
                    (dupe, cutoff),
                ).fetchone()
                if dup is not None:
                    raise AccessRequestError("duplicate", "recent identical request")
                conn.execute(
                    "INSERT INTO access_requests (id, created_at, fio, department,"
                    " phone, email, work_summary, status, decision_note, dupe_hash,"
                    " consent_at) VALUES (?,?,?,?,?,?,?,'new','',?,?)",
                    (rid, created, fio, department, phone, email, work_summary, dupe, created),
                )
                conn.execute(
                    "INSERT INTO access_request_events (request_id, ts, actor, event,"
                    " new_status) VALUES (?,?,?,?,?)",
                    (rid, created, "public", "created", "new"),
                )
                conn.commit()
                return self.get(rid)
            except AccessRequestError:
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise AccessRequestError("storage_error", type(exc).__name__) from None
            finally:
                conn.close()

    def get(self, request_id: str) -> AccessRequest:
        self.migrate()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM access_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise AccessRequestError("not_found", "no such request")
            return _row_to_request(row)
        finally:
            conn.close()

    def list(
        self,
        *,
        status: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        fio_query: str | None = None,
    ) -> list[AccessRequest]:
        """Список с фильтрами; поиск по ФИО — LIKE с экранированием (P2-1)."""
        self.migrate()
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if period_from:
            clauses.append("created_at>=?")
            params.append(period_from)
        if period_to:
            clauses.append("created_at<=?")
            params.append(period_to)
        if fio_query:
            escaped = fio_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(r"fio LIKE ? ESCAPE '\'")
            params.append(f"%{escaped}%")
        sql = "SELECT * FROM access_requests"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        conn = self._connect()
        try:
            return [_row_to_request(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def events(self, request_id: str) -> list[dict[str, Any]]:
        self.migrate()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts, actor, event, old_status, new_status, note"
                " FROM access_request_events WHERE request_id=? ORDER BY ts, id",
                (request_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def set_status(
        self,
        request_id: str,
        *,
        new_status: str,
        actor: str,
        note: str = "",
    ) -> AccessRequest:
        """Перевод статуса (только admin-хендлером); пишет событие."""
        if new_status not in STATUSES:
            raise AccessRequestError("bad_status", "unknown status")
        self.migrate()
        now = _now(self._clock)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with _WRITE_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM access_requests WHERE id=?", (request_id,)
                ).fetchone()
                if row is None:
                    raise AccessRequestError("not_found", "no such request")
                old = row["status"]
                conn.execute(
                    "UPDATE access_requests SET status=?, decision_note=?,"
                    " decided_at=?, decided_by=? WHERE id=?",
                    (new_status, note, ts, actor, request_id),
                )
                conn.execute(
                    "INSERT INTO access_request_events (request_id, ts, actor,"
                    " event, old_status, new_status, note) VALUES (?,?,?,?,?,?,?)",
                    (request_id, ts, actor, "status_change", old, new_status, note),
                )
                conn.commit()
            except AccessRequestError:
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise AccessRequestError("storage_error", type(exc).__name__) from None
            finally:
                conn.close()
        return self.get(request_id)

    def prune(self) -> int:
        """Retention-проход (тест/ручной): терминальные старше cutoff + их events."""
        self.migrate()
        with _WRITE_LOCK:
            conn = self._connect()
            try:
                return self._prune_locked(conn, _now(self._clock), commit=True)
            finally:
                conn.close()

    def _prune_locked(
        self, conn: sqlite3.Connection, now: datetime, *, commit: bool = False
    ) -> int:
        cutoff = (now - timedelta(days=self._retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        marks = ",".join("?" * len(TERMINAL_STATUSES))
        cur = conn.execute(
            "DELETE FROM access_requests WHERE status IN (" + marks + ")"
            " AND created_at<?",
            (*TERMINAL_STATUSES, cutoff),
        )
        removed = cur.rowcount or 0
        conn.execute(
            "DELETE FROM access_request_events WHERE ts<?", (cutoff,)
        )
        if removed:
            conn.execute("PRAGMA incremental_vacuum")
        if commit or removed:
            conn.commit()
        return removed

    def count(self) -> int:
        self.migrate()
        conn = self._connect()
        try:
            return conn.execute("SELECT count(*) FROM access_requests").fetchone()[0]
        finally:
            conn.close()
