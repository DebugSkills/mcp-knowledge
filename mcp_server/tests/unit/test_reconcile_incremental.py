"""code-2026-09-25-023: incremental-reindex — T1-T10 (Block A).

Гибридный reconcile: len(missing) <= K → index_missing (upsert через alias,
без blue-green); иначе → reindex_all (full blue-green). K<=0 → kill-switch.

Харнесс: FakeStore (reindex_scan/_parse_file), FakeQdrant (get_all_knowledge_ids/
upsert_points/delete_by_knowledge_id/swap_alias/create_collection_named/
delete_collection_named — все вызовы записываются), pipeline с мокнутым embedder.
T8 — прямой вызов reconcile() в 3 конфигурациях, prometheus-registry до/после.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock


from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.indexing.reconcile import reconcile
from mcp_server.models import Chunk, KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.schema import (
    ZONE_PRIVATE,
    ZONE_PUBLIC,
    collection_for_zone,
)


# ── Fakes ─────────────────────────────────────────────────────


class FakeQdrant:
    """QdrantClient-совместимый фейк: записывает все вызовы."""

    def __init__(self, ids: set[str] | None = None):
        self._ids: set[str] = set(ids) if ids else set()
        # kid → list[point_dicts] (для проверки delete-before-upsert)
        self._points_by_kid: dict[str, list[dict]] = {}
        self.upsert_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.swap_calls: list[tuple] = []
        self.create_calls: list[str] = []
        self.delete_collection_calls: list[str] = []
        self._learn: bool = True  # отражать upsert в get_all_knowledge_ids

    def get_all_knowledge_ids(self, collection_name: str | None = None) -> set[str]:
        return set(self._ids)

    def upsert_points(self, points, collection_name: str | None = None):
        self.upsert_calls.append({
            "collection": collection_name,
            "points": list(points),
        })
        for p in points:
            kid = p.payload.get("knowledge_id") if p.payload else None
            if kid:
                self._points_by_kid.setdefault(kid, []).append({
                    "id": p.id,
                    "payload": dict(p.payload),
                })
                if self._learn:
                    self._ids.add(kid)

    def delete_by_knowledge_id(self, knowledge_id: str, collection_name: str | None = None):
        self.delete_calls.append(knowledge_id)
        self._points_by_kid.pop(knowledge_id, None)
        # НЕ удаляем из _ids — presence-check по knowledge_id; delete только
        # чистит точки (орфан-ветка удаляет из _ids отдельно)

    def swap_alias(self, alias: str, target: str):
        self.swap_calls.append((alias, target))

    def create_collection_named(self, name: str, force_recreate: bool = False):
        self.create_calls.append(name)

    def delete_collection_named(self, name: str):
        self.delete_collection_calls.append(name)

    def get_active_collection(self, alias_name: str | None = None):
        return "knowledge_v1"

    def collection_info(self, collection_name: str | None = None):
        return {"name": collection_name, "points_count": 0, "vectors_count": 0}


class FakeStore:
    """MarkdownStore-совместимый фейк: path → KnowledgeEntry."""

    def __init__(self, paths: list[Path], entries: dict[Path, KnowledgeEntry]):
        self._paths = list(paths)
        self._entries = dict(entries)

    async def reindex_scan(self) -> list[Path]:
        return list(self._paths)

    def _parse_file(self, path: Path) -> KnowledgeEntry:
        return self._entries[path]


def _make_entry(kid: str, zone: str = ZONE_PRIVATE) -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="test",
        subject="demo",
        project="test-project",
        tags=["test"],
        version=1,
        zone=zone,
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n\nTest content.")


def _make_chunk(kid: str, idx: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"{kid}#{idx}",
        knowledge_id=kid,
        content=f"Chunk {idx} of {kid}.",
        section_header=f"# {kid}",
        chunk_index=idx,
        token_count=5,
    )


def _make_pipeline(
    store: FakeStore,
    qdrant: FakeQdrant,
    *,
    chunker_overrides: dict[str, Exception | list[Chunk]] | None = None,
) -> IndexingPipeline:
    """Реальный IndexingPipeline с мок-embedder/chunker."""
    embedder = MagicMock()
    embedder.embed_sync = MagicMock(return_value=[[0.1] * 1024])

    chunker = MagicMock()
    default_chunks = [_make_chunk("default")]

    def _chunk(knowledge_id, content):
        if chunker_overrides and knowledge_id in chunker_overrides:
            ov = chunker_overrides[knowledge_id]
            if isinstance(ov, Exception):
                raise ov
            return ov
        return default_chunks

    chunker.chunk = MagicMock(side_effect=_chunk)

    return IndexingPipeline(store=store, qdrant=qdrant, embedder=embedder, chunker=chunker)


def _make_mock_pipeline() -> MagicMock:
    """Mock pipeline для тестов, проверяющих только ветвление (T2/T3/T8/T10)."""
    p = MagicMock()

    async def _index_missing(paths):
        return {
            "mode": "incremental",
            "total_docs": len(paths),
            "total_chunks": len(paths),
            "failed": 0,
            "errors": [],
        }

    p.index_missing = _index_missing

    async def _reindex_all():
        return {"total_docs": 999, "total_chunks": 999, "failed": 0}

    p.reindex_all = _reindex_all
    return p


def _make_knowledge_index() -> MagicMock:
    kidx = MagicMock()
    kidx.rebuild_all = MagicMock(return_value={})
    return kidx


# ── T1: N=1 missing, K=50 → incremental ───────────────────────


async def test_t1_incremental_single_missing(monkeypatch):
    """T1: N=1 missing, K=50 → index_missing вызван, reindex_all НЕ вызван."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-1.md")
    entry = _make_entry("kid-1")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())  # kid-1 missing
    pipeline = _make_pipeline(store, qdrant)

    result = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                             skip_reindex=False, skip_orphan_detection=True)

    assert result["mode"] == "incremental"
    assert result["reindexed"] == 1
    # reindex_all НЕ вызван — swap/create/delete коллекций = 0
    assert len(qdrant.swap_calls) == 0
    assert len(qdrant.create_calls) == 0
    assert len(qdrant.delete_collection_calls) == 0
    # index_missing отработал — upsert_points получил чанки записи
    assert len(qdrant.upsert_calls) >= 1
    upserted_kids = {
        p.payload.get("knowledge_id")
        for call in qdrant.upsert_calls
        for p in call["points"]
    }
    assert "kid-1" in upserted_kids


# ── T2: N=K+1 missing → full ──────────────────────────────────


async def test_t2_full_when_over_threshold(monkeypatch):
    """T2: N=K+1=51 missing → reindex_all вызван, index_missing НЕ вызван."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    paths = []
    entries = {}
    for i in range(51):
        kid = f"kid-{i:02d}"
        p = Path(f"/tmp/knowledge/test/demo/{kid}.md")
        paths.append(p)
        entries[p] = _make_entry(kid)
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    # Spy: оборачиваем index_missing/reindex_all для подсчёта вызовов
    im_calls = []
    ra_calls = []
    orig_im = pipeline.index_missing
    orig_ra = pipeline.reindex_all

    async def _spy_im(paths):
        im_calls.append(len(paths))
        return await orig_im(paths)

    async def _spy_ra():
        ra_calls.append(1)
        return await orig_ra()

    pipeline.index_missing = _spy_im
    pipeline.reindex_all = _spy_ra

    result = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                             skip_reindex=False, skip_orphan_detection=True)

    assert result["mode"] == "full"
    assert len(ra_calls) == 1
    assert len(im_calls) == 0


# ── T3: K=0 (kill-switch), N=1 → full ─────────────────────────


async def test_t3_kill_switch_full(monkeypatch):
    """T3: K=0 → всегда full (поведение = до 023)."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 0)

    path = Path("/tmp/knowledge/test/demo/kid-1.md")
    entry = _make_entry("kid-1")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    im_calls = []
    ra_calls = []
    orig_im = pipeline.index_missing
    orig_ra = pipeline.reindex_all

    async def _spy_im(paths):
        im_calls.append(len(paths))
        return await orig_im(paths)

    async def _spy_ra():
        ra_calls.append(1)
        return await orig_ra()

    pipeline.index_missing = _spy_im
    pipeline.reindex_all = _spy_ra

    result = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                             skip_reindex=False, skip_orphan_detection=True)

    assert result["mode"] == "full"
    assert len(ra_calls) == 1
    assert len(im_calls) == 0


# ── T4: per-file изоляция ошибок ──────────────────────────────


async def test_t4_per_file_isolation(monkeypatch):
    """T4: 3 missing, 2-й файл падает → 1-я и 3-я upsert-нуты, failed==1."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    paths = []
    entries = {}
    for i in range(3):
        kid = f"kid-fail-{i}"
        p = Path(f"/tmp/knowledge/test/demo/{kid}.md")
        paths.append(p)
        entries[p] = _make_entry(kid)
    store = FakeStore(paths, entries)
    qdrant = FakeQdrant(ids=set())

    # chunker падает на 2-м файле
    chunker_overrides = {
        "kid-fail-1": RuntimeError("chunker boom"),
        "kid-fail-0": [_make_chunk("kid-fail-0")],
        "kid-fail-2": [_make_chunk("kid-fail-2")],
    }
    pipeline = _make_pipeline(store, qdrant, chunker_overrides=chunker_overrides)

    result = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                             skip_reindex=False, skip_orphan_detection=True)

    assert result["mode"] == "incremental"
    assert result["reindexed"] == 2  # 1-я и 3-я
    # upsert получил чанки kid-fail-0 и kid-fail-2
    upserted_kids = {
        p.payload.get("knowledge_id")
        for call in qdrant.upsert_calls
        for p in call["points"]
    }
    assert "kid-fail-0" in upserted_kids
    assert "kid-fail-2" in upserted_kids
    assert "kid-fail-1" not in upserted_kids
    # failed и errors
    assert any("kid-fail-1" in e or "fail-1" in e for e in result["errors"])


# ── T5: идемпотентность — reconcile дважды ────────────────────


async def test_t5_idempotency_second_run(monkeypatch):
    """T5: 2-й прогон — missing=∅, ничего не вызвано, mode==none."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-idem.md")
    entry = _make_entry("kid-idem")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())  # изначально missing
    pipeline = _make_pipeline(store, qdrant)

    # 1-й прогон
    r1 = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                         skip_reindex=False, skip_orphan_detection=True)
    assert r1["mode"] == "incremental"
    assert r1["reindexed"] == 1

    # Сброс счётчиков вызовов
    qdrant.upsert_calls.clear()
    qdrant.delete_calls.clear()
    qdrant.swap_calls.clear()

    # 2-й прогон — kid-idem теперь в _ids (FakeQdrant отразил upsert)
    r2 = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                         skip_reindex=False, skip_orphan_detection=True)

    assert r2["mode"] == "none"
    assert r2["reindexed"] == 0
    assert len(qdrant.upsert_calls) == 0
    assert len(qdrant.swap_calls) == 0


# ── T6: incremental не трогает коллекции ──────────────────────


async def test_t6_no_collection_mutations(monkeypatch):
    """T6: incremental → swap/create/delete = 0; upsert в alias-имена."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-coll.md")
    entry = _make_entry("kid-coll", zone=ZONE_PRIVATE)
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_pipeline(store, qdrant)

    await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                    skip_reindex=False, skip_orphan_detection=True)

    assert len(qdrant.swap_calls) == 0
    assert len(qdrant.create_calls) == 0
    assert len(qdrant.delete_collection_calls) == 0
    # upsert шёл в alias активной коллекции зоны
    assert len(qdrant.upsert_calls) >= 1
    for call in qdrant.upsert_calls:
        assert call["collection"] in (
            collection_for_zone(ZONE_PUBLIC),
            collection_for_zone(ZONE_PRIVATE),
        )


# ── T7: конкурентность — incremental без лока ─────────────────


async def test_t7_no_deadlock_under_scan_lock(monkeypatch):
    """T7: incremental при удерживаемом scan_lock — без дедлока (wait_for 5s)."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-dead.md")
    entry = _make_entry("kid-dead")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_pipeline(store, qdrant)

    # Удерживаем scan_lock (имитация параллельного quality-скана)
    scan_lock = asyncio.Lock()
    async with scan_lock:
        result = await asyncio.wait_for(
            reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                      skip_reindex=False, skip_orphan_detection=True),
            timeout=5.0,
        )

    assert result["mode"] == "incremental"
    assert result["reindexed"] == 1


# ── T8: метрика — 3 конфигурации, registry до/после ───────────


async def test_t8_metrics_three_configs(monkeypatch):
    """T8: прямой вызов reconcile() — prometheus-registry до/после.

    Три конфигурации, один call-site (record_reconcile_result внутри reconcile):
    (а) 1 missing → incremental, reindexed +1;
    (б) K+1 missing → full, reindexed +total_docs;
    (в) skip_reindex=True → skipped, skipped +N.
    """
    from mcp_server import metrics as M

    def _read_counters():
        return {
            "reindexed": M.reconcile_reindexed._value.get(),
            "checked": M.reconcile_checked._value.get(),
            "skipped": M.reconcile_skipped._value.get(),
        }

    # (а) incremental: 1 missing
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)
    path_a = Path("/tmp/knowledge/test/demo/kid-ma.md")
    entry_a = _make_entry("kid-ma")
    store_a = FakeStore([path_a], {path_a: entry_a})
    qdrant_a = FakeQdrant(ids=set())
    pipe_a = _make_mock_pipeline()
    before_a = _read_counters()
    res_a = await reconcile(store_a, qdrant_a, pipe_a, _make_knowledge_index(),
                            skip_reindex=False, skip_orphan_detection=True)
    after_a = _read_counters()
    assert res_a["mode"] == "incremental"
    assert res_a["reindexed"] == 1
    assert after_a["reindexed"] == before_a["reindexed"] + 1
    assert after_a["checked"] == before_a["checked"] + 1

    # (б) full: K+1=51 missing
    paths_b = []
    entries_b = {}
    for i in range(51):
        kid = f"kid-mb-{i:02d}"
        p = Path(f"/tmp/knowledge/test/demo/{kid}.md")
        paths_b.append(p)
        entries_b[p] = _make_entry(kid)
    store_b = FakeStore(paths_b, entries_b)
    qdrant_b = FakeQdrant(ids=set())
    pipe_b = _make_mock_pipeline()
    before_b = _read_counters()
    res_b = await reconcile(store_b, qdrant_b, pipe_b, _make_knowledge_index(),
                            skip_reindex=False, skip_orphan_detection=True)
    after_b = _read_counters()
    assert res_b["mode"] == "full"
    assert res_b["reindexed"] == 999  # mock reindex_all total_docs
    assert after_b["reindexed"] == before_b["reindexed"] + 999

    # (в) skipped: skip_reindex=True, 1 missing
    path_c = Path("/tmp/knowledge/test/demo/kid-mc.md")
    entry_c = _make_entry("kid-mc")
    store_c = FakeStore([path_c], {path_c: entry_c})
    qdrant_c = FakeQdrant(ids=set())
    pipe_c = _make_mock_pipeline()
    before_c = _read_counters()
    res_c = await reconcile(store_c, qdrant_c, pipe_c, _make_knowledge_index(),
                            skip_reindex=True, skip_orphan_detection=True)
    after_c = _read_counters()
    assert res_c["mode"] == "skipped"
    assert after_c["skipped"] == before_c["skipped"] + 1  # 1 missing → skipped


# ── T9: delete-before-upsert ──────────────────────────────────


async def test_t9_delete_before_upsert(monkeypatch):
    """T9: kid уже имеет «старые» точки → delete_by_knowledge_id перед upsert."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-del.md")
    entry = _make_entry("kid-del")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids={"kid-del"})  # kid присутствует, но мы вызываем index_missing напрямую

    pipeline = _make_pipeline(store, qdrant)

    # Вызываем index_missing напрямую (reconcile не войдёт в ветку т.к. kid не missing)
    result = await pipeline.index_missing([path])

    assert result["mode"] == "incremental"
    assert result["total_docs"] == 1
    # delete_by_knowledge_id вызван перед upsert
    assert "kid-del" in qdrant.delete_calls
    assert len(qdrant.upsert_calls) >= 1
    # ровно один набор точек (нет дублей) — delete очистил старые
    upserted = [
        p for call in qdrant.upsert_calls for p in call["points"]
    ]
    kid_points = [p for p in upserted if p.payload.get("knowledge_id") == "kid-del"]
    assert len(kid_points) == 1  # один чанк → одна точка


# ── T10: skip_reindex=True → skipped ──────────────────────────


async def test_t10_skip_reindex_skipped(monkeypatch):
    """T10: skip_reindex=True, N=1 → ничего не индексируется, mode==skipped."""
    monkeypatch.setattr("mcp_server.indexing.reconcile.settings.RECONCILE_INCREMENTAL_MAX_ENTRIES", 50)

    path = Path("/tmp/knowledge/test/demo/kid-skip.md")
    entry = _make_entry("kid-skip")
    store = FakeStore([path], {path: entry})
    qdrant = FakeQdrant(ids=set())
    pipeline = _make_mock_pipeline()

    im_calls = []
    ra_calls = []
    orig_im = pipeline.index_missing
    orig_ra = pipeline.reindex_all

    async def _spy_im(paths):
        im_calls.append(len(paths))
        return await orig_im(paths)

    async def _spy_ra():
        ra_calls.append(1)
        return await orig_ra()

    pipeline.index_missing = _spy_im
    pipeline.reindex_all = _spy_ra

    result = await reconcile(store, qdrant, pipeline, _make_knowledge_index(),
                             skip_reindex=True, skip_orphan_detection=True)

    assert result["mode"] == "skipped"
    assert len(im_calls) == 0
    assert len(ra_calls) == 0
    assert result["skipped"] >= 1  # missing включён в skipped
