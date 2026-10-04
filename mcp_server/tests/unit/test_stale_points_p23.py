"""P2-3 (bibliography): детерминированный point_id + delete-before-upsert + fail-loud cascade.

RED-контроль (на коде до фикса):
- point_id был uuid4 → два прогона одного батча дают РАЗНЫЕ id (дубли при повторном
  upsert/replace) → тест детерминизма падал;
- в write-пути не было delete-before-upsert → при укорочении записи старые чанки жили;
- `delete_entry` при ошибке Qdrant «тихо» удалял SSOT (глотание) → тест fail-loud падал.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from mcp_server.indexing.pipeline import IndexingPipeline
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.schema import POINT_ID_NAMESPACE, collection_for_zone


class _QdrantRecorder:
    """In-memory двойник Qdrant: события в порядке вызова (delete/upsert)."""

    def __init__(self):
        self.events: list[tuple] = []
        self.points: dict[str, dict] = {}
        self.fail_delete = False

    def delete_by_knowledge_id(self, knowledge_id: str, collection_name: str | None = None) -> None:
        if self.fail_delete:
            raise RuntimeError("qdrant delete failed")
        self.events.append(("delete", knowledge_id, collection_name))
        for pid, pt in list(self.points.items()):
            if pt["collection"] == collection_name and pt["knowledge_id"] == knowledge_id:
                del self.points[pid]

    def upsert_points(self, points, collection_name: str | None = None) -> None:
        for pt in points:
            pid = str(pt.id)
            self.events.append(("upsert", pid, collection_name))
            self.points[pid] = {
                "collection": collection_name,
                "knowledge_id": pt.payload.get("knowledge_id"),
                "chunk_id": pt.payload.get("chunk_id"),
            }

    def build_payload_point(self, *a, **k):  # pragma: no cover — не используется напрямую
        raise AssertionError("unexpected call")


def _entry(kid: str, content: str, *, zone: str = "private") -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid, domain="test", subject="p23", content_type="article", zone=zone
    )
    return KnowledgeEntry(frontmatter=fm, content=content)


def _pipeline(qdrant) -> IndexingPipeline:
    embedder = SimpleNamespace(embed_sync=lambda texts: [[0.0, 1.0]] * len(texts))
    return IndexingPipeline(store=SimpleNamespace(), qdrant=qdrant, embedder=embedder)


class TestDeterministicPointId:
    async def test_point_ids_are_stable_and_zone_scoped(self):
        q1, q2 = _QdrantRecorder(), _QdrantRecorder()
        entry = _entry("p23-det", "Some indexable body about caching and indexes. " * 4)
        for q in (q1, q2):
            await _pipeline(q)._process_batch([{"entry": entry, "retries": 0, "event": None}])
        ids1 = [k for k in q1.points]
        ids2 = [k for k in q2.points]
        assert ids1 and ids1 == ids2, "point_id не детерминирован (uuid4 → разные id при повторе)"
        for pid, meta in q1.points.items():
            expected = str(uuid.uuid5(POINT_ID_NAMESPACE, f"{'private'}:{meta['knowledge_id']}:{meta['chunk_id']}"))
            assert pid == expected, "point_id != uuid5(NAMESPACE, zone:kid:chunk_id)"

    async def test_repeated_upsert_does_not_duplicate(self):
        q = _QdrantRecorder()
        entry = _entry("p23-idem", "Idempotent body for repeated indexing. " * 4)
        pipe = _pipeline(q)
        await pipe._process_batch([{"entry": entry, "retries": 0, "event": None}])
        n1 = len(q.points)
        await pipe._process_batch([{"entry": entry, "retries": 0, "event": None}])
        assert len(q.points) == n1, "повторная индексация создала дубли точек"


class TestDeleteBeforeUpsert:
    async def test_delete_precedes_upsert_for_each_record(self):
        q = _QdrantRecorder()
        entry = _entry("p23-dbu", "Body for delete-before-upsert ordering check. " * 3)
        await _pipeline(q)._process_batch([{"entry": entry, "retries": 0, "event": None}])
        kinds = [e[0] for e in q.events]
        assert "delete" in kinds and "upsert" in kinds
        assert kinds.index("delete") < kinds.index("upsert"), "upsert до delete → stale-чанки возможны"
        del_kid, del_coll = q.events[kinds.index("delete")][1], q.events[kinds.index("delete")][2]
        assert del_kid == entry.frontmatter.knowledge_id
        assert del_coll == collection_for_zone("private")


class TestDeleteEntryFailLoud:
    """delete_entry: fail-loud при ошибке Qdrant; удаление во всех зонах."""

    @staticmethod
    def _prepare(app_state):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        app_state.store.read = AsyncMock(
            return_value=SimpleNamespace(frontmatter=SimpleNamespace(domain="test"))
        )
        app_state.store.delete = AsyncMock(return_value=True)
        app_state.qdrant.scroll = MagicMock(return_value=([], None))
        return app_state

    async def test_qdrant_failure_keeps_ssot(self, app_state):
        from unittest.mock import MagicMock

        from mcp_server.tools.crud import delete_entry

        app_state = self._prepare(app_state)
        app_state.qdrant.delete_by_knowledge_id = MagicMock(side_effect=RuntimeError("qdrant down"))
        result = await delete_entry({"knowledge_id": "p23-fail"}, app_state)
        assert "error" in result, "ошибка Qdrant проглочена (тихий успех)"
        assert app_state.store.delete.await_count == 0, "SSOT удалён до Qdrant (нарушен import-first)"

    async def test_deletes_in_both_zones(self, app_state):
        from unittest.mock import MagicMock

        from mcp_server.tools.crud import delete_entry

        app_state = self._prepare(app_state)
        app_state.qdrant.delete_by_knowledge_id = MagicMock()
        result = await delete_entry({"knowledge_id": "p23-zones"}, app_state)
        cols = {c.args[1] if len(c.args) > 1 else c.kwargs.get("collection_name")
                for c in app_state.qdrant.delete_by_knowledge_id.call_args_list}
        assert cols == {collection_for_zone("private"), collection_for_zone("public")}
        assert "error" not in result
