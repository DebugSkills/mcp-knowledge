<div align="center">

# 📚 mcp-knowledge

**MCP Knowledge Server** — семантическая база знаний для AI-агентов по протоколу **MCP** (Model Context Protocol)

🔗 [debugskills.ru](https://debugskills.ru/) · 🤝 Проект сообщества **DebugSkills**

🛠️ **20 MCP Tools** · 🧠 Markdown SSOT → Ollama embed → Qdrant · 🌐 air-gap ready · 🏭 production-ready

---

</div>

Хранение: Markdown SSOT → chunk → Ollama embed (nomic-embed-text) → Qdrant vector search. **20 MCP Tools**, air-gap совместимость (одноархивный deploy-bundle), production-ready (health, rate-limit, blue-green reindex, quality system). Веб-консоль **kb-console** (NiceGUI, :8085) для диагностики и обслуживания. Подключение AI-агентов (Kilo/Claude/Cline) — через **stdio-мост** (`mcp-stdio/bridge.py`, см. `docs/mcp-client-guide.md`). MCP-протокол: JSON-RPC 2.0 over HTTP (`POST /mcp`), `ping` → `{"result":{}}`, `notifications/initialized` → 204 (Фаза 13.21).

## 💎 Почему это ценно для сообщества

MCP-сервер — это **общий накопитель знаний для совместной разработки**: любое решение, инструкция или найденный ответ записывается один раз в единую базу и становится доступно всем участникам и их AI-агентам. Что это даёт:

- **📐 Единые стандарты** — одна классификация (domains → subjects → tags), один формат записей (frontmatter + Markdown), общие конвенции. Новые участники видят, как устроен проект, без долгих расспросов.
- **🧠 Накопление знаний** — опыт не теряется в чатах и переписке: решения, runbook'и, уроки и ответы фиксируются в SSOT, git-аудит хранит полную историю изменений, а quality system следит за актуальностью (staleness, review-очередь).
- **♻️ Переиспользование ценной информации** — семантический поиск находит релевантное за секунды: не нужно заново искать, переспрашивать или «изобретать велосипед», если проблема уже решена.
- **👥 Совместная работа без конфликтов** — multi-key доступ (read / import / write) и optimistic locking защищают данные, книги-коллекции позволяют импортировать и поддерживать целые материалы (документация, руководства) как единое целое.
- **🤖 Универсальность для AI-агентов** — стандартный протокол MCP: один раз развернул сервер — и знания доступны Kilo, Claude, Cline и любому другому MCP-клиенту без переписывания интеграций.
- **✅ Системное качество** — дубликаты, битые ссылки и устаревшие записи выявляются сканером автоматически, а не «когда-нибудь руками».
- **🔒 Приватность и автономность** — полная совместимость с air-gap: база может работать в изолированном контуре без интернета.

## 🏗️ Архитектура

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
│             /metrics · /upload · /imports ·        │
│             /imports/active · /imports/{id}/progress│
│             /imports/{id}/log · /imports/{id}/cancel│
│             /imports/{id}/remove · /imports/remove- │
│             finished · /quality/scan/progress      │
├────────────────────────────────────────────────────┤
│  Tools (20)                                        │
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

**kb-console** — отдельный самодостаточный контейнер (образ `kb-console:prod`): страницы **Статус** (health-карточки, метрики, 30 инструментов), **Книги** (список коллекций + оглавление), **Импорт** (загрузка материалов через `import_content`), **Поиск** (по корпусу), **Качество** (запуск quality-скана с живым прогрессом, review-очередь устаревших книг, issues, dedup-ревью 🟢/🟡). Может жить на клиентских хостах (`MCP_SERVER_URL` из env). Руководство: `kb-console/USER_GUIDE.md`.

## 🚀 Быстрый старт

```bash
# Разработка (venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -e mcp_server/ && pip install -e kb-console/   # kb-console для E2E S20

# Запуск (dev: host-network, хостовые Qdrant+Ollama)
cp .env.example .env && docker compose up -d mcp-server
docker compose up -d kb-console      # → http://localhost:8085

# Тесты (нужны запущенные Qdrant :6333 и Ollama :11434)
make e2e-slow                        # E2E S1-S20 (32 + S20 4/4)
.venv/bin/python -m pytest mcp_server/tests -q   # полный suite (629)
make console-test                    # unit + smoke kb-console (66)
.venv/bin/python -m pytest mcp-stdio/tests -q    # stdio-мост (19)
```

## 📦 Продовый деплой (air-gap, одноархивный bundle)

```bash
make bundle                          # машина с интернетом → mcp-kb-airgap-bundle.tar.gz (~1.2 GB)
# перенос архива на изолированный хост (USB/диск):
tar -xzf mcp-kb-airgap-bundle.tar.gz && cd staging
./scripts/offline-deploy.sh deploy   # образы (mcp-server+qdrant+kb-console) + Ollama-модели + запуск
./scripts/offline-deploy.sh verify   # smoke (5 проб, включая kb-console :8085) + E2E S1-S19
./scripts/offline-deploy.sh import --src /path/to/md/   # импорт знаний
```
Подробности: `docs/air-gap-validation.md` (в bundle — `DEPLOYMENT.md`), руководство консоли — `USER_GUIDE.md`.

## 🛠️ MCP Tools (20)

### 🔍 Search & Read
| # | Tool | Назначение |
|---|------|-----------|
| 1 | `search_knowledge` | Семантический поиск (Ollama embed) |
| 2 | `search_by_tags` | Поиск по тегам (payload-фильтр Qdrant) |
| 3 | `get_entry` | Получить полную запись (frontmatter + Markdown) |
| 4 | `get_knowledge_map` | Структурная карта: domains → subjects → IDs |

### ✍️ Write
| # | Tool | Назначение |
|---|------|-----------|
| 5 | `write_knowledge` | Создать: Markdown SSOT → chunk → embed → Qdrant |
| 6 | `update_entry` | Обновить с optimistic locking (version check) |
| 7 | `delete_entry` | Удалить: SSOT + Qdrant + Git commit |

### 🧭 Browse
| # | Tool | Назначение |
|---|------|-----------|
| 5 | `list_collections` | Список книг/коллекций с метаданными (title, domain/subject, tags, section_count) |
| 9 | `list_domains` | Список доменов (пагинация) |
| 10 | `list_subjects` | Список тем в домене |
| 11 | `list_projects` | Список проектов (domain/subject опционально) |

### ⚙️ Admin
| # | Tool | Назначение |
|---|------|-----------|
| 12 | `reindex` | Перестроить индекс: все .md → Qdrant (blue-green, zero-downtime) |

### 🩺 Quality (Фаза 4 + 13.14)
| # | Tool | Назначение |
|---|------|-----------|
| 13 | `review_queue` | Топ устаревших записей (staleness_score DESC) |
| 14 | `review_queue_books` | Топ устаревших КНИГ (агрегат по parent, доля устаревших секций) |
| 15 | `list_quality_issues` | Проблемы: дубликаты, edit-wars, битые ссылки |
| 16 | `resolve_quality_issue` | Разрешить: merge/deprecate/restore/resolve/ignore (cascade для книг) |
| 17 | `run_quality_scan` | Периодический scan (для cron, daily; фоновая задача с lock) |
| 18 | `cancel_quality_scan` | Отменить активный scan, освободить lock (Фаза 13.18) |

### 📥 Import (Фаза 5 + 13.8)
| # | Tool | Назначение |
|---|------|-----------|
| 19 | `import_content` | Декомпозиция + batch запись: content → collection (book, cross_subjects, wait_for_index) |
| 20 | `analyze_content` | AI-анализ контента: рекомендации content_type/domain/subject/tags (Ollama LLM + TF-IDF) |

## 💬 MCP Prompts

| Prompt | Назначение |
|--------|-----------|
| `how-to-structure-knowledge` | Рекомендации по структурированию знаний в Markdown (SSOT, frontmatter, теги) |
| `best-practice-write` | Best-practice для write_knowledge: SSOT, YAML frontmatter, теги, кросс-ссылки |
| `periodic_quality_cleanup` | Пошаговая инструкция для AI-агента: review_queue → list_issues → resolve |

## 🛡️ Quality System (Фаза 4)

| Механизм | Описание |
|----------|----------|
| **Pre-write gates** | Frontmatter-валидация (required → 409, recommended → warn) + семантический dup-check (cosine ≥0.92) |
| **Staleness scoring** | 5-факторная формула: возраст (evergreen-адаптивный 5×), дублирование, неполнота, edit-wars, битые ссылки |
| **Review queue** | Записи с score ≥0.45 → авто-очередь на ревизию |
| **Edit-war detection** | Git-based: ≥3 коммитов в 24ч → флаг |
| **Lifecycle** | 2-state: published ↔ deprecated (reversible restore) |
| **Quality SLO** | `review_queue_size > 50` → Prometheus/alertmanager alert |

## 📋 Конфигурация (env)

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

## 🗺️ Реализованные фазы

| Фаза | Статус | Ключевой результат |
|------|:------:|-------------------|
| 0-4 | ✅ | Scaffolding → Quality System (20 tools, 3 промпта, gates) |
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
| 13.12 | ✅ | stdio-мост для подключения mcp-knowledge к Kilo Code + обновление доков |
| 13.13 | ✅ | Карточка результата поиска — теги + открытие найденного фрагмента |
| 13.14 | ✅ | Вкладка «Качество» в kb-console: агрегация книг (parent+cascade), deprecate-фикс поиска |
| 13.15 | ✅ | Root-фикс зависания сервера: фоновый quality scan + asyncio.Lock + run_in_executor |
| 13.16 | ✅ | Таймаут переименования книги + прогресс-панель скана (была на «Поиск», с 13.27 — только «Качество») |
| 13.17-13.18 | ✅ | search dedup+junk-filter, DataCache+data_version, cancel_quality_scan, scanner .trash-фикс |
| 13.19 | ✅ | Nightly quality scan scheduler in-container + scan log volume + prune finished progress |
| 13.20 | ✅ | kb-console: развёрнутые секции без ручного F5 + живая консоль скана |
| 13.21 | ✅ | **PDF-импорт Фаза 3**: гибридный канал (POST /upload multipart + base64 MCP), pdfplumber + Tesseract OCR, декомпозиция по font-size, очередь импортов (1 за раз, фазы, checkpoint/resume, отмена), консоль очереди в UI (карточки + лог), POST /imports/{id}/cancel·remove + /imports/remove-finished + /imports/{id}/log, pure ASGI middleware (фикс Content-Length) |
| 13.22 | ✅ | Атомарная замена книги: replace_collection_id в import_content + HITL в консоли + batch-delete (1 git-commit на каскад) |
| 13.23 | ✅ | Qdrant-бэкап: sparse-фикс, снапшоты только своих коллекций, healthcheck /dev/tcp |
| 13.24 | ✅ | Advisory P2-фиксы: task-ref в hide, import asyncio наверх, +3 теста render_import_progress |
| 13.27 | ✅ | Прогресс скана: панель только на «Качестве» (построение при загрузке страницы, кнопка заблокирована на время скана, on_done однократно), персистентность scan_state.json + авто-resume прерванного скана после рестарта |
| 13.28 | ✅ | **Фаза 3 dedup: авто-deprecate 🟢-пачек (exact content-hash ONLY).** Серверный гейт целиком внутри `bulk_deprecate_duplicates` при `actor="auto"`: `AUTO_DEDUP_ENABLED` (по умолчанию **false**) + FP=0 за `AUTO_DEDUP_FP_FREE_SCANS=2` полных скана (`scan_completed`/`fp_rejection` в audit.jsonl) + `filter={hash_only}` (R1-предикат, cosine НИКОГДА не авто) + cooldown-щит `AUTO_DEDUP_RESTORE_COOLDOWN_SCANS=3` после restore (`restored_by_operator`) + cap `AUTO_DEDUP_MAX_PER_SCAN=100` + strict-audit (сбой аудита = abort пачки). Restore переоткрывает dup-issues (пара снова в Review Queue). UI «Качество»: панель «Журнал действий» (`list_audit_log`) со статусом гейта, ♻️ per-record restore, «Не дубль» → `marks_fp=True`. **Hot-reload НЕТ — флаги читаются при старте, изменение требует рестарта.** Включение: утром под присмотром, НЕ перед ночным cron 03:00 |

## 🧪 Тесты (актуальные цифры)

| Уровень | Результат |
|---------|-----------|
| Unit + integration (сервер) | 629 passed (2 pre-existing failures не связаны: s15 reindex blue-green, delete cascade) |
| E2E S1-S19 (реальные Qdrant+Ollama) | 43 passed (e2e-набор: http-contract S9-S12, mcp-protocol, russian-corpus, tools-coverage, console-client) |
| Конкурентный гейт (50× GET /imports) | PASS — регрессия Content-Length (pure ASGI middleware) закрыта |
| kb-console unit + smoke | 66/66 |
| mcp-stdio bridge tests | 19/19 (unit 18 + smoke 1) |
| Ruff | 0 ошибок |

## ⚠️ Known Limitations

- **Factual correctness:** не проверяется (требует LLM)
- **Coverage gaps:** не детектируются непокрытые темы
- **Conflicting entries:** тип зарезервирован (~90% FP)
- **Temporal dup-blind-spot:** async-окно 1-5с (компенсируется periodic scan)
- **Backlog (§11):** gRPC-сценарий 6334 (REST эквивалентен), F1 blue-green для legacy-коллекции, CLI subprocess-тест

## 📜 Коммиты (последние фазы)

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

*Актуально на 2026-08-08. 20 MCP Tools, 565 тестов mcp_server + 56 kb-console + 21 mcp-stdio, kb-console :8085, stdio-мост v1.1 (ping/notifications по MCP spec, HTTP 204 → без ответа), MCP_MAX_REQUEST_SIZE 128 МБ, qdrant ulimits 65535.*
