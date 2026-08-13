"""MCP Tools registry — 20 tools с JSON Schema (Блок A).

Реальные реализации в модулях: search.py, read.py, crud.py, browse.py, admin.py, content.py, collections.py.

G1-fix: list_subjects + list_projects зарегистрированы (ранее были импортированы, но не добавлены в TOOLS/TOOL_HANDLERS).
Variant A (13.10): +list_collections (список книг); search_knowledge обогащён (collection_id/content_type фильтры).
Фаза 13.14: +review_queue_books (агрегация книг по parent_knowledge_id).
"""

from __future__ import annotations

from typing import Any

from ..content.analyzer import analyze_content
from .admin import reindex
from .browse import list_domains, list_projects, list_subjects
from .collections import list_collections
from .content import cancel_import, extract_pdf_text, import_content
from .crud import delete_entry, update_entry, write_knowledge
from .fragments import add_fragment, delete_fragment, find_fragment, update_fragment
from .quality import (
    bulk_deprecate_duplicates,
    bulk_resolve_issues,
    cancel_quality_scan,
    list_quality_issues,
    resolve_quality_issue,
    review_duplicate_pairs,
    review_queue,
    review_queue_books,
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
        "collection_id": {"type": "string", "description": "Поиск внутри книги (фильтр по parent_knowledge_id)"},
        "content_type": {"type": "string", "description": "Фильтр по типу контента (book, collection)"},
        "include_deprecated": {"type": "boolean", "default": False, "description": "Показывать deprecated-записи в результатах (Фаза 13.14)"},
    },
    "required": ["query"],
}

_LIST_COLLECTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Фильтр по домену"},
        "cursor": {"type": "string", "description": "Курсор пагинации"},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
    },
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
        "cascade": {"type": "boolean", "default": False, "description": "Удалить также все дочерние секции по parent_knowledge_id (Фаза 13.14)"},
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

_REVIEW_QUEUE_BOOKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Фильтр по домену"},
        "subject": {"type": "string", "description": "Фильтр по предмету"},
        "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100, "description": "Макс. число книг в ответе"},
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
        "issue_id": {"type": "string", "description": "ID issue для разрешения (или knowledge_id для прямой операции — Фаза 13.14)"},
        "action": {
            "type": "string",
            "enum": ["merge", "deprecate", "restore", "resolve", "ignore"],
            "description": "Действие: merge (слить), deprecate (скрыть), restore (вернуть), resolve (исправлено), ignore (пропустить)",
        },
        "knowledge_id": {"type": "string", "description": "Прямая операция на запись без issue-lookup (Фаза 13.14)"},
        "target_id": {"type": "string", "description": "target knowledge_id (для merge)"},
        "reason": {"type": "string", "description": "Причина решения"},
        "cascade": {"type": "boolean", "default": False, "description": "Применить к дочерним секциям книги (Фаза 13.14)"},
    },
    "required": ["action"],
}

_RUN_QUALITY_SCAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "Опционально: скан только одного домена"},
    },
}

# ── P0: bulk_resolve_issues schema ────────────────────────────

_BULK_RESOLVE_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Фильтр по типам (duplicate, missing_field, orphaned, ...). None = все типы.",
        },
        "knowledge_id": {"type": "string", "description": "Опциональный фильтр по конкретной записи"},
        "status": {"type": "string", "default": "open", "description": "Исходный статус для выборки"},
        "action": {
            "type": "string",
            "enum": ["ignore", "resolve"],
            "default": "ignore",
            "description": "Действие: ignore (пропустить, обратимо) | resolve (исправлено)",
        },
        "reason": {"type": "string", "description": "Причина решения"},
    },
}


# ── Фаза 1 dedup: bulk_deprecate_duplicates schema ────────────

_BULK_DEPRECATE_DUPLICATES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Список issue_id → deprecate их knowledge_id (Фаза 1 dedup)",
        },
        "knowledge_id": {
            "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
            "description": "Прямой knowledge_id (или список) для deprecate",
        },
        "actor": {
            "type": "string",
            "default": "operator-batch",
            "description": "Кто выполняет: operator-batch | auto",
        },
        "reason": {"type": "string", "description": "Причина решения"},
    },
}


# ── Фаза 2 dedup: review_duplicate_pairs schema ───────────────

_REVIEW_DUPLICATE_PAIRS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "default": 200,
            "description": "Макс. число open dup-issues для анализа (Фаза 2 dedup)",
        },
    },
}


# ── 13.18: cancel_quality_scan schema ───────────────────────

_CANCEL_QUALITY_SCAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

# ── 13.21: cancel_import schema ─────────────────────────────

_CANCEL_IMPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "import_id": {"type": "string", "description": "ID импорта для отмены (опционально)"},
    },
}

# ── Fragment tool schemas (Фаза 13.23: фрагментные операции) ──

_ADD_FRAGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "collection_id": {"type": "string", "description": "ID книги-коллекции"},
        "title": {"type": "string", "description": "Заголовок нового раздела"},
        "content": {"type": "string", "description": "Содержание раздела (Markdown)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Дополнительные теги"},
    },
    "required": ["collection_id", "title", "content"],
}

_UPDATE_FRAGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fragment_id": {"type": "string", "description": "ID секции"},
        "content": {"type": "string", "description": "Новое содержание (Markdown)"},
        "title": {"type": "string", "description": "Новый заголовок"},
        "version": {"type": "integer", "description": "Optimistic locking: ожидаемая версия"},
        "wait_for_index": {"type": "boolean", "default": False},
    },
    "required": ["fragment_id"],
}

_DELETE_FRAGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fragment_id": {"type": "string", "description": "ID секции для удаления"},
    },
    "required": ["fragment_id"],
}

_FIND_FRAGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "collection_id": {"type": "string", "description": "ID книги-коллекции"},
        "query": {"type": "string", "description": "Поисковый запрос"},
        "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
    },
    "required": ["collection_id", "query"],
}

# ── import_content schema (Фаза 5) ─────────────────────────

_IMPORT_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "Исходный текст (Markdown/plain)"},
        "content_type": {"type": "string", "default": "book", "description": "Тип контента: book | pdf"},
        "domain": {"type": "string", "description": "Первичная классификация"},
        "subject": {"type": "string", "description": "Вторичная классификация"},
        "project": {"type": "string", "description": "Опциональный проект"},
        "title": {"type": "string", "description": "Заголовок коллекции (авто если не указан)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Унаследованные теги"},
        "cross_subjects": {"type": "array", "items": {"type": "string"}, "description": "Кросс-теги"},
        "max_chunk_tokens": {"type": "integer", "default": 512, "minimum": 64, "maximum": 2048},
        "wait_for_index": {"type": "boolean", "default": False},
        "cleanup_orphans": {"type": "boolean", "default": False},
        "replace_collection_id": {"type": "string", "description": "ID коллекции для ЗАМЕНЫ: после успешного импорта старая книга удаляется (cascade). Import-first: старая цела до подтверждения успеха новой."},
        "replace_on_partial": {"type": "boolean", "default": False, "description": "Удалить старую книгу даже при partial_success импорта (failed>0). Default False — безопасно."},
        "pdf_path": {"type": "string", "description": "Путь к PDF-файлу на сервере (из POST /upload, опционально для content_type=pdf)"},
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

# ── extract_pdf_text schema (PDF→текст для авто-классификации) ──

_EXTRACT_PDF_TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pdf_path": {"type": "string", "description": "Путь к PDF на сервере (из POST /upload)"},
    },
    "required": ["pdf_path"],
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
        "name": "list_collections",
        "description": "Список книг/коллекций с метаданными (title, domain/subject, tags, section_count, updated_at). Фильтр по домену.",
        "inputSchema": _LIST_COLLECTIONS_SCHEMA,
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
        "name": "review_queue_books",
        "description": "Топ устаревших КНИГ (агрегат по parent_knowledge_id). Возвращает книги с долей устаревших секций, максимальным staleness_score и top-5 секций. Фильтры domain/subject. (Фаза 13.14)",
        "inputSchema": _REVIEW_QUEUE_BOOKS_SCHEMA,
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
        "name": "bulk_resolve_issues",
        "description": "Пакетно резолвить/игнорировать issues по фильтру (типы/knowledge_id/status). Чистит накопленный шум за один вызов; меняет только issues.jsonl, не контент.",
        "inputSchema": _BULK_RESOLVE_ISSUES_SCHEMA,
    },
    {
        "name": "bulk_deprecate_duplicates",
        "description": "Пакетно deprecate записи-дубликаты (Фаза 1 dedup): скрыть из поиска (обратимо через restore), закрыть все их dup-issues, записать в audit.jsonl. Контент .md не трогается.",
        "inputSchema": _BULK_DEPRECATE_DUPLICATES_SCHEMA,
    },
    {
        "name": "review_duplicate_pairs",
        "description": "Ревью-очередь dup-пар (Фаза 2 dedup): ранжирование 🟢/🟡/🔴 по R1-R6 (exact hash, cosine+guards, антоним-guard). Read-only — утверждение через bulk_deprecate_duplicates.",
        "inputSchema": _REVIEW_DUPLICATE_PAIRS_SCHEMA,
    },
    {
        "name": "run_quality_scan",
        "description": "Запустить периодический quality scan: обход всех .md → staleness_score → dup-pair detection → issues + review_queue. Для cron (4.8).",
        "inputSchema": _RUN_QUALITY_SCAN_SCHEMA,
    },
    # ── 13.18: cancel quality scan ──────────────────────────
    {
        "name": "cancel_quality_scan",
        "description": "Отменить активный quality scan. Возвращает частичные метрики, освобождает lock для нового скана.",
        "inputSchema": _CANCEL_QUALITY_SCAN_SCHEMA,
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
    {
        "name": "extract_pdf_text",
        "description": "Конвертировать PDF → текст (pdfplumber + OCR fallback). Используется для авто-классификации PDF (Преобразовать→Обработать→Добавить).",
        "inputSchema": _EXTRACT_PDF_TEXT_SCHEMA,
    },
    # ── 13.21: cancel import ─────────────────────────────────
    {
        "name": "cancel_import",
        "description": "Отменить активный импорт (PDF/книга). Освобождает lock для следующей операции в очереди.",
        "inputSchema": _CANCEL_IMPORT_SCHEMA,
    },
    # ── Fragment tools (Фаза 13.23) ──────────────────────────
    {
        "name": "add_fragment",
        "description": "Добавить раздел в книгу: создаёт секцию с ID и sequence, индексирует, обновляет TOC.",
        "inputSchema": _ADD_FRAGMENT_SCHEMA,
    },
    {
        "name": "update_fragment",
        "description": "Обновить раздел книги: изменить содержание и/или заголовок с optimistic locking (version check).",
        "inputSchema": _UPDATE_FRAGMENT_SCHEMA,
    },
    {
        "name": "delete_fragment",
        "description": "Удалить раздел книги: soft-delete (→ .trash/) + удаление из Qdrant. Без каскада — только одна секция.",
        "inputSchema": _DELETE_FRAGMENT_SCHEMA,
    },
    {
        "name": "find_fragment",
        "description": "Найти разделы внутри книги по семантическому запросу. Возвращает fragment_id, title, score, snippet.",
        "inputSchema": _FIND_FRAGMENT_SCHEMA,
    },
]

# ── Handler dispatch table (реальные реализации) ───────────

TOOL_HANDLERS = {
    "search_knowledge": search_knowledge,
    "search_by_tags": search_by_tags,
    "get_entry": get_entry,
    "get_knowledge_map": get_knowledge_map,
    "list_collections": list_collections,
    "write_knowledge": write_knowledge,
    "update_entry": update_entry,
    "delete_entry": delete_entry,
    "list_domains": list_domains,
    "list_subjects": list_subjects,
    "list_projects": list_projects,
    "reindex": reindex,
    # Quality tools (Фаза 4)
    "review_queue": review_queue,
    "review_queue_books": review_queue_books,
    "list_quality_issues": list_quality_issues,
    "resolve_quality_issue": resolve_quality_issue,
    "bulk_resolve_issues": bulk_resolve_issues,
    "bulk_deprecate_duplicates": bulk_deprecate_duplicates,
    "review_duplicate_pairs": review_duplicate_pairs,
    "run_quality_scan": run_quality_scan,
    "cancel_quality_scan": cancel_quality_scan,
    "import_content": import_content,
    "analyze_content": analyze_content,
    "extract_pdf_text": extract_pdf_text,
    "cancel_import": cancel_import,
    # Fragment tools
    "add_fragment": add_fragment,
    "update_fragment": update_fragment,
    "delete_fragment": delete_fragment,
    "find_fragment": find_fragment,
}
