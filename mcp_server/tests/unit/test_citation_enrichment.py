"""Ф4b2: read-time batch-enrichment цитатами + TTL-кэш Source-frontmatter.

Trace: code-2026-10-02-bibliography, план §3.4:189 (проводка в search_knowledge /
find_fragment / get_entry: топ-K → distinct source_id → batch store.read →
TTL-кэш по паттерну config.py TOKEN_INDEX_TTL_SEC).

Контракты (§3.4:185-192):
- `citation` — ключ ТОЛЬКО при непустом решении (None → ключа НЕТ, не null);
- `citation_reason` — соседнее поле (НЕ внутри citation), только когда
  canonical отсутствует (classify_reason ≠ None);
- fail-closed: subscriber/private, public+restricted license → citation нет;
- CSL НЕ денормализуется в Qdrant payload (enrichment read-time only).

Без N+1: 3 хита с общим source → ОДИН store.read на distinct-источник;
повторный поиск → cache-hit (0 новых чтений). Инвалидация: write-хуки
source_ref_runtime (refresh/index_add/index_remove) + TTL.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from mcp_server.content.source_cache import (
    SourceFrontmatterCache,
    reset_source_cache,
)
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.schema import PAYLOAD_INDEXES, PAYLOAD_SCHEMA
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex

SID = "src-3f2a9c01b74d25e6"
SID2 = "src-aa11bb22cc33dd44"
CANON_SHA = "ab" * 32
READ_AUTH = {"level": "read"}
SUBSCRIBER_AUTH = {"level": "subscriber"}

CSL = {
    "title": "Основы надёжного тестирования",
    "author": [{"family": "Иванов", "given": "Иван Иванович"}],
    "issued": {"date-parts": [[2020]]},
    "publisher": "Диагностика",
}


@pytest.fixture(autouse=True)
def _fresh_source_cache():
    """Изоляция глобального кэша между тестами."""
    reset_source_cache()
    yield
    reset_source_cache()


# ── Fakes ────────────────────────────────────────────────────


class _CountingStore:
    """MarkdownStore-заглушка: async read со счётчиком + reindex_scan-протокол."""

    def __init__(self, entries=None):
        if entries is None:
            entries_map: dict[str, KnowledgeEntry] = {}
        elif isinstance(entries, dict):
            entries_map = dict(entries)
        else:  # iterable[KnowledgeEntry] — ключ = knowledge_id
            entries_map = {e.frontmatter.knowledge_id: e for e in entries}
        self.entries: dict[str, KnowledgeEntry] = entries_map
        self.read_calls: list[str] = []
        self.scan_calls = 0

    async def read(self, knowledge_id: str) -> KnowledgeEntry | None:
        self.read_calls.append(knowledge_id)
        return self.entries.get(knowledge_id)

    async def reindex_scan(self) -> list[str]:
        self.scan_calls += 1
        return list(self.entries)

    def _parse_file(self, path: str) -> KnowledgeEntry | None:
        return self.entries.get(path)


class _DocStore:
    """DocumentStore-заглушка: exists(sha256)."""

    def __init__(self, shas: set[str] | None = None):
        self._shas = set(shas or set())

    def exists(self, sha256: str) -> bool:
        return sha256 in self._shas


class _SearchQdrant:
    """Qdrant-заглушка: search возвращает засеянные точки (score убывает)."""

    def __init__(self, payloads: list[dict]):
        self.points = [SimpleNamespace(id=i, score=0.9 - i * 0.01, payload=p) for i, p in enumerate(payloads)]

    def search(
        self,
        vector=None,
        top_k=5,
        filters=None,
        score_threshold=0.0,
        exclude_content_types=None,
        exclude_statuses=None,
        offset=0,
        collection_name=None,
    ):
        return self.points[offset : offset + top_k]

    def search_by_tags(
        self,
        tags=None,
        match_all=True,
        limit=500,
        collection_name=None,
        exclude_content_types=None,
        exclude_statuses=None,
    ):
        return self.points[:limit]


def _source_entry(
    source_id: str,
    *,
    zone="private",
    status="published",
    license=None,
    public_allowed=None,
    csl=True,
    blobs=None,
    title=None,
) -> KnowledgeEntry:
    bibliography = dict(CSL)
    if title is not None:
        bibliography["title"] = title
    if not csl:
        bibliography = {"title": "Только название без авторов"}
    fm = KnowledgeFrontmatter(
        knowledge_id=source_id,
        domain="library",
        subject="sources",
        content_type="source",
        zone=zone,
        status=status,
        license=license,
        public_allowed=public_allowed,
        bibliography=bibliography,
        blobs=blobs,
        format="pdf" if blobs is not None else "url",
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {source_id}")


def _index_with(source_id: str, zone: str = "private") -> SourceRefIndex:
    index = SourceRefIndex()
    index.add(SourceRef(source_id=source_id, zone=zone, shas=(CANON_SHA,)))
    return index


def _app_state(store, doc_store, index, qdrant=None) -> SimpleNamespace:
    return SimpleNamespace(
        store=store,
        document_store=doc_store,
        source_ref_index=index,
        qdrant=qdrant or _SearchQdrant([]),
        qdrant_client=None,
        embedder=SimpleNamespace(embed_sync=lambda q: [0.0, 1.0]),
    )


def _hit_payload(kid: str, source_id: str | None = SID, *, with_locator: bool = True) -> dict:
    payload = {
        "knowledge_id": kid,
        "chunk_id": f"{kid}#0",
        "content": f"Осмысленный контент секции {kid}",
        "domain": "library",
        "subject": "bibliography",
        "tags": ["test"],
    }
    if source_id is not None:
        payload["source_id"] = source_id
        if with_locator:
            payload.update(locator_kind="page", locator_start=7, locator_end=8)
    return payload


async def _search(state, auth=READ_AUTH, query="тестирование"):
    from mcp_server.tools.search import search_knowledge

    return await search_knowledge({"query": query, "_auth": auth}, state)


# ── A. search_knowledge: batch + кэш + контракт ключей ──────


class TestSearchEnrichment:
    async def test_three_hits_one_source_single_read_then_cache_hit(self):
        """3 хита с общим source → ОДИН store.read; повторный поиск → 0 новых
        чтений (cache-hit); citation (level b) в каждом результате."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(
            store,
            _DocStore({CANON_SHA}),
            _index_with(SID),
            _SearchQdrant([_hit_payload(f"sec-{i}") for i in (1, 2, 3)]),
        )

        result = await _search(state)
        assert len(result["results"]) == 3
        assert store.read_calls.count(SID) == 1  # batch: 1 чтение на distinct-источник
        for r in result["results"]:
            assert r["citation"]["source_id"] == SID
            assert r["citation"]["viewer_url"] == f"/documents/{CANON_SHA}"
            assert "citation_reason" not in r  # canonical есть → причины нет

        await _search(state)
        assert store.read_calls.count(SID) == 1  # cache-hit: перечитывания НЕТ

    async def test_read_auth_private_source_citation_present(self):
        """read-ключ + private Source → citation ЕСТЬ (auth прокинут, не None)."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(
            store,
            _DocStore({CANON_SHA}),
            _index_with(SID),
            _SearchQdrant([_hit_payload("sec-1")]),
        )
        result = await _search(state)
        assert result["results"][0]["citation"]["source_id"] == SID

    async def test_no_source_id_no_citation_key(self):
        state = _app_state(
            _CountingStore(), _DocStore(), _index_with(SID), _SearchQdrant([_hit_payload("sec-1", source_id=None)])
        )
        result = await _search(state)
        assert "citation" not in result["results"][0]

    async def test_missing_source_no_citation_key_not_null(self):
        """source_id указывает на несуществующую запись → ключа citation НЕТ."""
        state = _app_state(_CountingStore(), _DocStore(), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        result = await _search(state)
        assert "citation" not in result["results"][0]
        assert result["results"][0].get("citation", "absent") is not None

    async def test_insufficient_csl_omitted_entirely(self):
        store = _CountingStore([_source_entry(SID, csl=False)])
        state = _app_state(store, _DocStore(), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        result = await _search(state)
        assert "citation" not in result["results"][0]
        assert "citation_reason" not in result["results"][0]

    async def test_canonical_absent_level_a_with_reason(self):
        """URL-only Source → citation уровня a (без viewer_url) +
        citation_reason=url_no_blobs (поле РЯДОМ, не внутри citation)."""
        store = _CountingStore([_source_entry(SID)])  # blobs=None → url_no_blobs
        state = _app_state(store, _DocStore(), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        result = await _search(state)
        r = result["results"][0]
        assert r["citation"]["source_id"] == SID
        assert "viewer_url" not in r["citation"]
        assert r["citation_reason"] == "url_no_blobs"
        assert "reason" not in r["citation"]

    async def test_canonical_present_no_reason_key(self):
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        result = await _search(state)
        assert "citation_reason" not in result["results"][0]

    async def test_subscriber_private_source_fail_closed(self):
        """subscriber + private Source → citation НЕТ (зоны не ослаблены)."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        result = await _search(state, auth=SUBSCRIBER_AUTH)
        assert "citation" not in result["results"][0]
        assert "citation_reason" not in result["results"][0]

    async def test_public_restricted_license_fail_closed(self):
        """public + license=restricted → citation НЕТ для любого auth (О-3)."""
        store = _CountingStore(
            [
                _source_entry(
                    SID,
                    zone="public",
                    license="restricted",
                    public_allowed=True,
                    blobs={"canonical": {"sha256": CANON_SHA}},
                ),
            ]
        )
        state = _app_state(
            store, _DocStore({CANON_SHA}), _index_with(SID, zone="public"), _SearchQdrant([_hit_payload("sec-1")])
        )
        result = await _search(state, auth=READ_AUTH)
        assert "citation" not in result["results"][0]

    async def test_csl_never_in_qdrant_payload(self):
        """Enrichment read-time only: payload точек не обогащается citation-
        ключами; PAYLOAD_SCHEMA/INDEXES не содержат citation-полей."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        qdrant = _SearchQdrant([_hit_payload("sec-1")])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), qdrant)
        await _search(state)
        for point in qdrant.points:
            for key in ("citation", "citation_reason", "formatted", "authors"):
                assert key not in point.payload, f"CSL утёк в payload: {key}"
        for key in ("citation", "citation_reason", "formatted"):
            assert key not in dict(PAYLOAD_SCHEMA)
            assert key not in {name for name, _ in PAYLOAD_INDEXES}

    async def test_search_by_tags_enriched(self):
        """search_by_tags: та же проводка (source_id из payload → citation)."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        from mcp_server.tools.search import search_by_tags

        result = await search_by_tags({"tags": ["test"], "_auth": READ_AUTH}, state)
        assert result["results"][0]["citation"]["source_id"] == SID


# ── B. TTL-кэш: свежесть ─────────────────────────────────────


def _ttl() -> int:
    from mcp_server.config import settings

    return settings.SOURCE_CACHE_TTL_SEC


class TestSourceCacheTtl:
    async def test_ttl_expiry_rereads_source(self):
        """Истечение TTL → перечитывание (монотонный clock под контролем)."""
        import mcp_server.content.source_cache as sc

        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        t = {"now": 1000.0}
        original = sc._monotonic
        sc._monotonic = lambda: t["now"]
        try:
            await _search(state)
            assert store.read_calls.count(SID) == 1
            t["now"] += float(_ttl()) + 1.0  # за пределами TTL
            await _search(state)
            assert store.read_calls.count(SID) == 2  # перечитано
        finally:
            sc._monotonic = original

    async def test_write_hook_invalidation_refreshes(self):
        """update_source-хук (refresh_source_ref_index) инвалидирует кэш:
        повторный поиск перечитывает и видит НОВЫЙ title."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        await _search(state)
        assert store.read_calls.count(SID) == 1

        # write-path: source обновлён → хук refresh → инвалидация
        store.entries[SID] = _source_entry(SID, title="Переиздание 2026", blobs={"canonical": {"sha256": CANON_SHA}})
        from mcp_server.tools.source_ref_runtime import refresh_source_ref_index

        await refresh_source_ref_index(state)

        result = await _search(state)
        assert store.read_calls.count(SID) == 2
        assert "Переиздание 2026" in result["results"][0]["citation"]["formatted"]

    async def test_index_add_entry_invalidates_cache(self):
        """Точечный write-хук (ingest/update Source) тоже инвалидирует."""
        store = _CountingStore([_source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}})])
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID), _SearchQdrant([_hit_payload("sec-1")]))
        await _search(state)
        updated = _source_entry(SID, title="Новая редакция", blobs={"canonical": {"sha256": CANON_SHA}})
        store.entries[SID] = updated  # write-path: запись обновлена в SSOT
        from mcp_server.tools.source_ref_runtime import index_add_entry

        assert index_add_entry(state, updated) is True
        result = await _search(state)
        assert store.read_calls.count(SID) == 2
        assert "Новая редакция" in result["results"][0]["citation"]["formatted"]


class TestSourceCacheUnit:
    def test_get_put_and_miss(self):
        cache = SourceFrontmatterCache(ttl_seconds=5.0, max_entries=8)
        cache.put(SID, {"knowledge_id": SID})
        assert cache.get(SID) == {"knowledge_id": SID}
        assert cache.get("src-missing") is None

    def test_ttl_expiry_unit(self):
        t = {"now": 0.0}
        cache = SourceFrontmatterCache(ttl_seconds=5.0, max_entries=8, clock=lambda: t["now"])
        cache.put(SID, {"v": 1})
        t["now"] = 4.9
        assert cache.get(SID) == {"v": 1}
        t["now"] = 5.1
        assert cache.get(SID) is None  # истёк → MISS

    def test_bounded_size_evicts_oldest(self):
        cache = SourceFrontmatterCache(ttl_seconds=60.0, max_entries=4)
        for i in range(6):
            cache.put(f"src-{i:04d}", {"i": i})
        assert len(cache) == 4
        assert cache.get("src-0000") is None  # старейший вытеснен
        assert cache.get("src-0005") == {"i": 5}

    def test_invalidate_single_and_all(self):
        cache = SourceFrontmatterCache(ttl_seconds=60.0, max_entries=8)
        cache.put(SID, {"a": 1})
        cache.put(SID2, {"b": 2})
        cache.invalidate(SID)
        assert cache.get(SID) is None
        assert cache.get(SID2) == {"b": 2}
        cache.invalidate()
        assert len(cache) == 0

    def test_thread_safety_hammer(self):
        """Параллельные put/get/invalidate из 8 потоков — без ошибок,
        размер остаётся ограниченным (lock на месте)."""
        cache = SourceFrontmatterCache(ttl_seconds=60.0, max_entries=64)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(300):
                    cache.put(f"src-{n}-{i % 32}", {"n": n, "i": i})
                    cache.get(f"src-{(n + 1) % 8}-{i % 32}")
                    if i % 100 == 0:
                        cache.invalidate(f"src-{n}-{i % 32}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(cache) <= 64


# ── C. find_fragment ─────────────────────────────────────────


def _root_collection() -> KnowledgeEntry:
    return KnowledgeEntry(
        frontmatter=KnowledgeFrontmatter(
            knowledge_id="book-root",
            domain="library",
            subject="bibliography",
            content_type="collection",
        ),
        content="# Книга",
    )


class TestFindFragmentEnrichment:
    async def test_fragment_carries_citation_and_reason(self):
        store = _CountingStore(
            {
                "book-root": _root_collection(),
                SID: _source_entry(SID),  # url-only → level a + reason
            }
        )
        state = _app_state(
            store,
            _DocStore(),
            _index_with(SID),
            _SearchQdrant(
                [
                    _hit_payload("book-root-sec-1"),
                    _hit_payload("book-root-sec-2"),
                ]
            ),
        )
        from mcp_server.tools.fragments import find_fragment

        result = await find_fragment(
            {"collection_id": "book-root", "query": "секция", "_auth": READ_AUTH},
            state,
        )
        assert result["total"] == 2
        for frag in result["fragments"]:
            assert frag["citation"]["source_id"] == SID
            assert frag["citation_reason"] == "url_no_blobs"
        # один batch-проход: одно чтение на distinct источник (+root на валидации)
        assert store.read_calls.count(SID) == 1

    async def test_fragment_without_citation_no_key(self):
        state = _app_state(
            _CountingStore({"book-root": _root_collection()}),
            _DocStore(),
            SourceRefIndex(),
            _SearchQdrant([_hit_payload("book-root-sec-1", source_id=None)]),
        )
        from mcp_server.tools.fragments import find_fragment

        result = await find_fragment(
            {"collection_id": "book-root", "query": "секция", "_auth": READ_AUTH},
            state,
        )
        assert "citation" not in result["fragments"][0]


# ── D. get_entry ─────────────────────────────────────────────


class TestGetEntryEnrichment:
    async def test_section_source_id_single_citation(self):
        section = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="book-sec-42",
                domain="library",
                subject="bibliography",
                content_type="book",
                source_id=SID,
            ),
            content="# Секция",
        )
        store = _CountingStore(
            {
                "book-sec-42": section,
                SID: _source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}}),
            }
        )
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID))
        from mcp_server.tools.read import get_entry

        resp = await get_entry({"knowledge_id": "book-sec-42", "_auth": READ_AUTH}, state)
        assert resp["citation"]["source_id"] == SID
        assert "citation_reason" not in resp
        assert store.read_calls.count(SID) == 1

    async def test_root_source_refs_multi_citations_list(self):
        """source_refs с 2 источниками → citations-список с поэлементным
        контрактом (нет цитаты → нет ключа в элементе)."""
        record = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="handmade-note",
                domain="library",
                subject="bibliography",
                content_type="book",
                source_refs=[
                    {"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}},
                    {"source_id": SID2},
                ],
            ),
            content="# Заметка",
        )
        store = _CountingStore(
            {
                "handmade-note": record,
                SID: _source_entry(SID, blobs={"canonical": {"sha256": CANON_SHA}}),
                SID2: _source_entry(SID2, csl=False),  # недостаточный CSL → без citation
            }
        )
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID))
        from mcp_server.tools.read import get_entry

        resp = await get_entry({"knowledge_id": "handmade-note", "_auth": READ_AUTH}, state)
        assert "citation" not in resp
        citations = resp["citations"]
        assert [c["source_id"] for c in citations] == [SID, SID2]
        assert citations[0]["citation"]["locator"] == {"kind": "page", "start": 3, "end": 5}
        assert "citation" not in citations[1]
        assert "citation_reason" not in citations[1]

    async def test_no_refs_no_keys(self):
        plain = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="plain-entry",
                domain="library",
                subject="bibliography",
            ),
            content="# Обычная запись",
        )
        state = _app_state(_CountingStore({"plain-entry": plain}), _DocStore(), SourceRefIndex())
        from mcp_server.tools.read import get_entry

        resp = await get_entry({"knowledge_id": "plain-entry", "_auth": READ_AUTH}, state)
        assert "citation" not in resp
        assert "citations" not in resp

    async def test_subscriber_private_source_fail_closed(self):
        section = KnowledgeEntry(
            frontmatter=KnowledgeFrontmatter(
                knowledge_id="book-sec-pub",
                domain="library",
                subject="bibliography",
                content_type="book",
                source_id=SID,
                zone="public",
            ),
            content="# Публичная секция",
        )
        store = _CountingStore(
            {
                "book-sec-pub": section,
                SID: _source_entry(SID, zone="private", blobs={"canonical": {"sha256": CANON_SHA}}),
            }
        )
        state = _app_state(store, _DocStore({CANON_SHA}), _index_with(SID))
        from mcp_server.tools.read import get_entry

        resp = await get_entry({"knowledge_id": "book-sec-pub", "_auth": SUBSCRIBER_AUTH}, state)
        assert "citation" not in resp  # private Source невидим subscriber'у


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
