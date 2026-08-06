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

# Приближённое число символов на токен XLM-R для кириллицы
# (точный токенайзер — в полном окружении; fallback — только для лёгкого образа)
_FALLBACK_CHARS_PER_TOKEN = 4


class _FallbackTokenizer:
    """Лёгкий токенайзер БЕЗ transformers (лёгкий Docker-образ без torch).

    Используется, когда transformers недоступен: приближённый подсчёт токенов
    для chunker (len(text)//4 ≈ XLM-R для кириллицы). Точность ниже, но
    нарезка стабильна и не требует скачивания модели (~1.1 ГБ).
    """

    vocab_size = 250_000

    def encode(self, text: str, add_special_tokens: bool = False) -> range:
        if not text:
            return range(0)
        n = max(1, (len(text) + _FALLBACK_CHARS_PER_TOKEN - 1) // _FALLBACK_CHARS_PER_TOKEN)
        return range(n)

    def count_tokens(self, text: str) -> int:
        """Быстрый подсчёт псевдо-токенов без аллокаций (O(1))."""
        if not text:
            return 0
        return max(1, (len(text) + _FALLBACK_CHARS_PER_TOKEN - 1) // _FALLBACK_CHARS_PER_TOKEN)

    def decode(self, token_ids: list[int], skip_special_tokens: bool = True) -> str:
        # Текст из псевдо-токенов не восстановить — chunker использует decode
        # только для truncate_to_tokens, который для fallback обрабатывается
        # отдельно (см. XlmRobertaTokenizer.truncate_to_tokens).
        return ""


def _load_tokenizer():
    """Ленивая загрузка XLM-RoBERTa токенайзера (с fallback без transformers)."""
    global _tokenizer_instance
    if _tokenizer_instance is None:
        try:
            from transformers import AutoTokenizer
        except ImportError:
            logger.warning(
                "transformers недоступен (лёгкий образ) — fallback на "
                "приближённый токенизатор (~%d симв/токен). Точная нарезка "
                "XLM-R для chunking отключена.",
                _FALLBACK_CHARS_PER_TOKEN,
            )
            _tokenizer_instance = _FallbackTokenizer()
            return _tokenizer_instance
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
        """Подсчитать реальное количество токенов XLM-RoBERTa.

        Для fallback-токенизатора использует быстрый count_tokens (O(1), без аллокаций).
        """
        if not text:
            return 0
        tok = self.tokenizer
        if isinstance(tok, _FallbackTokenizer):
            return tok.count_tokens(text)
        encoded = tok.encode(text, add_special_tokens=False)
        return len(encoded)

    def tokenize(self, text: str) -> list[int]:
        """Токенизировать текст в token IDs."""
        if not text:
            return []
        return self.tokenizer.encode(text, add_special_tokens=False)

    def decode(self, token_ids: list[int]) -> str:
        """Декодировать token IDs обратно в текст."""
        return self.tokenizer.decode(token_ids, skip_special_tokens=True)

    @property
    def is_fallback(self) -> bool:
        """True если активен лёгкий псевдо-токенизатор (без transformers).

        Fallback не умеет decode → chunker нарезает текст по символам.
        """
        return isinstance(self._tok, _FallbackTokenizer)

    @property
    def fallback_chars_per_token(self) -> int:
        """Символов на 1 псевдо-токен fallback-токенизатора."""
        return _FALLBACK_CHARS_PER_TOKEN

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Обрезать текст до max_tokens токенов."""
        if not text:
            return text
        if isinstance(self._tok, _FallbackTokenizer):
            # Псевдо-токены не декодируются — обрезаем по символам.
            return text[: max_tokens * _FALLBACK_CHARS_PER_TOKEN]
        token_ids = self.tokenize(text)
        if len(token_ids) <= max_tokens:
            return text
        truncated_ids = token_ids[:max_tokens]
        return self.decode(truncated_ids)


# Глобальный синглтон
tokenizer = XlmRobertaTokenizer()
