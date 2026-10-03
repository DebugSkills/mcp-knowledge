"""Ф3c1 (trace code-2026-10-02-bibliography): documents_check integrity.

Контракт (план §3.6:226, acceptance Ф3:336):
- каждый source_ref → Source существует ∧ blob существует ∧ sha256 сходится
  (authoritative re-hash ПОТОКОВО, не доверяя имени файла — CR2);
- canonical-chain: canonical жив ∧ provenance полон (derived: tool/
  tool_version/params_hash; PDF as-is: derived_from=null + tool="as-is");
- orphan-sweep: каждый blob в сторе → ∃ ≥1 ref (иначе documents_orphans,
  кандидат GC — НЕ удаляется здесь);
- дефекты → СУЩЕСТВУЮЩИЕ quality-issues (broken_link / orphaned),
  идемпотентно (детерминированный issue_id от type+kid+detail);
- fail-safe: ошибка чтения одного блоба → фиксируется, проверка
  продолжается, ok=false при любом найденном дефекте.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_server.content.source import register_source
from mcp_server.indexing.reconcile import reconcile
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.document_store import DocumentStore
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.storage.schema import ZONE_PRIVATE
from mcp_server.tools import documents_integrity
from mcp_server.tools.documents_integrity import check_documents, documents_check


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def quality_tempdir(tmp_path):
    """Изоляция issues-store (паттерн test_source_ref_index_runtime)."""
    from mcp_server.quality.issues import set_store_dir

    set_store_dir(str(tmp_path))
    yield str(tmp_path)
    set_store_dir(None)


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (SSOT-путь)."""
    import git

    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


@pytest.fixture
def ds(tmp_path):
    """Реальный DocumentStore во временном каталоге."""
    return DocumentStore(tmp_path / "documents", max_gb=1)


# ── Helpers ───────────────────────────────────────────────────


def _blob_path(ds: DocumentStore, sha: str) -> Path:
    return ds._shard_path(sha)


async def _seed_pdf_as_is(store, ds, n: int = 1) -> tuple[str, str]:
    """PDF as-is: canonical ≡ original, tool="as-is", derived_from=null."""
    data = f"pdf-content-{n}-".encode() * 16
    put = ds.put(data, mime="application/pdf", filename=f"doc{n}.pdf")
    blobs = {
        "original": {
            "sha256": put.sha256,
            "mime": "application/pdf",
            "size": put.size,
            "original_filename": f"doc{n}.pdf",
        },
        "canonical": {
            "sha256": put.sha256,
            "role": "canonical",
            "derived_from": None,
            "tool": "as-is",
            "tool_version": None,
        },
        "derived": [],
    }
    res = await register_source(store, original_sha256=put.sha256, format="pdf", blobs=blobs)
    return res["knowledge_id"], put.sha256


async def _seed_derived(store, ds, n: int = 2, *, complete_provenance: bool = True) -> tuple[str, str, str]:
    """Non-PDF → конвертер: canonical derived_from=original (полный/усечённый provenance)."""
    original_data = f"original-{n}-".encode() * 16
    canon_data = f"canonical-{n}-".encode() * 16
    o = ds.put(original_data, mime="application/vnd.oasis.opendocument.text", filename=f"doc{n}.odt")
    c = ds.put(
        canon_data, mime="application/pdf", filename=f"doc{n}.pdf", role="canonical",
        derived_from=o.sha256, tool="libreoffice", tool_version="7.6.2", params_hash="ab12cd34",
    )
    canonical: dict = {
        "sha256": c.sha256,
        "role": "canonical",
        "derived_from": o.sha256,
        "tool": "libreoffice",
    }
    if complete_provenance:
        canonical["tool_version"] = "7.6.2"
        canonical["params_hash"] = "ab12cd34"
    blobs = {
        "original": {
            "sha256": o.sha256,
            "mime": "application/vnd.oasis.opendocument.text",
            "size": o.size,
            "original_filename": f"doc{n}.odt",
        },
        "canonical": canonical,
        "derived": [],
    }
    res = await register_source(store, original_sha256=o.sha256, format="odt", blobs=blobs)
    return res["knowledge_id"], o.sha256, c.sha256


def _issues(issue_type: str) -> list:
    from mcp_server.quality.issues import list_issues

    return list_issues(types=[issue_type], status="open", limit=200)


# ── 1. Clean store ────────────────────────────────────────────


async def test_clean_store_ok(store, ds, quality_tempdir):
    """Чистый стор: ok=true, checked=N, все списки дефектов пусты."""
    await _seed_pdf_as_is(store, ds, n=1)
    await _seed_derived(store, ds, n=2)

    report = await documents_check(store, ds)

    assert report["ok"] is True
    assert report["checked"] == 2
    for key in ("missing_blob", "sha_mismatch", "canonical_missing",
                "provenance_incomplete", "orphans", "errors"):
        assert report[key] == [], f"{key} must be empty: {report[key]}"
    # 3 физических блоба (as-is дедупит canonical), 4 ссылки (2+2), все референсены
    assert report["counts"]["blobs"] == 3
    assert report["counts"]["refs"] == 4
    assert report["counts"]["referenced"] == 3


# ── 2. Source-без-blob ────────────────────────────────────────


async def test_missing_blob_reports_and_issues(store, ds, quality_tempdir):
    """Запись Source есть, файл удалён → missing_blob + issue broken_link."""
    source_id, sha = await _seed_pdf_as_is(store, ds, n=1)
    _blob_path(ds, sha).unlink()

    report = await documents_check(store, ds)

    assert report["ok"] is False
    assert report["missing_blob"] == [
        {"source_id": source_id, "sha": sha, "ref": "original"}
    ]
    broken = [i for i in _issues("broken_link") if i.knowledge_id == source_id]
    assert broken, "broken_link issue must be created for source-without-blob"
    assert sha in broken[0].detail


# ── 3. sha-mismatch (re-hash ловит порчу при верном имени) ────


async def test_sha_mismatch_detected_by_rehash(store, ds, quality_tempdir):
    """Файл повреждён, имя (sha) верное → sha_mismatch: re-hash реально ловит."""
    source_id, orig_sha, canon_sha = await _seed_derived(store, ds, n=2)
    # Порчим ORIGINAL (canonical остаётся живым — изолируем дефект)
    _blob_path(ds, orig_sha).write_bytes(b"corrupted-payload" * 16)

    report = await documents_check(store, ds)

    assert report["ok"] is False
    mismatches = report["sha_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["source_id"] == source_id
    assert mismatches[0]["sha"] == orig_sha
    assert mismatches[0]["ref"] == "original"
    actual = hashlib.sha256(b"corrupted-payload" * 16).hexdigest()
    assert mismatches[0]["actual"] == actual
    # canonical жив — не потерян
    assert report["canonical_missing"] == []
    assert [i for i in _issues("broken_link") if i.knowledge_id == source_id]


# ── 4. Canonical-потеря (CR2-строка) ──────────────────────────


async def test_canonical_loss_is_degradation_not_silent_lie(store, ds, quality_tempdir):
    """Canonical удалён, original жив → canonical_missing (деградация)."""
    source_id, orig_sha, canon_sha = await _seed_derived(store, ds, n=2)
    _blob_path(ds, canon_sha).unlink()

    report = await documents_check(store, ds)

    assert report["ok"] is False
    assert report["canonical_missing"] == [{"source_id": source_id, "sha": canon_sha}]
    # Отсутствие canonical не дублируется в missing_blob (семантика разделена)
    assert report["missing_blob"] == []
    assert report["sha_mismatch"] == []
    assert [i for i in _issues("broken_link") if i.knowledge_id == source_id]


# ── 5. Provenance ─────────────────────────────────────────────


async def test_provenance_incomplete_derived(store, ds, quality_tempdir):
    """Derived-canonical без tool_version/params_hash → provenance_incomplete."""
    source_id, _, canon_sha = await _seed_derived(
        store, ds, n=2, complete_provenance=False
    )

    report = await documents_check(store, ds)

    assert report["provenance_incomplete"] == [{
        "source_id": source_id,
        "ref": "canonical",
        "sha": canon_sha,
        "missing": ["tool_version", "params_hash"],
    }]
    assert report["ok"] is False
    assert [i for i in _issues("broken_link") if i.knowledge_id == source_id]


async def test_pdf_as_is_provenance_not_incomplete(store, ds, quality_tempdir):
    """Негатив: PDF as-is (derived_from=null, tool="as-is") → НЕ неполный."""
    await _seed_pdf_as_is(store, ds, n=1)

    report = await documents_check(store, ds)

    assert report["provenance_incomplete"] == []
    assert report["ok"] is True


# ── 6. Orphan blob ────────────────────────────────────────────


async def test_orphan_blob_reported_and_issued(store, ds, quality_tempdir):
    """Blob есть, ref нет → orphans + issue orphaned (кандидат GC, не удаляем)."""
    await _seed_pdf_as_is(store, ds, n=1)
    orphan_data = b"orphan-blob-content" * 8
    orphan_put = ds.put(orphan_data)
    assert ds._shard_path(orphan_put.sha256).exists()

    report = await documents_check(store, ds)

    assert report["ok"] is False
    assert report["orphans"] == [
        {"sha256": orphan_put.sha256, "size": len(orphan_data)}
    ]
    # Blob НЕ удалён проверкой (кандидат GC — отдельная операция)
    assert ds._shard_path(orphan_put.sha256).exists()
    orphans = [i for i in _issues("orphaned") if orphan_put.sha256 in i.detail]
    assert orphans, "orphaned issue must be created for blob-without-ref"


# ── 7. Идемпотентность issues ─────────────────────────────────


async def test_issues_idempotent_double_run(store, ds, quality_tempdir):
    """×2 прогон → issues не дублируются (детерминированный issue_id)."""
    from mcp_server.quality.issues import set_store_dir

    source_id, sha = await _seed_pdf_as_is(store, ds, n=1)
    # Дефект-микс: source-без-blob (файл удалён) + blob-без-ref (put мимо Source)
    _blob_path(ds, sha).unlink()
    ds.put(b"orphan" * 8)

    first = await documents_check(store, ds)
    second = await documents_check(store, ds)

    set_store_dir(quality_tempdir)
    all_first = _issues("broken_link") + _issues("orphaned")
    ids_first = {i.issue_id for i in all_first}
    all_second = _issues("broken_link") + _issues("orphaned")
    ids_second = {i.issue_id for i in all_second}
    assert ids_first == ids_second
    assert len(all_first) == len(all_second)
    # Отчёты эквивалентны по дефектам (counts issues_created падает до 0)
    assert first["missing_blob"] == second["missing_blob"]
    assert first["orphans"] == second["orphans"]
    assert first["counts"]["issues_created"] > 0
    assert second["counts"]["issues_created"] == 0


# ── 8. Fail-safe: ошибка чтения одного блоба ──────────────────


async def test_read_error_fail_safe(store, ds, quality_tempdir, monkeypatch):
    """OSError на одном блобе → фиксируется, проверка продолжается, ok=false."""
    sid1, sha1 = await _seed_pdf_as_is(store, ds, n=1)
    sid2, sha2, _ = await _seed_derived(store, ds, n=2)

    original_hash = documents_integrity._hash_file

    def _hash_with_failure(path: Path) -> str:
        if path.name == sha1:
            raise OSError("simulated read failure")
        return original_hash(path)

    monkeypatch.setattr(documents_integrity, "_hash_file", _hash_with_failure)

    report = await documents_check(store, ds)

    assert report["ok"] is False
    # as-is: original и canonical — ОДИН физический blob, но ДВЕ ссылки →
    # ошибка фиксируется per-ref (честная гранулярность отчёта)
    assert len(report["errors"]) == 2
    assert {e["ref"] for e in report["errors"]} == {"original", "canonical"}
    assert all(e["sha"] == sha1 and e["source_id"] == sid1 for e in report["errors"])
    assert all("simulated read failure" in e["error"] for e in report["errors"])
    # Второй source проверен ПОЛНОСТЬЮ (fail-safe: не падаем целиком)
    assert report["sha_mismatch"] == []
    assert report["canonical_missing"] == []
    assert report["counts"]["refs"] == 4
    assert [i for i in _issues("broken_link") if i.knowledge_id == sid1]


# ── 9. Синхронное ядро: записи на входе ───────────────────────


async def test_check_documents_sync_core(store, ds, quality_tempdir):
    """check_documents(entries, ds) — ядро без скана (прямые entries)."""
    _, sha = await _seed_pdf_as_is(store, ds, n=1)
    paths = await store.reindex_scan()
    entries = [store._parse_file(p) for p in paths]
    assert len(entries) == 1

    report = check_documents(entries, ds)

    assert report["ok"] is True
    assert report["checked"] == 1
    assert report["counts"]["referenced"] == 1


async def test_check_documents_no_sources(store, ds, quality_tempdir):
    """Пустой SSOT (0 Source) → ok=true, checked=0 (не падаем)."""
    report = check_documents([], ds)
    assert report["ok"] is True
    assert report["checked"] == 0
    assert report["counts"] == {
        "sources": 0, "refs": 0, "entry_refs": 0, "blobs": 0, "referenced": 0,
        "issues_created": 0, "issues_refreshed": 0, "issues_skipped": 0,
    }
    # jobs_pruned добавляет только async-обёртка (prune-проводка) — в отчёте
    # синхронного ядра ключа нет.


# ── 10. Wire: reconcile вызывает documents_check ──────────────


class _FakeQdrant:
    def get_knowledge_updated_at(self, collection_name=None):
        return {}

    def delete_by_knowledge_id(self, knowledge_id, collection_name=None):
        raise AssertionError("no orphans expected")


class _FakeStore:
    def __init__(self, entries):
        self._entries = entries

    async def reindex_scan(self):
        return [Path("/fake/a.md"), Path("/fake/b.md")]

    def _parse_file(self, path):
        return self._entries[0] if path.name == "a.md" else self._entries[1]


class _FakeKnowledgeIndex:
    def rebuild_all(self):
        return {}


def _source_entry(kid: str, blobs: dict | None) -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="source",
        zone=ZONE_PRIVATE,
        blobs=blobs,
        updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n")


async def test_reconcile_wires_documents_check(monkeypatch, quality_tempdir):
    """reconcile(document_store=...) вызывает documents_check после reindex-цикла."""
    entries = [
        _source_entry("src-aaa", {"original": {"sha256": "a" * 64}, "derived": []}),
        _source_entry("src-bbb", None),
    ]
    calls: list = []

    async def _fake_check(store, document_store, *, create_issues=True):
        calls.append((store, document_store, create_issues))
        return {"ok": False, "checked": 2, "counts": {"sources": 2}}

    monkeypatch.setattr(documents_integrity, "documents_check", _fake_check)

    summary = await reconcile(
        _FakeStore(entries), _FakeQdrant(), MagicMock(), _FakeKnowledgeIndex(),
        skip_reindex=True, skip_orphan_detection=True,
        document_store="FAKE_DS",
    )

    assert len(calls) == 1
    assert calls[0][1] == "FAKE_DS"
    assert calls[0][2] is True
    assert summary["documents_integrity"] == {"ok": False, "checked": 2, "counts": {"sources": 2}}


async def test_reconcile_documents_check_fail_safe(monkeypatch, quality_tempdir):
    """documents_check упал → reconcile НЕ падает, ошибка в errors."""
    entries = [_source_entry("src-aaa", None)]

    async def _boom(store, document_store, *, create_issues=True):
        raise RuntimeError("documents check exploded")

    monkeypatch.setattr(documents_integrity, "documents_check", _boom)

    summary = await reconcile(
        _FakeStore(entries), _FakeQdrant(), MagicMock(), _FakeKnowledgeIndex(),
        skip_reindex=True, skip_orphan_detection=True,
        document_store="FAKE_DS",
    )

    assert any("Documents integrity" in e for e in summary["errors"])


async def test_reconcile_without_document_store_no_call(monkeypatch, quality_tempdir):
    """document_store=None (легаси-вызовы) → documents_check НЕ вызывается."""
    entries = [_source_entry("src-aaa", None)]
    calls: list = []

    async def _fake_check(store, document_store, *, create_issues=True):
        calls.append(document_store)
        return {"ok": True}

    monkeypatch.setattr(documents_integrity, "documents_check", _fake_check)

    summary = await reconcile(
        _FakeStore(entries), _FakeQdrant(), MagicMock(), _FakeKnowledgeIndex(),
        skip_reindex=True, skip_orphan_detection=True,
    )

    assert calls == []
    assert summary["documents_integrity"] is None


# ── 11. Ф3c2c: entry→Source dangling refs + jobs prune ────────


async def _seed_entry_with_refs(store, sid: str, kid: str) -> None:
    """Import-путь (Ф3c2a _batch_write_sections): Knowledge-запись со source_refs → sid.

    Именно так записи получают source_refs в проде (import PDF с source_id в
    meta секции) — проверяем traversal на РЕАЛЬНОМ проводке, не на ручном YAML.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from mcp_server.content.preprocessor import Section
    from mcp_server.tools.content import _batch_write_sections

    app_state = MagicMock()
    app_state.store = store
    pipeline = MagicMock()
    pipeline.enqueue = AsyncMock(return_value=None)
    app_state.pipeline = pipeline
    app_state.import_progress = None
    section = Section(
        title="Секция библиографии",
        body="Тело секции со ссылкой на источник",
        sequence_number=1,
        tags=[],
        meta={
            "knowledge_id": kid,
            "domain": "library",
            "subject": "bibliography",
            "content_type": "pdf",
            "source_id": sid,
        },
    )
    result = await _batch_write_sections(
        [section],
        {"domain": "library", "subject": "bibliography", "title": "Книга тестов",
         "tags": [], "cross_subjects": []},
        app_state, "", asyncio.Event(),
    )
    assert result["imported"] == 1 and result["failed"] == 0


async def test_entry_refs_traversed_non_vacuous(store, ds, quality_tempdir):
    """Ф3c2a-проводка создала entry со source_refs → traversal ВИДИТ refs.

    Невакуумность: без проводки Ф3c2a (import не пишет source_refs) traversal
    был бы пуст (entry_refs=0) и dangling-детект — мёртвый код. Здесь живой
    Source + живая ссылка → entry_refs>0, dangling пуст, ok=True.
    """
    sid, _sha = await _seed_pdf_as_is(store, ds, n=1)
    await _seed_entry_with_refs(store, sid, "lib-bib-sec-live")

    report = await documents_check(store, ds)

    assert report["counts"]["entry_refs"] == 1
    assert report["dangling_source_refs"] == []
    assert report["ok"] is True


async def test_dangling_source_ref_reported(store, ds, quality_tempdir):
    """Entry ссылается на несуществующий Source → dangling + broken_link + ok=False."""
    await _seed_pdf_as_is(store, ds, n=1)  # живой Source (не влияет на дефект)
    missing = "src-missing0000000000"
    await _seed_entry_with_refs(store, missing, "lib-bib-sec-dang")

    report = await documents_check(store, ds)

    assert report["ok"] is False
    assert report["dangling_source_refs"] == [
        {"knowledge_id": "lib-bib-sec-dang", "source_id": missing}
    ]
    broken = [i for i in _issues("broken_link") if i.knowledge_id == "lib-bib-sec-dang"]
    assert broken, "broken_link issue must be created for dangling source_ref"
    assert missing in broken[0].detail


async def test_malformed_ref_without_source_id_skipped(store, ds, quality_tempdir):
    """Malformed ref (без source_id) — skip: не dangling, не entry_refs (прецедент: ref без sha)."""
    sid, _sha = await _seed_pdf_as_is(store, ds, n=1)
    await _seed_entry_with_refs(store, sid, "lib-bib-sec-malf")
    await store.update(
        "lib-bib-sec-malf",
        metadata={"source_refs": [{"locator": {"kind": "page", "start": 1, "end": 2}}]},
    )

    report = await documents_check(store, ds)

    assert report["counts"]["entry_refs"] == 0
    assert report["dangling_source_refs"] == []
    assert report["ok"] is True


async def test_documents_check_prunes_old_jobs(store, ds, quality_tempdir):
    """prune в documents_check: старые done/failed удалены, свежие и running/pending живы."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    fresh = now.isoformat()
    with ds._connect() as conn:
        for sha, status, created, updated in (
            ("a" * 64, "done", old, old),          # старый done → pruned
            ("b" * 64, "failed", old, old),        # старый failed → pruned
            ("c" * 64, "done", fresh, fresh),      # свежий done → жив
            ("d" * 64, "running", old, old),       # старый running → жив
            ("e" * 64, "pending", old, None),      # pending, updated_at NULL → жив (COALESCE)
        ):
            conn.execute(
                "INSERT INTO canonicalization_jobs (sha256, format, status, "
                "created_at, updated_at) VALUES (?, 'pdf', ?, ?, ?)",
                (sha, status, created, updated),
            )

    report = await documents_check(store, ds)

    assert report["counts"]["jobs_pruned"] == 2
    assert ds.job_status("a" * 64) is None
    assert ds.job_status("b" * 64) is None
    assert ds.job_status("c" * 64)["status"] == "done"
    assert ds.job_status("d" * 64)["status"] == "running"
    assert ds.job_status("e" * 64)["status"] == "pending"


async def test_documents_check_prune_fail_safe(store, ds, quality_tempdir, monkeypatch):
    """prune_jobs raise → проверка жива, jobs_pruned=0 (fail-safe)."""

    def _boom(retention_days: int) -> int:
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(ds, "prune_jobs", _boom)

    report = await documents_check(store, ds)

    assert report["counts"]["jobs_pruned"] == 0
    assert report["ok"] is True


class _BareDS:
    """Фейк document_store БЕЗ prune_jobs — duck-typing путь getattr-None."""

    def exists(self, sha: str) -> bool:
        return False

    def _shard_path(self, sha: str):  # pragma: no cover — не вызывается
        raise AssertionError("no blobs expected")

    def _iter_blob_files(self):
        return iter([])


async def test_documents_check_prune_duck_typing_no_method(store, quality_tempdir):
    """document_store без prune_jobs (фейк) → jobs_pruned=0, проверка не падает."""
    report = await documents_check(store, _BareDS())

    assert report["counts"]["jobs_pruned"] == 0
    assert report["ok"] is True
