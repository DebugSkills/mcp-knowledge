"""Qdrant payload-схема и константы коллекции knowledge.

Фаза 3 F1: Blue-green reindex через Qdrant Collection Aliases.
- COLLECTION_ALIAS = "knowledge" — имя alias (search/upsert прозрачны)
- Реальные коллекции: knowledge_v1, knowledge_v2 (чередуются)
"""

from __future__ import annotations

from qdrant_client.http import models as qmodels

# Alias name (search/upsert прозрачны — код Ф1/Ф2 не меняется)
COLLECTION_ALIAS = "knowledge"

# Legacy: backward-compatible (используется как default для операций)
COLLECTION_NAME = COLLECTION_ALIAS

# Naming convention для blue-green коллекций
COLLECTION_V1 = "knowledge_v1"
COLLECTION_V2 = "knowledge_v2"

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
    "parent_knowledge_id": qmodels.PayloadSchemaType.KEYWORD,  # Фаза 5: parent-child collection
    "content_type": qmodels.PayloadSchemaType.KEYWORD,  # Фаза 5: book | pdf | collection
}

# Индексы для payload-полей
PAYLOAD_INDEXES: list[tuple[str, qmodels.PayloadSchemaType]] = [
    ("knowledge_id", qmodels.PayloadSchemaType.KEYWORD),
    ("domain", qmodels.PayloadSchemaType.KEYWORD),
    ("subject", qmodels.PayloadSchemaType.KEYWORD),
    ("project", qmodels.PayloadSchemaType.KEYWORD),
    ("tags", qmodels.PayloadSchemaType.KEYWORD),
    ("updated_at", qmodels.PayloadSchemaType.DATETIME),
    ("parent_knowledge_id", qmodels.PayloadSchemaType.KEYWORD),  # Фаза 5
    ("content_type", qmodels.PayloadSchemaType.KEYWORD),  # Фаза 5
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


def build_collection_params(collection_name: str | None = None) -> dict:
    """Параметры создания коллекции.

    Возвращает dict для прямой передачи в QdrantClient.create_collection(**kwargs).
    Совместим с qdrant-client >=1.13 (CreateCollection model API меняется между версиями).

    Args:
        collection_name: имя коллекции (default: COLLECTION_ALIAS).
    """
    return {
        "collection_name": collection_name or COLLECTION_ALIAS,
        "vectors_config": qmodels.VectorParams(
            size=VECTOR_SIZE,
            distance=DISTANCE_METRIC,
        ),
        "hnsw_config": HNSW_CONFIG,
        "optimizers_config": OPTIMIZERS_CONFIG,
    }


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
    parent_knowledge_id: str | None = None,
    content_type: str | None = None,
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
    if parent_knowledge_id:
        payload["parent_knowledge_id"] = parent_knowledge_id
    if content_type:
        payload["content_type"] = content_type
    return qmodels.PointStruct(id=point_id, vector=vector, payload=payload)
