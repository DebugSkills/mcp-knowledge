"""Тесты DocumentStore (bibliography Ф1): put/get/dedup/shard/atomic/quota/rebuild/verify/schema."""

from __future__ import annotations

import hashlib

import pytest

from mcp_server.storage.document_store import (
    DocumentStore,
    InvalidSha256Error,
    QuotaExceededError,
)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "documents", max_gb=10)


def test_put_returns_full_sha256(store):
    data = b"hello world" * 100
    r = store.put(data, mime="text/plain", filename="note.txt")
    assert r.sha256 == hashlib.sha256(data).hexdigest()
    assert len(r.sha256) == 64
    assert r.size == len(data)
    assert r.deduplicated is False


def test_shard_path_ab_cd(store):
    r = store.put(b"shard-me", mime="application/pdf", filename="x.pdf")
    sha = r.sha256
    path = store._root / sha[0:2] / sha[2:4] / sha
    assert path.exists()


def test_get_roundtrip(store):
    data = b"some-bytes-\x00\x01\x02"
    r = store.put(data, mime="application/octet-stream", filename="blob.bin")
    assert store.get(r.sha256) == data


def test_exists_and_info(store):
    r = store.put(b"exists", mime="text/plain", filename="e.txt")
    assert store.exists(r.sha256) is True
    assert store.exists("0" * 64) is False
    info = store.info(r.sha256)
    assert info is not None and info.mime == "text/plain" and info.original_filename == "e.txt"


def test_dedup_no_rewrite(store):
    r1 = store.put(b"same-bytes", mime="text/plain", filename="a.txt")
    r2 = store.put(b"same-bytes", mime="text/plain", filename="b.txt")
    assert r1.sha256 == r2.sha256
    assert r2.deduplicated is True and r2.existed is True
    assert store.count() == 1  # один физический blob
    # provenance не перезаписан
    assert store.info(r1.sha256).original_filename == "a.txt"


def test_atomic_no_part_left(store):
    store.put(b"atomic", mime="text/plain", filename="a.txt")
    assert list(store._tmp.glob("*.part")) == []


def test_quota_reject_before_write(tmp_path):
    store = DocumentStore(tmp_path / "docs", max_gb=0)
    with pytest.raises(QuotaExceededError):
        store.put(b"data", mime="text/plain", filename="q.txt")
    assert store.count() == 0
    # физически ничего не записано (кроме служебных registry.db*)
    blobs = [p for p in store._root.rglob("*")
             if p.is_file() and p.name not in ("registry.db", "registry.db-wal", "registry.db-shm")]
    assert blobs == []


def test_quota_skipped_on_dedup(tmp_path):
    store = DocumentStore(tmp_path / "docs", max_gb=10)
    store.put(b"dedup-me", mime="text/plain", filename="d.txt")
    store._max_bytes = 0  # симулируем исчерпание квоты
    r = store.put(b"dedup-me", mime="text/plain", filename="d.txt")
    assert r.deduplicated is True  # дедуп ДО квоты


def test_verify_detects_corruption(store):
    r = store.put(b"integrity-check", mime="text/plain", filename="i.txt")
    assert store.verify(r.sha256) is True
    # портим файл
    (store._root / r.sha256[0:2] / r.sha256[2:4] / r.sha256).write_bytes(b"corrupted")
    assert store.verify(r.sha256) is False


def test_invalid_sha256(store):
    with pytest.raises(InvalidSha256Error):
        store.get("deadbeef")
    assert store.exists("zz" * 32) is False  # не-hex → False, не exception


def test_rebuild_from_fs_and_ssot(store):
    r = store.put(b"ORIGINAL-BYTES", mime="application/pdf", filename="book.pdf")
    sha = r.sha256
    # сносим реестр (симулируем потерю registry.db)
    with store._connect() as conn:
        conn.execute("DELETE FROM blobs")
    assert store.info(sha) is None

    sources = [{"blobs": {"original": {
        "sha256": sha, "mime": "application/pdf", "original_filename": "book.pdf",
    }}}]
    result = store.rebuild(sources)
    assert result["blobs"] == 1
    info = store.info(sha)
    assert info is not None
    assert info.original_filename == "book.pdf" and info.mime == "application/pdf"


def test_rebuild_orphan_guard(store):
    r = store.put(b"orphan", mime="application/octet-stream", filename="orphan.bin")
    result = store.rebuild([])  # без SSOT
    assert result["orphans"] == 1
    info = store.info(r.sha256)
    assert info.mime == "application/pdf"      # guard-значение
    assert info.original_filename == r.sha256  # guard-значение


def test_registry_schema_no_security_fields(store):
    """Инвариант: реестр НЕ содержит security-полей (zone/status/ref_count/refs)."""
    with store._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(blobs)").fetchall()}
    for forbidden in ("zone", "status", "ref_count", "refs"):
        assert forbidden not in cols


def test_vacuum_into_backup(store, tmp_path):
    store.put(b"backup-me", mime="text/plain", filename="b.txt")
    dest = store.vacuum_into(tmp_path / "backup-registry.db")
    assert dest.exists() and dest.stat().st_size > 0


# ── prune_jobs (Ф3c2c: ретеншн canonicalization_jobs) ─────────


def _insert_job(store, sha: str, status: str, created_at: str, updated_at: str | None):
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO canonicalization_jobs (sha256, format, status, "
            "created_at, updated_at) VALUES (?, 'pdf', ?, ?, ?)",
            (sha, status, created_at, updated_at),
        )


def test_prune_jobs_old_done_failed_removed(store):
    """Старые done/failed (за cutoff) удаляются; rowcount = числу удалённых."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    _insert_job(store, "a" * 64, "done", old, old)
    _insert_job(store, "b" * 64, "failed", old, old)

    assert store.prune_jobs(30) == 2
    assert store.job_status("a" * 64) is None
    assert store.job_status("b" * 64) is None


def test_prune_jobs_fresh_and_active_preserved(store):
    """Свежий done + старые running/pending НЕ удаляются (возраст от завершения)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = now.isoformat()
    old = (now - timedelta(days=40)).isoformat()
    _insert_job(store, "c" * 64, "done", fresh, fresh)
    _insert_job(store, "d" * 64, "running", old, old)
    _insert_job(store, "e" * 64, "pending", old, None)  # COALESCE(updated_at, created_at)

    assert store.prune_jobs(30) == 0
    assert store.job_status("c" * 64)["status"] == "done"
    assert store.job_status("d" * 64)["status"] == "running"
    assert store.job_status("e" * 64)["status"] == "pending"


def test_prune_jobs_empty_store_zero(store):
    """Пустая таблица → 0 (идемпотентный no-op)."""
    assert store.prune_jobs(30) == 0


def test_prune_jobs_zero_retention_boundary(store):
    """retention_days=0 → cutoff=now: недавний done уже «просрочен» (граница)."""
    from datetime import datetime, timezone

    fresh = datetime.now(timezone.utc).isoformat()
    _insert_job(store, "f" * 64, "done", fresh, fresh)

    assert store.prune_jobs(0) == 1
    assert store.job_status("f" * 64) is None
