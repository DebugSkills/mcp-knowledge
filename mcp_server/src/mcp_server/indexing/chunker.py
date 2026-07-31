"""Markdown-чанкер: разбиение по ## заголовкам с XLM-RoBERTa-токенизацией (#13, #20).

Задача 1.4 плана Фазы 1.

Алгоритм:
1. Разбить Markdown по границам `## ` заголовков
2. Каждый чанк ≤ 512 токенов XLM-RoBERTa (реальный подсчёт)
3. Overlap = 64–100 токенов на границах
4. Сохранять заголовок секции в каждом чанке
5. Для русского текста — реальный токенайзер (не приблизительно)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..config import settings
from ..embedding.tokenizer import tokenizer as xlmr_tokenizer
from ..models import Chunk

logger = logging.getLogger("mcp_knowledge.chunker")

# Разделитель: заголовки уровня 2 (##)
_H2_PATTERN = re.compile(r"^##\s+", re.MULTILINE)


class MarkdownChunker:
    """Разбивает Markdown на чанки по ## заголовкам с токен-лимитом."""

    def __init__(
        self,
        max_tokens: int = settings.CHUNK_MAX_TOKENS,
        overlap_tokens: int = settings.CHUNK_OVERLAP,
    ):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # Минимальный overlap: 64 токена (из плана)
        self._min_overlap = 64

    def chunk(self, knowledge_id: str, content: str,
              section_header: str = "") -> list[Chunk]:
        """Разбить Markdown-контент на чанки.

        Args:
            knowledge_id: ID записи
            content: Markdown-текст (без YAML frontmatter)
            section_header: заголовок родительской секции (для вложенных)

        Returns:
            Список Chunk-объектов
        """
        if not content.strip():
            return []

        sections = self._split_by_h2(content)
        chunks: list[Chunk] = []
        chunk_index = 0

        for sec_title, sec_body in sections:
            # Если секция короткая — один чанк
            token_count = xlmr_tokenizer.count_tokens(sec_body)
            if token_count <= self.max_tokens and sec_body.strip():
                chunks.append(Chunk(
                    chunk_id=f"{knowledge_id}#{chunk_index}",
                    knowledge_id=knowledge_id,
                    content=sec_body,
                    section_header=sec_title or section_header,
                    chunk_index=chunk_index,
                    token_count=token_count,
                ))
                chunk_index += 1
                continue

            # Длинная секция — разбиваем с overlap
            sec_chunks = self._split_long_section(
                knowledge_id=knowledge_id,
                text=sec_body,
                section_header=sec_title or section_header,
                start_index=chunk_index,
            )
            chunks.extend(sec_chunks)
            chunk_index += len(sec_chunks)

        # Если нет чанков (пустой документ) — создаём один пустой
        if not chunks:
            chunks.append(Chunk(
                chunk_id=f"{knowledge_id}#0",
                knowledge_id=knowledge_id,
                content=content[:self.max_tokens * 4],  # грубая оценка ~4 символа/токен
                section_header=section_header,
                chunk_index=0,
                token_count=xlmr_tokenizer.count_tokens(content),
            ))

        logger.debug("chunk: %s → %d чанков", knowledge_id, len(chunks))
        return chunks

    def _split_by_h2(self, content: str) -> list[tuple[str, str]]:
        """Разбить текст по ## заголовкам → список (title, body)."""
        # Находим все позиции ## заголовков
        matches = list(_H2_PATTERN.finditer(content))

        if not matches:
            return [("", content)]

        sections = []
        for i, match in enumerate(matches):
            start = match.end()  # конец "## "
            # Заголовок: от "## " до конца строки
            line_end = content.find("\n", start)
            title = content[start:line_end].strip() if line_end != -1 else content[start:].strip()

            # Тело: от конца строки заголовка до начала следующего ##
            body_start = line_end + 1 if line_end != -1 else len(content)
            if i + 1 < len(matches):
                body_end = matches[i + 1].start()
            else:
                body_end = len(content)

            body = content[body_start:body_end].strip()
            if body:
                sections.append((title, body))

        return sections

    def _split_long_section(
        self,
        knowledge_id: str,
        text: str,
        section_header: str,
        start_index: int,
    ) -> list[Chunk]:
        """Разбить длинную секцию на overlapping чанки."""
        chunks = []
        tokens = xlmr_tokenizer.tokenize(text)
        total_tokens = len(tokens)

        if total_tokens <= self.max_tokens:
            return [Chunk(
                chunk_id=f"{knowledge_id}#{start_index}",
                knowledge_id=knowledge_id,
                content=text,
                section_header=section_header,
                chunk_index=start_index,
                token_count=total_tokens,
            )]

        # Overlap: берём max(overlap_tokens, min_overlap), но не больше max_tokens/2
        effective_overlap = max(self.overlap_tokens, self._min_overlap)
        effective_overlap = min(effective_overlap, self.max_tokens // 2)

        chunk_idx = start_index
        pos = 0
        while pos < total_tokens:
            end = min(pos + self.max_tokens, total_tokens)
            chunk_tokens = tokens[pos:end]
            chunk_text = xlmr_tokenizer.decode(chunk_tokens)

            chunks.append(Chunk(
                chunk_id=f"{knowledge_id}#{chunk_idx}",
                knowledge_id=knowledge_id,
                content=f"## {section_header}\n\n{chunk_text}" if section_header else chunk_text,
                section_header=section_header,
                chunk_index=chunk_idx,
                token_count=len(chunk_tokens),
            ))
            chunk_idx += 1

            # Следующий блок начинается с отступом overlap
            pos = end - effective_overlap
            if pos >= total_tokens:
                break
            # Не даём pos застрять (если overlap >= max_tokens)
            if pos <= 0:
                pos = end

        return chunks
