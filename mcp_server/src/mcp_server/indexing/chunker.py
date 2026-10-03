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
              section_header: str = "",
              locator_spans: list[dict] | None = None) -> list[Chunk]:
        """Разбить Markdown-контент на чанки.

        Args:
            knowledge_id: ID записи
            content: Markdown-текст (без YAML frontmatter)
            section_header: заголовок родительской секции (для вложенных)
            locator_spans: спаны локаторов СЕКЦИИ (§3.2, Ф2b1) — наследуются
                каждым чанком как есть; маппинг «чанк → локаторы» (спаны,
                пересекающие [char_start, char_end)) — locator.locators_for_chunk.
                None → у чанков поля locator_spans НЕТ (Л1: не фабриковать).

        Returns:
            Список Chunk-объектов
        """
        if not content.strip():
            return []

        sections = self._split_by_h2(content)
        chunks: list[Chunk] = []
        chunk_index = 0

        for sec_title, sec_body, sec_start, sec_end in sections:
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
                    char_start=sec_start,
                    char_end=sec_end,
                    locator_spans=locator_spans,
                ))
                chunk_index += 1
                continue

            # Длинная секция — разбиваем с overlap
            sec_chunks = self._split_long_section(
                knowledge_id=knowledge_id,
                text=sec_body,
                section_header=sec_title or section_header,
                start_index=chunk_index,
                base_offset=sec_start,
                locator_spans=locator_spans,
            )
            chunks.extend(sec_chunks)
            chunk_index += len(sec_chunks)

        # Если нет чанков (пустой документ) — создаём один пустой
        if not chunks:
            empty_content = content[:self.max_tokens * 4]  # грубая оценка ~4 символа/токен
            chunks.append(Chunk(
                chunk_id=f"{knowledge_id}#0",
                knowledge_id=knowledge_id,
                content=empty_content,
                section_header=section_header,
                chunk_index=0,
                token_count=xlmr_tokenizer.count_tokens(content),
                char_start=0,
                char_end=len(empty_content),
            ))

        logger.debug("chunk: %s → %d чанков", knowledge_id, len(chunks))
        return chunks

    def _split_by_h2(self, content: str) -> list[tuple[str, str, int, int]]:
        """Разбить текст по ## заголовкам → список (title, body, start, end).

        Ф2b1 (инвариант C): start/end — границы [start, end) СТРИПНУТОГО тела
        секции в координатах ИСХОДНОГО content. Вычисляются ДО мутации .strip()
        (иначе чанкер терял бы привязку к locator_spans секции, чьи offsets
        определены относительно сериализованного тела).
        """
        # Находим все позиции ## заголовков
        matches = list(_H2_PATTERN.finditer(content))

        if not matches:
            return [("", content, 0, len(content))]

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

            raw_body = content[body_start:body_end]
            body = raw_body.strip()
            if body:
                # Ф2b1 (C): offsets ДО мутаций — границы stripped-тела
                # в координатах исходного content.
                lead = len(raw_body) - len(raw_body.lstrip())
                sec_start = body_start + lead
                sections.append((title, body, sec_start, sec_start + len(body)))

        return sections

    def _split_long_section(
        self,
        knowledge_id: str,
        text: str,
        section_header: str,
        start_index: int,
        base_offset: int = 0,
        locator_spans: list[dict] | None = None,
    ) -> list[Chunk]:
        """Разбить длинную секцию на overlapping чанки."""
        chunks = []

        # Overlap: берём max(overlap_tokens, min_overlap), но не больше max_tokens/2
        effective_overlap = max(self.overlap_tokens, self._min_overlap)
        effective_overlap = min(effective_overlap, self.max_tokens // 2)

        # Fallback-токенизатор не умеет decode → нарезаем по символам напрямую,
        # БЕЗ вызова tokenize() (избегаем материализации range/list на 100K+ элементах).
        if xlmr_tokenizer.is_fallback:
            return self._split_long_section_by_chars(
                knowledge_id, text, section_header, start_index,
                effective_overlap,
                base_offset=base_offset,
                locator_spans=locator_spans,
            )

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
                char_start=base_offset,
                char_end=base_offset + len(text),
                locator_spans=locator_spans,
            )]

        chunk_idx = start_index
        pos = 0
        char_cursor = 0
        while pos < total_tokens:
            end = min(pos + self.max_tokens, total_tokens)
            chunk_tokens = tokens[pos:end]
            chunk_text = xlmr_tokenizer.decode(chunk_tokens)

            # Ф2b1 (C): char-границы чанка ДО мутации — вставка "## header"
            # ниже НЕ сдвигает offsets (они указывают на chunk_text-часть
            # тела, без префикса). pos монотонно растёт → поиск с курсором
            # от начала предыдущего чанка. decode() может нормализовать
            # пробелы: подстрока не найдена ⇒ границы неизвестны точно →
            # НЕ фабрикуем (Л1), char_start/char_end остаются None.
            found_at = text.find(chunk_text, char_cursor)
            if found_at != -1:
                char_start = base_offset + found_at
                char_end = char_start + len(chunk_text)
                char_cursor = found_at
            else:
                char_start = None
                char_end = None

            chunks.append(Chunk(
                chunk_id=f"{knowledge_id}#{chunk_idx}",
                knowledge_id=knowledge_id,
                content=f"## {section_header}\n\n{chunk_text}" if section_header else chunk_text,
                section_header=section_header,
                chunk_index=chunk_idx,
                token_count=len(chunk_tokens),
                char_start=char_start,
                char_end=char_end,
                locator_spans=locator_spans,
            ))
            chunk_idx += 1

            # БЕЗОПАСНЫЙ ВЫХОД: end == total_tokens → НЕ сдвигать pos назад,
            # иначе (pos = end - overlap < total) последний чанк дублируется
            # бесконечно → OOM (инцидент 2026-08-06, фикс синхронизирован
            # с _split_long_section_by_chars).
            if end == total_tokens:
                break
            # Следующий блок начинается с отступом overlap
            pos = end - effective_overlap
            # Не даём pos застрять (если overlap >= max_tokens)
            if pos <= 0:
                pos = end

        return chunks

    def _split_long_section_by_chars(
        self,
        knowledge_id: str,
        text: str,
        section_header: str,
        start_index: int,
        effective_overlap: int,
        base_offset: int = 0,
        locator_spans: list[dict] | None = None,
    ) -> list[Chunk]:
        """Разбить длинную секцию по символам (fallback-режим, без токенизации).

        Использует _FALLBACK_CHARS_PER_TOKEN для пересчёта токен-лимита
        в символьный. Не аллоцирует список токенов — только нарезка строк.
        """
        from ..embedding.tokenizer import _FALLBACK_CHARS_PER_TOKEN

        chars_per_token = _FALLBACK_CHARS_PER_TOKEN
        max_chars = self.max_tokens * chars_per_token
        overlap_chars = effective_overlap * chars_per_token
        total_chars = len(text)

        chunks = []
        chunk_idx = start_index
        pos = 0
        while pos < total_chars:
            end = min(pos + max_chars, total_chars)
            chunk_text = text[pos:end]

            # Ф2b1 (C): fallback режет по символам БЕЗ мутаций текста —
            # границы точные; вставка "## header" в content не сдвигает их.
            chunks.append(Chunk(
                chunk_id=f"{knowledge_id}#{chunk_idx}",
                knowledge_id=knowledge_id,
                content=f"## {section_header}\n\n{chunk_text}" if section_header else chunk_text,
                section_header=section_header,
                chunk_index=chunk_idx,
                token_count=max(1, len(chunk_text) // chars_per_token),
                char_start=base_offset + pos,
                char_end=base_offset + end,
                locator_spans=locator_spans,
            ))
            chunk_idx += 1

            # БЕЗОПАСНЫЙ ВЫХОД: если дошли до конца текста (end == total),
            # НЕ сдвигать pos назад — иначе (end - overlap < total) цикл
            # вечно дублирует последний чанк → OOM (инцидент 2026-08-06).
            if end == total_chars:
                break
            pos = end - overlap_chars
            if pos <= 0:
                pos = end

        return chunks
