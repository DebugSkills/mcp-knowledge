"""Ф3-fix1 (P1-2 от Critic Ф3 iter.1): bulk_deprecate_duplicates — SSOT-first.

Критик-факт: payload-only ветка (tools/quality.py, ветка «qdrant-only» в
bulk_deprecate_duplicates) не писала frontmatter.status → полный reindex
строит payload из SSOT (published) и ВОСКРЕШАЕТ bulk-скрытия (зонд критика
.trash/phase3-bulk-resurrect-e2e.py: visible=['bulk-book'] — RESURRECTED;
acceptance-2 нарушался для bulk/auto-класса). Тот же класс уже закрыт для
одиночного deprecate и merge (_lifecycle_transition, комментарий
«иначе полный reindex воскрешал merge-жертву»); здесь закрывается граница.

Контракт Ф3-fix1:
- SSOT-first batch: ОДИН MarkdownStore.set_status_many на пачку →
  ОДИН git-коммит на пачку (прецедент M11/delete_many);
- payload — вторым шагом, ТОЛЬКО записям с подтверждённым SSOT-статусом
  (производная не опережает источник правды);
- регресс «resurrect»: bulk → ПОЛНЫЙ reindex (реальный _index_chunks,
  мок только embedder/qdrant) → записи скрыты (0 visible);
- идемпотентность ×2: второй прогон без нового git-коммита;
- audit сохранён (action=bulk_deprecate, metadata.issues_closed);
- Source-цели: bulk инвалидирует source_ref_index (refresh из SSOT —
  ref.status=deprecated; availability больше не выдаёт blob ложно).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.quality import bulk_deprecate_duplicates

BULK_A = "bulk-dup-a"
BULK_B = "bulk-dup-b"
KEEP = "bulk-keep"


# ── Fixtures (паттерн test_deprecate_lifecycle) ───────────────


@pytest.fixture
def quality_tempdir(tmp_path):
    from mcp_server.quality.audit import set_store_dir as audit_set_dir
    from mcp_server.quality.issues import set_store_dir

    set_store_dir(str(tmp_path))
    audit_set_dir(str(tmp_path))
    yield str(tmp_path)


@pytest.fixture
def store(tmp_path):
    import git

    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


def _fm(kid: str, **overrides) -> KnowledgeFrontmatter:
    kwargs: dict = {"knowledge_id": kid, "domain": "library",
                    "subject": "bibliography"}
    kwargs.update(overrides)
    return KnowledgeFrontmatter(**kwargs)


async def _seed(store: MarkdownStore, *entries: KnowledgeEntry) -> None:
    for e in entries:
        await store.write_entry(e)
    await store.flush("seed: bulk-ssot fixtures")


class _FakeQdrant:
    """I/O-заглушка Qdrant (scroll/set_payload по Filter; must + must_not)."""

    def __init__(self):
        self.points: list[SimpleNamespace] = []
        self.set_payload_calls: list[dict] = []

    @staticmethod
    def _match(payload: dict, flt) -> bool:
        if flt is None:
            return True
        for cond in getattr(flt, "must", None) or []:
            if payload.get(cond.key) != cond.match.value:
                return False
        for cond in getattr(flt, "must_not", None) or []:
            if payload.get(cond.key) == cond.match.value:
                return False
        return True

    def seed(self, kid: str) -> None:
        self.points.append(SimpleNamespace(
            id=f"pid-{kid}", payload={"knowledge_id": kid, "chunk_id": f"{kid}#0"},
        ))

    def payloads(self, kid: str) -> list[dict]:
        return [p.payload for p in self.points if p.payload.get("knowledge_id") == kid]

    def scroll(self, scroll_filter=None, limit=1000, offset=None,
               with_payload=None, with_vectors=False, collection_name=None):
        matched = [p for p in self.points if self._match(p.payload, scroll_filter)]
        start = int(offset or 0)
        page = matched[start:start + limit]
        nxt = start + len(page) if start + len(page) < len(matched) else None
        return page, nxt

    def upsert_points(self, pts, collection_name=None):
        """Reindex-upsert: старые точки kid заменяются новыми (как _reindex_into)."""
        for pt in pts:
            kid = pt.payload.get("knowledge_id")
            self.points = [
                p for p in self.points if p.payload.get("knowledge_id") != kid
            ]
            self.points.append(SimpleNamespace(id=pt.id, payload=dict(pt.payload)))

    def set_payload(self, payload, points_filter=None, collection_name=None):
        self.set_payload_calls.append({"payload": dict(payload)})
        n = 0
        for p in self.points:
            if self._match(p.payload, points_filter):
                p.payload.update(payload)
                n += 1
        return n


def _app(store, qdrant, **extra) -> SimpleNamespace:
    return SimpleNamespace(store=store, qdrant=qdrant, data_version=0, **extra)


def _git_commits(store: MarkdownStore) -> int:
    return len(list(store._repo.iter_commits()))


def _fallback_chunker():
    from unittest.mock import patch

    from mcp_server.embedding.tokenizer import _FallbackTokenizer, XlmRobertaTokenizer
    from mcp_server.indexing.chunker import MarkdownChunker

    tok = XlmRobertaTokenizer()
    tok._tok = _FallbackTokenizer()
    return patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok), MarkdownChunker()


async def _full_reindex(store: MarkdownStore, qdrant, kids: list[str]) -> None:
    """«Полный reindex»: старые точки стёрты, каждый .md заново через реальный
    IndexingPipeline._index_chunks (мок ТОЛЬКО embedder/qdrant)."""
    qdrant.points.clear()
    patcher, chunker = _fallback_chunker()
    pipe = IndexingPipeline(
        store=SimpleNamespace(), qdrant=qdrant,
        embedder=SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts)),
        chunker=chunker,
    )
    with patcher:
        for kid in kids:
            entry = await store.read(kid)
            chunks = chunker.chunk(
                kid, entry.content, locator_spans=entry.frontmatter.locator_spans,
            )
            await pipe._index_chunks(entry, chunks)


# ── Тесты ─────────────────────────────────────────────────────


class TestBulkSsotFirst:
    async def test_bulk_writes_ssot_single_commit_payload_and_audit(
        self, quality_tempdir, store,
    ):
        """bulk → frontmatter.status=deprecated (ОДИН git-коммит на пачку),
        payload вторым шагом, issues закрыты, audit записан."""
        from mcp_server.quality.audit import count_actions
        from mcp_server.quality.issues import create_issue, list_issues

        await _seed(
            store,
            KnowledgeEntry(frontmatter=_fm(BULK_A), content="# A\nДубль."),
            KnowledgeEntry(frontmatter=_fm(BULK_B), content="# B\nДубль."),
            KnowledgeEntry(frontmatter=_fm(KEEP), content="# Keep\nОригинал."),
        )
        create_issue("duplicate", BULK_A, "warn", "Dup of KEEP")
        create_issue("duplicate", BULK_A, "warn", "Dup of KEEP (2)")
        qdrant = _FakeQdrant()
        qdrant.seed(BULK_A)
        qdrant.seed(BULK_B)
        commits_before = _git_commits(store)

        result = await bulk_deprecate_duplicates(
            {"knowledge_id": [BULK_A, BULK_B], "reason": "Ф3-fix1"}, _app(store, qdrant),
        )

        assert result["resolved"] is True, result
        assert result["deprecated_count"] == 2
        # SSOT: обе цели deprecated, непричастная запись нетронута
        assert (await store.read(BULK_A)).frontmatter.status == "deprecated"
        assert (await store.read(BULK_B)).frontmatter.status == "deprecated"
        assert (await store.read(KEEP)).frontmatter.status == "published"
        # ОДИН git-коммит на пачку (не по одному на запись)
        assert _git_commits(store) == commits_before + 1
        # payload — вторым шагом
        assert qdrant.payloads(BULK_A)[0]["status"] == "deprecated"
        assert qdrant.payloads(BULK_B)[0]["status"] == "deprecated"
        # issues закрыты, audit по каждой записи
        assert result["issues_closed"] == 2
        assert not [i for i in list_issues(status="open") if i.knowledge_id == BULK_A]
        assert count_actions(action="bulk_deprecate") == 2

    async def test_bulk_then_full_reindex_stays_hidden(self, quality_tempdir, store):
        """РЕГРЕСС resurrect (ядро P1-2): bulk → ПОЛНЫЙ reindex → записи
        скрыты (status переносится в payload, поиск 0 visible)."""
        from mcp_server.quality.lifecycle import build_search_filter

        await _seed(
            store,
            KnowledgeEntry(frontmatter=_fm(BULK_A), content="# A\nДубль."),
            KnowledgeEntry(frontmatter=_fm(KEEP), content="# Keep\nОригинал."),
        )
        qdrant = _FakeQdrant()
        qdrant.seed(BULK_A)
        qdrant.seed(KEEP)

        result = await bulk_deprecate_duplicates(
            {"knowledge_id": BULK_A, "reason": "resurrect regression"}, _app(store, qdrant),
        )
        assert result["resolved"] is True and result["deprecated_count"] == 1

        await _full_reindex(store, qdrant, [BULK_A, KEEP])

        # SSOT → payload: метка НЕ смыта reindex-ом (нет resurrect)
        assert qdrant.payloads(BULK_A)[0].get("status") == "deprecated", (
            "RESURRECTED: bulk-скрытие вернулось после полного reindex"
        )
        assert "status" not in qdrant.payloads(KEEP)[0]
        # Поиск: 0 visible среди deprecated
        flt = build_search_filter(include_deprecated=False)
        visible = [
            p.payload.get("knowledge_id") for p in qdrant.points
            if all(p.payload.get(c["key"]) != c["match"]["value"] for c in flt["must_not"])
        ]
        assert BULK_A not in visible, f"RESURRECTED (visible): {visible}"
        assert visible == [KEEP]

    async def test_bulk_idempotent_double_run(self, quality_tempdir, store):
        """×2 bulk на те же цели → второй прогон: без нового git-коммита,
        статусы стабильны, issues не пере-закрываются, resolved=True."""
        await _seed(
            store,
            KnowledgeEntry(frontmatter=_fm(BULK_A), content="# A\nДубль."),
            KnowledgeEntry(frontmatter=_fm(BULK_B), content="# B\nДубль."),
        )
        qdrant = _FakeQdrant()
        qdrant.seed(BULK_A)
        qdrant.seed(BULK_B)
        app = _app(store, qdrant)

        first = await bulk_deprecate_duplicates({"knowledge_id": [BULK_A, BULK_B]}, app)
        assert first["resolved"] is True and first["deprecated_count"] == 2
        commits_after_first = _git_commits(store)

        second = await bulk_deprecate_duplicates({"knowledge_id": [BULK_A, BULK_B]}, app)

        assert second["resolved"] is True, second
        assert second["deprecated_count"] == 2  # записи ЕСТЬ deprecated
        assert _git_commits(store) == commits_after_first, "дубль git-коммита запрещён"
        assert second["issues_closed"] == 0
        assert (await store.read(BULK_A)).frontmatter.status == "deprecated"
        assert qdrant.payloads(BULK_A)[0]["status"] == "deprecated"

    async def test_bulk_missing_ssot_entry_skips_payload(self, quality_tempdir, store):
        """Цели нет в SSOT → FAILED side-effect, deprecated_count=0,
        payload НЕ пишется (производная не опережает источник правды)."""
        await _seed(
            store,
            KnowledgeEntry(frontmatter=_fm(KEEP), content="# Keep\nОригинал."),
        )
        qdrant = _FakeQdrant()
        qdrant.seed("bulk-ghost")

        result = await bulk_deprecate_duplicates(
            {"knowledge_id": "bulk-ghost"}, _app(store, qdrant),
        )

        assert result["resolved"] is True, result
        assert result["deprecated_count"] == 0
        assert any("bulk-ghost" in s for s in result["side_effects"])
        assert qdrant.set_payload_calls == [], "payload без SSOT запрещён"

    async def test_bulk_source_entry_invalidates_ref_index(
        self, quality_tempdir, store,
    ):
        """Source-цель → bulk инвалидирует source_ref_index: ref.status
        становится deprecated (refresh из SSOT), blob больше не «доступен»."""
        from mcp_server.tools.source_ref_index import SourceRefIndex
        from mcp_server.tools.source_ref_runtime import (
            SOURCE_REF_INDEX_ATTR,
            init_source_ref_index,
        )

        sha = "ab" * 32
        await _seed(
            store,
            KnowledgeEntry(
                frontmatter=_fm(
                    "bulk-src", content_type="source",
                    blobs={"original": {"sha256": sha, "size": 10}},
                ),
                content="# Source\nblob.",
            ),
        )
        index: SourceRefIndex = await init_source_ref_index(store)
        assert index.get(sha) and index.get(sha)[0].status == "published"

        qdrant = _FakeQdrant()
        qdrant.seed("bulk-src")
        app = _app(store, qdrant, **{SOURCE_REF_INDEX_ATTR: index})

        result = await bulk_deprecate_duplicates(
            {"knowledge_id": "bulk-src", "reason": "src dup"}, app,
        )

        assert result["resolved"] is True and result["deprecated_count"] == 1
        refs = index.get(sha)
        assert refs and refs[0].status == "deprecated", (
            "source_ref_index не инвалидирован: stale ref держит published"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
