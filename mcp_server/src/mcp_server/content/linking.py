"""Parent-Child Collection Linking (#35) — root TOC + children.

Фаза 5 §6.1-6.2:
- Root-запись: content_type="collection", children[] = TOC
- Дети: parent_knowledge_id=<root_id>, sequence_number=N
- Навигация: get_entry(root_id) → TOC
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import KnowledgeFrontmatter


@dataclass
class CollectionRoot:
    """Root-запись коллекции — «оглавление»."""

    knowledge_id: str
    title: str
    domain: str
    subject: str
    project: str | None = None
    children: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cross_subjects: list[str] = field(default_factory=list)
    content_type: str = "collection"
    zone: str = "private"  # W1: зона доступа книги (public | private)

    def to_frontmatter(self) -> KnowledgeFrontmatter:
        """Создать KnowledgeFrontmatter для root-записи."""
        now = datetime.now(timezone.utc)
        return KnowledgeFrontmatter(
            knowledge_id=self.knowledge_id,
            domain=self.domain,
            subject=self.subject,
            project=self.project,
            content_type=self.content_type,
            tags=self.tags,
            cross_subjects=self.cross_subjects,
            children=self.children,
            parent_knowledge_id=None,
            sequence_number=None,
            zone=self.zone,
            created_at=now,
            updated_at=now,
        )


@dataclass
class ChildEntry:
    """Запись-ребёнок коллекции."""

    knowledge_id: str
    title: str
    body: str
    sequence_number: int
    parent_knowledge_id: str
    domain: str
    subject: str
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    cross_subjects: list[str] = field(default_factory=list)
    content_type: str = "book"
    zone: str = "private"  # W1: зона доступа секции (наследуется от книги)

    def to_frontmatter(self) -> KnowledgeFrontmatter:
        """Создать KnowledgeFrontmatter для child-записи."""
        now = datetime.now(timezone.utc)
        return KnowledgeFrontmatter(
            knowledge_id=self.knowledge_id,
            domain=self.domain,
            subject=self.subject,
            project=self.project,
            content_type=self.content_type,
            parent_knowledge_id=self.parent_knowledge_id,
            sequence_number=self.sequence_number,
            tags=self.tags,
            cross_subjects=self.cross_subjects,
            zone=self.zone,
            created_at=now,
            updated_at=now,
        )


def _transliterate_cyrillic(text: str) -> str:
    """Транслит кириллицы в латиницу для slug."""
    mapping = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
        "А": "a", "Б": "b", "В": "v", "Г": "g", "Д": "d", "Е": "e",
        "Ё": "yo", "Ж": "zh", "З": "z", "И": "i", "Й": "y", "К": "k",
        "Л": "l", "М": "m", "Н": "n", "О": "o", "П": "p", "Р": "r",
        "С": "s", "Т": "t", "У": "u", "Ф": "f", "Х": "kh", "Ц": "ts",
        "Ч": "ch", "Ш": "sh", "Щ": "shch", "Ъ": "", "Ы": "y", "Ь": "",
        "Э": "e", "Ю": "yu", "Я": "ya",
    }
    result = ""
    for ch in text:
        result += mapping.get(ch, ch)
    return result


def slugify(text: str, max_len: int = 60) -> str:
    """Преобразовать заголовок в kebab-case slug (с транслитом кириллицы)."""
    text = _transliterate_cyrillic(text)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = slug.strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def make_knowledge_id(
    domain: str,
    subject: str,
    title: str,
    sequence_number: int,
    content_hash: str | None = None,
) -> str:
    """Сгенерировать knowledge_id для секции: domain-subject-slug-seq.

    Args:
        domain: домен (напр. "engineering")
        subject: предмет (напр. "python")
        title: заголовок секции
        sequence_number: порядковый номер в коллекции
        content_hash: опциональный хеш для уникальности (8 hex chars)
    """
    title_slug = slugify(title if title else "section", 40)
    # domain/subject тоже слагфицируются: subject может прийти с пробелом/кириллицей
    # от авто-классификации (напр. "physical training" → "physical-training"),
    # иначе knowledge_id не пройдёт паттерн '^[a-z0-9][a-z0-9_-]{2,127}$'.
    d = slugify(domain, 40) or "domain"
    s = slugify(subject, 40) or "subject"
    suffix = content_hash[:8] if content_hash else f"{sequence_number:03d}"
    result = f"{d}-{s}-{title_slug}-{suffix}"
    if len(result) > 127:
        # Паттерн допускает ≤127 символов: урезаем title_slug, сохраняя суффикс (уникальность)
        budget = 127 - len(d) - len(s) - len(suffix) - 3  # 3 разделителя '-'
        title_slug = title_slug[: max(budget, 0)]
        result = f"{d}-{s}-{title_slug}-{suffix}"
    return result


def make_collection_id(
    domain: str,
    subject: str,
    title: str,
) -> str:
    """Сгенерировать knowledge_id для root-коллекции."""
    slug = slugify(title if title else "collection", 40)
    d = slugify(domain, 40) or "domain"
    s = slugify(subject, 40) or "subject"
    result = f"{d}-{s}-{slug}-collection"
    if len(result) > 127:
        budget = 127 - len(d) - len(s) - len("-collection") - 2  # 2 разделителя '-'
        slug = slug[: max(budget, 0)]
        result = f"{d}-{s}-{slug}-collection"
    return result


def build_collection(
    domain: str,
    subject: str,
    project: str | None,
    title: str,
    section_titles: list[str],
    section_ids: list[str],
    tags: list[str],
    cross_subjects: list[str],
    zone: str = "private",
) -> CollectionRoot:
    """Создать root-коллекцию с TOC.

    Args:
        domain, subject, project: классификация
        title: заголовок коллекции (из import params или авто)
        section_titles: заголовки секций-детей
        section_ids: knowledge_id всех секций-детей (в порядке sequence)
        tags: унаследованные теги коллекции
        cross_subjects: кросс-теги
        zone: зона доступа книги (W1): public | private
    """
    collection_id = make_collection_id(domain, subject, title)

    children = []
    for i, (child_title, child_id) in enumerate(
        zip(section_titles, section_ids), 1
    ):
        children.append({
            "knowledge_id": child_id,
            "title": child_title,
            "sequence_number": i,
        })

    return CollectionRoot(
        knowledge_id=collection_id,
        title=title,
        domain=domain,
        subject=subject,
        project=project,
        children=children,
        tags=tags,
        cross_subjects=cross_subjects,
        zone=zone,
    )
