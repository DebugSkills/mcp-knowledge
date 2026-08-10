"""ContentPreprocessor ABC — базовый контракт для всех content_type-препроцессоров.

Фаза 5 §3.2: абстракция препроцессора.
Каждый новый content_type (book, pdf, docs) — новый класс + 1 строка в registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ImportMeta:
    """Метаданные импорта — унаследованы всеми секциями-детьми."""

    domain: str
    subject: str
    project: str | None = None
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    cross_subjects: list[str] = field(default_factory=list)
    source_path: str | None = None  # путь к бинарному файлу на диске (для PDF и др.)


@dataclass
class ValidationResult:
    """Результат валидации контента препроцессором."""

    valid: bool
    error: str | None = None
    content_size: int = 0
    estimated_sections: int = 0


@dataclass
class Section:
    """Семантическая секция (→ одна knowledge-запись).

    После декомпозиции каждая секция становится отдельной .md записью
    в SSOT с frontmatter из meta + auto-extracted полей.
    """

    title: str
    body: str
    sequence_number: int
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # доп. поля для frontmatter


class ContentPreprocessor(ABC):
    """Базовый контракт для всех content_type-препроцессоров.

    content_type: str — уникальный идентификатор ("book" | "pdf" | "docs" | ...)
    """

    content_type: str

    @abstractmethod
    def validate(self, content: str, metadata: ImportMeta) -> ValidationResult:
        """Проверка пригодности контента (non-empty, размер, кодировка)."""

    @abstractmethod
    async def decompose(self, content: str, metadata: ImportMeta) -> list[Section]:
        """Декомпозиция контента в упорядоченный список семантических секций."""
