"""Фаза 5: Content Preprocessor Pipeline — модульный импорт крупных текстов.

MCP Tool import_content + модульные препроцессоры (content_type → preprocessor) +
гибридное разбиение на семантические секции + parent-child связывание +
best-effort batch с orphan cleanup.
"""

from . import registry
from .book_preprocessor import BookPreprocessor
from .pdf_preprocessor import PDFPreprocessor
from .preprocessor import ContentPreprocessor, ImportMeta, Section, ValidationResult

# ── Регистрация препроцессоров ────────────────────────────
# BookPreprocessor: content_type="book" — структурные книги/документы
# embedder/token_counter = None — clustering/recursive split gracefully fallback
registry.register(BookPreprocessor(embedder=None, token_counter=None))

# PDFPreprocessor: content_type="pdf" — PDF через pdfplumber + Tesseract OCR (13.21)
registry.register(PDFPreprocessor())

__all__ = [
    "BookPreprocessor",
    "ContentPreprocessor",
    "ImportMeta",
    "PDFPreprocessor",
    "Section",
    "ValidationResult",
    "registry",
]
