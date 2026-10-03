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
from typing import Any, Literal

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
    project: str | None = Field(None, description="Опциональный проект")
    cross_subjects: list[str] = Field(default_factory=list, description="Кросс-теги (#4)")
    tags: list[str] = Field(default_factory=list, description="Свободные теги")
    version: int = Field(default=1, ge=1, description="Optimistic locking (P2)")
    status: str = Field(default="published", description="Lifecycle: published | deprecated (4.7)")
    zone: Literal["public", "private"] = Field(
        default="private",
        description="Зона доступа (Фаза W1, двухконтурная модель): public | private",
    )
    evergreen: bool = Field(default=False, description="Фундаментальное знание — медленное старение (4.5 R1)")
    source: str | None = Field(None, description="URL источника (link_health 4.5)")
    # ── Фаза 5: parent-child collection fields ──────────────
    parent_knowledge_id: str | None = Field(None, description="ID родительской коллекции (null для root)")
    sequence_number: int | None = Field(None, ge=1, description="Порядковый номер в коллекции (1..N)")
    content_type: str | None = Field(None, description="Тип контента: book | pdf | collection | source | ...")
    children: list[dict] | None = Field(None, description="Список children для collection-root (TOC)")
    # ── Source SSOT (code-2026-10-02-bibliography, Фаза 1, план §3.1) ──
    # Источник документа: format/locator_kind/blobs/bibliography/license.
    # Запись content_type="source" НЕ индексируется (INDEX_EXCLUDED_CONTENT_TYPES).
    format: str | None = Field(None, description="Формат источника: pdf|docx|md|epub|html|txt|audio|video|image|sheet|url")
    locator_kind: str | None = Field(None, description="Ось адресации цитат (page|timestamp|image|sheet_row|...)")
    ingest_policy_applied: str | None = Field(None, description="Журнал политики ingest (normalize|pdf_only)")
    bibliography: dict | None = Field(None, description="CSL-библиография (type/author/title/issued/publisher/...)")
    license: str | None = Field(None, description="Лицензия: own|cc-*|licensed|restricted|unknown")
    public_allowed: bool | None = Field(None, description="Машиночитаемая политика публикации (из license)")
    blobs: dict | None = Field(None, description="blobs {original, canonical, derived[]} с sha256/provenance")
    source_refs: list[dict] | None = Field(None, description="Ссылки Knowledge-записи на Source (source_id + locator)")
    # ── Locator-aware pipeline (bibliography Ф2b1, план §3.2:127-131) ──
    # Спаны локаторов СЕКЦИИ: [{locator: {kind, start, end, display},
    # offset_start, offset_end}]. Offsets — полуоткрытый интервал [start, end)
    # относительно тела секции КАК ОНО СЕРИАЛИЗУЕТСЯ В .md (entry.content
    # после `---`). Round-trip инвариант (§3.2:130): write→read возвращает
    # те же offsets. Л1 provenance: спанов нет → поля нет (None;
    # exclude_none=True в _write_file не пишет ключ в YAML).
    locator_spans: list[dict] | None = Field(
        None,
        description="Спаны локаторов секции: [{locator{kind,start,end,display}, offset_start, offset_end}]",
    )
    # Ф2b2 (bibliography, план §3.1:120): «секции — source_id + locator_spans».
    # ID Source-записи (src-<sha256_16>); продюсер Ф2b3 заполняет из Section.meta.
    # None → ключ не пишется в YAML (exclude_none) и не попадает в payload (Л1).
    source_id: str | None = Field(
        None,
        description="ID Source-записи для секции/записи с локаторами (src-<sha256_16>)",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Дата создания",
    )
    # 025-D: derived-маркер явности created_at (та же ловушка default_factory:
    # без него fieldless-запись показывает время парса как «дату создания»).
    created_at_explicit: bool = Field(
        default=True,
        exclude=True,
        description="Было ли created_at явно задано в исходном frontmatter (derived)",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Дата обновления (← ключ reconciliation #19)",
    )
    # 024: derived-маркер «поле updated_at было ЯВНО задано в frontmatter».
    # Отсутствие поля парсер заполняет default_factory=now — это НЕ признак
    # свежести документа; reconcile._is_drifted по маркеру не даёт ложный дрейф.
    # exclude=True: не персистится в YAML/JSON (проверено Critic 024, pydantic v2).
    updated_at_explicit: bool = Field(
        default=True,
        exclude=True,
        description="Было ли updated_at явно задано в исходном frontmatter (derived)",
    )

    @field_validator("knowledge_id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_-]{2,127}$", v):
            raise ValueError(
                f"knowledge_id '{v}' must match [a-z0-9][a-z0-9_-]{{2,127}}"
            )
        return v

    @field_validator("zone", mode="before")
    @classmethod
    def validate_zone(cls, v: Any) -> str:
        """W1.1: zone ∈ {public, private}; неизвестное значение → ошибка."""
        if v not in ("public", "private"):
            raise ValueError(f"zone '{v}' must be 'public' or 'private'")
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

    @property
    def zone(self) -> str:
        """Зона доступа записи (W1.3)."""
        return self.frontmatter.zone


# ── Indexability (bibliography Фаза 1, план §3.5) ──────────

# Единый предикат «Source НЕ индексируется»: типы контента, исключённые
# из векторизации (индексация/реконсиляция/выдача — три контура, N1 choke point).
INDEX_EXCLUDED_CONTENT_TYPES = frozenset({"source"})


def is_indexable(entry_or_frontmatter) -> bool:
    """Предикат индексации: Source-записи (content_type="source") исключены.

    Принимает KnowledgeEntry или KnowledgeFrontmatter (duck-typing).
    """
    fm = getattr(entry_or_frontmatter, "frontmatter", entry_or_frontmatter)
    content_type = getattr(fm, "content_type", None)
    return content_type not in INDEX_EXCLUDED_CONTENT_TYPES


# ── Chunk ──────────────────────────────────────────────────

class Chunk(BaseModel):
    """Чанк Markdown-документа для векторизации."""

    chunk_id: str = Field(..., description="Уникальный ID чанка (knowledge_id#N)")
    knowledge_id: str
    content: str = Field(..., description="Текст чанка (≤ 512 токенов XLM-R)")
    section_header: str = Field("", description="Заголовок ## секции-родителя")
    chunk_index: int = Field(..., ge=0, description="Индекс чанка внутри документа")
    token_count: int = Field(..., description="Фактическое количество токенов XLM-R")
    # ── Locator-aware pipeline (bibliography Ф2b1, план §3.2:147) ──
    # char_start/char_end — [start, end) в координатах тела секции КАК ОНО
    # СЕРИАЛИЗУЕТСЯ В .md; вычисляются ДО мутаций чанкера (.strip() тела и
    # вставка "## header" их не сдвигают: offsets относятся к исходному телу,
    # а не к мутированному чанк-контенту). locator_spans — спаны СЕКЦИИ,
    # унаследованные чанком как есть; выборка по пересечению —
    # content.locator.locators_for_chunk. Л1: спанов нет → поля НЕТ (None).
    char_start: int | None = Field(None, ge=0, description="Начало чанка [char_start, char_end), координаты сериализованного тела секции")
    char_end: int | None = Field(None, ge=0, description="Конец чанка [char_start, char_end), координаты сериализованного тела секции")
    locator_spans: list[dict] | None = Field(None, description="Спаны локаторов секции (унаследованы; маппинг — locators_for_chunk)")


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
    project: str | None = None
    cross_subjects: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    knowledge_id: str | None = Field(
        None, description="ID (если None → авто-генерация из domain/subject/title)"
    )
    wait_for_index: bool = Field(
        default=False, description="Ждать завершения индексации (GPU ≤5s)"
    )
    zone: str = Field(
        default="private", description="Зона доступа (W1.2): public | private"
    )
    source_refs: list[dict] | None = Field(
        None,
        description="Ссылки на Source-записи (source_id + locator) — bibliography Ф3c2",
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
