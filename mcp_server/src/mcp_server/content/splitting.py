"""Hybrid Semantic Splitting (#34) — 3-стадийное разбиение.

Фаза 5 §6.3: structural → embedding clustering fallback → recursive split.
Гарантия: после всех стадий каждая секция ≤ max_chunk_tokens (512 XLM-R).

Стадии:
  1. Structural: парсинг #/##/### → Section[]
  2. Clustering (fallback): если < MIN_SECTIONS ИЛИ есть oversized-секция
     → paragraph embed (BGE-M3, run_in_executor) → cosine matrix смежных
     → AgglomerativeClustering → группы
  3. Recursive: split по границам предложений до ≤ max_chunk_tokens
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("mcp_knowledge.content.splitting")

# ── Конфигурационные константы ────────────────────────────

MAX_CHUNK_TOKENS = 512       # XLM-RoBERTa токенов на секцию (#13/#20)
MIN_SECTIONS = 2             # если structural дал <2 → fallback clustering
CLUSTER_COSINE = 0.75        # порог cosine для Agglomerative clustering
CLUSTER_BATCH_SIZE = 64      # Фаза 13.21 P2-5: размер батча для embed_paragraphs_async
                              # (64 параграфа за вызов Ollama — хардкод для первой итерации)
CLUSTER_MAX_PARAGRAPHS = 2000  # Фаза 13.21 P1-2: максимальное число параграфов для clustering
                               # (>2000 → skip clustering, fallback на recursive_split во избежание OOM)


@dataclass
class Chunk:
    """Результат разбиения — одна секция с текстом и метаданными."""

    title: str
    body: str
    sequence_number: int = 0


def _split_sentences(text: str) -> list[str]:
    """Разбить текст на предложения (RU+EN, без nltk — regex fallback).

    План §13: nltk НЕ доступен → re.split по границам предложений.
    """
    if not text:
        return []
    # Split by sentence-ending punctuation followed by whitespace
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in raw if s.strip()]


def structural_split(content: str) -> list[Chunk]:
    """Stage 1: структурный парсинг по заголовкам #/##/###.

    Разбивает markdown-контент на секции по границам заголовков.
    Каждый заголовок становится title секции, тело — текст до следующего заголовка.

    Returns:
        list[Chunk]: если заголовков < 2, возвращает 1 секцию со всем контентом.
    """
    if not content or not content.strip():
        return []

    # Pattern: lines starting with 1-3 # followed by space and title
    header_pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

    matches = list(header_pattern.finditer(content))

    if len(matches) < 2:
        # Single section: entire content, title from first header or auto-generated
        title = matches[0].group(2).strip() if matches else "Untitled"
        return [Chunk(title=title, body=content.strip(), sequence_number=1)]

    chunks: list[Chunk] = []
    seq = 0

    for i, match in enumerate(matches):
        seq = i + 1
        title = match.group(2).strip()
        start = match.start()
        # Для первой секции включаем preamble (текст до первого заголовка)
        if i == 0 and start > 0:
            preamble = content[:start].strip()
            if preamble:
                title = title or "Preamble"
        else:
            preamble = ""
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        if preamble and i == 0:
            body = preamble + "\n\n" + body

        chunks.append(Chunk(title=title, body=body, sequence_number=seq))

    return chunks


def _embed_paragraphs(
    paragraphs: list[str],
    embedder,
) -> list[list[float]]:
    """Embed paragraphs через EmbeddingManager (должен вызываться в run_in_executor)."""
    if not paragraphs:
        return []
    return embedder.embed_sync(paragraphs)


async def embed_paragraphs_async(
    paragraphs: list[str],
    embedder,
) -> list[list[float]]:
    """Embed paragraphs в run_in_executor (не блокирует event loop).

    Фаза 13.21 P2-5: батчинг по CLUSTER_BATCH_SIZE=64 — предотвращает
    отправку тысяч параграфов одним вызовом Ollama (OOM guard).
    """
    loop = asyncio.get_running_loop()
    if len(paragraphs) <= CLUSTER_BATCH_SIZE:
        return await loop.run_in_executor(None, _embed_paragraphs, paragraphs, embedder)

    # Батчинг: разбиваем на группы по CLUSTER_BATCH_SIZE и embed итеративно
    all_embeddings: list[list[float]] = []
    for i in range(0, len(paragraphs), CLUSTER_BATCH_SIZE):
        batch = paragraphs[i : i + CLUSTER_BATCH_SIZE]
        batch_embeddings = await loop.run_in_executor(None, _embed_paragraphs, batch, embedder)
        all_embeddings.extend(batch_embeddings)
    return all_embeddings


def _cosine_clustering(
    embeddings,  # numpy ndarray — type omitted (lazy import of numpy)
    threshold: float = CLUSTER_COSINE,
) -> list[int]:
    """Аггломеративная кластеризация на основе cosine similarity с оконным ограничением.

    Использует AgglomerativeClustering с cosine метрикой.
    Смежные параграфы группируются если их cosine ≥ threshold.

    Args:
        embeddings: numpy array (n_paragraphs, embed_dim)
        threshold: порог cosine для группировки (0.75 default)

    Returns:
        list[int]: метки кластеров для каждого параграфа
    """
    # Lazy imports (P1-1: avoid numpy double-import during coverage measurement)
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics.pairwise import cosine_similarity

    n = embeddings.shape[0]
    if n <= 1:
        return [0] * n

    # Compute pairwise cosine distances (1 - cosine_similarity)
    distance_matrix = 1.0 - cosine_similarity(embeddings)

    # AgglomerativeClustering с threshold
    clustering = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=1.0 - threshold,  # distance threshold = 1 - cosine
        metric="precomputed",
        linkage="average",
    )
    labels = clustering.fit_predict(distance_matrix)

    return labels.tolist()


def _merge_clustered_paragraphs(
    paragraphs: list[str],
    labels: list[int],
) -> list[tuple[str, int]]:
    """Объединить параграфы по кластерным меткам.

    Смежные параграфы с одинаковой меткой объединяются в одну секцию.

    Returns:
        list of (body, paragraph_count) tuples
    """
    if not paragraphs:
        return []

    merged: list[tuple[str, int]] = []
    current_text = paragraphs[0]
    current_label = labels[0]
    count = 1

    for i in range(1, len(paragraphs)):
        if labels[i] == current_label:
            current_text += "\n\n" + paragraphs[i]
            count += 1
        else:
            merged.append((current_text, count))
            current_text = paragraphs[i]
            current_label = labels[i]
            count = 1

    merged.append((current_text, count))
    return merged


def clustering_split(
    content: str,
    embeddings: list[list[float]],
) -> list[Chunk]:
    """Stage 2: кластеризация абзацев по embedding-близости.

    Args:
        content: исходный текст
        embeddings: векторы абзацев (list of float vectors)

    Returns:
        list[Chunk]: сгруппированные секции
    """
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        return [Chunk(title="Content", body=content.strip(), sequence_number=1)]

    # Lazy import numpy (P1-1: avoid double-import during coverage measurement)
    import numpy as np
    emb_array = np.array(embeddings, dtype=np.float64)
    labels = _cosine_clustering(emb_array, CLUSTER_COSINE)
    merged = _merge_clustered_paragraphs(paragraphs, labels)

    chunks: list[Chunk] = []
    for i, (body, _) in enumerate(merged, 1):
        # Extract title from first line or use auto-generated
        first_line = body.split("\n")[0].strip()
        title = first_line[:120] if first_line else f"Section {i}"
        chunks.append(Chunk(title=title, body=body, sequence_number=i))

    return chunks


def recursive_split(
    chunk: Chunk,
    max_tokens: int = MAX_CHUNK_TOKENS,
    token_counter=None,
) -> list[Chunk]:
    """Stage 3: recursive split — гарантия ≤ max_tokens на секцию.

    Если token_counter=None (нет XLM-RoBERTa), используется char-count fallback:
    ~4 символа/токен для русского + английского (грубая оценка).

    Args:
        chunk: секция для проверки/разбиения
        max_tokens: лимит токенов XLM-R (default 512)
        token_counter: XlmRobertaTokenizer (если None — char-count fallback)

    Returns:
        list[Chunk]: 1 или более секций, каждая ≤ max_tokens
    """
    # ── Определяем «токенность» с fallback на char-count ──
    if token_counter is not None:
        def _count(text: str) -> int:
            return token_counter.count_tokens(text)

        def _truncate(text: str, limit: int) -> str:
            return token_counter.truncate_to_tokens(text, limit)
    else:
        # Char-count fallback: ~4 символа/токен (русский + английский)
        CHARS_PER_TOKEN = 4
        

        def _count(text: str) -> int:
            return len(text) // CHARS_PER_TOKEN

        def _truncate(text: str, limit: int) -> str:
            return text[:limit * CHARS_PER_TOKEN]

    if _count(chunk.body) <= max_tokens:
        return [chunk]

    sentences = _split_sentences(chunk.body)
    if len(sentences) <= 1:
        # Одно предложение > max_tokens — обрезаем
        truncated = _truncate(chunk.body, max_tokens)
        return [Chunk(
            title=chunk.title,
            body=truncated,
            sequence_number=chunk.sequence_number,
        )]

    # Greedy grouping: набираем предложения пока ≤ max_tokens
    result: list[Chunk] = []
    current_parts: list[str] = []
    current_tokens = 0
    sub_seq = 0

    for sent in sentences:
        sent_tokens = _count(sent)
        if current_parts and current_tokens + sent_tokens > max_tokens:
            sub_seq += 1
            result.append(Chunk(
                title=f"{chunk.title} (part {sub_seq})",
                body=" ".join(current_parts),
                sequence_number=chunk.sequence_number,
            ))
            current_parts = [sent]
            current_tokens = sent_tokens
        else:
            current_parts.append(sent)
            current_tokens += sent_tokens

    if current_parts:
        sub_seq += 1
        suffix = f" (part {sub_seq})" if sub_seq > 1 else ""
        result.append(Chunk(
            title=f"{chunk.title}{suffix}",
            body=" ".join(current_parts),
            sequence_number=chunk.sequence_number,
        ))

    return result if result else [chunk]


async def hybrid_split(
    content: str,
    embedder,  # EmbeddingManager
    token_counter,  # XlmRobertaTokenizer
    max_tokens: int = MAX_CHUNK_TOKENS,
    min_sections: int = MIN_SECTIONS,
) -> list[Chunk]:
    """Основная точка входа: 3-стадийное гибридное разбиение.

    Args:
        content: исходный текст (Markdown/plain)
        embedder: EmbeddingManager.embed_sync(texts) → list[list[float]]
        token_counter: XlmRobertaTokenizer.count_tokens(text) → int
        max_tokens: лимит токенов (default 512)
        min_sections: минимальное число секций до fallback (default 2)

    Returns:
        list[Chunk]: секции, каждая ≤ max_tokens
    """
    # Stage 1: Structural parse
    chunks = structural_split(content)

    # Проверяем: нужен ли fallback на clustering?
    need_clustering = len(chunks) < min_sections
    if not need_clustering and token_counter is not None:
        # Проверяем oversized-секции
        for ch in chunks:
            if token_counter.count_tokens(ch.body) > max_tokens:
                need_clustering = True
                break

    # Stage 2: Clustering fallback
    if need_clustering:
        logger.info(
            "Hybrid split: structural gave %d sections (<%d) or oversized — "
            "fallback to embedding clustering",
            len(chunks), min_sections,
        )
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        if len(paragraphs) > CLUSTER_MAX_PARAGRAPHS:
            # Фаза 13.21 P1-2: OOM guard — при >2000 параграфов skip clustering,
            # сразу переходим к recursive_split (без построения N×N матрицы).
            logger.warning(
                "Hybrid split: %d paragraphs > CLUSTER_MAX_PARAGRAPHS=%d — "
                "skipping clustering fallback, using structural result",
                len(paragraphs), CLUSTER_MAX_PARAGRAPHS,
            )
        elif len(paragraphs) >= 2 and embedder is not None:
            try:
                embeddings = await embed_paragraphs_async(paragraphs, embedder)
                chunks = clustering_split(content, embeddings)
                logger.info(
                    "Clustering fallback: %d paragraphs → %d sections",
                    len(paragraphs), len(chunks),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Clustering fallback failed: %s — using structural result", e
                )

    # Stage 3: Recursive split — гарантия ≤ max_tokens
    final_chunks: list[Chunk] = []
    for ch in chunks:
        split_result = recursive_split(ch, max_tokens, token_counter)
        final_chunks.extend(split_result)

    # Пересчёт sequence_number после возможного recursive split
    for i, ch in enumerate(final_chunks, 1):
        ch.sequence_number = i

    return final_chunks
