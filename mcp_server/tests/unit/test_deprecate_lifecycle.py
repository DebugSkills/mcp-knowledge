"""Ф3a (trace code-2026-10-02-bibliography): lifecycle deprecated — двойная запись SSOT-first.

План §3.6:225, фаза Ф3 (строка 336). Контракт Ф3a:
- deprecate → frontmatter.status=="deprecated" (РЕАЛЬНЫЙ MarkdownStore + git)
  И payload status=="deprecated" (I/O-заглушка Qdrant);
- полный reindex (реальный IndexingPipeline._index_chunks, мок ТОЛЬКО
  embedder/qdrant) → статус сохранился в payload (build_payload_point
  += status из frontmatter — reindex не смывает метку);
- root (книга, parent_knowledge_id=None) deprecate → cascade ДЕФОЛТ и
  единственный путь: все секции deprecated в SSOT и payload;
- SSOT-first: payload-путь падает после успешной SSOT → SSOT остаётся
  deprecated (winner); порядок записи SSOT→payload (обратный запрещён);
- идемпотентность: повторный deprecate — no-op (нет дубль git-коммитов,
  версий, data_version);
- restore симметричен (root cascade default, published в SSOT и payload);
- решение PAYLOAD_INDEXES: status НЕ индексируется (прецедент факт №16 —
  без Qdrant-индексов миграция коллекций не нужна; search-фильтр must_not
  работает и по неиндексированному payload; availability Ф3b читает SSOT).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.storage.schema import PAYLOAD_INDEXES, build_payload_point
from mcp_server.tools.quality import resolve_quality_issue
from mcp_server.tools.search import search_knowledge

ROOT_ID = "test-ph3a-book"
SEC1_ID = "test-ph3a-book-sec-1"
SEC2_ID = "test-ph3a-book-sec-2"


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def quality_tempdir(tmp_path):
    """Изоляция issues/audit store (как test_quality_tools.quality_tempdir)."""
    from mcp_server.quality.audit import set_store_dir as audit_set_dir
    from mcp_server.quality.issues import set_store_dir

    set_store_dir(str(tmp_path))
    audit_set_dir(str(tmp_path))
    yield str(tmp_path)


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (SSOT-first путь с коммитами)."""
    import git

    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


def _fm(kid: str, *, parent: str | None = None, seq: int | None = None,
        status: str | None = None) -> KnowledgeFrontmatter:
    kwargs: dict = {}
    if status is not None:
        kwargs["status"] = status
    return KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="book",
        parent_knowledge_id=parent,
        sequence_number=seq,
        **kwargs,
    )


async def _seed_book(store: MarkdownStore) -> None:
    """Root-книга + 2 секции в SSOT (один git-коммит на посев)."""
    root = KnowledgeEntry(frontmatter=_fm(ROOT_ID), content="# Книга Ф3a\nКонтент корня.")
    sec1 = KnowledgeEntry(frontmatter=_fm(SEC1_ID, parent=ROOT_ID, seq=1), content="Секция 1.")
    sec2 = KnowledgeEntry(frontmatter=_fm(SEC2_ID, parent=ROOT_ID, seq=2), content="Секция 2.")
    for e in (root, sec1, sec2):
        await store.write_entry(e)
    await store.flush("seed: Ф3a fixtures")


class _FakeQdrant:
    """I/O-заглушка Qdrant: scroll/set_payload/upsert_points по Filter.

    Точки живут в payload-словарях — set_payload мутирует их на месте,
    что позволяет читать итоговый статус напрямую.
    """

    def __init__(self):
        self.points: list[SimpleNamespace] = []
        self.set_payload_calls: list[dict] = []
        self.events: list[str] = []  # порядок операций (SSOT-first тест)
        self.fail_set_payload = False

    # -- helpers -------------------------------------------------------

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

    def seed(self, kid: str, parent: str | None = None) -> None:
        payload = {"knowledge_id": kid, "chunk_id": f"{kid}#0"}
        if parent:
            payload["parent_knowledge_id"] = parent
        self.points.append(SimpleNamespace(id=f"pid-{kid}", payload=payload))

    def payloads(self, kid: str) -> list[dict]:
        return [p.payload for p in self.points if p.payload.get("knowledge_id") == kid]

    # -- Qdrant API ----------------------------------------------------

    def upsert_points(self, pts, collection_name=None):
        self.points.extend(
            SimpleNamespace(id=p.id, payload=dict(p.payload)) for p in pts
        )

    def scroll(self, scroll_filter=None, limit=1000, offset=None,
               with_payload=None, with_vectors=False, collection_name=None):
        matched = [p for p in self.points if self._match(p.payload, scroll_filter)]
        start = int(offset or 0)
        page = matched[start:start + limit]
        next_off = start + len(page)
        nxt = next_off if next_off < len(matched) else None
        return page, nxt

    def search(self, vector, top_k=5, filters=None, score_threshold=0.0,
               with_vectors=False, exclude_content_types=None,
               exclude_statuses=None, offset=0, collection_name=None):
        """Семантика storage.qdrant_client.search: must + must_not
        (exclude_statuses/exclude_content_types) → ScoredPoint-заглушки."""
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        must, must_not = [], []
        for key, value in (filters or {}).items():
            if isinstance(value, list):
                must.append(FieldCondition(key=key, match=MatchAny(any=value)))
            else:
                must.append(FieldCondition(key=key, match=MatchValue(value=value)))
        must_not.extend(
            FieldCondition(key="content_type", match=MatchValue(value=ct))
            for ct in exclude_content_types or []
        )
        must_not.extend(
            FieldCondition(key="status", match=MatchValue(value=st))
            for st in exclude_statuses or []
        )
        flt = Filter(must=must or None, must_not=must_not or None) if (must or must_not) else None
        matched = [p for p in self.points if self._match(p.payload, flt)]
        page = matched[int(offset):int(offset) + int(top_k)]
        return [
            SimpleNamespace(id=p.id, payload=dict(p.payload), score=0.99)
            for p in page
        ]

    def set_payload(self, payload, points_filter=None, collection_name=None):
        self.set_payload_calls.append({"payload": dict(payload)})
        self.events.append("payload")
        if self.fail_set_payload:
            raise RuntimeError("simulated payload-path failure (Ф3a test)")
        n = 0
        for p in self.points:
            if self._match(p.payload, points_filter):
                p.payload.update(payload)
                n += 1
        return n


def _app_state(store, qdrant, data_version: int = 7) -> SimpleNamespace:
    return SimpleNamespace(store=store, qdrant=qdrant, data_version=data_version)


def _git_commits(store: MarkdownStore) -> int:
    return len(list(store._repo.iter_commits()))


def _fallback_chunker():
    """Chunker с fallback-токенайзером (как test_source_locator_payload)."""
    from unittest.mock import patch

    from mcp_server.embedding.tokenizer import _FallbackTokenizer, XlmRobertaTokenizer
    from mcp_server.indexing.chunker import MarkdownChunker

    tok = XlmRobertaTokenizer()
    tok._tok = _FallbackTokenizer()
    return patch("mcp_server.indexing.chunker.xlmr_tokenizer", tok), MarkdownChunker()


def _pipeline(qdrant) -> IndexingPipeline:
    embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
    _, chunker = _fallback_chunker()
    return IndexingPipeline(
        store=SimpleNamespace(), qdrant=qdrant, embedder=embedder, chunker=chunker
    )


# ── 1. Двойная запись: SSOT frontmatter + payload ─────────────


class TestDeprecateDoubleWrite:
    async def test_deprecate_writes_frontmatter_and_payload(self, quality_tempdir, store):
        """deprecate → frontmatter.status=="deprecated" (реальный store+git)
        И payload status=="deprecated"."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        commits_before = _git_commits(store)

        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate", "reason": "Ф3a test"},
            _app_state(store, qdrant),
        )

        assert result["resolved"] is True, result
        entry = await store.read(ROOT_ID)
        assert entry.frontmatter.status == "deprecated"
        assert _git_commits(store) == commits_before + 1  # SSOT-коммит создан
        assert qdrant.payloads(ROOT_ID)[0]["status"] == "deprecated"

    async def test_deprecate_missing_ssot_entry_aborts(self, quality_tempdir, store):
        """Записи нет в SSOT → deprecate НЕ пишет payload (SSOT-first:
        производную нельзя писать без источника правды)."""
        qdrant = _FakeQdrant()
        qdrant.seed("test-ph3a-ghost")

        result = await resolve_quality_issue(
            {"knowledge_id": "test-ph3a-ghost", "action": "deprecate"}, _app_state(store, qdrant)
        )

        assert result["resolved"] is False
        assert "SSOT" in result["error"]
        assert qdrant.set_payload_calls == []


# ── 2. Root cascade — ДЕФОЛТ и единственный путь ──────────────


class TestRootCascadeDefault:
    async def test_root_deprecate_cascades_without_param(self, quality_tempdir, store):
        """Root (parent_knowledge_id=None) → cascade без параметра:
        ВСЕ секции deprecated в SSOT и в payload."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        qdrant.seed(SEC1_ID, parent=ROOT_ID)
        qdrant.seed(SEC2_ID, parent=ROOT_ID)

        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"},  # БЕЗ cascade
            _app_state(store, qdrant),
        )

        assert result["resolved"] is True
        assert result["cascade_affected"] == 2
        for kid in (ROOT_ID, SEC1_ID, SEC2_ID):
            entry = await store.read(kid)
            assert entry.frontmatter.status == "deprecated", kid
            assert qdrant.payloads(kid)[0]["status"] == "deprecated", kid

    async def test_root_cascade_forced_even_if_false_requested(self, quality_tempdir, store):
        """Для root cascade=True — единственный путь: явный cascade=False
        для книги НЕ отключает каскад (иначе SSOT-потомки разъедутся с root)."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        qdrant.seed(SEC1_ID, parent=ROOT_ID)

        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate", "cascade": False},
            _app_state(store, qdrant),
        )

        assert result["resolved"] is True
        assert result["cascade_affected"] == 1
        assert (await store.read(SEC1_ID)).frontmatter.status == "deprecated"

    async def test_section_deprecate_respects_param(self, quality_tempdir, store):
        """Секционная запись → режим как был: без cascade ТОЛЬКО сама запись."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(SEC1_ID, parent=ROOT_ID)
        qdrant.seed(SEC2_ID, parent=ROOT_ID)

        result = await resolve_quality_issue(
            {"knowledge_id": SEC1_ID, "action": "deprecate"}, _app_state(store, qdrant)
        )

        assert result["resolved"] is True
        assert result["cascade_affected"] == 0
        assert (await store.read(SEC1_ID)).frontmatter.status == "deprecated"
        assert (await store.read(SEC2_ID)).frontmatter.status == "published"
        assert qdrant.payloads(SEC2_ID)[0].get("status") != "deprecated"


# ── 3. Полный reindex не смывает метку ────────────────────────


class TestReindexPreservesStatus:
    async def test_reindex_of_deprecated_entry_keeps_status(self, quality_tempdir, store):
        """Deprecated-запись → реальный _index_chunks → payload точки несёт
        status="deprecated" (метка из frontmatter, не из payload-инференса)."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)

        await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, _app_state(store, qdrant)
        )
        qdrant.points.clear()  # «полный reindex»: старые точки стёрты

        entry = await store.read(ROOT_ID)  # перечитано с диска (SSOT)
        assert entry.frontmatter.status == "deprecated"
        with _fallback_chunker()[0]:
            chunks = _fallback_chunker()[1].chunk(
                ROOT_ID, entry.content, locator_spans=entry.frontmatter.locator_spans
            )
            pipe = _pipeline(qdrant)
            await pipe._index_chunks(entry, chunks)

        assert qdrant.payloads(ROOT_ID), "reindex должен создать точки"
        assert qdrant.payloads(ROOT_ID)[0]["status"] == "deprecated"

    async def test_reindex_of_published_entry_no_status_key(self, quality_tempdir, store):
        """Published-запись → reindex → ключа status НЕТ (sparse-запись:
        отсутствие = published, backward-compatible с Фазами 0-13)."""
        await _seed_book(store)
        qdrant = _FakeQdrant()

        entry = await store.read(ROOT_ID)
        assert entry.frontmatter.status == "published"
        with _fallback_chunker()[0]:
            chunks = _fallback_chunker()[1].chunk(
                ROOT_ID, entry.content, locator_spans=entry.frontmatter.locator_spans
            )
            await _pipeline(qdrant)._index_chunks(entry, chunks)

        assert "status" not in qdrant.payloads(ROOT_ID)[0]


# ── 4. SSOT-first: winner при расхождении, порядок записи ─────


class TestSsotFirst:
    async def test_payload_failure_ssot_wins(self, quality_tempdir, store):
        """Payload-путь падает ПОСЛЕ успешной SSOT → итоговое состояние
        deprecated (SSOT winner), операция резолвится с фиксацией расхождения."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.fail_set_payload = True
        qdrant.seed(ROOT_ID)

        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, _app_state(store, qdrant)
        )

        assert result["resolved"] is True, "SSOT записан → deprecated (winner)"
        entry = await store.read(ROOT_ID)
        assert entry.frontmatter.status == "deprecated"
        assert result.get("payload_error"), "расхождение должно быть зафиксировано"

    async def test_ssot_write_happens_before_payload(self, quality_tempdir, store):
        """Порядок: SSOT-запись STRICTLY до set_payload (обратный запрещён)."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        events: list[str] = []

        original = store.set_status_many

        async def traced(kids, status, commit_message=None):
            events.append("ssot")
            return await original(kids, status, commit_message=commit_message)

        store.set_status_many = traced

        await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, _app_state(store, qdrant)
        )

        merged = events + qdrant.events
        assert merged[0] == "ssot", f"первой должна быть SSOT-запись, было: {merged}"
        assert "payload" in merged


# ── 5. Идемпотентность ────────────────────────────────────────


class TestIdempotency:
    async def test_double_deprecate_no_dup_effects(self, quality_tempdir, store):
        """×2 deprecate → второй: без нового git-коммита, без роста версии,
        без data_version-инкремента, с флагом idempotent."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        app = _app_state(store, qdrant)

        first = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, app
        )
        assert first["resolved"] is True
        assert first.get("idempotent") is not True

        commits_after_first = _git_commits(store)
        version_after_first = (await store.read(ROOT_ID)).frontmatter.version
        dv_after_first = app.data_version

        second = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, app
        )

        assert second["resolved"] is True
        assert second.get("idempotent") is True
        assert _git_commits(store) == commits_after_first, "дубль git-коммита запрещён"
        assert (await store.read(ROOT_ID)).frontmatter.version == version_after_first
        assert app.data_version == dv_after_first, "data_version не должен расти на no-op"
        # Состояние стабильно deprecated в обоих носителях
        assert (await store.read(ROOT_ID)).frontmatter.status == "deprecated"
        assert qdrant.payloads(ROOT_ID)[0]["status"] == "deprecated"


# ── 6. Restore — симметрия ────────────────────────────────────


class TestRestoreSymmetry:
    async def test_restore_double_write_and_root_cascade(self, quality_tempdir, store):
        """deprecate root (каскад) → restore root (БЕЗ параметра) → все
        записи published в SSOT и payload."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        qdrant.seed(SEC1_ID, parent=ROOT_ID)
        qdrant.seed(SEC2_ID, parent=ROOT_ID)
        app = _app_state(store, qdrant)

        await resolve_quality_issue({"knowledge_id": ROOT_ID, "action": "deprecate"}, app)
        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "restore", "reason": "Ф3a restore"}, app
        )

        assert result["resolved"] is True, result
        assert result["cascade_affected"] == 2
        for kid in (ROOT_ID, SEC1_ID, SEC2_ID):
            entry = await store.read(kid)
            assert entry.frontmatter.status == "published", kid
            assert qdrant.payloads(kid)[0]["status"] == "published", kid

    async def test_restore_idempotent_when_published(self, quality_tempdir, store):
        """Restore уже-published → no-op (idempotent, без коммита/версии)."""
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        app = _app_state(store, qdrant)
        commits_before = _git_commits(store)

        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "restore"}, app
        )

        assert result["resolved"] is True
        assert result.get("idempotent") is True
        assert _git_commits(store) == commits_before
        assert (await store.read(ROOT_ID)).frontmatter.status == "published"


# ── 7. Решение PAYLOAD_INDEXES + sparse-запись build_payload_point ──


class TestPayloadIndexesDecision:
    def test_status_not_in_payload_indexes(self):
        """Решение Ф3a: status НЕ в PAYLOAD_INDEXES/PAYLOAD_SCHEMA —
        прецедент факт №16 (локаторные поля): без Qdrant-индексов миграция
        коллекций не нужна; must_not-фильтр search работает и так;
        availability (Ф3b) читает SSOT, не payload."""
        names = {name for name, _ in PAYLOAD_INDEXES}
        assert "status" not in names
        from mcp_server.storage.schema import PAYLOAD_SCHEMA
        assert "status" not in PAYLOAD_SCHEMA

    def test_build_payload_point_sparse_status(self):
        """status пишется ТОЛЬКО при непубличном значении: deprecated → ключ
        есть; published/None → ключа НЕТ (отсутствие = published)."""
        base = dict(
            point_id="00000000-0000-0000-0000-000000000000",
            vector=[0.0, 1.0],
            knowledge_id="test-ph3a-unit",
            chunk_id="test-ph3a-unit#0",
            content="текст",
            domain="library",
            subject="bibliography",
            project=None,
            tags=[],
            cross_subjects=[],
            section_header="",
            chunk_index=0,
        )
        assert build_payload_point(**base, status="deprecated").payload["status"] == "deprecated"
        assert "status" not in build_payload_point(**base, status="published").payload
        assert "status" not in build_payload_point(**base).payload


# ── 8. Acceptance-4 (план §3.6:336): root deprecated → секции недоступны ──


class TestAcceptance4RootDeprecatedE2E:
    """Коммитный e2e acceptance-4 (Ф3-fix1, P1-1 от Critic Ф3 iter.1).

    Цепочка ЦЕЛИКОМ на реальных компонентах (git-tmp MarkdownStore +
    реальный IndexingPipeline._index_chunks; мок ТОЛЬКО embedder/qdrant —
    паттерн test_source_locator_payload):
    root deprecate (cascade=дефолт) → ПОЛНЫЙ reindex (старые точки стёрты,
    payload строится из SSOT frontmatter) → все секции status=deprecated
    в SSOT и payload → поиск не возвращает НИ ОДНОЙ (0 visible).
    """

    async def test_root_deprecated_full_reindex_sections_unsearchable(
        self, quality_tempdir, store,
    ):
        await _seed_book(store)
        qdrant = _FakeQdrant()
        qdrant.seed(ROOT_ID)
        qdrant.seed(SEC1_ID, parent=ROOT_ID)
        qdrant.seed(SEC2_ID, parent=ROOT_ID)
        app = _app_state(store, qdrant)

        # deprecate root БЕЗ параметра cascade (дефолт для root = каскад)
        result = await resolve_quality_issue(
            {"knowledge_id": ROOT_ID, "action": "deprecate"}, app,
        )
        assert result["resolved"] is True, result
        assert result["cascade_affected"] == 2

        # ПОЛНЫЙ reindex: старые точки стёрты, каждая запись заново через
        # реальный пайплайн (эквивалент _reindex_into: payload из SSOT).
        qdrant.points.clear()
        patcher, chunker = _fallback_chunker()
        pipe = _pipeline(qdrant)
        with patcher:
            for kid in (ROOT_ID, SEC1_ID, SEC2_ID):
                entry = await store.read(kid)
                chunks = chunker.chunk(
                    kid, entry.content, locator_spans=entry.frontmatter.locator_spans,
                )
                await pipe._index_chunks(entry, chunks)

        # (а) SSOT: root и ВСЕ секции deprecated (перечитано с диска)
        for kid in (ROOT_ID, SEC1_ID, SEC2_ID):
            assert (await store.read(kid)).frontmatter.status == "deprecated", kid
        # (б) payload: реальный reindex перенёс статус из frontmatter
        for kid in (ROOT_ID, SEC1_ID, SEC2_ID):
            assert qdrant.payloads(kid), f"reindex не создал точки для {kid}"
            assert qdrant.payloads(kid)[0]["status"] == "deprecated", kid

        # (в) фильтр выдачи: build_search_filter не пропускает ни одной точки
        from mcp_server.quality.lifecycle import build_search_filter

        flt = build_search_filter(include_deprecated=False)
        assert flt and flt.get("must_not")
        visible_ids = [
            p.payload.get("knowledge_id")
            for p in qdrant.points
            if all(
                p.payload.get(c["key"]) != c["match"]["value"]
                for c in flt["must_not"]
            )
        ]
        assert visible_ids == [], f"deprecated visible после reindex: {visible_ids}"

        # (г) search_knowledge (реальный tool; fake qdrant.search с must_not):
        # без include_deprecated — 0 результатов; с include_deprecated=True
        # находятся все три (исключение обусловлено статусом, не поломкой).
        app.embedder = SimpleNamespace(embed_sync=lambda text: [0.0, 1.0])
        hidden = await search_knowledge({"query": "книга секция", "top_k": 10}, app)
        assert hidden["total"] == 0, hidden
        shown = await search_knowledge(
            {"query": "книга секция", "top_k": 10, "include_deprecated": True}, app,
        )
        assert {r["knowledge_id"] for r in shown["results"]} == {
            ROOT_ID, SEC1_ID, SEC2_ID,
        }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
