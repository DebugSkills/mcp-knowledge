"""Ф3b2 (trace code-2026-10-02-bibliography): SourceRefIndex в runtime.

Контракт (план §3.4:166 «startup-скан SSOT + ведение write-path'ом», §3.6:226):
- startup: init_source_ref_index(store) — полный SSOT-скан (reindex_scan +
  parse в executor); пустой SSOT → пустой индекс; ошибка скана → пустой
  индекс БЕЗ краха сервера (fail-safe);
- fail-closed: пустой индекс → blob_available_indexed=False (не True);
- ingest → index.add(ref) (точечный, persisted-entry);
- deprecate/restore (lifecycle) → rescan → инвалидация БЕЗ рестарта;
- set_zone public→private → public-auth False немедленно;
- update_source license cc→restricted → fail-closed немедленно;
- delete_entry: least-strict — при 2 refs (public+private) blob доступен
  full-auth; удаление последнего ref → False;
- refresh_source_ref_index(app_state) — единая точка рескана; ошибка →
  индекс ОПУСТОШАЕТСЯ (недоступность, не ложная выдача).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_server.content.source import make_source_id, register_source
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.availability import blob_available_indexed
from mcp_server.tools.source_ref_index import SourceRefIndex

SUBSCRIBER = {"level": "subscriber"}   # только public-зона
READ = {"level": "read"}               # public + private


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def quality_tempdir(tmp_path):
    """Изоляция issues/audit store (как test_deprecate_lifecycle)."""
    from mcp_server.quality.audit import set_store_dir as audit_set_dir
    from mcp_server.quality.issues import set_store_dir

    set_store_dir(str(tmp_path))
    audit_set_dir(str(tmp_path))
    yield str(tmp_path)


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (SSOT-путь с коммитами)."""
    import git

    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


def _sha(n: int) -> str:
    """Distinct sha256: различаются ПЕРВЫЕ 16 hex (make_source_id = src-<sha16>)."""
    return f"{n:016x}" + "0" * 48


async def _seed_source(store, n: int, *, zone: str = "public",
                       license: str | None = "cc-by-4.0",
                       canonical: int | None = None,
                       derived: list[int] | None = None) -> str:
    """Source в SSOT: original=sha(n), опц. canonical/derived (shared-blob тесты)."""
    blobs: dict = {"original": {"sha256": _sha(n)}, "derived": []}
    if canonical is not None:
        blobs["canonical"] = {"sha256": _sha(canonical)}
    if derived:
        blobs["derived"] = [{"sha256": _sha(d)} for d in derived]
    result = await register_source(
        store, original_sha256=_sha(n), format="pdf",
        blobs=blobs, zone=zone, license=license,
    )
    return result["knowledge_id"]


class _StubPipeline:
    """pipeline.enqueue-заглушка (set_zone/update_entry пишут в очередь)."""

    async def enqueue(self, entry, wait_for_index=False):
        return SimpleNamespace(indexed=True, pending=False)


class _FakeQdrant:
    """Qdrant-заглушка для delete_entry (только delete_by_knowledge_id)."""

    def __init__(self):
        self.deleted: list[tuple] = []

    def delete_by_knowledge_id(self, kid, collection_name=None):
        self.deleted.append((kid, collection_name))

    def scroll(self, *a, **kw):
        return [], None


def _app(store, index: SourceRefIndex, **extra) -> SimpleNamespace:
    state = SimpleNamespace(
        store=store, qdrant=None, pipeline=_StubPipeline(),
        data_version=0, source_ref_index=index,
    )
    for k, v in extra.items():
        setattr(state, k, v)
    return state


def _exists_true(sha256: str) -> bool:
    return True


# ── 1. Startup-скан ───────────────────────────────────────────


class TestStartupScan:
    async def test_scan_builds_index_from_ssot(self, store):
        from mcp_server.tools.source_ref_runtime import init_source_ref_index

        await _seed_source(store, 1, zone="public")
        await _seed_source(store, 2, zone="private")

        index = await init_source_ref_index(store)

        refs_pub = index.get(_sha(1))
        refs_priv = index.get(_sha(2))
        assert len(refs_pub) == 1 and refs_pub[0].zone == "public"
        assert len(refs_priv) == 1 and refs_priv[0].zone == "private"
        assert index.size() == {"blobs": 2, "refs": 2}
        # предикаты уже работают на стартовом индексе
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)
        assert not blob_available_indexed(_sha(2), SUBSCRIBER, exists_fn=_exists_true, index=index)
        assert blob_available_indexed(_sha(2), READ, exists_fn=_exists_true, index=index)

    async def test_empty_ssot_empty_index(self, store):
        from mcp_server.tools.source_ref_runtime import init_source_ref_index

        index = await init_source_ref_index(store)

        assert len(index) == 0
        assert index.size() == {"blobs": 0, "refs": 0}

    async def test_fail_safe_scan_error_empty_index(self, store, monkeypatch):
        """Скан падает → пустой индекс, БЕЗ raise (сервер не умирает молча)."""
        from mcp_server import metrics
        from mcp_server.tools.source_ref_runtime import init_source_ref_index

        async def _boom():
            raise RuntimeError("simulated FS failure")

        monkeypatch.setattr(store, "reindex_scan", _boom)

        index = await init_source_ref_index(store)

        assert len(index) == 0
        assert metrics.source_ref_index_errors.labels(op="startup")._value.get() >= 1

    def test_fail_closed_empty_index_never_true(self):
        """Пустой индекс + blob физически есть → False для ЛЮБОГО auth."""
        index = SourceRefIndex()
        for auth in (SUBSCRIBER, READ, {"level": "write"}, {}):
            assert blob_available_indexed(
                _sha(1), auth, exists_fn=_exists_true, index=index
            ) is False


# ── 2. Ingest-хук: точечный add ───────────────────────────────


class TestIngestHook:
    async def test_ingest_adds_ref_to_index(self, store, tmp_path):
        from mcp_server.content.ingest import ingest_source
        from mcp_server.storage.document_store import DocumentStore

        doc_store = DocumentStore(tmp_path / "documents", 1)
        index = SourceRefIndex()
        app = _app(store, index, document_store=doc_store)

        result = await ingest_source(
            app, format="pdf", domain="library", subject="bibliography",
            data=b"%PDF-1.4 %f3b2 runtime wiring test",
            license="cc-by-4.0", zone="public", title="Ф3b2 public PDF",
        )

        sha = result["original"]["sha256"]
        refs = index.get(sha)
        assert len(refs) == 1
        assert refs[0].source_id == result["source_id"] == make_source_id(sha)
        assert refs[0].zone == "public"
        # availability работает сразу, без рестарта/рескана
        assert blob_available_indexed(
            sha, SUBSCRIBER, exists_fn=doc_store.exists, index=index
        )


# ── 3. Lifecycle-хук: deprecate/restore → rescan ──────────────


class TestLifecycleHook:
    async def test_deprecate_public_source_invalidates_immediately(
        self, quality_tempdir, store
    ):
        from mcp_server.tools.quality import resolve_quality_issue

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)

        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)

        result = await resolve_quality_issue(
            {"knowledge_id": sid, "action": "deprecate", "reason": "Ф3b2 test"}, app
        )

        assert result["resolved"] is True, result
        # ИНВАЛИДАЦИЯ БЕЗ РЕСТАРТА: статус из SSOT уже в индексе
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index) is False

    async def test_restore_returns_availability(self, quality_tempdir, store):
        from mcp_server.tools.quality import resolve_quality_issue

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)
        await resolve_quality_issue({"knowledge_id": sid, "action": "deprecate"}, app)

        result = await resolve_quality_issue(
            {"knowledge_id": sid, "action": "restore", "reason": "Ф3b2 test"}, app
        )

        assert result["resolved"] is True, result
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)


# ── 4. set_zone-хук: public→private немедленно ────────────────


class TestSetZoneHook:
    async def test_set_zone_public_to_private_immediate(self, quality_tempdir, store):
        from mcp_server.tools.admin import set_zone

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)

        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)

        result = await set_zone(
            {"knowledge_id": sid, "zone": "private", "reason": "Ф3b2 test"}, app
        )

        assert result.get("ok") is True, result
        # public-auth теряет доступ НЕМЕДЛЕННО (без рестарта)
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index) is False
        # full-auth сохраняет: private-зона доступна read-уровню
        assert blob_available_indexed(_sha(1), READ, exists_fn=_exists_true, index=index)


# ── 5. update_source: license → fail-closed немедленно ───────


class TestUpdateSource:
    async def test_license_cc_to_restricted_fail_closed(self, quality_tempdir, store):
        from mcp_server.tools.source_ops import update_source

        sid = await _seed_source(store, 1, zone="public", license="cc-by-4.0")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)

        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)

        result = await update_source(app, sid, license="restricted", reason="Ф3b2 test")

        assert result.get("ok") is True, result
        # restricted не проходит public-license гейт → немедленный отказ
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index) is False
        # SSOT тоже обновлён (индекс не источник правды)
        entry = await store.read(sid)
        assert entry.frontmatter.license == "restricted"

    async def test_public_allowed_rederived_from_license(self, quality_tempdir, store):
        from mcp_server.tools.source_ops import update_source

        sid = await _seed_source(store, 1, zone="public", license="cc-by-4.0")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)
        await update_source(app, sid, license="restricted")

        entry = await store.read(sid)
        assert entry.frontmatter.public_allowed is False  # re-derived (ingest-семантика)

    async def test_non_source_rejected(self, quality_tempdir, store):
        from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
        from mcp_server.tools.source_ops import update_source

        fm = KnowledgeFrontmatter(knowledge_id="test-f3b2-note", domain="library",
                                  subject="bibliography", content_type="note")
        await store.write_entry(KnowledgeEntry(frontmatter=fm, content="# note"))
        app = _app(store, SourceRefIndex())

        result = await update_source(app, "test-f3b2-note", license="own")

        assert "error" in result

    async def test_nothing_to_update_rejected(self, quality_tempdir, store):
        from mcp_server.tools.source_ops import update_source

        sid = await _seed_source(store, 1)
        app = _app(store, SourceRefIndex())

        result = await update_source(app, sid)

        assert "error" in result


# ── 6. delete_entry: least-strict при частичной ссылке ───────


class TestDeleteHook:
    async def test_delete_least_strict_and_last_ref(self, quality_tempdir, store):
        from mcp_server.tools.crud import delete_entry

        # blob X=_sha(9) referenced: public-src (canonical) + private-src (derived)
        sid_pub = await _seed_source(store, 1, zone="public", canonical=9)
        sid_priv = await _seed_source(store, 2, zone="private", derived=[9])
        index = SourceRefIndex.build(
            [await store.read(sid_pub), await store.read(sid_priv)]
        )
        app = _app(store, index, qdrant=_FakeQdrant())

        shared = _sha(9)
        assert blob_available_indexed(shared, SUBSCRIBER, exists_fn=_exists_true, index=index)
        assert blob_available_indexed(shared, READ, exists_fn=_exists_true, index=index)

        # Удаляем PUBLIC-ref → blob остаётся доступен full-auth (least-strict)
        result = await delete_entry({"knowledge_id": sid_pub}, app)
        assert result.get("deleted") is True, result
        assert blob_available_indexed(shared, SUBSCRIBER, exists_fn=_exists_true, index=index) is False
        assert blob_available_indexed(shared, READ, exists_fn=_exists_true, index=index)

        # Удаляем ПОСЛЕДНИЙ ref → недоступен всем
        result = await delete_entry({"knowledge_id": sid_priv}, app)
        assert result.get("deleted") is True, result
        assert blob_available_indexed(shared, READ, exists_fn=_exists_true, index=index) is False

    async def test_update_entry_keeps_source_ref_fresh(self, quality_tempdir, store):
        from mcp_server.tools.crud import update_entry

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)

        result = await update_entry(
            {"knowledge_id": sid, "content": "# Source\nОбновлён Ф3b2."}, app
        )

        assert "error" not in result, result
        refs = index.get(_sha(1))
        assert len(refs) == 1 and refs[0].source_id == sid


# ── 7. refresh API: единая точка рескана ─────────────────────


class TestRefreshApi:
    async def test_refresh_rebuilds_from_ssot(self, store):
        from mcp_server.tools.source_ref_runtime import refresh_source_ref_index

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)
        index._by_sha.clear()   # «порча» индекса (симуляция рассинхрона)
        index._shas_of.clear()
        assert index.get(_sha(1)) == []

        result = await refresh_source_ref_index(app)

        assert result["refreshed"] is True
        assert len(index.get(_sha(1))) == 1

    async def test_refresh_fail_closed_on_error(self, store, monkeypatch):
        """Рескан после мутации упал → индекс ОПУСТОШАЕТСЯ (fail-closed:
        недоступность блобов, не ложная выдача по устаревшему кешу)."""
        from mcp_server.tools.source_ref_runtime import refresh_source_ref_index

        sid = await _seed_source(store, 1, zone="public")
        index = SourceRefIndex.build([await store.read(sid)])
        app = _app(store, index)
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index)

        async def _boom():
            raise RuntimeError("simulated FS failure on refresh")

        monkeypatch.setattr(store, "reindex_scan", _boom)
        result = await refresh_source_ref_index(app)

        assert result["refreshed"] is False
        assert result.get("fail_closed") is True
        assert len(index) == 0
        assert blob_available_indexed(_sha(1), SUBSCRIBER, exists_fn=_exists_true, index=index) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
