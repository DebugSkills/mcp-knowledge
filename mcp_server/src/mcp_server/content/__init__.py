"""Фаза 5: Content Preprocessor Pipeline — модульный импорт крупных текстов.

MCP Tool import_content + модульные препроцессоры (content_type → preprocessor) +
гибридное разбиение на семантические секции + parent-child связывание +
best-effort batch с orphan cleanup.
"""

from .preprocessor import ContentPreprocessor, Section, ImportMeta, ValidationResult
from . import registry
from .book_preprocessor import BookPreprocessor

# ── Регистрация препроцессоров ────────────────────────────
# BookPreprocessor: content_type="book" — структурные книги/документы
# embedder/token_counter = None — clustering/recursive split gracefully fallback
registry.register(BookPreprocessor(embedder=None, token_counter=None))

__all__ = [
    "ContentPreprocessor",
    "Section",
    "ImportMeta",
    "ValidationResult",
    "registry",
    "BookPreprocessor",
]
