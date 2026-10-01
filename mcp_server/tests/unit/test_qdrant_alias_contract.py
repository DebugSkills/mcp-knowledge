"""Контрактные тесты Qdrant-алиасов — регресс инцидента 2026-10-01 (aikb).

Инцидент: `QdrantSDKClient.get_aliases()` в продовом (remote) flavour возвращает pydantic
`CollectionsAliasesResponse`, итерация которой даёт КОРТЕЖИ `('aliases', [...])`. Из-за этого
`desc.alias_name` падал с `AttributeError`, `except Exception: pass` в `get_active_collection`
глушил ошибку → blue-green считал `active = alias` → `target = v1` →
`create_collection_named(v1, force_recreate=True)` УДАЛЯЛ живую коллекцию вместе с алиасом
(`knowledge_public` пропадал, `/health` = degraded 404, полный reindex на каждом старте).

Тесты фиксируют:
- поддержку ОБЕИХ форм ответа (pydantic-модель и список кортежей локального flavour);
- что активная коллекция реально резолвится (а не молча уходит в fallback);
- что `swap_alias` делает ОДИН атомарный вызов с delete+create;
- что `has_points` не даёт blue-green выбрать целью коллекцию с данными.
"""

from __future__ import annotations

from types import SimpleNamespace

from qdrant_client.http.models import AliasDescription, CollectionsAliasesResponse

from mcp_server.storage.qdrant_client import QdrantClient, _alias_pairs


class FakeClient:
    """Мини-двойник Qdrant SDK: фиксирует вызовы и отдаёт заданные ответы."""

    def __init__(self, aliases=None, collections=None, points=None) -> None:
        self._aliases = aliases if aliases is not None else []
        self._collections: dict[str, bool] = collections or {}
        self._points: dict[str, int] = points or {}
        self.alias_ops: list = []

    def get_aliases(self):
        return self._aliases

    def update_collection_aliases(self, change_aliases_operations):
        self.alias_ops.append(change_aliases_operations)

    def collection_exists(self, name: str) -> bool:
        return name in self._collections

    def get_collection(self, name: str):
        return SimpleNamespace(points_count=self._points.get(name, 0))


def make_wrapper(fake: FakeClient) -> QdrantClient:
    """Обёртка без __init__ — живой Qdrant для этих тестов не нужен."""
    q = QdrantClient.__new__(QdrantClient)
    q._client = fake  # type: ignore[assignment]
    return q


def pydantic_response(*pairs: tuple[str, str]) -> CollectionsAliasesResponse:
    """Продовая форма ответа: pydantic-модель CollectionsAliasesResponse."""
    return CollectionsAliasesResponse(
        aliases=[AliasDescription(alias_name=a, collection_name=c) for a, c in pairs]
    )


class TestAliasPairs:
    def test_pydantic_response_is_normalised(self):
        pairs = list(_alias_pairs(FakeClient(pydantic_response(("knowledge_public", "knowledge_public_v1")))))
        assert pairs == [("knowledge_public", "knowledge_public_v1")]

    def test_plain_tuples_supported(self):
        """Локальный flavour (in-memory) отдаёт список кортежей."""
        pairs = list(_alias_pairs(FakeClient([("knowledge_private", "knowledge_private_v1")])))
        assert pairs == [("knowledge_private", "knowledge_private_v1")]

    def test_pydantic_model_is_not_iterated_directly(self):
        """Регресс: итерация модели даёт ('aliases', [...]) — именно это и ломало прод."""
        resp = pydantic_response(("a", "b"))
        assert list(iter(resp))[0][0] == "aliases"
        assert list(_alias_pairs(FakeClient(resp))) == [("a", "b")]


class TestGetActiveCollection:
    def test_resolves_through_pydantic_response(self):
        w = make_wrapper(FakeClient(pydantic_response(("knowledge_public", "knowledge_public_v1"))))
        assert w.get_active_collection("knowledge_public") == "knowledge_public_v1"

    def test_missing_alias_falls_back_to_alias_name(self):
        w = make_wrapper(FakeClient(pydantic_response(("other", "other_v1"))))
        assert w.get_active_collection("knowledge_public") == "knowledge_public"


class TestSwapAlias:
    def test_existing_alias_is_moved_atomically(self):
        fake = FakeClient(pydantic_response(("knowledge_public", "knowledge_public_v1")))
        make_wrapper(fake).swap_alias("knowledge_public", "knowledge_public_v2")

        assert len(fake.alias_ops) == 1, "swap обязан быть ОДНИМ вызовом (атомарно)"
        ops = fake.alias_ops[0]
        deleted = [o for o in ops if getattr(o, "delete_alias", None) is not None]
        created = [o for o in ops if getattr(o, "create_alias", None) is not None]
        assert len(deleted) == 1
        assert deleted[0].delete_alias.alias_name == "knowledge_public"
        assert len(created) == 1
        assert created[0].create_alias.collection_name == "knowledge_public_v2"
        assert created[0].create_alias.alias_name == "knowledge_public"

    def test_alias_without_binding_is_only_created(self):
        fake = FakeClient(pydantic_response())
        make_wrapper(fake).swap_alias("knowledge_public", "knowledge_public_v1")

        ops = fake.alias_ops[0]
        assert len(ops) == 1
        assert getattr(ops[0], "create_alias", None) is not None


class TestHasPoints:
    def test_missing_collection_is_false(self):
        assert make_wrapper(FakeClient()).has_points("knowledge_public_v1") is False

    def test_empty_collection_is_false(self):
        w = make_wrapper(FakeClient(collections={"knowledge_public_v1": True}))
        assert w.has_points("knowledge_public_v1") is False

    def test_filled_collection_is_true(self):
        w = make_wrapper(
            FakeClient(collections={"knowledge_public_v1": True}, points={"knowledge_public_v1": 42})
        )
        assert w.has_points("knowledge_public_v1") is True
