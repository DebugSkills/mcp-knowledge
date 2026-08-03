"""B3: MCP Prompts — подсказки для AI-агентов.

Три промпта:
- how-to-structure-knowledge — как структурировать знание в Markdown
- best-practice-write — best-practice для write_knowledge: SSOT, frontmatter, теги
- periodic_quality_cleanup — регулярная чистка качества БЗ (Фаза 4, §5.1)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("mcp_knowledge.prompts")

# ── Prompt definitions for prompts/list ────────────────────

PROMPTS = [
    {
        "name": "how-to-structure-knowledge",
        "description": "Рекомендации по структурированию знаний в Markdown-формате для MCP Knowledge Server.",
        "arguments": [],
    },
    {
        "name": "best-practice-write",
        "description": "Best-practice рекомендации для write_knowledge: SSOT, YAML frontmatter, теги, кросс-ссылки.",
        "arguments": [],
    },
    {
        "name": "periodic_quality_cleanup",
        "description": "Регулярная чистка качества базы знаний: review_queue + открытые issues → merge/deprecate/restore/resolve/ignore (Фаза 4).",
        "arguments": [
            {
                "name": "domain",
                "description": "Фильтр по домену (опционально)",
                "required": False,
            },
            {
                "name": "max_actions",
                "description": "Максимальное число действий за один запуск (default 10)",
                "required": False,
            },
        ],
    },
]

# ── Prompt templates ───────────────────────────────────────

_HOW_TO_STRUCTURE_KNOWLEDGE = """# Как структурировать знание в Markdown

## 1. Формат файла
Каждое знание — это отдельный `.md` файл с YAML frontmatter в начале.

## 2. YAML Frontmatter (обязательно)
```yaml
---
knowledge_id: "ru-python-async-patterns"
domain: "engineering"
subject: "python"
project: "backend"
cross_subjects: ["devops", "architecture"]
tags: ["asyncio", "best-practice", "patterns"]
version: 1
created_at: "2026-07-21T10:00:00+03:00"
updated_at: "2026-07-21T10:00:00+03:00"
---
```

### Поля frontmatter:
- **knowledge_id** (обязательно): уникальный ID, формат `[a-z0-9][a-z0-9_-]{2,127}`
- **domain** (обязательно): первичная классификация (например: engineering, devops, frontend)
- **subject** (обязательно): вторичная классификация (например: python, docker, react)
- **project** (опционально): проект, к которому относится знание
- **cross_subjects** (опционально): кросс-предметные связи
- **tags** (опционально): свободные теги для поиска
- **version** (обязательно): optimistic locking, начинается с 1
- **created_at / updated_at** (обязательно): ISO 8601 с timezone

## 3. Структура контента
- Начинайте с заголовка H1 (`# Заголовок`)
- Используйте H2 (`##`) для секций
- Включайте примеры кода в markdown code blocks с указанием языка
- Добавляйте ссылки на связанные знания через `knowledge_id`

## 4. Best Practices
- **SSOT (Single Source of Truth):** одна запись = один файл = один концепт
- **Атомарность:** не смешивайте несколько концептов в одной записи
- **Самодостаточность:** запись должна быть понятна без внешнего контекста
- **Теги:** используйте 3-7 релевантных тегов (не больше 10)
- **Кросс-ссылки:** указывайте cross_subjects для междисциплинарных знаний

## 5. Пример хорошо структурированного знания
```markdown
---
knowledge_id: "ru-docker-multi-stage-builds"
domain: "devops"
subject: "docker"
project: "backend"
cross_subjects: ["engineering"]
tags: ["docker", "optimization", "best-practice", "Dockerfile"]
version: 1
created_at: "2026-07-21T10:00:00+03:00"
updated_at: "2026-07-21T10:00:00+03:00"
---

# Multi-stage сборки в Docker

## Проблема
Обычные Dockerfile создают большие образы из-за сборочных зависимостей...

## Решение
Multi-stage builds позволяют разделить сборку и рантайм...

```dockerfile
# Stage 1: build
FROM golang:1.21 AS builder
WORKDIR /app
COPY . .
RUN go build -o server .

# Stage 2: runtime
FROM alpine:3.18
COPY --from=builder /app/server /usr/local/bin/server
CMD ["server"]
```

## Преимущества
- Меньший размер образа (до 90% экономии)
- Безопасность (нет компиляторов в production)
- Чистый Docker history
```
"""

_BEST_PRACTICE_WRITE = """# Best Practice: write_knowledge

## SSOT (Single Source of Truth)

Каждый вызов `write_knowledge` создаёт **единственный** `.md` файл в директории `knowledge/{domain}/{subject}/`.
Это SSOT для всего пайплайна:

```
write_knowledge(content, domain, subject, ...)
    │
    ├── 1. store.write(entry)           → Markdown .md + git commit (SSOT)
    ├── 2. pipeline.enqueue(entry)       → chunk → embed → Qdrant upsert
    └── 3. knowledge_index.update(...)   → INDEX.gen.yaml
```

## Three-way Write Flow

1. **Markdown SSOT** — первичное хранилище. Всегда консистентно.
2. **Qdrant Vector DB** — семантический индекс. Может быть рассинхронизирован (восстанавливается через reconciliation).
3. **INDEX.gen.yaml** — структурная карта. Best-effort обновление.

## YAML Frontmatter (обязательные поля)

| Поле | Назначение | Пример |
|------|-----------|--------|
| `knowledge_id` | Уникальный ID | `ru-docker-best-practices` |
| `domain` | Первичная классификация | `devops` |
| `subject` | Вторичная классификация | `docker` |
| `tags` | 3-7 релевантных тегов | `["docker","best-practice"]` |
| `version` | Optimistic locking | `1` |
| `created_at` | ISO 8601 дата создания | `2026-07-21T10:00:00+03:00` |
| `updated_at` | ISO 8601 дата обновления | `2026-07-21T10:00:00+03:00` |

## Правила тегирования

- **3-7 тегов** на запись (не больше 10)
- Используйте **lowercase + kebab-case**: `best-practice`, `ci-cd`, `async-await`
- Теги должны быть **конкретными**: `docker-compose` лучше чем `docker`
- Не дублируйте domain/subject в тегах
- `cross_subjects` для междисциплинарных связей (например: devops-знание может иметь `cross_subjects: ["engineering"]`)

## Wait-for-index флаг

```python
# Быстрая запись (асинхронная индексация):
write_knowledge(content="...", domain="devops", subject="docker", wait_for_index=False)
# → {"knowledge_id": "...", "indexed": false, "pending": true}

# Гарантированная консистентность (до 30 сек):
write_knowledge(content="...", domain="devops", subject="docker", wait_for_index=True)
# → {"knowledge_id": "...", "indexed": true, "pending": false}
```

## Анти-паттерны

❌ **Не делайте:**
- Записывать несколько концептов в одну knowledge-запись
- Использовать пробелы или спецсимволы в knowledge_id
- Пропускать YAML frontmatter
- Использовать теги верхнего уровня (domain/subject-level теги)
- Писать >10 тегов (размывает поиск)

✅ **Делайте:**
- Одна запись = один концепт
- knowledge_id по шаблону: `{lang}-{topic}-{specific}` (например: `ru-python-async-patterns`)
- Всегда указывать `domain` и `subject`
- Использовать `cross_subjects` для междисциплинарных знаний
- Проверять `get_knowledge_map(domain)` перед записью — возможно, такое знание уже существует
"""


_PERIODIC_QUALITY_CLEANUP = """# Periodic Quality Cleanup (Фаза 4)

Ты управляешь качеством базы знаний. Действуй по шагам:

## Шаг 1: Получить очередь устаревших записей
```
review_queue(domain={domain}, limit={max_actions})
```
→ топ записей по staleness_score DESC с причинами (`reasons[]`).

## Шаг 2: Получить открытые issues
```
list_quality_issues(status="open", limit={max_actions})
```
→ открытые проблемы: duplicates, missing_field, edit_war.

## Шаг 3: Принять решение по каждой записи/issue
- Устарела и есть дубль → `resolve_quality_issue(action="merge", target_id=<лучший дубль>)`
- Устарела и бесполезна → `resolve_quality_issue(action="deprecate")`
- Ошибочно скрыта (deprecated по ошибке) → `resolve_quality_issue(action="restore")`
- False positive → `resolve_quality_issue(action="ignore")`
- Актуальна после правки → `resolve_quality_issue(action="resolve")`

## Шаг 4: SLO-мониторинг
Если `review_queue_size` остаётся высоким (>50) после действий — сообщить оператору
(SLO-breach: KnowledgeReviewQueueSLOBreach).

## Критично
- `merge`/`deprecate` необратимы через `restore` только частично — подтверждай destructive-действия.
- `merge` требует `target_id` — убедись что target существует и лучше источника.
- Не глуши все issues подряд — `ignore` только для false positives.
"""


def get_prompt(name: str) -> dict | None:
    """Получить содержимое промпта по имени.

    Returns:
        {"name": str, "description": str, "messages": [...]} или None
    """
    if name == "how-to-structure-knowledge":
        return {
            "name": name,
            "description": "Рекомендации по структурированию знаний в Markdown-формате.",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": _HOW_TO_STRUCTURE_KNOWLEDGE,
                    },
                }
            ],
        }
    elif name == "best-practice-write":
        return {
            "name": name,
            "description": "Best-practice для write_knowledge: SSOT, frontmatter, теги.",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": _BEST_PRACTICE_WRITE,
                    },
                }
            ],
        }
    elif name == "periodic_quality_cleanup":
        return {
            "name": name,
            "description": "Регулярная чистка качества БЗ: review_queue + issues → merge/deprecate/restore.",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": _PERIODIC_QUALITY_CLEANUP,
                    },
                }
            ],
        }
    else:
        logger.warning("Prompt not found: '%s'", name)
        return None
