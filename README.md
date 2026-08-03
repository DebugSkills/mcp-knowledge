# mcp-knowledge

MCP Knowledge Server — семантическая база знаний для AI-агентов по протоколу MCP (Model Context Protocol). Проект сообщества DebugSkills.

Хранение: Markdown SSOT → chunk → BGE-M3/Ollama embed → Qdrant vector search. 15 MCP Tools, air-gap совместимость, production-ready (health, rate-limit, blue-green reindex).

## Архитектура

```
MCP Client (Claude/Cline/Kilo)
    │ JSON-RPC 2.0 over HTTP
    ▼
┌─────────────────────────────────────┐
│  FastAPI + MCP Handler (v0.1.0)     │
│  Auth: multi-key (read/write)       │
│  Rate-limit: token bucket            │
├─────────────────────────────────────┤
│  Tools (15)                         │
│  search read crud browse admin      │
│  review_queue list_quality_issues   │
│  resolve_quality_issue run_scan     │
├─────────────────────────────────────┤
│  Pipeline: chunk → embed → upsert   │
│  Qdrant (vector DB)                 │
│  Ollama (mxbai-embed-large 1024d)   │
├─────────────────────────────────────┤
│  Quality System                     │
│  gates scoring scanner lifecycle     │
│  edit-war dup-gate issues embedder  │
├─────────────────────────────────────┤
│  Markdown SSOT (knowledge/)         │
│  Git audit (#21)                    │
│  Backups (Qdrant snapshots)         │
└─────────────────────────────────────┘
```

## Быстрый старт

```bash
# Клонирование
git clone <repo-url> && cd mcp-knowledge

# Продакшен (Docker)
docker compose up -d

# Разработка (venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -e mcp_server/

# Тесты
python -m pytest mcp_server/tests/ -v

# Запуск quality scan (cron)
./scripts/quality_scan.sh
```

## MCP Tools (15)

### Search & Read
| # | Tool | Назначение |
|---|------|-----------|
| 1 | `search_knowledge` | Семантический поиск (BGE-M3/Ollama embed) |
| 2 | `search_by_tags` | Поиск по тегам (payload-фильтр Qdrant) |
| 3 | `get_entry` | Получить полную запись (frontmatter + Markdown) |
| 4 | `get_knowledge_map` | Структурная карта: domains → subjects → IDs |

### Write
| # | Tool | Назначение |
|---|------|-----------|
| 5 | `write_knowledge` | Создать: Markdown SSOT → chunk → embed → Qdrant |
| 6 | `update_entry` | Обновить с optimistic locking (version check) |
| 7 | `delete_entry` | Удалить: SSOT + Qdrant + Git commit |

### Browse
| # | Tool | Назначение |
|---|------|-----------|
| 8 | `list_domains` | Список доменов (пагинация) |
| 9 | `list_subjects` | Список тем в домене |
| 10 | `list_projects` | Список проектов (domain/subject опционально) |

### Admin
| # | Tool | Назначение |
|---|------|-----------|
| 11 | `reindex` | Перестроить индекс: все .md → Qdrant (blue-green, zero-downtime) |

### Quality (Фаза 4)
| # | Tool | Назначение |
|---|------|-----------|
| 12 | `review_queue` | Топ устаревших записей (staleness_score DESC) |
| 13 | `list_quality_issues` | Проблемы: дубликаты, edit-wars, битые ссылки |
| 14 | `resolve_quality_issue` | Разрешить: merge/deprecate/restore/resolve/ignore |
| 15 | `run_quality_scan` | Периодический scan (для cron, daily) |

## MCP Prompts

| Prompt | Назначение |
|--------|-----------|
| `periodic_quality_cleanup` | Пошаговая инструкция для AI-агента: review_queue → list_issues → resolve |

## Quality System (Фаза 4)

| Механизм | Описание |
|----------|----------|
| **Pre-write gates** | Frontmatter-валидация (required → 409, recommended → warn) + семантический dup-check (cosine ≥0.92) |
| **Staleness scoring** | 5-факторная формула: возраст (evergreen-адаптивный 5×), дублирование, неполнота, edit-wars, битые ссылки |
| **Review queue** | Записи с score ≥0.45 → авто-очередь на ревизию |
| **Edit-war detection** | Git-based: ≥3 коммитов в 24ч → флаг |
| **Lifecycle** | 2-state: published ↔ deprecated (reversible restore) |
| **Quality SLO** | `review_queue_size > 50` → Prometheus/alertmanager alert |

## Конфигурация

| Переменная | Default | Описание |
|-----------|---------|----------|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant векторная БД |
| `KNOWLEDGE_DIR` | `knowledge/` | Markdown SSOT |
| `MCP_READ_KEYS` | `[...]` | API-ключи для чтения |
| `MCP_WRITE_KEYS` | `[...]` | API-ключи для записи |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API (эмбеддинг + LLM) |
| `REVIEW_THRESHOLD` | 0.45 | Порог для review-очереди |
| `DUP_SIMILARITY_THRESHOLD` | 0.92 | Cosine-порог для дублей |
| `HF_HUB_OFFLINE` | — | Air-gap режим (не качать модели) |

## Реализованные фазы

| Фаза | Статус | Ключевой результат |
|------|:------:|-------------------|
| 0 | ✅ | Scaffolding: Pydantic-модели, Qdrant setup |
| 1 | ✅ | SSOT: Markdown-хранение, INDEX.gen.yaml |
| 2 | ✅ | MCP Server: 11 Tools, FastAPI, auth, rate-limit |
| 3 | ✅ | Production: health, blue-green, backup/restore, air-gap |
| 4 | ✅ | Quality: gates, scoring, lifecycle, 15 Tools, 133 теста |

## Known Limitations

- **Factual correctness:** не проверяется (требует LLM)
- **Coverage gaps:** не детектируются непокрытые темы
- **Conflicting entries:** тип зарезервирован (~90% FP)
- **Temporal dup-blind-spot:** async-окно 1-5с (компенсируется periodic scan)

## Коммиты (Фаза 4)

```
b8b4772 Ollama embedder adapter — replaces BGE-M3 for air-gap
89ae16c quality/__init__.py — dup_gate + lifecycle exports
7bf041e Quality Management docs + MCP Prompt
92baa15 integration tests — quality flow E2E
83a2f64 semantic dup-gate — cosine check
a4e5570 lifecycle slice — published|deprecated
143b5b8 cron scan + run_quality_scan tool
ef55e84 MCP Quality Tools — review_queue + list + resolve
09bcf8e edit-war detection — git-based
8677d90 quality foundation — issues + gates + scoring + scanner
```

---

*Фаза 4 завершена. 15 MCP Tools, 1 Prompt, 133 теста, Ollama mxbai-embed-large.*
