"""BookPreprocessor — первая реализация ContentPreprocessor для content_type="book".

Фаза 5 §3.2, задача 5.2 (#33/#35):
- Structural parse: вызов splitting.structural_split
- Auto-frontmatter: knowledge_id slug, title, domain/subject/tags унаследованы
- Keyword extraction: TF-IDF через keywords.extract_keywords → теги
- Hybrid splitting: splitting.hybrid_split (structural → clustering → recursive)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

# F4: Глобальный синглтон XLM-RoBERTa токенизатора для точного подсчёта токенов
from ..embedding.tokenizer import tokenizer as xlmr_tokenizer
from .keywords import deduplicate_tags, extract_keywords
from .linking import make_knowledge_id
from .preprocessor import (
    ContentPreprocessor,
    ImportMeta,
    Section,
    ValidationResult,
)
from .splitting import hybrid_split

logger = logging.getLogger("mcp_knowledge.content.book_preprocessor")

# Минимальный размер контента для обработки
MIN_CONTENT_LENGTH = 50


class BookPreprocessor(ContentPreprocessor):
    """Препроцессор для content_type="book" — книги и структурированные документы."""

    content_type = "book"

    def __init__(
        self,
        embedder=None,  # EmbeddingManager (опционально, для clustering fallback)
        token_counter=xlmr_tokenizer,  # XlmRobertaTokenizer — глобальный синглтон (F4 fix)
        max_chunk_tokens: int = 512,
    ):
        self._embedder = embedder
        self._token_counter = token_counter
        self._max_chunk_tokens = max_chunk_tokens

    def validate(self, content: str, metadata: ImportMeta) -> ValidationResult:
        """Проверка: non-empty, мин. размер, кодировка."""
        if not content or not content.strip():
            return ValidationResult(
                valid=False,
                error="Content is empty",
                content_size=0,
            )

        content_size = len(content)
        if content_size < MIN_CONTENT_LENGTH:
            return ValidationResult(
                valid=False,
                error=f"Content too short: {content_size} chars (min {MIN_CONTENT_LENGTH})",
                content_size=content_size,
            )

        # Оценка числа секций: грубо по заголовкам
        import re
        header_count = len(re.findall(r"^#{1,3}\s+", content, re.MULTILINE))
        estimated = max(header_count, 1)

        return ValidationResult(
            valid=True,
            content_size=content_size,
            estimated_sections=estimated,
        )

    async def decompose(
        self,
        content: str,
        metadata: ImportMeta,
        cancel_event: "asyncio.Event | None" = None,  # noqa: ARG002 — V3 13.26: единый контракт (книги не поддерживают отмену)
    ) -> list[Section]:
        """Декомпозиция книги в список Section через hybrid_split + keywords."""
        logger.info(
            "BookPreprocessor.decompose: domain=%s subject=%s size=%d",
            metadata.domain, metadata.subject, len(content),
        )

        # Шаг 1: Hybrid splitting (structural → clustering → recursive)
        chunks = await hybrid_split(
            content=content,
            embedder=self._embedder,
            token_counter=self._token_counter,
            max_tokens=self._max_chunk_tokens,
        )

        if not chunks:
            logger.warning("BookPreprocessor: hybrid_split вернул 0 секций")
            return []

        # Шаг 2: Keyword extraction (TF-IDF по всем секциям как корпус)
        chunk_texts = [ch.body for ch in chunks]
        all_keywords = extract_keywords(chunk_texts, top_n=5)

        # Шаг 3: Сборка Section[] с auto-frontmatter
        sections: list[Section] = []
        for i, (ch, kw_list) in enumerate(zip(chunks, all_keywords), 1):
            # Генерируем knowledge_id
            content_hash = hashlib.sha256(
                ch.body[:200].encode()
            ).hexdigest()
            knowledge_id = make_knowledge_id(
                domain=metadata.domain,
                subject=metadata.subject,
                title=ch.title,
                sequence_number=i,
                content_hash=content_hash,
            )

            # Дедупликация тегов: inherited + auto
            tags = deduplicate_tags(kw_list, metadata.tags)

            section = Section(
                title=ch.title,
                body=ch.body,
                sequence_number=i,
                tags=tags,
                meta={
                    "knowledge_id": knowledge_id,
                    "domain": metadata.domain,
                    "subject": metadata.subject,
                    "project": metadata.project,
                    "content_type": "book",
                    "cross_subjects": metadata.cross_subjects,
                },
            )
            sections.append(section)

        logger.info(
            "BookPreprocessor: decomposed %d sections from %d chars",
            len(sections), len(content),
        )
        return sections
