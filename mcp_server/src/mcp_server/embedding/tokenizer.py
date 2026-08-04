"""XLM-RoBERTa токенайзер для BGE-M3 chunking (#20).

BGE-M3 использует XLM-RoBERTa токенизатор. Для русского текста
коэффициент токен/слово выше, чем для английского — нельзя
приблизительно считать токены, нужно использовать реальный токенайзер.

Задача 1.4 / 1.10 плана Фазы 1.
"""

from __future__ import annotations

import logging
import os

from ..config import settings

logger = logging.getLogger("mcp_knowledge.tokenizer")

# Ленивая загрузка transformers (P1-2: экономия памяти при старте)
_tokenizer_instance = None


def _load_tokenizer():
    """Ленивая загрузка XLM-RoBERTa токенайзера."""
    global _tokenizer_instance
    if _tokenizer_instance is None:
        from transformers import AutoTokenizer
        logger.info("Загрузка токенайзера XLM-RoBERTa для %s...", settings.EMBEDDING_MODEL)
        cache_dir = settings.MODELS_CACHE_DIR
        try:
            # Создать кэш-директорию, если не существует (idempotent; в Docker
            # volume уже смонтирован, локально — создаём/используем fallback).
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            # Кэш-путь недоступен (напр. локально нет /app) → transformers
            # использует стандартный HF-кэш (~/.cache/huggingface).
            logger.warning("Кэш-директория недоступна (%s); fallback на HF-кэш: %s", exc, cache_dir)
            cache_dir = None
        _tokenizer_instance = AutoTokenizer.from_pretrained(
            settings.EMBEDDING_MODEL,
            cache_dir=cache_dir,
        )
        logger.info("Токенайзер загружен (vocab_size=%d)", _tokenizer_instance.vocab_size)
    return _tokenizer_instance


class XlmRobertaTokenizer:
    """Обёртка над XLM-RoBERTa токенайзером BGE-M3."""

    def __init__(self):
        self._tok = None  # ленивая инициализация

    @property
    def tokenizer(self):
        if self._tok is None:
            self._tok = _load_tokenizer()
        return self._tok

    def count_tokens(self, text: str) -> int:
        """Подсчитать реальное количество токенов XLM-RoBERTa."""
        if not text:
            return 0
        encoded = self.tokenizer.encode(text, add_special_tokens=False)
        return len(encoded)

    def tokenize(self, text: str) -> list[int]:
        """Токенизировать текст в token IDs."""
        if not text:
            return []
        return self.tokenizer.encode(text, add_special_tokens=False)

    def decode(self, token_ids: list[int]) -> str:
        """Декодировать token IDs обратно в текст."""
        return self.tokenizer.decode(token_ids, skip_special_tokens=True)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Обрезать текст до max_tokens токенов."""
        if not text:
            return text
        token_ids = self.tokenize(text)
        if len(token_ids) <= max_tokens:
            return text
        truncated_ids = token_ids[:max_tokens]
        return self.decode(truncated_ids)


# Глобальный синглтон
tokenizer = XlmRobertaTokenizer()
