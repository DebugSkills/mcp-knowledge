"""Semantic duplicate gate — pre-write проверка дублей через Qdrant (4.3).

Перед write_knowledge/update_entry:
1. embed репрезентативного вектора (заголовок + первый чанк) через BGE-M3
2. qdrant.search(top_k=8, filter={domain}) → cosine ≥ DUP_SIMILARITY_THRESHOLD=0.92
3. Исключает self (тот же knowledge_id при update)
4. Возвращает список кандидатов-дубликатов

ADVISORY по умолчанию (E5): не блокирует запись, возвращает candidates в ответе.
strict=true → 409 BLOCK при наличии дублей.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("mcp_knowledge.quality.dup_gate")

# ── Конфигурация ─────────────────────────────────────────────

DUP_SIMILARITY_THRESHOLD: float = 0.92  # cosine-порог
SEARCH_TOP_K: int = 8                    # число ближайших соседей


def compute_cosine(a: list[float], b: list[float]) -> float:
    """Вычисляет косинусное сходство двух векторов.

    Чистая функция, тестируется без моков.
    """
    if len(a) != len(b):
        raise ValueError(f"Vector dimension mismatch: {len(a)} vs {len(b)}")
    if len(a) == 0:
        return 0.0

    dot = sum(ai * bi for ai, bi in zip(a, b))
    norm_a = sum(ai * ai for ai in a) ** 0.5
    norm_b = sum(bi * bi for bi in b) ** 0.5

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


def find_duplicates(
    query_vector: list[float],
    candidates: list[tuple[str, list[float]]],  # [(knowledge_id, vector), ...]
    *,
    threshold: float = DUP_SIMILARITY_THRESHOLD,
    exclude_id: Optional[str] = None,
) -> list[dict]:
    """Находит дубликаты среди кандидатов по косинусному сходству.

    Args:
        query_vector: эмбеддинг нового контента.
        candidates: список (knowledge_id, vector) из Qdrant search.
        threshold: минимальный cosine для дубликата.
        exclude_id: knowledge_id для self-exclusion (при update).

    Returns:
        список [{knowledge_id, score}] отсортированный по score DESC.
    """
    duplicates: list[dict] = []
    for kid, vec in candidates:
        if exclude_id is not None and kid == exclude_id:
            continue
        score = compute_cosine(query_vector, vec)
        if score >= threshold:
            duplicates.append({"knowledge_id": kid, "score": round(score, 4)})

    duplicates.sort(key=lambda d: d["score"], reverse=True)
    return duplicates


async def check_duplicates(
    content: str,
    domain: str,
    knowledge_id: Optional[str],
    *,
    embedder=None,         # SentenceTransformer (BGE-M3) — внедряется из app_state
    qdrant_client=None,    # QdrantClient — внедряется из app_state
    threshold: float = DUP_SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Проверяет контент на семантические дубликаты.

    Полный pipeline:
    1. Извлекает заголовок + первый параграф как репрезентативный текст
    2. embed → вектор
    3. Qdrant search по domain
    4. find_duplicates() с self-exclusion

    Args:
        content: полный markdown-контент.
        domain: домен для фильтрации Qdrant search.
        knowledge_id: ID записи (для self-exclusion при update).
        embedder: SentenceTransformer instance.
        qdrant_client: QdrantClient instance.
        threshold: cosine-порог.

    Returns:
        список [{knowledge_id, score}] дубликатов.
    """
    if embedder is None or qdrant_client is None:
        logger.warning("Embedder or Qdrant client not available, skipping dup check")
        return []

    # Шаг 1: извлекаем репрезентативный текст (заголовок + первый чанк)
    representative = _extract_representative_text(content)

    # Шаг 2: embed
    try:
        loop = asyncio.get_event_loop()
        vec = await loop.run_in_executor(None, embedder.encode, representative)
        query_vector = vec.tolist() if hasattr(vec, 'tolist') else list(vec)
    except Exception as exc:
        logger.error("Embed failed for dup-gate: %s", exc)
        return []

    # Шаг 3: Qdrant search
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        # domain-фильтр только если domain указан (для update_entry domain может быть неизвестен)
        query_filter = None
        if domain:
            query_filter = Filter(
                must=[FieldCondition(key="domain", match=MatchValue(value=domain))]
            )

        results = qdrant_client.search(
            collection_name="knowledge",
            query_vector=query_vector,
            limit=SEARCH_TOP_K,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=True,
        )
    except Exception as exc:
        logger.error("Qdrant search failed for dup-gate: %s", exc)
        return []

    # Шаг 4: find_duplicates
    candidates = [
        (hit.payload.get("knowledge_id", str(hit.id)), hit.vector)
        for hit in results
        if hit.vector is not None
    ]
    return find_duplicates(
        query_vector, candidates, threshold=threshold, exclude_id=knowledge_id
    )


def _extract_representative_text(content: str) -> str:
    """Извлекает заголовок + первый параграф как репрезентативный текст."""
    lines = content.strip().split("\n")
    result: list[str] = []
    in_fm = False
    fm_closed = False
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if stripped == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                in_fm = False
                fm_closed = True
                continue
        if in_fm:
            continue
        if in_code_block:
            continue
        if fm_closed or not content.startswith("---"):
            if stripped.startswith("#"):
                result.append(stripped.lstrip("#").strip())
            elif stripped:
                result.append(stripped)
                if len(result) >= 2:
                    break

    return " ".join(result) if result else content[:500]
