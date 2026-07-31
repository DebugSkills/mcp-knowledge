"""Qdrant payload-схема и константы коллекции knowledge."""

from __future__ import annotations

from qdrant_client.http import models as qmodels

COLLECTION_NAME = "knowledge"
VECTOR_SIZE = 1024  # BGE-M3
DISTANCE_METRIC = qmodels.Distance.COSINE

# Payload-поля с индексами для фильтрации
PAYLOAD_SCHEMA = {
    "knowledge_id": qmodels.PayloadSchemaType.KEYWORD,
    "chunk_id": qmodels.PayloadSchemaType.KEYWORD,
    "domain": qmodels.PayloadSchemaType.KEYWORD,
    "subject": qmodels.PayloadSchemaType.KEYWORD,
    "project": qmodels.PayloadSchemaType.KEYWORD,
    "tags": qmodels.PayloadSchemaType.KEYWORD,  # массив — каждый элемент индексируется
    "cross_subjects": qmodels.PayloadSchemaType.KEYWORD,
    "section_header": qmodels.PayloadSchemaType.KEYWORD,
    "chunk_index": qmodels.PayloadSchemaType.INTEGER,
    "updated_at": qmodels.PayloadSchemaType.DATETIME,
}

# Индексы для payload-полей
PAYLOAD_INDEXES: list[tuple[str, qmodels.PayloadSchemaType]] = [
    ("knowledge_id", qmodels.PayloadSchemaType.KEYWORD),
    ("domain", qmodels.PayloadSchemaType.KEYWORD),
    ("subject", qmodels.PayloadSchemaType.KEYWORD),
    ("project", qmodels.PayloadSchemaType.KEYWORD),
    ("tags", qmodels.PayloadSchemaType.KEYWORD),
    ("updated_at", qmodels.PayloadSchemaType.DATETIME),
]

# HNSW-параметры
HNSW_CONFIG = qmodels.HnswConfigDiff(
    m=16,  # количество рёбер на вершину (баланс скорость/точность)
    ef_construct=100,  # размер динамического списка при построении
)

# Оптимизаторы
OPTIMIZERS_CONFIG = qmodels.OptimizersConfigDiff(
    default_segment_number=2,
    indexing_threshold=20_000,  # отложенная индексация для массового импорта
)


def build_collection_params() -> qmodels.CreateCollection:
    """Параметры создания коллекции knowledge."""
    return qmodels.CreateCollection(
        collection_name=COLLECTION_NAME,
        vectors_config=qmodels.VectorParams(
            size=VECTOR_SIZE,
            distance=DISTANCE_METRIC,
        ),
        hnsw_config=HNSW_CONFIG,
        optimizers_config=OPTIMIZERS_CONFIG,
    )


def build_payload_point(
    point_id: str,
    vector: list[float],
    knowledge_id: str,
    chunk_id: str,
    content: str,
    domain: str,
    subject: str,
    project: str | None,
    tags: list[str],
    cross_subjects: list[str],
    section_header: str,
    chunk_index: int,
    updated_at: str,
) -> qmodels.PointStruct:
    """Собрать PointStruct для upsert."""
    payload = {
        "knowledge_id": knowledge_id,
        "chunk_id": chunk_id,
        "content": content,
        "domain": domain,
        "subject": subject,
        "project": project or "",
        "tags": tags,
        "cross_subjects": cross_subjects,
        "section_header": section_header,
        "chunk_index": chunk_index,
        "updated_at": updated_at,
    }
    return qmodels.PointStruct(id=point_id, vector=vector, payload=payload)
