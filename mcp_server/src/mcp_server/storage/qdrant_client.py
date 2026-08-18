# ruff: noqa: BLE001, S110
"""Qdrant gRPC-клиент (#1): создание коллекции, upsert, search, delete.

Фаза 3 F1: Blue-green reindex через Collection Aliases.
- COLLECTION_ALIAS = "knowledge" → search/upsert прозрачны
- Реальные коллекции: knowledge_v1, knowledge_v2 (чередуются)
"""

from __future__ import annotations

import logging

from qdrant_client import QdrantClient as QdrantSDKClient
from qdrant_client.http import models as qmodels

from ..config import settings
from .schema import (
    COLLECTION_ALIAS,
    COLLECTION_NAME,
    PAYLOAD_INDEXES,
    VECTOR_SIZE,
    ZONE_PRIVATE,
    ZONE_PUBLIC,
    blue_green_names_for_zone,
    build_collection_params,
)

logger = logging.getLogger("mcp_knowledge.qdrant")


class QdrantClient:
    """Асинхронная обёртка над Qdrant gRPC SDK."""

    def __init__(self, url: str = settings.QDRANT_URL):
        # Qdrant SDK синхронный, вызовы через run_in_executor
        self._client = QdrantSDKClient(url=url, prefer_grpc=settings.QDRANT_PREFER_GRPC)
        self._url = url
        logger.info("QdrantClient: url=%s", url)

    @staticmethod
    def _require_collection(collection_name: str | None) -> str:
        """W2.6: зональный контракт — коллекцию обязан резолвить вызывающий.

        Зону резолвит вызывающий через collection_for_zone(zone);
        зональные методы не имеют дефолтной коллекции.
        """
        if collection_name is None:
            raise ValueError("collection_name is required")
        return collection_name

    # ── Collection management ─────────────────────────────

    def ensure_collection(self, force_recreate: bool = False) -> bool:
        """Wrapper (legacy-контракт cli.py/main.py): гарантировать зональные коллекции."""
        return self.ensure_zonal_collections(force_recreate=force_recreate)

    def ensure_zonal_collections(self, force_recreate: bool = False) -> bool:
        """W2.5: гарантировать blue-green коллекции обеих зон (public, private).

        Для каждой зоны:
        - определить активную коллекцию за alias через get_active_collection();
        - если активная есть — она остаётся (idempotent);
        - если нет — создать v1 и атомарно привязать alias → v1.

        Args:
            force_recreate: пересоздать v1, если она существует без активной
                коллекции за alias (игнорируется, если активная уже есть).

        Returns:
            True, если была создана хотя бы одна коллекция (контракт main.py:164).
        """
        created_any = False
        for zone in (ZONE_PUBLIC, ZONE_PRIVATE):
            v1, _v2, alias = blue_green_names_for_zone(zone)

            # Активная коллекция за alias: если есть — сохранить как есть
            try:
                candidate = self.get_active_collection(alias)
            except Exception:
                candidate = alias  # fallback: alias не настроен

            if candidate != alias:
                logger.info("Зона '%s': активная коллекция '%s' уже существует", zone, candidate)
                continue

            # Активной нет — создать v1 и привязать alias
            self.create_collection_named(v1, force_recreate=force_recreate)
            self.swap_alias(alias, v1)
            created_any = True
            logger.info("Зона '%s': коллекция '%s' создана, alias '%s' → '%s'",
                        zone, v1, alias, v1)

        return created_any

    # ── F1: Blue-green alias management ────────────────────

    def create_collection_named(self, name: str, force_recreate: bool = False) -> bool:
        """Создать коллекцию с заданным именем (для blue-green).

        Args:
            name: имя коллекции (например, knowledge_v2)
            force_recreate: пересоздать если уже существует

        Returns:
            True если коллекция была создана, False если уже существовала.
        """
        if self._client.collection_exists(name):
            if force_recreate:
                logger.warning("Пересоздание коллекции %s", name)
                self._client.delete_collection(name)
            else:
                logger.info("Коллекция %s уже существует", name)
                return False

        params = build_collection_params(name)
        self._client.create_collection(**params)

        # Создаём payload-индексы
        for field_name, field_type in PAYLOAD_INDEXES:
            self._client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=field_type,
            )

        logger.info("Коллекция %s создана (dim=%d, distance=COSINE)", name, VECTOR_SIZE)
        return True

    def swap_alias(self, alias: str, target: str) -> None:
        """Атомарно переключить alias на новую коллекцию.

        Удаляет все существующие привязки alias и создаёт новую.
        Операция атомарна со стороны Qdrant (<1 сек downtime).
        """
        self._client.update_collection_aliases(
            change_aliases_operations=[
                qmodels.CreateAliasOperation(
                    create_alias=qmodels.CreateAlias(
                        collection_name=target,
                        alias_name=alias,
                    )
                )
            ],
        )
        logger.info("Alias '%s' → '%s' (swap complete)", alias, target)

    def delete_collection_named(self, name: str) -> None:
        """Удалить коллекцию по имени (cleanup старой после blue-green swap)."""
        if self._client.collection_exists(name):
            self._client.delete_collection(name)
            logger.info("Коллекция '%s' удалена (cleanup)", name)

    def has_alias(self, alias_name: str) -> bool:
        """Проверить, существует ли alias."""
        try:
            aliases = self._client.get_aliases()
            for desc in aliases:
                if desc.alias_name == alias_name:
                    return True
        except Exception:
            pass
        return False

    def delete_alias(self, alias_name: str) -> None:
        """W2.11: удалить alias (идемпотентно — отсутствующий alias не ошибка).

        Использует update_collection_aliases (transport-agnostic: gRPC/REST),
        как swap_alias. Повторный вызов после удаления — no-op.
        """
        if not self.has_alias(alias_name):
            logger.info("Alias '%s' не существует — delete пропущен", alias_name)
            return
        self._client.update_collection_aliases(
            change_aliases_operations=[
                qmodels.DeleteAliasOperation(
                    delete_alias=qmodels.DeleteAlias(alias_name=alias_name)
                )
            ],
        )
        logger.info("Alias '%s' удалён", alias_name)

    def rename_collection(self, old_name: str, new_name: str) -> None:
        """Переименовать коллекцию (для legacy migration).

        Args:
            old_name: текущее имя
            new_name: новое имя
        """
        if not self._client.collection_exists(old_name):
            raise ValueError(f"Collection '{old_name}' does not exist, cannot rename")
        self._client.update_collection_aliases(
            change_aliases_operations=[
                qmodels.RenameAliasOperation(
                    rename_alias=qmodels.RenameAlias(
                        old_alias_name=old_name,
                        new_alias_name=new_name,
                    )
                )
            ],
        )
        logger.info("Collection renamed: '%s' → '%s'", old_name, new_name)

    def create_alias(self, alias_name: str, collection_name: str) -> None:
        """Создать alias, указывающий на коллекцию."""
        self._client.update_collection_aliases(
            change_aliases_operations=[
                qmodels.CreateAliasOperation(
                    create_alias=qmodels.CreateAlias(
                        collection_name=collection_name,
                        alias_name=alias_name,
                    )
                )
            ],
        )
        logger.info("Alias '%s' → '%s' created", alias_name, collection_name)

    def get_active_collection(self, alias_name: str | None = None) -> str:
        """Получить имя активной коллекции за alias'ом.

        Args:
            alias_name: имя alias для поиска (default: COLLECTION_ALIAS = "knowledge").

        Returns:
            Имя реальной коллекции (knowledge_v1 или knowledge_v2),
            или alias_name если alias не настроен.
        """
        alias = alias_name or COLLECTION_ALIAS
        try:
            aliases = self._client.get_aliases()
            for desc in aliases:
                if desc.alias_name == alias:
                    return desc.collection_name
        except Exception:
            pass
        return alias

    # ── Point operations ──────────────────────────────────

    def upsert_points(
        self,
        points: list[qmodels.PointStruct],
        collection_name: str | None = None,
    ) -> None:
        """Вставить/обновить точки в Qdrant.

        Args:
            points: список PointStruct для вставки
            collection_name: имя коллекции (default: COLLECTION_NAME alias).
                Для blue-green: передать "knowledge_v2" явно.
        """
        self._client.upsert(
            collection_name=collection_name or COLLECTION_NAME,
            points=points,
            wait=True,
        )

    def delete_by_knowledge_id(
        self,
        knowledge_id: str,
        collection_name: str | None = None,
    ) -> None:
        """Удалить все точки (чанки) записи.

        wait=True — синхронное удаление (qdrant-client 1.18.0 default wait=False):
        без него точка может оставаться видимой для поиска сразу после delete
        (eventual consistency) → тест-изоляция и cleanup ломаются.
        """
        collection_name = self._require_collection(collection_name)
        self._client.delete(
            collection_name=collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="knowledge_id",
                            match=qmodels.MatchValue(value=knowledge_id),
                        )
                    ]
                )
            ),
            wait=True,
        )

    def set_payload(
        self,
        payload: dict,
        points_filter: qmodels.Filter | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Обновить payload для точек по фильтру (lifecycle: deprecated|published).

        Обёртка над raw set_payload — зону резолвит вызывающий
        (quality-инструменты: resolve_quality_issue: deprecate/restore).

        ВАЖНО: raw SDK (qdrant-client 1.18) принимает selector позиционным
        аргументом `points` (Filter/FilterSelector/PointIdsList), НЕ `points_filter`.
        """
        collection_name = self._require_collection(collection_name)
        self._client.set_payload(
            collection_name=collection_name,
            payload=payload,
            points=points_filter,
        )

    def delete_all(self, collection_name: str | None = None) -> None:
        """Удалить все точки (для сине-зелёного reindex)."""
        collection_name = self._require_collection(collection_name)
        self._client.delete(
            collection_name=collection_name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter()  # пустой фильтр = все точки
            ),
            wait=True,
        )

    # ── Search ────────────────────────────────────────────

    def search(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict | None = None,
        score_threshold: float = 0.0,
        with_vectors: bool = False,
        exclude_content_types: list[str] | None = None,
        exclude_statuses: list[str] | None = None,
        offset: int = 0,
        collection_name: str | None = None,
    ) -> list[qmodels.ScoredPoint]:
        """Семантический поиск по вектору.

        Args:
            vector: вектор запроса.
            top_k: число результатов.
            filters: dict {key: value} для payload-фильтрации.
            score_threshold: минимальный cosine-порог.
            with_vectors: вернуть векторы в результатах (для dup-gate).
            exclude_content_types: исключить точки с этими content_type
                (например ["collection"] — root-заглушки книг из результатов поиска).
            exclude_statuses: исключить точки с этими статусами (например ["deprecated"]).
                Фаза 13.14: добавлен must_not по полю status для lifecycle-фильтрации.

        Returns:
            list[qmodels.ScoredPoint] с payload (и векторами если with_vectors=True).
        """
        collection_name = self._require_collection(collection_name)
        query_filter = None
        must_conditions = []
        must_not_conditions = []
        if filters:
            for key, value in filters.items():
                if isinstance(value, list):
                    must_conditions.append(
                        qmodels.FieldCondition(
                            key=key,
                            match=qmodels.MatchAny(any=value),
                        )
                    )
                else:
                    must_conditions.append(
                        qmodels.FieldCondition(
                            key=key,
                            match=qmodels.MatchValue(value=value),
                        )
                    )
        if exclude_content_types:
            must_not_conditions.extend(
                qmodels.FieldCondition(
                    key="content_type",
                    match=qmodels.MatchValue(value=ct),
                )
                for ct in exclude_content_types
            )
        if exclude_statuses:
            must_not_conditions.extend(
                qmodels.FieldCondition(
                    key="status",
                    match=qmodels.MatchValue(value=st),
                )
                for st in exclude_statuses
            )
        if must_conditions or must_not_conditions:
            query_filter = qmodels.Filter(
                must=must_conditions or None,
                must_not=must_not_conditions or None,
            )

        results = self._client.query_points(
            collection_name=collection_name,
            query=vector,
            limit=top_k,
            offset=offset,
            query_filter=query_filter,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=with_vectors,
        )
        return results.points

    def search_by_tags(
        self,
        tags: list[str],
        match_all: bool = True,
        limit: int = 500,
        collection_name: str | None = None,
    ) -> list[qmodels.ScoredPoint]:
        """Поиск по тегам через payload filter (без embedding, без GPU)."""
        collection_name = self._require_collection(collection_name)
        if match_all:
            # AND: все теги должны присутствовать — N отдельных MatchValue условий
            must_conditions = [
                qmodels.FieldCondition(
                    key="tags",
                    match=qmodels.MatchValue(value=tag),
                )
                for tag in tags
            ]
            query_filter = qmodels.Filter(must=must_conditions)
        else:
            # OR: любой из тегов
            should_conditions = [
                qmodels.FieldCondition(
                    key="tags",
                    match=qmodels.MatchValue(value=tag),
                )
                for tag in tags
            ]
            query_filter = qmodels.Filter(should=should_conditions)

        results = self._client.scroll(
            collection_name=collection_name,
            scroll_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return results[0]  # (points, next_page_offset)

    def scroll(
        self,
        scroll_filter: qmodels.Filter | None = None,
        limit: int = 100,
        offset: object = None,
        with_payload: list[str] | bool = True,
        with_vectors: bool = False,
        collection_name: str | None = None,
    ) -> tuple[list[qmodels.Record], object]:
        """Scroll по payload-фильтру (для list_collections и обходов).

        Args:
            scroll_filter: Qdrant Filter (payload-условия).
            limit: число точек за один scroll.
            offset: курсор пагинации (None = с начала).
            with_payload: список полей payload или True (все).
            with_vectors: возвращать ли векторы.
            collection_name: имя зональной коллекции (обязателен).

        Returns:
            (points, next_page_offset) — как в qdrant SDK.
        """
        collection_name = self._require_collection(collection_name)
        return self._client.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=limit,
            offset=offset,
            with_payload=with_payload,
            with_vectors=with_vectors,
        )

    # ── Reconciliation helpers ────────────────────────────

    def get_all_knowledge_ids(self, collection_name: str | None = None) -> set[str]:
        """Получить все knowledge_id в Qdrant (для reconciliation, задача 2.9)."""
        collection_name = self._require_collection(collection_name)
        ids = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=collection_name,
                limit=1000,
                offset=offset,
                with_payload=["knowledge_id"],
                with_vectors=False,
            )
            for point in points:
                kid = point.payload.get("knowledge_id") if point.payload else None
                if kid:
                    ids.add(kid)
            if offset is None:
                break
        return ids

    def collection_info(self, collection_name: str | None = None) -> dict:
        """Информация о коллекции для /health.

        Фаза 12 fix: REST-mode Qdrant (qdrant-client 1.18) возвращает
        CollectionInfo без vectors_count — используем indexed_vectors_count
        как fallback (getattr безопасен для gRPC и REST).
        """
        collection_name = self._require_collection(collection_name)
        info = self._client.get_collection(collection_name)
        return {
            "name": collection_name,
            "points_count": getattr(info, "points_count", 0),
            "vectors_count": getattr(
                info,
                "vectors_count",
                getattr(info, "indexed_vectors_count", 0),
            ),
        }

    def scroll_unique_values(
        self,
        field: str,
        domain_filter: str | None = None,
        subject_filter: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        max_scan: int = 50_000,
        collection_name: str | None = None,
    ) -> tuple[list[str], str | None, int]:
        """Собрать уникальные значения поля через Qdrant scroll().

        Используется для list_domains/subjects/projects.
        Returns: (results, next_cursor, total_unique_count)
        """
        collection_name = self._require_collection(collection_name)
        must_conditions = []
        if domain_filter:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="domain",
                    match=qmodels.MatchValue(value=domain_filter),
                )
            )
        if subject_filter:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="subject",
                    match=qmodels.MatchValue(value=subject_filter),
                )
            )

        scroll_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

        offset = None
        if cursor:
            try:
                offset = int(cursor) if str(cursor).isdigit() else None
            except (ValueError, AttributeError):
                offset = None

        seen: set[str] = set()
        results: list[str] = []
        total_scanned = 0

        while len(results) < limit and total_scanned < max_scan:
            points, next_offset = self._client.scroll(
                collection_name=collection_name,
                limit=min(1000, limit * 2),
                offset=offset,
                scroll_filter=scroll_filter,
                with_payload=[field],
                with_vectors=False,
            )

            for point in points:
                if point.payload:
                    val = point.payload.get(field)
                    if val and isinstance(val, str) and val not in seen:
                        seen.add(val)
                        results.append(val)
                        if len(results) >= limit:
                            break

            total_scanned += len(points)
            if next_offset is None or len(points) == 0:
                offset = None
                break
            offset = next_offset

        next_cursor = str(offset) if offset is not None else None
        return results[:limit], next_cursor, len(seen)

    def close(self) -> None:
        """Закрыть gRPC-соединение."""
        self._client.close()
