# mcp-knowledge

MCP Knowledge Server — семантическая база знаний для AI-агентов по протоколу MCP (Model Context Protocol). Проект сообщества DebugSkills.

Хранение: Markdown SSOT → chunk → Ollama embed (nomic-embed-text) → Qdrant vector search. **18 MCP Tools**, air-gap совместимость (одноархивный deploy-bundle), production-ready (health, rate-limit, blue-green reindex, quality system). Веб-консоль **kb-console** (NiceGUI, :8085) для диагностики и обслуживания. Подключение AI-агентов (Kilo/Claude/Cline) — через **stdio-мост** (`mcp-stdio/bridge.py`, см. `docs/mcp-client-guide.md`).

## Архитектура

```
Kilo/Claude/Cline ──┐
(stdio-мост)        │  kb-console (NiceGUI, :8085)
mcp-stdio/bridge.py │    │  диаг. :8085 → :8000
    │ POST /mcp     │    │  (или с клиентского хоста)
    ▼               ▼    ▼
┌────────────────────────────────────────────────────┐
│  FastAPI + MCP Handler (v0.1.0)                    │
│  Auth: multi-key (read/import/write, X-API-Key)    │
│  Rate-limit: token bucket (REST, 429)              │
│  Endpoints: POST /mcp · /health · /health/live ·   │
│             /metrics · /imports/{id}/progress      │
├────────────────────────────────────────────────────┤
│  Tools (18)                                        │
│  search read crud browse admin quality import analyze │
├────────────────────────────────────────────────────┤
│  Pipeline: chunk → embed → upsert (async worker)   │
│  Qdrant (vector DB, REST 6333 / gRPC 6334)         │
│  Ollama (nomic-embed-text 768d, системный сервис)   │
├────────────────────────────────────────────────────┤
│  Quality System (Фаза 4)                           │
│  gates scoring scanner lifecycle dup-gate issues   │
├────────────────────────────────────────────────────┤
│  Markdown SSOT (knowledge/) + Git audit            │
│  DLQ (dead-letter queue) · Backups (snapshots)     │
└────────────────────────────────────────────────────┘
```

**kb-console** — отдельный самодостаточный контейнер (образ `kb-console:prod`): страницы **Статус** (health-карточки, метрики, 18 инструментов), **Книги** (список коллекций + оглавление), **Импорт** (загрузка материалов через `import_content`), **Поиск** (по корпусу). Может жить на клиентских хостах (`MCP_SERVER_URL` из env). Руководство: `kb-console/USER_GUIDE.md`.

## Быстрый старт

```bash
# Разработка (venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -e mcp_server/ && pip install -e kb-console/   # kb-console для E2E S20

# Запуск (dev: host-network, хостовые Qdrant+Ollama)
cp .env.example .env && docker compose up -d mcp-server
docker compose up -d kb-console      # → http://localhost:8085

# Тесты (нужны запущенные Qdrant :6333 и Ollama :11434)
make e2e-slow                        # E2E S1-S20 (32 + S20 4/4)
.venv/bin/python -m pytest mcp_server/tests -q   # полный suite (482)
make console-test                    # unit + smoke kb-console (33)
.venv/bin/python -m pytest mcp-stdio/tests -q    # stdio-мост (19)
```

## Продовый деплой (air-gap, одноархивный bundle)

```bash
make bundle                          # машина с интернетом → mcp-kb-airgap-bundle.tar.gz (~1.2 GB)
# перенос архива на изолированный хост (USB/диск):
tar -xzf mcp-kb-airgap-bundle.tar.gz && cd staging
./scripts/offline-deploy.sh deploy   # образы (mcp-server+qdrant+kb-console) + Ollama-модели + запуск
./scripts/offline-deploy.sh verify   # smoke (5 проб, включая kb-console :8085) + E2E S1-S19
./scripts/offline-deploy.sh import --src /path/to/md/   # импорт знаний
```
Подробности: `docs/air-gap-validation.md` (в bundle — `DEPLOYMENT.md`), руководство консоли — `USER_GUIDE.md`.

## MCP Tools (18)

### Search & Read
| # | Tool | Назначение |
|---|------|-----------|
| 1 | `search_knowledge` | Семантический поиск (Ollama embed) |
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
| 5 | `list_collections` | Список книг/коллекций с метаданными (title, domain/subject, tags, section_count) |
| 9 | `list_domains` | Список доменов (пагинация) |
| 10 | `list_subjects` | Список тем в домене |
| 11 | `list_projects` | Список проектов (domain/subject опционально) |

### Admin
| # | Tool | Назначение |
|---|------|-----------|
| 12 | `reindex` | Перестроить индекс: все .md → Qdrant (blue-green, zero-downtime) |

### Quality (Фаза 4)
| # | Tool | Назначение |
|---|------|-----------|
| 13 | `review_queue` | Топ устаревших записей (staleness_score DESC) |
| 14 | `list_quality_issues` | Проблемы: дубликаты, edit-wars, битые ссылки |
| 15 | `resolve_quality_issue` | Разрешить: merge/deprecate/restore/resolve/ignore |
| 16 | `run_quality_scan` | Периодический scan (для cron, daily) |

### Import (Фаза 5 + 13.8)
| # | Tool | Назначение |
|---|------|-----------|
| 17 | `import_content` | Декомпозиция + batch запись: content → collection (book, cross_subjects, wait_for_index) |
| 18 | `analyze_content` | AI-анализ контента: рекомендации content_type/domain/subject/tags (Ollama LLM + TF-IDF) |

## MCP Prompts

| Prompt | Назначение |
|--------|-----------|
| `how-to-structure-knowledge` | Рекомендации по структурированию знаний в Markdown (SSOT, frontmatter, теги) |
| `best-practice-write` | Best-practice для write_knowledge: SSOT, YAML frontmatter, теги, кросс-ссылки |
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

## Конфигурация (env)

| Переменная | Default | Описание |
|-----------|---------|----------|
| `EMBEDDING_BACKEND` | `ollama` | Прод-эмбеддер (Ollama, без torch) |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `mxbai-embed-large` | Эмбеддер |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | HF-токенизатор chunker (НЕ Ollama-модель; fallback без transformers) |
| `QDRANT_URL` / `QDRANT_PREFER_GRPC` | `http://localhost:6333` / `true` | Qdrant (REST; gRPC при контейнерной сети) |
| `KNOWLEDGE_DIR` | `knowledge/` | Markdown SSOT |
| `MCP_READ_KEYS` / `MCP_WRITE_KEYS` | `[]` | API-ключи (read/write; пусто = без auth) |
| `MCP_IMPORT_KEYS` | `[]` | Import-ключи (read + import_content, без delete/reindex) |
| `MCP_API_KEY` | — | Ключ kb-console (должен входить в read/import/write keys) |
| `MCP_SERVER_URL` / `CONSOLE_PORT` | `http://localhost:8000` / `8085` | kb-console: адрес сервера / порт UI |
| `OLLAMA_CHAT_MODEL` | `qwen2.5:7b` | LLM для analyze_content (рекомендации) |
| `ANALYZE_FRAGMENT_CHARS` / `ANALYZE_TIMEOUT` | `8000` / `60s` | Лимиты анализа контента |
| `RATE_LIMIT_READ_PER_MIN` / `RATE_LIMIT_WRITE_PER_MIN` | `100` / `20` | Rate-limit (429) |
| `REVIEW_THRESHOLD` | 0.45 | Порог для review-очереди |
| `DUP_SIMILARITY_THRESHOLD` | 0.92 | Cosine-порог для дублей |

## Реализованные фазы

| Фаза | Статус | Ключевой результат |
|------|:------:|-------------------|
| 0-4 | ✅ | Scaffolding → Quality System (18 tools, 3 промпта, gates) |
| 9 | ✅ | Idempotent E2E-сьют (S1-S8), фикс latent dup-gate бага |
| 12 | ✅ | HTTP-level E2E (S9-S12: health/metrics/429/409/503) + observability-метрики |
| 13 | ✅ | Полное E2E-покрытие (S13-S19) + 24 quality unit-теста + 2 прод-фикса |
| 13.5 | ✅ | Ollama как прод-embedder + лёгкий Docker-образ (без torch, 424 passed) |
| 13.6 | ✅ | Air-gap deploy bundle: один архив (образы + Ollama-модели), deploy/verify/import |
| 13.7 | ✅ | kb-console (NiceGUI :8085): диагностика, импорт, поиск; E2E S20; bundle с 3 образами |
| 13.8 | ✅ | analyze_content (AI-рекомендации через Ollama + TF-IDF), unwrap-фикс kb-console |
| 13.9 | ✅ | Прогресс импорта (GET /imports/{id}/progress) + 1 коммит на книгу |
| 13.10 | ✅ | list_collections + enriched search (фильтры collection_id/content_type) |
| 13.11 | ✅ | kb-console UX: модалка, прелоадер, редактируемый title, отдельные эндпоинты |

## Тесты (актуальные цифры)

| Уровень | Результат |
|---------|-----------|
| Unit + integration (сервер) | 393 passed |
| E2E S1-S19 (реальные Qdrant+Ollama) | 32/32 (S8 — e2e_slow) |
| E2E S20 (kb-console MCPClient ↔ сервер) | 4/4 (в контейнере mcp-server — skip, exit 0) |
| kb-console unit + smoke | 33/33 |
| mcp-stdio bridge tests | 19/19 (unit 18 + smoke 1) |
| Полный suite (`make test`) | **482 passed**, 2 skipped, 1 deselected |
| Docker (в контейнере mcp-server) | 424 passed / E2E 31 passed |
| Ruff | 0 ошибок |

## Known Limitations

- **Factual correctness:** не проверяется (требует LLM)
- **Coverage gaps:** не детектируются непокрытые темы
- **Conflicting entries:** тип зарезервирован (~90% FP)
- **Temporal dup-blind-spot:** async-окно 1-5с (компенсируется periodic scan)
- **Backlog (§11):** gRPC-сценарий 6334 (REST эквивалентен), F1 blue-green для legacy-коллекции, CLI subprocess-тест

## Коммиты (последние фазы)

```
e0973b9 fix(phase13.7): S20 importorskip — E2E зелёный в verify
1414acd chore(phase13.7): порт kb-console 8080 → 8085
c8044e3 docs(phase13.7): user guide kb-console (USER_GUIDE.md)
0a83736 feat(phase13.7): kb-console — NiceGUI-клиент (диагностика + импорт + поиск)
3530d47 feat(phase13.6): air-gap deploy bundle — one-archive install for isolated hosts
487542c feat(phase13.5): Ollama as prod embedder + lightweight Docker (no torch)
5f48c3c feat(phase13): full E2E coverage — S13-S19 + 24 quality unit tests
a2e6479 feat(phase12): HTTP-level E2E S9-S12 + 5 observability metrics
```

---

*Актуально на 2026-08-07. 18 MCP Tools, 482 тестов (+ mcp-stdio: 19), kb-console :8085, stdio-мост для Kilo/Claude/Cline, air-gap bundle 1.1 GB.*
