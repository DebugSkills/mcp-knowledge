"""Фаза 5: Content Preprocessor Pipeline — модульный импорт крупных текстов.

MCP Tool import_content + модульные препроцессоры (content_type → preprocessor) +
гибридное разбиение на семантические секции + parent-child связывание +
best-effort batch с orphan cleanup.
"""

from . import registry
from .book_preprocessor import BookPreprocessor
from .locator import PDFLocatorExtractor, register_locator_extractor
from .pdf_preprocessor import PDFPreprocessor
from .preprocessor import ContentPreprocessor, ImportMeta, Section, ValidationResult

# ── Регистрация препроцессоров ────────────────────────────
# BookPreprocessor: content_type="book" — структурные книги/документы
# embedder/token_counter = None — clustering/recursive split gracefully fallback
registry.register(BookPreprocessor(embedder=None, token_counter=None))

# PDFPreprocessor: content_type="pdf" — PDF через pdfplumber + Tesseract OCR (13.21)
registry.register(PDFPreprocessor())

# ── Ф2b3 (bibliography): авторегистрация PDF-экстрактора локаторов ──
# Тот же паттерн, что у препроцессоров выше — 1 строка на старте пакета:
# PDFPreprocessor.decompose разрешает экстрактор через реестр (сегментный
# режим — единственный источник разбиения PDF по страницам, план §3.2:145).
register_locator_extractor(PDFLocatorExtractor())

__all__ = [
    "BookPreprocessor",
    "ContentPreprocessor",
    "ImportMeta",
    "PDFLocatorExtractor",
    "PDFPreprocessor",
    "Section",
    "ValidationResult",
    "register_locator_extractor",
    "registry",
]
