"""W2.12: Постоянные unit-тесты двухколлекционной модели (public/private).

Перенос smoke-кейсов из .trash/w211_migrate_smoke.py на stateful mock
raw-Qdrant-клиента (`_client`). Тестируется реальная логика обёртки
QdrantClient через `__new__` + инъекция fake-raw-клиента (без сети):

1. Миграция legacy 'knowledge' (main._migrate_legacy_collection):
   ветка A (alias → private zero-copy), ветка B (real collection),
   идемпотентность рестарта, partial-heal, rollback (P1-2), fresh install.
2. ensure_zonal_collections: обе зоны + идемпотентность.
3. P0-1: reindex_zone("public") не смешивает зоны (zone_filter до upsert).
4. Fail-loud: search без collection_name → ValueError.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.main import _migrate_legacy_collection
from mcp_server.storage.qdrant_client import QdrantClient
from mcp_server.storage.schema import (
    COLLECTION_PRIVATE,
    COLLECTION_PUBLIC,
    LEGACY_ALIAS,
    PRIVATE_V1,
    PUBLIC_V1,
    PUBLIC_V2,
)

pytestmark = pytest.mark.asyncio


# ── Fake raw-Qdrant-клиент (эмулирует QdrantSDK `_client`) ──


class FakeRawQdrant:
    """Stateful fake raw-клиента: collections + aliases + журнал вызовов."""

    def __init__(self, collections: set | None = None, aliases: dict | None = None):
        self.collections = set(collections or set())
        self.aliases = dict(aliases or {})
        self.calls: list[str] = []

    # ── Raw QdrantSDK API (то, что дергает обёртка QdrantClient) ──

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def get_aliases(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(alias_name=alias, collection_name=collection)
            for alias, collection in self.aliases.items()
        ]

    def create_collection(self, **params) -> None:
        name = params["collection_name"]
        self.collections.add(name)
        self.calls.append(f"create:{name}")

    def create_payload_index(self, **kwargs) -> None:
        pass  # индексы не релевантны для миграции/ensure

    def delete_collection(self, name: str) -> None:
        if name in self.collections:
            self.collections.remove(name)
            self.calls.append(f"del_col:{name}")

    def update_collection_aliases(self, change_aliases_operations: list) -> None:
        for op in change_aliases_operations:
            create = getattr(op, "create_alias", None)
            delete = getattr(op, "delete_alias", None)
            if create is not None:
                self.aliases[create.alias_name] = create.collection_name
                self.calls.append(f"create_alias:{create.alias_name}->{create.collection_name}")
            elif delete is not None:
                self.aliases.pop(delete.alias_name, None)
                self.calls.append(f"delete_alias:{delete.alias_name}")


def _make_wrapper(raw: FakeRawQdrant) -> QdrantClient:
    """Обёртка QdrantClient с fake-raw-клиентом (без реального подключения)."""
    client = QdrantClient.__new__(QdrantClient)
    client._client = raw
    return client


# ── W2.11: миграция legacy-коллекции 'knowledge' ────────────


class TestMigrateLegacyCollection:
    """Двухветочная зональная миграция (перенос smoke-кейсов 1:1)."""

    async def test_fresh_install_is_noop(self):
        """Ни коллекции, ни алиаса → no-op (ensure_zonal_collections создаст зоны)."""
        raw = FakeRawQdrant()
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert raw.calls == []
        assert raw.collections == set()
        assert raw.aliases == {}

    async def test_branch_a_alias_zero_copy(self):
        """'knowledge' = alias на коллекцию с данными → private-alias на ту же
        коллекцию, legacy-alias удалён (R1), данные целы."""
        raw = FakeRawQdrant(
            collections={"knowledge_v1"},
            aliases={LEGACY_ALIAS: "knowledge_v1"},
        )
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert LEGACY_ALIAS not in raw.aliases
        assert raw.aliases[COLLECTION_PRIVATE] == "knowledge_v1"
        assert "knowledge_v1" in raw.collections

    async def test_branch_a_idempotent_restart(self):
        """Повторный старт после успешной миграции A → no-op."""
        raw = FakeRawQdrant(
            collections={"knowledge_v1"},
            aliases={COLLECTION_PRIVATE: "knowledge_v1"},
        )
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert raw.calls == []

    async def test_branch_b_real_collection(self):
        """'knowledge' = реальная коллекция → PRIVATE_V1 создана, alias
        'knowledge_private' наведён, legacy удалена."""
        raw = FakeRawQdrant(collections={LEGACY_ALIAS})
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert LEGACY_ALIAS not in raw.collections
        assert PRIVATE_V1 in raw.collections
        assert raw.aliases[COLLECTION_PRIVATE] == PRIVATE_V1

    async def test_branch_b_idempotent_restart(self):
        """Повторный старт после успешной миграции B → no-op."""
        raw = FakeRawQdrant(
            collections={PRIVATE_V1},
            aliases={COLLECTION_PRIVATE: PRIVATE_V1},
        )
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert raw.calls == []

    async def test_branch_b_partial_heals(self):
        """Legacy есть + private уже настроен (crash между шагами) →
        только delete legacy, без повторного swap."""
        raw = FakeRawQdrant(
            collections={LEGACY_ALIAS, PRIVATE_V1},
            aliases={COLLECTION_PRIVATE: PRIVATE_V1},
        )
        await _migrate_legacy_collection(_make_wrapper(raw))
        assert LEGACY_ALIAS not in raw.collections
        assert not any(c.startswith("create_alias:") for c in raw.calls)

    async def test_branch_b_rollback_on_swap_failure(self):
        """P1-2: сбой swap в ветке B → созданная v1 удалена, legacy цел."""

        class FailingSwap(FakeRawQdrant):
            def update_collection_aliases(self, change_aliases_operations: list) -> None:
                raise RuntimeError("qdrant down")

        raw = FailingSwap(collections={LEGACY_ALIAS})
        with pytest.raises(RuntimeError, match="qdrant down"):
            await _migrate_legacy_collection(_make_wrapper(raw))
        assert PRIVATE_V1 not in raw.collections  # rollback
        assert LEGACY_ALIAS in raw.collections    # legacy цел


# ── W2.5: ensure_zonal_collections ──────────────────────────


class TestEnsureZonalCollections:
    """Обе зоны создаются (v1 + alias), повторный вызов идемпотентен."""

    async def test_creates_both_zones(self):
        raw = FakeRawQdrant()
        created = _make_wrapper(raw).ensure_zonal_collections()
        assert created is True
        assert raw.collections == {PUBLIC_V1, PRIVATE_V1}
        assert raw.aliases[COLLECTION_PUBLIC] == PUBLIC_V1
        assert raw.aliases[COLLECTION_PRIVATE] == PRIVATE_V1

    async def test_idempotent_second_call(self):
        raw = FakeRawQdrant(
            collections={PUBLIC_V1, PRIVATE_V1},
            aliases={COLLECTION_PUBLIC: PUBLIC_V1, COLLECTION_PRIVATE: PRIVATE_V1},
        )
        calls_before = list(raw.calls)
        created = _make_wrapper(raw).ensure_zonal_collections()
        assert created is False
        assert raw.calls == calls_before  # ничего не пересоздано


# ── P0-1: reindex per zone не смешивает зоны ────────────────


class TestReindexZoneIsolation:
    """reindex_zone("public") индексирует ТОЛЬКО public-файлы в public-пару."""

    @staticmethod
    def _make_pipeline() -> IndexingPipeline:
        pipeline = IndexingPipeline(
            store=MagicMock(), qdrant=MagicMock(), embedder=MagicMock(),
        )
        pipeline._qdrant.get_active_collection = MagicMock(return_value=PUBLIC_V1)
        pipeline._index_chunks = AsyncMock()
        return pipeline

    async def test_public_zone_filters_files_before_upsert(self):
        """store.reindex_scan → 2 файла (public + private) → upsert ТОЛЬКО
        public-файла в knowledge_public_v2 (private отфильтрован до индексации)."""
        pipeline = self._make_pipeline()

        def _entry(knowledge_id: str, zone: str) -> SimpleNamespace:
            return SimpleNamespace(
                frontmatter=SimpleNamespace(knowledge_id=knowledge_id, zone=zone),
                content=f"# {knowledge_id}\n\nSection content.",
            )

        async def _reindex_scan() -> list[str]:
            return ["/tmp/pub.md", "/tmp/priv.md"]

        def _parse_file(path: str) -> SimpleNamespace:
            if path.endswith("pub.md"):
                return _entry("pub-1", "public")
            return _entry("priv-1", "private")

        pipeline._store.reindex_scan = _reindex_scan
        pipeline._store._parse_file = _parse_file

        result = await pipeline.reindex_zone("public")

        assert result["total_docs"] == 1
        assert result["active"] == PUBLIC_V1
        assert result["target"] == PUBLIC_V2
        assert result["alias_swapped"] is True
        # Upsert ровно один — public-файл в public v2; private-файл не тронут.
        assert pipeline._index_chunks.await_count == 1
        entry, _chunks = pipeline._index_chunks.call_args.args
        assert entry.frontmatter.knowledge_id == "pub-1"
        assert pipeline._index_chunks.call_args.kwargs["collection_name"] == PUBLIC_V2

    async def test_unknown_zone_fails_loud(self):
        """reindex_zone("x") → ValueError (fail loud, зона не размывается)."""
        pipeline = self._make_pipeline()
        with pytest.raises(ValueError, match="unknown zone"):
            await pipeline.reindex_zone("x")


# ── Fail-loud: зональный контракт без дефолтной коллекции ──


class TestFailLoudCollectionRequired:
    """Методы с зональным контрактом требуют collection_name."""

    async def test_search_without_collection_name_raises(self):
        """search() без collection_name → ValueError (до обращения к _client)."""
        client = QdrantClient.__new__(QdrantClient)
        with pytest.raises(ValueError, match="collection_name"):
            client.search(vector=[0.1, 0.2])
