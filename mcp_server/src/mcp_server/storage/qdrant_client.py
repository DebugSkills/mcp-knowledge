"""Qdrant gRPC-клиент (#1): создание коллекции, upsert, search, delete."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from qdrant_client import QdrantClient as QdrantSDKClient
from qdrant_client.http import models as qmodels

from ..config import settings
from .schema import (
    COLLECTION_NAME,
    PAYLOAD_INDEXES,
    build_collection_params,
    build_payload_point,
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
        self._client.create_collection(**params.model_dump())

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

    # ── Point operations ──────────────────────────────────

    def upsert_points(self, points: list[qmodels.PointStruct]) -> None:
        """Вставить/обновить точки в Qdrant."""
        self._client.upsert(
            collection_name=COLLECTION_NAME,
            points=points,
            wait=True,
        )

    def delete_by_knowledge_id(self, knowledge_id: str) -> None:
        """Удалить все точки (чанки) записи."""
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
        )

    def delete_all(self) -> None:
        """Удалить все точки (для сине-зелёного reindex)."""
        self._client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter()  # пустой фильтр = все точки
            ),
        )

    # ── Search ────────────────────────────────────────────

    def search(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: Optional[dict] = None,
        score_threshold: float = 0.0,
    ) -> list[qmodels.ScoredPoint]:
        """Семантический поиск по вектору."""
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

        results = self._client.search(
            collection_name=COLLECTION_NAME,
            query_vector=vector,
            limit=top_k,
            query_filter=query_filter,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return results

    def search_by_tags(
        self,
        tags: list[str],
        match_all: bool = True,
        limit: int = 500,
    ) -> list[qmodels.ScoredPoint]:
        """Поиск по тегам через payload filter (без embedding, без GPU)."""
        if match_all:
            # AND: все теги должны присутствовать
            must_conditions = [
                qmodels.FieldCondition(
                    key="tags",
                    match=qmodels.MatchAny(any=tags),
                )
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
                with_payload=qmodels.WithPayloadSelector(
                    include=["knowledge_id"]
                ),
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
        """Информация о коллекции для /health."""
        info = self._client.get_collection(COLLECTION_NAME)
        return {
            "name": COLLECTION_NAME,
            "points_count": info.points_count,
            "vectors_count": info.vectors_count,
        }

    def close(self) -> None:
        """Закрыть gRPC-соединение."""
        self._client.close()
