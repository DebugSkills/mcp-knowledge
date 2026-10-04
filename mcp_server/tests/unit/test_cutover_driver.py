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


# ── C1 (code-2026-10-02-bibliography): keep-правило ∪ fail-closed ∪ assert delete ──


class TestKeepRuleSections:
    def test_keeps_sections_and_root_collection_of_kept_source(self, tmp_path):
        # P0 keep-rule: Source (живой blob ∧ зелёный) ∪ секции со source_refs/
        # source_id на kept-Source ∪ корневая коллекция с детьми из keep.
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        source = _source_entry("src-ecc538dc3d8e9cb3", _live_blobs(ds))

        sec_refs = KnowledgeEntry(frontmatter=KnowledgeFrontmatter(
            knowledge_id="sec-refs", domain="networking", subject="http3",
            content_type="pdf", zone="private", status="published",
            parent_knowledge_id="coll-rfc",
            source_refs=[{"source_id": "src-ecc538dc3d8e9cb3",
                          "locator": {"kind": "page", "start": 1, "end": 1}}],
        ), content="# sec-refs\n")
        sec_sid = KnowledgeEntry(frontmatter=KnowledgeFrontmatter(
            knowledge_id="sec-sid", domain="networking", subject="http3",
            content_type="pdf", zone="private", status="published",
            parent_knowledge_id="coll-rfc",
            source_id="src-ecc538dc3d8e9cb3",
        ), content="# sec-sid\n")
        root = KnowledgeEntry(frontmatter=KnowledgeFrontmatter(
            knowledge_id="coll-rfc", domain="networking", subject="http3",
            content_type="collection", zone="private", status="published",
        ), content="# coll\n")
        # секция, ссылающаяся на НЕ-kept Source → teardown (не спасена)
        sec_dangling = KnowledgeEntry(frontmatter=KnowledgeFrontmatter(
            knowledge_id="sec-dangling", domain="networking", subject="http3",
            content_type="pdf", zone="private", status="published",
            parent_knowledge_id="coll-rfc",
            source_id="src-missing",
        ), content="# sec-dangling\n")
        legacy_book = _entry("legacy-book", "book")

        keep, teardown, _ = plan_teardown(
            [source, sec_refs, sec_sid, root, sec_dangling, legacy_book], ds
        )

        assert keep == {"src-ecc538dc3d8e9cb3", "sec-refs", "sec-sid", "coll-rfc"}
        assert teardown == {"sec-dangling", "legacy-book"}


class TestFailClosed:
    def test_zero_blobs_with_source_refs_refuses(self, tmp_path):
        # P1: 0 физических блобов при Source с непустыми refs → отказ (не снести всё)
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        source = _source_entry(
            "src-orphaned",
            {"original": {"sha256": "0" * 64, "mime": "application/pdf",
                          "size": 1, "original_filename": "m.pdf"}, "derived": []},
        )
        with pytest.raises(CutoverRefusal):
            plan_teardown([source], ds)

    def test_unavailable_store_refuses(self, tmp_path):
        # P1: blob-store недоступен (каталог не существует) → 0 блобов → отказ
        ds = cutover._ReadOnlyDocumentStore(tmp_path / "nonexistent" / "documents")
        source = _source_entry(
            "src-x",
            {"original": {"sha256": "0" * 64, "mime": "application/pdf",
                          "size": 1, "original_filename": "m.pdf"}, "derived": []},
        )
        with pytest.raises(CutoverRefusal):
            plan_teardown([source], ds)

    def test_does_not_fire_with_live_blob(self, tmp_path):
        # P1 негатив-контроль: живой блоб есть → guard НЕ срабатывает ложно
        ds = DocumentStore(tmp_path / "documents", max_gb=1)
        source = _source_entry("src-live", _live_blobs(ds))
        keep, teardown, _ = plan_teardown([source], ds)
        assert keep == {"src-live"}
        assert teardown == set()


class TestPartialDeleteAssert:
    def test_action_teardown_raises_on_partial_delete(self, tmp_path, monkeypatch):
        # P2: delete_many вернул < len(teardown) → CutoverError (тихий частичный снос запрещён)
        import asyncio

        from mcp_server.storage.markdown_store import MarkdownStore

        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        store = MarkdownStore(knowledge_root=cfg.knowledge_dir)
        book = _entry("legacy-book", "book")
        asyncio.run(store.write_entry(book))

        async def fake_delete_many(self, knowledge_ids, commit_message=None):
            return 0  # симулируем: ни один файл не удалился

        monkeypatch.setattr(MarkdownStore, "delete_many", fake_delete_many)
        cfg.clear_qdrant = lambda _c: None  # изолируем: CutoverError — ТОЛЬКО от assert delete
        driver = CutoverDriver(cfg)
        with pytest.raises(CutoverError):
            driver._action_teardown()


# ── C2: wiring (пути/коллекции/команды/гейты) ─────────────────


class TestWiring:
    def test_main_defaults_paths_collections_commands(self, monkeypatch):
        captured = {}

        def fake_run(self, **kw):
            captured["cfg"] = self.cfg
            return {"dry_run": True, "apply": False, "steps": []}

        monkeypatch.setattr(cutover.CutoverDriver, "run", fake_run)
        assert cutover.main([]) == 0
        cfg = captured["cfg"]
        base = ROOT.parent  # <repo>/mcp-knowledge
        assert cfg.knowledge_dir == base / "knowledge"
        assert cfg.documents_dir == base / "data" / "documents"
        assert cfg.qdrant_collections == ["knowledge_public", "knowledge_private"]
        assert cfg.legacy_collections == ["knowledge_v1"]
        assert cfg.legacy_collection_prefixes == ("knowledge_e2e_",)
        for name in (
            "stop_cmd", "deploy_cmd", "reindex_cmd", "smoke_cmd",
            "import_pilot_cmd", "snapshot_qdrant", "clear_qdrant",
        ):
            assert getattr(cfg, name) is not None, name
        assert (base / "knowledge" / ".git") in cfg.chown_targets
        assert cfg.env_file == ROOT / ".env"

    def test_main_cli_overrides(self, monkeypatch):
        captured = {}

        def fake_run(self, **kw):
            captured["cfg"] = self.cfg
            return {"dry_run": True, "apply": False, "steps": []}

        monkeypatch.setattr(cutover.CutoverDriver, "run", fake_run)
        assert cutover.main(["--knowledge-dir", "/tmp/k", "--pilot-pdf", "/tmp/p.pdf"]) == 0
        cfg = captured["cfg"]
        assert cfg.knowledge_dir == Path("/tmp/k")
        assert cfg.pilot_pdf == Path("/tmp/p.pdf")

    def test_clear_qdrant_only_legacy_not_aliased(self, tmp_path, monkeypatch):
        deleted = []

        def fake_http(method, url, payload=None, timeout=120):
            if url.endswith("/collections"):
                return {"result": {"collections": [
                    {"name": "knowledge_public_v2"}, {"name": "knowledge_private_v2"},
                    {"name": "knowledge_v1"}, {"name": "knowledge_e2e_public_v2"},
                    {"name": "knowledge_e2e_private_v2"}]}}
            if url.endswith("/aliases"):
                return {"result": {"aliases": [
                    {"alias_name": "knowledge_public", "collection_name": "knowledge_public_v2"},
                    {"alias_name": "knowledge_private", "collection_name": "knowledge_private_v2"}]}}
            if method == "DELETE":
                deleted.append(url.rsplit("/", 1)[-1])
                return {}
            raise AssertionError(url)

        monkeypatch.setattr(cutover, "_http_json", fake_http)
        cutover._cmd_qdrant_clear_legacy(_cfg(tmp_path))
        assert deleted == ["knowledge_v1", "knowledge_e2e_public_v2", "knowledge_e2e_private_v2"]

    def test_apply_snapshot_gate(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        driver = CutoverDriver(cfg)
        assert driver.apply_snapshot_ok("H") is False
        write_marker(driver._marker(2), "H", step=2, name="snapshot")
        assert driver.apply_snapshot_ok("H") is True
        assert driver.apply_snapshot_ok("OTHER") is False
        assert driver.apply_snapshot_ok(None) is False

    def test_preflight_raises_with_hint_when_git_broken(self, tmp_path, monkeypatch):
        kd = tmp_path / "knowledge"
        (kd / ".git").mkdir(parents=True)
        cfg = _cfg(tmp_path, knowledge_dir=kd)

        class R:
            returncode = 1

        monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: R())
        with pytest.raises(CutoverError, match="safe.directory"):
            CutoverDriver(cfg)._action_preflight()

    def test_preflight_skips_non_git_dir(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        CutoverDriver(cfg)._action_preflight()  # не должно бросать

    def test_post_chown_existing_targets_only(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(cutover, "_run", lambda cmd: calls.append(cmd))
        existing = tmp_path / "e"
        existing.mkdir()
        cfg = _cfg(
            tmp_path,
            chown_owner="ladmin:ladmin",
            chown_targets=[existing, tmp_path / "missing"],
        )
        CutoverDriver(cfg)._action_post_chown()
        assert calls == [["chown", "-R", "ladmin:ladmin", str(existing)]]

    def test_smoke_requires_min_tools(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(cutover, "_http_json", lambda *a, **k: {"status": "healthy"})
        monkeypatch.setattr(
            cutover, "_mcp_rpc",
            lambda *a, **k: {"tools": [{"name": f"t{i}"} for i in range(12)]},
        )
        with pytest.raises(CutoverError, match="<30"):
            cutover._cmd_smoke(cfg)
        monkeypatch.setattr(
            cutover, "_mcp_rpc",
            lambda *a, **k: {"tools": [{"name": f"t{i}"} for i in range(31)]},
        )
        assert cutover._cmd_smoke(cfg)["tools"] == 31

    def test_read_env_key_first_value_never_logs(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('MCP_WRITE_KEYS=["k1","k2"]\nOTHER=1\n')
        assert cutover._read_env_key(env, "MCP_WRITE_KEYS") == "k1"
        assert cutover._read_env_key(env, "ABSENT") == ""

    def test_only_steps_partial_run(self, tmp_path, monkeypatch):
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
        cfg.knowledge_dir.mkdir(parents=True)
        driver = CutoverDriver(cfg)
        monkeypatch.setattr(driver, "_action_snapshot", lambda: None)
        report = driver.run(
            dry_run=False, apply=True, only_steps={1, 2},
            confirm_destructive=DEFAULT_DESTRUCTIVE_TOKEN,
        )
        assert [s["step"] for s in report["steps"] if s["status"] != "skipped"] == [1, 2]
        assert calls == []  # снос/стек не запускались
        assert not driver._marker(4).exists()

    def test_checksum_knowledge_ignores_derived_gen_yaml(self, tmp_path):
        kd = tmp_path / "knowledge"
        (kd / "networking").mkdir(parents=True)
        (kd / "networking" / "a.md").write_text("content\n")
        gen = kd / "networking" / "_INDEX.gen.yaml"
        gen.write_text("version: 1\n")
        h1 = cutover.checksum_knowledge(kd)
        gen.write_text("version: 2\n")          # сервер регенерировал индекс
        (kd / "INDEX.gen.yaml").write_text("x: 1\n")
        assert cutover.checksum_knowledge(kd) == h1   # derived — не вход
        (kd / "networking" / "a.md").write_text("content changed\n")
        assert cutover.checksum_knowledge(kd) != h1   # контент — вход

    def test_teardown_tolerates_missing_file_and_keeps_pdf_cache(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        calls = []
        cfg.clear_qdrant = lambda _c: calls.append("clear_qdrant")
        driver = CutoverDriver(cfg)
        # план с «призраком» (нет файла) и без keep
        monkeypatch.setattr(driver, "_load_sources_and_plan", lambda: (set(), {"ghost-id"}, {}))
        (cfg.quality_dir).mkdir(parents=True, exist_ok=True)
        (cfg.quality_dir / "q.json").write_text("{}")
        (cfg.dlq_dir).mkdir(parents=True, exist_ok=True)
        (cfg.dlq_dir / "d.json").write_text("{}")
        cfg.pdf_cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.pdf_cache_dir / "seg.json").write_text("{}")
        driver._action_teardown()  # не должно бросать
        assert calls == ["clear_qdrant"]
        assert list(cfg.quality_dir.iterdir()) == []
        assert list(cfg.dlq_dir.iterdir()) == []
        # keep пуст → pdf_cache очищается
        assert list(cfg.pdf_cache_dir.iterdir()) == []
        # keep непуст → pdf_cache сохраняется
        (cfg.pdf_cache_dir / "seg2.json").write_text("{}")
        monkeypatch.setattr(driver, "_load_sources_and_plan", lambda: ({"keep-1"}, set(), {}))
        driver._action_teardown()
        assert (cfg.pdf_cache_dir / "seg2.json").exists()

    def test_basis_allows_resume_after_partial_apply(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), "H1", step=2, name="snapshot")
        assert driver.apply_snapshot_ok("H2") is False      # базиса нет → отказ
        driver._record_basis("H1")                          # базис зафиксирован на старте apply
        assert driver.apply_snapshot_ok("H2") is True       # resume: сверка с базисом
        assert driver.apply_snapshot_ok("H1") is True       # нет дрейфа
        write_marker(driver._marker(2), "H3", step=2, name="snapshot")
        assert driver.apply_snapshot_ok("H2") is False      # базис не совпал с маркером

    def test_record_basis_noop_when_marker_mismatch(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.knowledge_dir.mkdir(parents=True)
        driver = CutoverDriver(cfg)
        write_marker(driver._marker(2), "H1", step=2, name="snapshot")
        driver._record_basis("OTHER")
        assert read_marker(driver._basis_path()) is None

