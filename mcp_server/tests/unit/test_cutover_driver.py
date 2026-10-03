"""Ф6b (bibliography): идемпотентный cutover-драйвер (scripts/cutover.py).

Покрывает:
- (а) dry-run ничего не изменяет (файлы/маркеры до == после), печатает план;
- (б) идемпотентность: повторный прогон при валидном маркере → skipped;
- (в) деструктивный шаг без --confirm/без снапшота → отказ (dry-run и apply);
- (г) исключение Source: живой blob + зелёный integrity сохраняется, прочие — снос;
- (д) GC на пустом корпусе: аномалия (orphan) → стоп с CutoverError;
- (е) прерывание на середине → перезапуск продолжает с маркера, не с нуля.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.document_store import DocumentStore

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("cutover", ROOT / "scripts" / "cutover.py")
cutover = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cutover  # dataclasses внутри модуля требуют регистрации
_spec.loader.exec_module(cutover)

CutoverConfig = cutover.CutoverConfig
CutoverDriver = cutover.CutoverDriver
CutoverError = cutover.CutoverError
CutoverRefusal = cutover.CutoverRefusal
DEFAULT_DESTRUCTIVE_TOKEN = cutover.DEFAULT_DESTRUCTIVE_TOKEN
plan_teardown = cutover.plan_teardown
gc_on_empty = cutover.gc_on_empty
write_marker = cutover.write_marker
read_marker = cutover.read_marker
marker_valid = cutover.marker_valid
sha256_text = cutover.sha256_text
checksum_tree = cutover.checksum_tree


# ── Helpers ───────────────────────────────────────────────────


def _cfg(tmp_path, **overrides):
    kwargs = dict(
        knowledge_dir=tmp_path / "knowledge",
        documents_dir=tmp_path / "documents",
        backup_dir=tmp_path / "backup",
        run_dir=tmp_path / "run",
        pdf_cache_dir=tmp_path / "pdf_cache",
        quality_dir=tmp_path / "quality",
        dlq_dir=tmp_path / "dlq",
    )
    kwargs.update(overrides)
    return CutoverConfig(**kwargs)


def _source_entry(kid, blobs):
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="source",
        zone="private",
        status="published",
        format="pdf",
        blobs=blobs,
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n")


def _entry(kid, content_type):
    """non-Source запись (book/collection/pdf-секция) — legacy-корпус без blobs."""
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type=content_type,
        zone="private",
        status="published",
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n")


def _live_blobs(ds: DocumentStore, data: bytes = b"pdf-content") -> dict:
    put = ds.put(data, mime="application/pdf", filename="d.pdf")
    return {
        "original": {
            "sha256": put.sha256,
            "mime": "application/pdf",
            "size": put.size,
            "original_filename": "d.pdf",
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


def _snapshot_fs(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _statuses(report: dict) -> dict:
    return {s["step"]: s["status"] for s in report["steps"]}


# ── (г) plan_teardown: исключение Source ─────────────────────


class TestPlanTeardown:
    def test_keeps_live_green_source(self, tmp_path):
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        live = _source_entry("src-live", _live_blobs(ds))
        missing = _source_entry(
            "src-missing",
            {"original": {"sha256": "0" * 64, "mime": "application/pdf",
                          "size": 1, "original_filename": "m.pdf"}, "derived": []},
        )
        no_blob = _source_entry("src-no-blob", {})

        keep, teardown, report = plan_teardown([live, missing, no_blob], ds)

        assert keep == {"src-live"}
        assert teardown == {"src-missing", "src-no-blob"}
        # missing_blob дефект → корпус не «зелёный» в целом
        assert report["missing_blob"] and report["ok"] is False

    def test_non_source_legacy_goes_to_teardown(self, tmp_path):
        # F-1: книги/collection/pdf-секции и прочие non-Source → снос (не no-op)
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        live = _source_entry("src-live", _live_blobs(ds))
        bad = _source_entry(
            "src-bad",
            {"original": {"sha256": "0" * 64, "mime": "application/pdf",
                          "size": 1, "original_filename": "m.pdf"}, "derived": []},
        )
        book = _entry("legacy-book", "book")
        collection = _entry("legacy-collection", "collection")
        pdf_section = _entry("legacy-pdf-section", "pdf")

        keep, teardown, report = plan_teardown(
            [live, bad, book, collection, pdf_section], ds
        )

        assert keep == {"src-live"}
        assert teardown == {
            "src-bad",
            "legacy-book",
            "legacy-collection",
            "legacy-pdf-section",
        }


# ── (д) gc_on_empty: аномалия → стоп ─────────────────────────


class TestGcOnEmpty:
    def test_empty_corpus_passes(self, tmp_path):
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        r = gc_on_empty(ds, index=None, grace_days=30)
        assert r["deleted"] == 0
        assert r["orphans"] == 0 and r["candidates"] == 0 and r["physical"] == 0

    def test_fresh_orphan_reported_not_fatal(self, tmp_path):
        # F-2: unreferenced-в-grace блоб — норма после сноса (сообщается, не падает)
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        ds.put(b"orphan-blob" * 8)  # свежий blob без Source-ref (в пределах grace)
        r = gc_on_empty(ds, index=None, grace_days=30)
        assert r["deleted"] == 0
        assert r["orphans"] == 1
        assert r["candidates"] == 0

    def test_aged_unreferenced_raises(self, tmp_path):
        # F-2: unreferenced СТАРШЕ grace → GC снёс бы → аномалия → стоп
        import os
        import time

        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        put = ds.put(b"old-orphan-blob" * 8)
        old = time.time() - 31 * 86400  # > grace (30 дней)
        os.utime(ds._shard_path(put.sha256), (old, old))
        with pytest.raises(CutoverError):
            gc_on_empty(ds, index=None, grace_days=30)


# ── (а) dry-run ничего не изменяет ───────────────────────────


class TestDryRun:
    def test_dry_run_does_not_mutate(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        (cfg.knowledge_dir / "a.md").write_text(
            "---\nknowledge_id: x\ndomain: library\nsubject: bibliography\n---\n# x\n"
        )
        cfg.documents_dir.mkdir(parents=True)
        (cfg.documents_dir / "blob.bin").write_bytes(b"data")

        before = _snapshot_fs(tmp_path)
        report = CutoverDriver(cfg).run(dry_run=True, apply=False)
        after = _snapshot_fs(tmp_path)

        assert before == after  # инвариант: ни один файл не создан/изменён
        statuses = _statuses(report)
        assert statuses[2] == "planned"
        assert statuses[4] == "refused"  # dry-run без confirm/snapshot
        assert statuses[7] == "planned"
        # маркеры и backup-каталог не созданы
        assert not cfg.run_dir.exists()
        assert not cfg.backup_dir.exists()


# ── (б) идемпотентность: маркер → skipped ────────────────────


class TestIdempotency:
    def test_skip_when_marker_valid(self, tmp_path):
        cfg = _cfg(tmp_path)
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        report = driver.run(dry_run=True, apply=False)
        statuses = _statuses(report)
        assert statuses[2] == "skipped"
        assert statuses[3] == "planned"


# ── (в) деструктивный шаг: отказ без confirm/снапшота ────────


class TestTeardownGate:
    def test_refused_dry_run_without_confirm(self, tmp_path):
        cfg = _cfg(tmp_path)
        report = CutoverDriver(cfg).run(dry_run=True, apply=False, confirm_destructive=None)
        det = [s for s in report["steps"] if s["step"] == 4][0]
        assert det["status"] == "refused"
        assert "--confirm-destructive" in det["detail"]
        assert "валидный снапшот" in det["detail"]

    def test_planned_when_gates_ok(self, tmp_path):
        cfg = _cfg(tmp_path)
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        report = driver.run(
            dry_run=True, apply=False, confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN
        )
        assert _statuses(report)[4] == "planned"

    def test_refused_apply_without_confirm(self, tmp_path):
        cfg = _cfg(tmp_path)
        calls = []
        cfg.clear_qdrant = lambda _c: calls.append("clear_qdrant")
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        write_marker(driver._marker(3), sha256_text("stop-v1"), step=3, name="stop")
        with pytest.raises(CutoverRefusal):
            driver.run(dry_run=False, apply=True, confirm_destructive=None)
        assert "clear_qdrant" not in calls

    def test_refused_apply_without_snapshot(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        calls = []
        cfg.clear_qdrant = lambda _c: calls.append("clear_qdrant")
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        write_marker(driver._marker(3), sha256_text("stop-v1"), step=3, name="stop")
        monkeypatch.setattr(driver, "_snapshot_marker_valid", lambda: False)
        with pytest.raises(CutoverRefusal):
            driver.run(dry_run=False, apply=True, confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN)
        assert "clear_qdrant" not in calls


# ── (ж) реальный delete-путь _action_teardown в apply-режиме ──


class TestTeardownApply:
    def test_action_teardown_deletes_non_kept_keeps_source(self, tmp_path):
        # F-3: не только plan/refusal — реальный снос SSOT-файлов + идемпотентность.
        import asyncio

        from mcp_server.storage.markdown_store import MarkdownStore

        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)

        # живой blob в documents-сторе
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        blobs = _live_blobs(ds)

        # реальные SSOT-записи (green Source / битый Source / non-Source book)
        store = MarkdownStore(knowledge_root=cfg.knowledge_dir)
        live = _source_entry("src-live", blobs)
        bad = _source_entry(
            "src-bad",
            {"original": {"sha256": "0" * 64, "mime": "application/pdf",
                          "size": 1, "original_filename": "m.pdf"}, "derived": []},
        )
        book = _entry("legacy-book", "book")
        asyncio.run(store.write_entry(live))
        asyncio.run(store.write_entry(bad))
        asyncio.run(store.write_entry(book))

        calls = []
        cfg.clear_qdrant = lambda _c: calls.append("clear_qdrant")
        cfg.stop_cmd = lambda: calls.append("stop")
        cfg.deploy_cmd = lambda: calls.append("deploy")
        cfg.reindex_cmd = lambda: calls.append("reindex")
        cfg.smoke_cmd = lambda: calls.append("smoke")
        cfg.import_pilot_cmd = lambda: calls.append("pilot")

        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        write_marker(driver._marker(3), sha256_text("stop-v1"), step=3, name="stop")

        report = driver.run(
            dry_run=False, apply=True, confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN
        )
        assert _statuses(report)[4] == "done"

        kb = cfg.knowledge_dir / "library" / "bibliography"
        # kept Source остался на месте; non-Source и битый Source — снесены (в .trash)
        assert (kb / "src-live.md").exists()
        assert not (kb / "legacy-book.md").exists()
        assert not (kb / "src-bad.md").exists()
        assert (cfg.knowledge_dir / ".trash" / "legacy-book.md").exists()
        # маркер шага 4 записан; clear_qdrant вызван
        assert driver._marker(4).exists()
        assert "clear_qdrant" in calls

        # повторный прогон → шаг 4 skip (деструктивный шаг по НАЛИЧИЮ маркера)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        write_marker(driver._marker(3), sha256_text("stop-v1"), step=3, name="stop")
        report2 = driver.run(
            dry_run=False, apply=True, confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN
        )
        assert _statuses(report2)[4] == "skipped"
        assert calls.count("clear_qdrant") == 1  # повторного сноса не было


# ── (з) снапшот-хеш: derived/VCS-метаданные НЕ вход ────────────


class TestSnapshotHashExcludesDerived:
    def test_registry_mutation_does_not_invalidate_snapshot(self, tmp_path):
        # (а) позитив: SQLite-реестр (registry.db*) — derived/rebuildable, не вход.
        # Его байты меняются между маркером и прогоном → маркер НЕ должен протухать.
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        ds.put(b"blob-content")
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        assert driver._snapshot_marker_valid()
        # прямое вмешательство в реестр (не трогая контент-блобы)
        (tmp_path / "documents" / "registry.db").write_bytes(b"tampered-registry-bytes")
        assert driver._snapshot_marker_valid()

    def test_blob_content_tamper_invalidates_snapshot(self, tmp_path):
        # (б) негатив-контроль: реальное изменение КОНТЕНТА блоба → маркер невалиден.
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        put = ds.put(b"original-blob-content")
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        assert driver._snapshot_marker_valid()
        ds._shard_path(put.sha256).write_bytes(b"tampered-blob-content")
        assert not driver._snapshot_marker_valid()

    def test_knowledge_md_invalidates_but_git_metadata_ignored(self, tmp_path):
        # (в) knowledge: изменение .md (контент) инвалидирует; .git/ — нет.
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        cfg.documents_dir.mkdir(parents=True)
        (cfg.knowledge_dir / "a.md").write_text("hello")
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        assert driver._snapshot_marker_valid()
        (cfg.knowledge_dir / "a.md").write_text("HELLO")
        assert not driver._snapshot_marker_valid()
        # снова валидный маркер → служебная запись под .git/ не инвалидирует
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        assert driver._snapshot_marker_valid()
        (cfg.knowledge_dir / ".git").mkdir(parents=True, exist_ok=True)
        (cfg.knowledge_dir / ".git" / "index").write_text("git-meta-change")
        assert driver._snapshot_marker_valid()


# ── (е) прерывание → перезапуск с маркера ─────────────────────


class TestResume:
    def test_resume_from_marker(self, tmp_path):
        calls = []
        cfg = _cfg(
            tmp_path,
            stop_cmd=lambda: calls.append("stop"),
            deploy_cmd=lambda: calls.append("deploy"),
            reindex_cmd=lambda: calls.append("reindex"),
            smoke_cmd=lambda: calls.append("smoke"),
            import_pilot_cmd=lambda: calls.append("pilot"),
            clear_qdrant=lambda _c: calls.append("clear_qdrant"),
        )
        driver = CutoverDriver(cfg)
        # «прерывание»: шаги 2 и 3 уже выполнены (маркеры стоят)
        write_marker(driver._marker(2), driver._snapshot_input_hash(), step=2, name="snapshot")
        write_marker(driver._marker(3), sha256_text("stop-v1"), step=3, name="stop")

        report = driver.run(
            dry_run=False, apply=True, confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN
        )

        statuses = _statuses(report)
        assert statuses[2] == "skipped"
        assert statuses[3] == "skipped"
        assert statuses[4] == "done"
        assert statuses[5] == "done"
        assert statuses[6] == "done"
        assert statuses[7] == "done"
        assert statuses[8] == "done"
        # остановка (шаг 3) НЕ перезапускалась; деплой (шаг 5) — да
        assert "stop" not in calls
        assert "deploy" in calls
        assert "clear_qdrant" in calls


# ── Маркеры/checksums (формат) ───────────────────────────────


class TestMarkers:
    def test_marker_roundtrip(self, tmp_path):
        p = tmp_path / "m.complete"
        write_marker(p, "abc123", step=2, name="snapshot")
        m = read_marker(p)
        assert m["input_hash"] == "abc123" and m["step"] == 2
        assert marker_valid(p, "abc123")
        assert not marker_valid(p, "xyz")
        assert read_marker(tmp_path / "nope.complete") is None

    def test_checksum_tree_deterministic(self, tmp_path):
        d = tmp_path / "t"
        d.mkdir()
        (d / "a.txt").write_text("hello")
        (d / "b.txt").write_text("world")
        c1 = checksum_tree(d)
        assert checksum_tree(d) == c1  # детерминизм
        (d / "b.txt").write_text("WORLD")
        assert checksum_tree(d) != c1  # контент-чувствительность
