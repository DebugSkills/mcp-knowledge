"""MCP Tools registry — 17 tools с JSON Schema (Блок A).

Реальные реализации в модулях: search.py, read.py, crud.py, browse.py, admin.py, content.py.

G1-fix: list_subjects + list_projects зарегистрированы (ранее были импортированы, но не добавлены в TOOLS/TOOL_HANDLERS).
"""

from __future__ import annotations

from typing import Any

from ..content.analyzer import analyze_content
from .admin import reindex
from .browse import list_domains, list_projects, list_subjects
from .content import import_content
from .crud import delete_entry, update_entry, write_knowledge
from .quality import (
    list_quality_issues,
    resolve_quality_issue,
    review_queue,
    run_quality_scan,
)
from .read import get_entry, get_knowledge_map
from .search import search_by_tags, search_knowledge

# ── JSON Schema fragments ──────────────────────────────────

_SEARCH_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Поисковый запрос (естественный язык)"},
        "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
        "domain": {"type": "string", "description": "Фильтр по домену"},
        "subject": {"type": "string", "description": "Фильтр по предмету"},
        "project": {"type": "string", "description": "Фильтр по проекту"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Фильтр по тегам"},
        "score_threshold": {"type": "number", "default": 0.0, "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["query"],
}

_SEARCH_TAGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Теги для поиска"},
        "match_all": {"type": "boolean", "default": True, "description": "AND (true) или OR (false)"},
        "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 1000},
    },
    "required": ["tags"],
}

_GET_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "knowledge_id": {"type": "string", "description": "ID записи"},
    },
    "required": ["knowledge_id"],
}

_GET_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Опциональный фильтр по домену"},
    },
}

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Markdown-контент"},
        "domain": {"type": "string"},
        "subject": {"type": "string"},
        "project": {"type": "string"},
        "cross_subjects": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "knowledge_id": {"type": "string", "description": "Опциональный ID (авто-генерация если не указан)"},
        "wait_for_index": {"type": "boolean", "default": False},
    },
    "required": ["content", "domain", "subject"],
}

_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "knowledge_id": {"type": "string"},
        "content": {"type": "string"},
        "version": {"type": "integer", "description": "Optimistic locking: ожидаемая версия"},
    },
    "required": ["knowledge_id", "content"],
}

_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "knowledge_id": {"type": "string"},
    },
    "required": ["knowledge_id"],
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cursor": {"type": "string", "description": "Курсор пагинации"},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
        "domain": {"type": "string", "description": "Фильтр по домену (опционально)"},
        "subject": {"type": "string", "description": "Фильтр по subject (опционально)"},
    },
}

_REINDEX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Опционально: переиндексировать только один домен"},
    },
}

# ── Quality Tool schemas (Фаза 4) ────────────────────────────

_REVIEW_QUEUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Фильтр по домену"},
        "subject": {"type": "string", "description": "Фильтр по предмету"},
        "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
    },
}

_LIST_QUALITY_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "types": {
            "type": "array",
            "items": {"type": "string", "enum": ["duplicate", "missing_field", "edit_war", "broken_link", "conflicting", "orphaned"]},
            "description": "Фильтр по типам issues",
        },
        "status": {"type": "string", "default": "open", "enum": ["open", "resolved", "ignored"]},
        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
    },
}

_RESOLVE_QUALITY_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue_id": {"type": "string", "description": "ID issue для разрешения"},
        "action": {
            "type": "string",
            "enum": ["merge", "deprecate", "restore", "resolve", "ignore"],
            "description": "Действие: merge (слить), deprecate (скрыть), restore (вернуть), resolve (исправлено), ignore (пропустить)",
        },
        "target_id": {"type": "string", "description": "target knowledge_id (для merge)"},
        "reason": {"type": "string", "description": "Причина решения"},
    },
    "required": ["issue_id", "action"],
}

_RUN_QUALITY_SCAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Опционально: скан только одного домена"},
    },
}

# ── import_content schema (Фаза 5) ─────────────────────────

_IMPORT_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Исходный текст (Markdown/plain)"},
        "content_type": {"type": "string", "default": "book", "description": "Тип контента: book"},
        "domain": {"type": "string", "description": "Первичная классификация"},
        "subject": {"type": "string", "description": "Вторичная классификация"},
        "project": {"type": "string", "description": "Опциональный проект"},
        "title": {"type": "string", "description": "Заголовок коллекции (авто если не указан)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Унаследованные теги"},
        "cross_subjects": {"type": "array", "items": {"type": "string"}, "description": "Кросс-теги"},
        "max_chunk_tokens": {"type": "integer", "default": 512, "minimum": 64, "maximum": 2048},
        "wait_for_index": {"type": "boolean", "default": False},
        "cleanup_orphans": {"type": "boolean", "default": False},
    },
    "required": ["content", "domain", "subject"],
}

# ── analyze_content schema (Фаза 13.8) ─────────────────────

_ANALYZE_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Текст контента для анализа"},
        "max_fragment_chars": {"type": "integer", "description": "Максимальный размер фрагмента для анализа (по умолчанию ANALYZE_FRAGMENT_CHARS)"},
    },
    "required": ["content"],
}

# ── Tool definitions ───────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge",
        "description": "Семантический поиск по базе знаний. Возвращает наиболее релевантные записи с оценкой релевантности.",
        "inputSchema": _SEARCH_QUERY_SCHEMA,
    },
    {
        "name": "search_by_tags",
        "description": "Поиск записей по тегам через payload-фильтр Qdrant (без GPU). Поддерживает AND/OR семантику.",
        "inputSchema": _SEARCH_TAGS_SCHEMA,
    },
    {
        "name": "get_entry",
        "description": "Получить полную запись (frontmatter + Markdown-контент) по knowledge_id.",
        "inputSchema": _GET_ENTRY_SCHEMA,
    },
    {
        "name": "get_knowledge_map",
        "description": "Структурная карта знаний: domains → subjects → knowledge_ids. Поддерживает фильтрацию по домену.",
        "inputSchema": _GET_MAP_SCHEMA,
    },
    {
        "name": "write_knowledge",
        "description": "Записать новое знание: SSOT Markdown → chunk → embed → Qdrant upsert → INDEX update.",
        "inputSchema": _WRITE_SCHEMA,
    },
    {
        "name": "update_entry",
        "description": "Обновить существующую запись с optimistic locking (version check).",
        "inputSchema": _UPDATE_SCHEMA,
    },
    {
        "name": "delete_entry",
        "description": "Удалить запись: Markdown SSOT + Qdrant точки + Git commit.",
        "inputSchema": _DELETE_SCHEMA,
    },
    {
        "name": "list_domains",
        "description": "Список всех доменов знаний с пагинацией (cursor-based).",
        "inputSchema": _LIST_SCHEMA,
    },
    {
        "name": "list_subjects",
        "description": "Список subjects (тем) в заданном домене с пагинацией (cursor-based).",
        "inputSchema": _LIST_SCHEMA,
    },
    {
        "name": "list_projects",
        "description": "Список проектов (опционально: в заданном domain/subject) с пагинацией (cursor-based).",
        "inputSchema": _LIST_SCHEMA,
    },
    {
        "name": "reindex",
        "description": "Перестроить индекс: перечитать все Markdown-файлы → переиндексировать в Qdrant.",
        "inputSchema": _REINDEX_SCHEMA,
    },
    # ── Quality tools (Фаза 4) ───────────────────────────────
    {
        "name": "review_queue",
        "description": "Получить топ устаревших записей по staleness_score DESC. Записи с score ≥ 0.45 требуют ревизии.",
        "inputSchema": _REVIEW_QUEUE_SCHEMA,
    },
    {
        "name": "list_quality_issues",
        "description": "Список проблем качества: дубликаты, отсутствующие поля, edit-wars, битые ссылки. Фильтрация по типу и статусу.",
        "inputSchema": _LIST_QUALITY_ISSUES_SCHEMA,
    },
    {
        "name": "resolve_quality_issue",
        "description": "Разрешить проблему качества: merge (слить), deprecate (скрыть), restore (вернуть), resolve (исправлено), ignore (пропустить).",
        "inputSchema": _RESOLVE_QUALITY_ISSUE_SCHEMA,
    },
    {
        "name": "run_quality_scan",
        "description": "Запустить периодический quality scan: обход всех .md → staleness_score → dup-pair detection → issues + review_queue. Для cron (4.8).",
        "inputSchema": _RUN_QUALITY_SCAN_SCHEMA,
    },
    # ── import_content (Фаза 5) ──────────────────────────────
    {
        "name": "import_content",
        "description": "Импорт крупных текстов (книги, документация) в SSOT: декомпозиция на секции + авто-frontmatter + parent-child коллекции + best-effort batch запись. Переиспользует существующий write-path (markdown_store + pipeline + git).",
        "inputSchema": _IMPORT_CONTENT_SCHEMA,
    },
    {
        "name": "analyze_content",
        "description": "AI-анализ контента: рекомендации content_type/domain/subject/tags через Ollama LLM + TF-IDF fallback.",
        "inputSchema": _ANALYZE_CONTENT_SCHEMA,
    },
]

# ── Handler dispatch table (реальные реализации) ───────────

TOOL_HANDLERS = {
    "search_knowledge": search_knowledge,
    "search_by_tags": search_by_tags,
    "get_entry": get_entry,
    "get_knowledge_map": get_knowledge_map,
    "write_knowledge": write_knowledge,
    "update_entry": update_entry,
    "delete_entry": delete_entry,
    "list_domains": list_domains,
    "list_subjects": list_subjects,
    "list_projects": list_projects,
    "reindex": reindex,
    # Quality tools (Фаза 4)
    "review_queue": review_queue,
    "list_quality_issues": list_quality_issues,
    "resolve_quality_issue": resolve_quality_issue,
    "run_quality_scan": run_quality_scan,
    "import_content": import_content,
    "analyze_content": analyze_content,
}
