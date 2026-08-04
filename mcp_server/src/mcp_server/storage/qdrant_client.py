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
    build_collection_params,
)

logger = logging.getLogger("mcp_knowledge.qdrant")


class QdrantClient:
    """Асинхронная обёртка над Qdrant gRPC SDK."""

    def __init__(self, url: str = settings.QDRANT_URL):
        # Qdrant SDK синхронный, вызовы через run_in_executor
        self._client = QdrantSDKClient(url=url, prefer_grpc=True)
        self._url = url
        logger.info("QdrantClient: url=%s", url)

    # ── Collection management ─────────────────────────────

    def ensure_collection(self, force_recreate: bool = False) -> bool:
        """Создать коллекцию knowledge если не существует."""
        if self._client.collection_exists(COLLECTION_NAME):
            if force_recreate:
                logger.warning("Пересоздание коллекции %s", COLLECTION_NAME)
                self._client.delete_collection(COLLECTION_NAME)
            else:
                logger.info("Коллекция %s уже существует", COLLECTION_NAME)
                return False

        params = build_collection_params()
        self._client.create_collection(**params)

        # Создаём payload-индексы для фильтрации
        for field_name, field_type in PAYLOAD_INDEXES:
            self._client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field_name,
                field_schema=field_type,
            )

        logger.info("Коллекция %s создана (dim=%d, distance=COSINE, HNSW)",
                     COLLECTION_NAME, 1024)
        return True

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

        logger.info("Коллекция %s создана (dim=%d, distance=COSINE)", name, 1024)
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

    def delete_by_knowledge_id(self, knowledge_id: str) -> None:
        """Удалить все точки (чанки) записи.

        wait=True — синхронное удаление (qdrant-client 1.18.0 default wait=False):
        без него точка может оставаться видимой для поиска сразу после delete
        (eventual consistency) → тест-изоляция и cleanup ломаются.
        """
        self._client.delete(
            collection_name=COLLECTION_NAME,
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

    def delete_all(self) -> None:
        """Удалить все точки (для сине-зелёного reindex)."""
        self._client.delete(
            collection_name=COLLECTION_NAME,
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
    ) -> list[qmodels.ScoredPoint]:
        """Семантический поиск по вектору.

        Args:
            vector: вектор запроса.
            top_k: число результатов.
            filters: dict {key: value} для payload-фильтрации.
            score_threshold: минимальный cosine-порог.
            with_vectors: вернуть векторы в результатах (для dup-gate).

        Returns:
            list[qmodels.ScoredPoint] с payload (и векторами если with_vectors=True).
        """
        query_filter = None
        if filters:
            must_conditions = []
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
            if must_conditions:
                query_filter = qmodels.Filter(must=must_conditions)

        results = self._client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=top_k,
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
    ) -> list[qmodels.ScoredPoint]:
        """Поиск по тегам через payload filter (без embedding, без GPU)."""
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
            collection_name=COLLECTION_NAME,
            scroll_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return results[0]  # (points, next_page_offset)

    # ── Reconciliation helpers ────────────────────────────

    def get_all_knowledge_ids(self) -> set[str]:
        """Получить все knowledge_id в Qdrant (для reconciliation, задача 2.9)."""
        ids = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=COLLECTION_NAME,
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

    def collection_info(self) -> dict:
        """Информация о коллекции для /health.

        Фаза 12 fix: REST-mode Qdrant (qdrant-client 1.18) возвращает
        CollectionInfo без vectors_count — используем indexed_vectors_count
        как fallback (getattr безопасен для gRPC и REST).
        """
        info = self._client.get_collection(COLLECTION_NAME)
        return {
            "name": COLLECTION_NAME,
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
    ) -> tuple[list[str], str | None, int]:
        """Собрать уникальные значения поля через Qdrant scroll().

        Используется для list_domains/subjects/projects.
        Returns: (results, next_cursor, total_unique_count)
        """
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
                collection_name=COLLECTION_NAME,
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
