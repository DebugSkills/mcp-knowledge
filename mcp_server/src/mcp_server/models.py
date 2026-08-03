"""Pydantic-модели MCP Knowledge Server.

Схема YAML frontmatter (см. план §3.1):
---
knowledge_id: "ru-python-async-patterns"
domain: "engineering"
subject: "python"
project: "backend"
cross_subjects: ["devops", "architecture"]
tags: ["asyncio", "best-practice"]
version: 1
created_at: "2026-07-21T10:00:00+03:00"
updated_at: "2026-07-21T10:00:00+03:00"
---
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Frontmatter ────────────────────────────────────────────

class KnowledgeFrontmatter(BaseModel):
    """YAML frontmatter записи знаний (SSOT)."""

    knowledge_id: str = Field(
        ...,
        description="Уникальный ID записи (→ имя файла без .md)",
        pattern=r"^[a-z0-9][a-z0-9_-]{2,127}$",
    )
    domain: str = Field(..., description="Первичная классификация")
    subject: str = Field(..., description="Вторичная классификация")
    project: Optional[str] = Field(None, description="Опциональный проект")
    cross_subjects: list[str] = Field(default_factory=list, description="Кросс-теги (#4)")
    tags: list[str] = Field(default_factory=list, description="Свободные теги")
    version: int = Field(default=1, ge=1, description="Optimistic locking (P2)")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Дата создания",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Дата обновления (← ключ reconciliation #19)",
    )

    @field_validator("knowledge_id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_-]{2,127}$", v):
            raise ValueError(
                f"knowledge_id '{v}' must match [a-z0-9][a-z0-9_-]{{2,127}}"
            )
        return v


# ── Knowledge Entry ────────────────────────────────────────

class KnowledgeEntry(BaseModel):
    """Полная запись: frontmatter + Markdown-контент."""

    frontmatter: KnowledgeFrontmatter
    content: str = Field(..., description="Markdown-контент после YAML frontmatter")

    @property
    def file_path(self) -> str:
        """Относительный путь к .md файлу в knowledge/."""
        parts = [self.frontmatter.domain, self.frontmatter.subject]
        if self.frontmatter.project:
            parts.append(self.frontmatter.project)
        parts.append(f"{self.frontmatter.knowledge_id}.md")
        return "/".join(parts)


# ── Chunk ──────────────────────────────────────────────────

class Chunk(BaseModel):
    """Чанк Markdown-документа для векторизации."""

    chunk_id: str = Field(..., description="Уникальный ID чанка (knowledge_id#N)")
    knowledge_id: str
    content: str = Field(..., description="Текст чанка (≤ 512 токенов XLM-R)")
    section_header: str = Field("", description="Заголовок ## секции-родителя")
    chunk_index: int = Field(..., ge=0, description="Индекс чанка внутри документа")
    token_count: int = Field(..., description="Фактическое количество токенов XLM-R")


# ── Qdrant Point ──────────────────────────────────────────

class QdrantPoint(BaseModel):
    """Точка в Qdrant — чанк + вектор + payload."""

    id: str  # UUID v4
    vector: list[float]  # 1024d BGE-M3
    payload: dict[str, Any]


# ── Search ─────────────────────────────────────────────────

class SearchResult(BaseModel):
    """Результат поиска."""

    knowledge_id: str
    chunk_id: str
    content: str
    score: float
    frontmatter: KnowledgeFrontmatter
    section_header: str = ""


# ── Write/Update request ───────────────────────────────────

class WriteRequest(BaseModel):
    """Запрос на запись нового знания."""

    content: str = Field(..., min_length=1, description="Markdown-контент")
    domain: str = Field(..., min_length=1)
    subject: str = Field(..., min_length=1)
    project: Optional[str] = None
    cross_subjects: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    knowledge_id: Optional[str] = Field(
        None, description="ID (если None → авто-генерация из domain/subject/title)"
    )
    wait_for_index: bool = Field(
        default=False, description="Ждать завершения индексации (GPU ≤5s)"
    )


class WriteResult(BaseModel):
    """Результат write_knowledge."""

    knowledge_id: str
    indexed: bool = False
    pending: bool = True  # True если indexing ещё в очереди


# ── F2: Optimistic locking exception ──────────────────────

class VersionConflictError(Exception):
    """Conflict: клиент передал expected_version, не совпадающий с актуальным.

    Возникает при update_entry(expected_version=N) когда текущая версия ≠ N.
    Атрибуты: knowledge_id, expected, actual.
    """
    def __init__(self, knowledge_id: str, expected: int, actual: int):
        self.knowledge_id = knowledge_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict for '{knowledge_id}': "
            f"expected v{expected}, actual v{actual}"
        )
