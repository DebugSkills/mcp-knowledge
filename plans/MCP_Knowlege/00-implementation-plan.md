# 📊 ПЛАН РЕАЛИЗАЦИИ: MCP Knowledge Server

> **trace_id:** `code-2026-07-21-001` | **Автор:** analyst | **Дата:** 2026-07-29
> **Версия:** 3.1 | **Статус:** готов к реализации (post-Critic Gate, PASS 0.84)
> **Архитектура:** **Hybrid (Semantic RAG + Hierarchical Cascade Indexing)** — Qdrant + BGE-M3 + INDEX.gen.yaml (FPF score: 0.83)
>
> **История версий:**
> - **v1.0** (2026-07-21) — базовый план: 15 решений, 4 фазы, ~102 ч
> - **v2.0** (2026-07-21) — **доработка по внешней рецензии** (оценка 8/10): адаптация под **Podman/Quadlet**, разделение CPU/GPU метрик, reconciliation при старте, мульти-ключи, токенайзер BGE-M3, sync-CPU ограничение, интеграция идей оптимизации (#7–#11). Добавлены решения #16–#21. Оценка пересмотрена: ~119 ч базовых / ~143 ч с резервом.
> - **v2.1** (2026-07-21) — **возврат на Docker-стек:** внешний рецензент ошибочно предположил окружение Podman; оператор подтвердил стек команды — **Docker**. Удалены решение #16 (Runtime Podman + Quadlet), задача 0.9 (валидация Podman), задача 3.8 (Quadlet для production), риск R7, противоречие C4, директория `quadlet/`, все упоминания Quadlet/CDI/podman-compose. Замены: `podman`→`docker`, `podman-compose`→`docker compose`, `Containerfile`→`Dockerfile`, CDI→`--gpus` (nvidia-container-toolkit). Docker поддерживает `healthcheck` + `restart: unless-stopped` + `depends_on: condition: service_healthy` и `--gpus all` нативно — адаптация не требуется. Трудозатраты: −10 ч → **~109 ч базовых / ~131 ч с резервом**. Roadmap сжат до **14 дней**. Сохранены все остальные правки v2 (#17–#21, разделённые CPU/GPU-метрики, reconciliation, Prometheus в Фазе 2, кириллические тесты, все P0/P1/P2 из Critic Gate).

> - **v2.2** (2026-07-21) — **точечная доработка по второй внешней рецензии** (влезает в существующий contingency ~5 ч, без новых фаз/решений): 1) зафиксирован инвариант **1 uvicorn worker** (задача 0.4 — валидация `WORKERS=1`; критерии Ф2) — масштабирование через внешнюю очередь, а не флаг запуска; 2) CPU-embed через **`run_in_executor`** (задачи 1.5/1.6) + E2E-тест «поиск под нагрузкой импорта на `EMBEDDING_BACKEND=cpu`, p95 ≤ 400 ms»; 3) **`asyncio.Lock`** вокруг git-операций против гонки на `index.lock` (задача 1.1 + unit-тест конкурентной записи); 4) **обратная сверка** в reconciliation — точки Qdrant без `.md` → delete (задача 2.9); 5) **бэкап SSOT** (`git push`/`tar knowledge/`) + переименование `backup_qdrant.sh` → `backup.sh` (задача 3.2); 6) **`make dlq-replay`** для возврата задач из `data/dlq/` (задача 2.11 + Makefile). Косметика: stdio = локальный доверенный режим без auth; Roadmap **14 → 14–17 дней**; chunk=512 — выбор в пользу качества retrieval, а не лимит BGE-M3 (держит 8192). Структура фаз/задач и трудозатраты **не изменены**.

> - **v2.3** (2026-07-21) — **инфраструктурные решения (storage & deployment):** 1) **Локальный диск обязателен для runtime** (#27) — SMB/CIFS технически несовместим с тремя механизмами плана: `fcntl.flock` (Фаза 4 §4.1), git-операциями (#21), Qdrant mmap (задача 1.3); 2) **Два отдельных git-репозитория** (#28) — `mcp-knowledge/` (код сервера) и `knowledge/` (Markdown SSOT) как независимые репо: разный lifecycle, cadence коммитов и backup; 3) **SMB только как backup-target** (#29) — runtime на локальном диске, `backup.sh` копирует snapshots + tar на шару; 4) **Ansible playbook** для автоматизации развёртывания на сервере (§1.5) — повторяемость и видимость конфигурации. Обновлены: §2 (+3 решения #27–#29), §3 (структура двух репозиториев + ansible/), новый §1.5 (storage strategy). Трудозатраты: **+~4 ч** (Ansible playbook, в contingency Фазы 0/3).
>
> - **v3.0** (2026-07-29) — **Hybrid Search Architecture (Semantic RAG + Hierarchical Cascade Indexing), FPF 0.83.** Решение по результатам брейншторма: текущий семантический RAG (v2.3) — safety net для контентного поиска, но **слеп** к структурной навигации и exhaustive tag search. Добавлено: 1) **INDEX.gen.yaml generation** (#30) — авто-генерация root + per-section структурных индексов из Markdown SSOT; root ≤4 KB (sections + top-20 tags + how_to_orient + completeness), per-section ≤8 KB (files + domain_tags + gap_analysis + adaptive L3 при >20 файлов); встроена в reconciliation pipeline + при каждом write/update/delete; 2) **Два новых MCP Tools** (#31) — `get_knowledge_map(domain?)` (структурная карта, p95 < 5 ms, без GPU) + `search_by_tags(tags[], match?)` (exhaustive поиск через Qdrant payload filter, без GPU); итого **9 MCP Tools**; 3) **Gap analysis** (#32) — proactive валидация frontmatter при `write_knowledge` + per-section `top-5 missing` + `suggested_tags` + `coverage%` в `get_knowledge_map()`. Новая трёхинструментная поисковая модель: агент сам выбирает tool'ы (последовательно или параллельно). Обновлены: §1.1 (9 Tools + INDEX pipeline), §2 (+3 решения #30–#32), §3 (+`indexing/knowledge_index.py`), Фаза 1 (+задача 1.11, ~4 ч), Фаза 2 (+задачи 2.14/2.15, ~3 ч), §8 (113→120 ч), §9 (+2 метрики latency), §12 (14→15 дней), §13 (+слепые зоны SB1–SB6). Трудозатраты: **+7 ч** → Ф0–Ф3 **120 ч** (с резервом ~144 ч).
>
> - **v3.1** (2026-07-29) — **доработка по P1-замечаниям Critic Gate (PASS 0.84).** 6 P1-замечаний интегрированы в существующие задачи (без новых фаз/решений): **P1-1** — **cache-invalidation** для `get_knowledge_map`: in-memory кэш распарсенного INDEX + инвалидация при write/update/delete (задача 1.11 + 2.14, критерии Ф2); **P1-2** — `search_by_tags` **limit=500 + `truncated:true`** в ответе + WARN-лог при достижении лимита (задача 2.15, критерии Ф2); **P1-3** — **INDEX size enforcement** truncation-стратегия: root `top-N` tags + счётчик «…and M more», per-section `top-N` + adaptive L3 при >N файлов, жёсткие лимиты ≤4 KB/≤8 KB с assert (задача 1.11, критерии Ф1); **P1-4** — **`suggested_tags` алгоритм**: frequency-based (top-N тегов секции, отсутствующих у файла) + path-based (извлечение тегов из компонентов пути директории) (задача 1.11); **P1-5** — **INDEX update при structural change** (rename/move/delete директории) → полная перестройка root INDEX + обеих затронутых per-section индексов (задача 1.11, критерии Ф1); **P1-6** — **4 новых риска R14–R17** (§10.4). Трудозатраты: **+2 ч** → Ф0–Ф3 **122 ч** (с резервом ~146 ч). Покрытие: §13.7.

---

## 0. 🚦 Статус реализации (актуализация 2026-07-31)

> **Метод:** сверка плана с фактическим кодом в [`mcp_server/src/mcp_server/`](mcp_server/src/mcp_server/). Критерий «готово» — модуль существует и покрывает задачи фазы (по докстрингам и контрактам). Легенда: ✅ реализовано · 🟡 частично · ❌ не начато (только план).

### 0.1 Сводка по фазам

| Фаза | Статус | Доказательство по коду | Milestone |
|------|:------:|------------------------|:---------:|
| **Ф0: Scaffolding + Docker** | ✅ | [`docker-compose.yml`](docker-compose.yml:1) (rw-mount после P0-фикса), [`Dockerfile`](mcp_server/Dockerfile:1), [`config.py`](mcp_server/src/mcp_server/config.py:42) (инвариант `WORKERS=1`), [`Makefile`](Makefile:1), `ansible/` — | ✅ |
| **Ф1: Ядро + эмбеддинги + INDEX** | ✅ | [`markdown_store.py`](mcp_server/src/mcp_server/storage/markdown_store.py:1), [`qdrant_client.py`](mcp_server/src/mcp_server/storage/qdrant_client.py:1), [`manager.py`](mcp_server/src/mcp_server/embedding/manager.py:1) GPU/CPU, [`chunker.py`](mcp_server/src/mcp_server/indexing/chunker.py:1), [`pipeline.py`](mcp_server/src/mcp_server/indexing/pipeline.py:1), [`knowledge_index.py`](mcp_server/src/mcp_server/indexing/knowledge_index.py:1). Critic Gate REVISE→исправлено | ✅ **M1** |
| **Ф2: MCP-сервер + устойчивость** | ❌ | Нет `auth.py`, пакета `tools/`, `resources.py`, `prompts.py`, `metrics.py`, `reconcile.py`. e2e-тест — пустой docstring. Готов план: [`02-phase2-mcp-server.md`](02-phase2-mcp-server.md) + [`.board.md`](.board.md:1) | ⏳ **M2** |
| **Ф3: Production + Air-gap** | 🟡 | Скрипты есть: [`backup.sh`](scripts/backup.sh:1), [`offline-deploy.sh`](scripts/offline-deploy.sh:1), [`reindex.sh`](scripts/reindex.sh:1). НО: rate-limit, blue-green reindex, conflict resolution, runbook ротации — не сделаны (зависят от Ф2) | ⏳ **M3** |
| **Ф4: Knowledge Quality** | ❌ | Нет пакета `quality/`; [`quality_scan.sh`](scripts/quality_scan.sh:13) — stub | — |
| **Ф5: Content Import** | ❌ | Нет пакета `content/` | — |

> **Тесты:** директории [`tests/unit`](mcp_server/tests/unit/__init__.py:1) · [`tests/integration`](mcp_server/tests/integration/__init__.py:1) · [`tests/e2e`](mcp_server/tests/e2e/test_russian_corpus.py:1) — пусты/только docstring. Покрытие E2E на русском корпусе — цель Фазы 2 (критерий #10).

### 0.2 Детализация по задачам Ф0–Ф1 (реализованные)

| Задача | Статус | Где |
|--------|:------:|-----|
| 0.1–0.8 Scaffolding | ✅ | `docker-compose.yml`, `Dockerfile`, `config.py`, `health.py`, `Makefile`, `.env.example` |
| 0.4 Инвариант `WORKERS=1` | ✅ | [`config.py:42`](mcp_server/src/mcp_server/config.py:42) (валидация при старте) |
| 0.6 `make dlq-replay` | ✅ | [`Makefile:32`](Makefile:32) + [`cli.py`](mcp_server/src/mcp_server/cli.py:54) |
| 0.9 Ansible playbook | 🟡 | `ansible/` (структура ролей есть, наполнение — в ходе Ф3) |
| 1.1 Markdown SSOT + git-аудит + `asyncio.Lock` | ✅ | [`markdown_store.py:49`](mcp_server/src/mcp_server/storage/markdown_store.py:49) |
| 1.2 Гибридная иерархия | ✅ | `markdown_store.py` (CRUD `domain/subject/project`) |
| 1.3 Qdrant-коллекция + payload | ✅ | [`qdrant_client.py`](mcp_server/src/mcp_server/storage/qdrant_client.py:1) + [`schema.py`](mcp_server/src/mcp_server/storage/schema.py:1) |
| 1.4 Chunker XLM-RoBERTa (512 токенов) | ✅ | [`chunker.py`](mcp_server/src/mcp_server/indexing/chunker.py:1) + [`tokenizer.py`](mcp_server/src/mcp_server/embedding/tokenizer.py:1) |
| 1.5/1.6 In-process embed GPU/CPU + `run_in_executor` | ✅ | [`manager.py`](mcp_server/src/mcp_server/embedding/manager.py:1), [`pipeline.py:247`](mcp_server/src/mcp_server/indexing/pipeline.py:247) |
| 1.7 `/health` embedding-проверка | ✅ | [`health.py:34`](mcp_server/src/mcp_server/health.py:34) |
| 1.8 Async pipeline (queue+batch+backpressure) | ✅ | [`pipeline.py:33`](mcp_server/src/mcp_server/indexing/pipeline.py:33) |
| 1.9 Полный reindex | ✅ | [`pipeline.py:135`](mcp_server/src/mcp_server/indexing/pipeline.py:135) `reindex_all()` |
| 1.11 INDEX.gen.yaml + gap + cache + size-enforcement | ✅ | [`knowledge_index.py:50`](mcp_server/src/mcp_server/indexing/knowledge_index.py:50) |

> **⚠️ Носитель P0-фикса (pre-flight Ф2):** [`qdrant_client.py`](mcp_server/src/mcp_server/storage/qdrant_client.py:135) `search_by_tags` — логика AND (`match="all"`) требует N условий `MatchValue` вместо `MatchAny(any=tags)`. Зафиксировано в [`02-phase2-mcp-server.md`](02-phase2-mcp-server.md) §PRE-FLIGHT P0-1.

### 0.3 Следующий шаг

**Фаза 2 — единственный блокирующий пробел.** Без неё сервер не отвечает на запросы агентов (нет MCP JSON-RPC, auth, tools). Детальный план: [`02-phase2-mcp-server.md`](02-phase2-mcp-server.md) (4 блока B→A→C→D, 15 задач, ~37 ч). Ф3 завершается после Ф2 (rate-limit/ротация требуют `auth.py`).

---

## 📑 Содержание

1. [Обзор архитектуры](#1-обзор-архитектуры)
2. [Архитектурные решения (26)](#2-архитектурные-решения-26)
3. [Структура проекта](#3-структура-проекта)
4. [Фаза 0: Scaffolding и инфраструктура (Docker)](#4-фаза-0-scaffolding-и-инфраструктура-docker)
5. [Фаза 1: Ядро хранения и эмбеддингов (in-process)](#5-фаза-1-ядро-хранения-и-эмбеддингов-in-process)
6. [Фаза 2: MCP-сервер, протокол и устойчивость](#6-фаза-2-mcp-сервер-протокол-и-устойчивость)
7. [Фаза 3: Production-готовность и Air-gap (Docker)](#7-фаза-3-production-готовность-и-air-gap-docker)
8. [Сводная оценка трудозатрат](#8-сводная-оценка-трудозатрат)
9. [Критерии успеха проекта](#9-критерии-успеха-проекта)
10. [Реестр рисков и mitigation](#10-реестр-рисков-и-mitigation)
11. [Air-gap Deployment Checklist (Docker)](#11-air-gap-deployment-checklist-docker)
12. [Roadmap (временная шкала)](#12-roadmap-временная-шкала)
13. [Матрица покрытия критики](#13-матрица-покрытия-критики)
14. [Лог внешней рецензии v2](#14-лог-внешней-рецензии-v2)

---

## 1. Обзор архитектуры

### 1.1 Компонентная схема (MVP: 2 сервиса)

> **Изменение v2 (#17):** для MVP эмбеддинги считаются **in-process** внутри `mcp-server` (ONNX Runtime на CPU / sentence-transformers на GPU через `--gpus all`, nvidia-container-toolkit). Отдельный `embedding-svc` выносится в опциональный сервис только при появлении реальной потребности (GPU на отдельном хосте). Это экономит ~8–12 ч на инфраструктуре и упрощает air-gap-развёртывание (3 контейнера → 2).

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AI Agent (LLM клиент)                         │
│                   MCP JSON-RPC over stdio / HTTP                     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  X-API-Key (read-keys / write-keys)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  mcp-server  (порт 8000)  — FastAPI + fastmcp                        │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │  9 MCP Tools │  │  Resources   │  │  Prompts (шаблоны)        │  │
│  │  search/get/ │  │  список entr │  │  best-practice /          │  │
│  │  write/upd/  │  │  y по доменам│  │  knowledge-template       │  │
│  │  del/list/   │  │              │  │                           │  │
│  │  reindex+map │  │              │  │                           │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────────────┘  │
│         │     ┌──────────────────────────────────────────────┐       │
│         │     │  Indexing Pipeline (asyncio.Queue + WAL)     │       │
│         │ wr ─▶│  ┌─▶ chunk ─▶ embed ─▶ Qdrant upsert        │       │
│         │     │  │   (## split, XLM-RoBERTa 512 tok)         │       │
│         │     │  │       (DLQ + retry)                       │       │
│         │     │  └─ sync-флаг ?wait_for_index=true ◀──┐      │       │
│         │     │  Reconciliation при старте:           │   │   │       │
│         │     │  Markdown updated_at ↔ Qdrant payload │   │   │       │
│         │     │  + INDEX.gen.yaml rebuild 🆕v3.0(#30) │   │   │       │
│         │     └───────────────────────────────────────┘   │   │       │
│         │  ┌──────────────────────────────────────────────┘   │       │
│         │  │  Embedding (IN-PROCESS) ← #17 MVP                │       │
│         │  │  • GPU: BGE-M3 via sentence-transformers (--gpus)│       │
│         │  │  • CPU: BGE-M3 via ONNX Runtime (fallback)       │       │
│         │  └──────────────────────────────────────────────────┘       │
│         │  Markdown SSOT (knowledge/*.md) + git audit (#21)           │
│  ┌──────┴──────────────────────────────────────────────────────────┐  │
│  │  /health  + /metrics (Prometheus #2.13)                          │  │
│  │  runtime: Docker; production: docker compose + restart (#10)     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└──────────┬────────────────────────────────────────────────────────────┘
           │ gRPC (6334) / REST (6333)
           ▼
┌─────────────────────────────────────┐
│  qdrant  (порты 6333/6334)          │     ┌──────────────────────────────────┐
│  collection: knowledge              │     │  embedding-svc  (ОПЦИОНАЛЬНО)    │
│  HNSW + payload filtering           │  ▶  │  выносится только когда GPU на   │
│  Snapshots API (бэкап)              │     │  отдельном хосте (Phase 3+, #17) │
└─────────────────────────────────────┘     └──────────────────────────────────┘
```

> **🆕 v2.2 (clarification — transport):** `stdio` = **локальный доверенный режим без аутентификации** (внутрипроцессное подключение LLM-клиента на той же машине; `X-API-Key` не проверяется). HTTP (`:8000`) — сетевой режим с обязательной проверкой мульти-ключей (#6, #18). Для MVP реализуются оба transport'а; production/air-gap — HTTP с мульти-ключами.

> **🆕 v3.0 (Hybrid Search Architecture, #30–#32):** `9 MCP Tools` = 7 базовых + `get_knowledge_map(domain?)` + `search_by_tags(tags[], match?)`. INDEX.gen.yaml generation встроена в reconciliation pipeline: при старте — полная перестройка root + per-section; при каждом write/update/delete — инкрементальное обновление затронутой секции. Root INDEX ≤4 KB (sections + top-20 tags + how_to_orient + completeness%). Per-section `_INDEX.gen.yaml` ≤8 KB (files + domain_tags + gap_analysis + adaptive L3 при >20 файлов). Секции определяются из структуры директорий `knowledge/`. **БЕЗ** внешнего `cascade.yaml`.

> **🆕 v3.0 — Трёхинструментная поисковая модель:**

| Tool | Назначение | GPU | Exhaustive |
|------|-----------|:---:|:---:|
| `get_knowledge_map(domain?)` | Структурная ориентация — INDEX.gen.yaml | ❌ | ✅ |
| `search_by_tags(tags[], match?)` | Точный перебор по метаданным (Qdrant payload filter) | ❌ | ✅ |
| `search_knowledge(query, filters?, top_k)` | Семантический контентный поиск (embed + HNSW) | ✅ | ❌ |

> Агент сам решает какие tool'ы вызывать (последовательно или параллельно). `get_knowledge_map` и `search_by_tags` работают **без GPU** — мгновенная навигация даже на CPU-only.

### 1.2 Потоки данных

| Поток | Источник → Назначение | Канал | Латентность |
|-------|----------------------|-------|-------------|
| **Read (search/get)** | Agent → mcp-server → qdrant → Agent | gRPC | < 100 ms (p95) |
| **Write (async)** | Agent → mcp-server → Markdown SSOT (+git commit) → Queue → embed (in-process) → qdrant | in-proc + gRPC | 1–5 сек (фон) |
| **Write (sync)** | Agent → mcp-server → ... → await index done → Agent | in-proc + gRPC | **GPU: ≤ 5 сек; CPU: `pending:true`** |
| **Embedding (in-process)** | pipeline → embedding-модуль (GPU/CPU) | in-proc call | GPU 50–200 мс; CPU 600–1000 мс / чанк |
| **Reconciliation** | startup → Markdown frontmatter ↔ Qdrant payload | in-proc | см. задачу 2.9 |
| **Backup** | qdrant snapshot + SSOT (`git push`/tar `knowledge/`) → `data/` | REST API + git/cron | по расписанию |
| **Health / Metrics** | Docker → `/health`, `/metrics` каждого сервиса | HTTP | каждые 15–30 сек |
| **🆕 v3.0: Knowledge map** | Agent → mcp-server → INDEX.gen.yaml (in-memory) → Agent | in-proc read | **< 5 ms** (p95) |
| **🆕 v3.0: Tag search** | Agent → mcp-server → qdrant payload filter (no embed) → Agent | gRPC | **< 50 ms** (p95) |
| **🆕 v3.0: INDEX gen** | write/reconcile → parse frontmatter → root + per-section INDEX.gen.yaml | in-proc | синхронно с write |

### 1.3 Принцип SSOT (Single Source of Truth)

**Markdown на диске = единственный источник правды.** Qdrant и INDEX.gen.yaml — **два вычисляемых индекса**, полностью перестраиваемых из Markdown через `reindex()`. Это гарантирует:
- Recovery без потери данных (даже при полной потере Qdrant ИЛИ INDEX)
- **Git-versioning знаний (#21):** `git commit` после каждого write/update/delete — бесплатная история, `git blame` и откат
- Человекочитаемость (редактирование без MCP)
- **🆕 v3.0:** INDEX.gen.yaml = структурная карта (sections + tags + gap_analysis), Qdrant = семантический индекс (векторы + HNSW). Оба восстанавливаются из Markdown SSOT.

### 1.4 Runtime: Docker (air-gap ready)

> **Подтверждено оператором:** окружение — **Docker** (стек команды). Docker нативно поддерживает `healthcheck` + `restart: unless-stopped` + `depends_on: condition: service_healthy`, GPU-проброс через `--gpus all` (nvidia-container-toolkit), `docker save/load` для air-gap переноса. Всё работает «из коробки» без адаптации.

### 1.5 Storage & Deployment Strategy 🆕 v2.3

> **Ключевой принцип:** runtime-данные — **только на локальном диске** сервера. SMB/сетевые шары — **исключительно как backup-target**, не как рабочее хранилище.

**Почему НЕ SMB для runtime (3 технических блокера):**

| Блокер | Где в плане | Причина |
|--------|------------|---------|
| `fcntl.flock` не работает над SMB/CIFS | Фаза 4 §4.1 (`issues.jsonl`) | POSIX advisory locks не поддерживаются большинством SMB-серверов → порча данных при конкурентной записи scanner + write-gate |
| Git на сетевой ФС — антипаттерн | Фаза 1 §1.1 (#21) | `git commit` = десятки мелких I/O; stale `.git/index.lock` при обрыве; Git FAQ предупреждает |
| Qdrant mmap corruption | Фаза 1 §1.3 (задача 1.3) | mmap-хранилище на сетевой ФС → несогласованность кэша, silent corruption; Qdrant требует локальный диск |

**Два отдельных git-репозитория (#28):**

| Репозиторий | Содержимое | Cadence коммитов | Backup |
|-------------|-----------|------------------|--------|
| `mcp-knowledge/` | Код сервера (Python, Dockerfile, compose, тесты) | Релизы (разработчик) | GitLab/GitHub push |
| `knowledge/` | Markdown SSOT — `.md` файлы знаний (#21) | **Каждый** `write_knowledge` = git commit | `git push` → bare-remote + `tar` → SMB |

> `knowledge/` **обязана быть отдельной репой** (не подпапкой кода): разный lifecycle (агенты пишут в runtime), частая история коммитов, отдельный backup через bare-remote (task 3.2), air-gap-перенос (§11).

**Структура на сервере:**

```
/opt/mcp-knowledge/                          # РАБОЧАЯ ДИРЕКТОРИЯ (локальный диск сервера)
├── mcp-knowledge/                           # РЕПА №1: код сервера (git → GitLab)
│   ├── docker-compose.yml
│   ├── mcp_server/
│   └── ...
├── knowledge/                               # РЕПА №2: Markdown SSOT (git → bare-remote)
│   ├── .git/                                #   ОТДЕЛЬНЫЙ репозиторий (#21)
│   ├── {domain}/{subject}/{project}/*.md
│   └── .trash/
├── data/                                    # runtime-данные (НЕ в git, Docker bind mounts)
│   ├── qdrant/                              #   Qdrant storage (mmap → ТОЛЬКО локальный диск)
│   ├── qdrant/snapshots/                    #   бэкапы (#11)
│   ├── dlq/                                 #   Dead Letter Queue (#14)
│   ├── quality/issues.jsonl                 #   quality log (Фаза 4, fcntl.flock!)
│   └── backups/                             #   tar knowledge/ (task 3.2)
└── models_cache/                            # BGE-M3 pre-downloaded (air-gap #15)

/opt/mcp-knowledge-bare/knowledge.git        # BARE-РЕПО для git-push backup SSOT (task 3.2)
/mnt/smb-backup/                             # ОПЦИОНАЛЬНО: SMB-шара — только backup-target
```

**Развёртывание через Ansible (#29, повторяемость + видимость конфигурации):**

Полное развёртывание автоматизируется **Ansible playbook** — повторяемый, декларативный, с версионированием инфраструктуры:

```
ansible/                                     # IaC для развёртывания (в репе mcp-knowledge/)
├── playbook.yml                             # site playbook
├── inventory.yml                            # целевой хост
├── roles/
│   ├── mcp_kb_dirs/                         # создание /opt/mcp-knowledge/ + bare-remote
│   ├── mcp_kb_repos/                        # git clone mcp-knowledge + init knowledge/
│   ├── mcp_kb_docker/                       # docker compose up + health-check
│   └── mcp_kb_cron/                         # backup.sh + quality_scan.sh cron
└── templates/
    ├── .env.j2                              # MCP_*_KEYS (Ansible Vault)
    └── docker-compose.override.yml.j2       # volume paths для конкретного хоста
```

> Ansible playbook покрывает шаги 1–6 из §11.2 (Air-gap deploy) и задачу 0.2 (docker compose). Secrets (`MCP_*_KEYS`) через **Ansible Vault**. Трудозатраты: ~4 ч (в contingency Фазы 0/3).

---

## 2. Архитектурные решения (26)

### Базовые (User Gate)

| # | Решение | Детали | Покрытие |
|---|---------|--------|:--------:|
| 1 | Векторное хранилище | **Qdrant** (gRPC 6334, HNSW, payload filtering) | Фаза 1 |
| 2 | Формат знаний | **Markdown + YAML frontmatter** (SSOT на диске) | Фаза 1 |
| 3 | Embedding | **BGE-M3** (1024d, GPU primary + CPU fallback ONNX) | Фаза 1 |
| 4 | Иерархия | **Гибрид**: директории (primary domain/subject/project) + кросс-теги | Фаза 1 |
| 5 | MCP | **Tools (9) + Resources + Prompts** — 🆕 v3.0: +`get_knowledge_map`, +`search_by_tags` | Фаза 2 |
| 6 | Аутентификация | **API-ключи** (read-key + write-key; **мульти-ключи #18**) | Фаза 2 |
| 7 | Индексация | **Async** (Markdown SSOT → asyncio.Queue → Qdrant upsert) + sync флаг | Фаза 1/2 |

### Доработки после Critic Gate (P0+P1)

| # | Приоритет | Решение | Покрытие |
|---|:---------:|---------|:--------:|
| 8 | P0 | **CPU fallback**: `EMBEDDING_BACKEND=auto|cpu|gpu` через ONNX Runtime | Фаза 1 |
| 9 | P0 | **Sync-флаг**: `write_knowledge(?wait_for_index=true)` (GPU-гарантия ≤5с; на CPU → `pending`, см. #6-S) | Фаза 2 |
| 10 | P0 | **Health checks**: `/health` на всех сервисах + docker `healthcheck` + `restart: unless-stopped` | Фаза 0/3 |
| 11 | P1 | **Qdrant Snapshots API** для инкрементального бэкапа | Фаза 3 |
| 12 | P1 | **Ротация API-ключей** через env vars `MCP_READ_KEY(S)`, `MCP_WRITE_KEY(S)` (мульти-ключи см. #18) | Фаза 3 |
| 13 | P1 | **Chunking**: разбиение по `##` заголовкам, max 512 **токенов BGE-M3** (XLM-RoBERTa, см. #20) | Фаза 1 |
| 14 | P1 | **Dead Letter Queue**: упавшие embedding-задачи → retry (3x) → DLQ → алерт | Фаза 2 |
| 15 | P1 | **Air-gap playbook**: `scripts/offline-deploy.sh` (`docker save/load`, pip wheelhouse) | Фаза 3 |

### Доработки после внешней рецензии (v2) 🔶

> **v2.1:** решение #16 (Runtime Podman + Quadlet) удалено — оператор подтвердил Docker-стек. Оставшиеся правки v2 сохранены.

| # | Приоритет | Решение | Покрытие |
|---|:---------:|---------|:--------:|
| 17 | 🟠 P1 | **In-process embedding (MVP)**: эмбеддинги внутри `mcp-server`; `embedding-svc` опционален при GPU на отдельном хосте | Фаза 1 |
| 18 | 🔴 P0 | **Мульти-ключи (graceful rotation)**: `MCP_READ_KEYS=k1,k2` / `MCP_WRITE_KEYS=k1,k2`; добавил новый → перевёл клиентов → убрал старый | Фаза 2/3 |
| 19 | 🔴 P0 | **Reconciliation при старте**: сверка `updated_at` из Markdown frontmatter с payload Qdrant; доиндексация расхождений | Фаза 2 |
| 20 | 🟠 P1 | **Токенайзер BGE-M3 для chunking**: считать токены XLM-RoBERTa (не приблизительно); для русского текста коэффициент токен/слово выше | Фаза 1 |
| 21 | 🟡 P2 | **Git-аудит Markdown SSOT**: `git add && git commit` после каждого write/update/delete | Фаза 1 |

### Инфраструктурные решения (v2.3) 🆕

| # | Приоритет | Решение | Детали | Покрытие |
|---|:---------:|---------|--------|:--------:|
| 27 | 🔴 P0 | **Локальный диск для runtime** | Qdrant (`data/qdrant/`), git (`knowledge/.git/`), `issues.jsonl` — **только локальный диск**. SMB/CIFS блокирует `fcntl.flock` (Фаза 4 §4.1), тормозит git (#21), ломает Qdrant mmap (задача 1.3). См. §1.5 | Фаза 0 |
| 28 | 🔴 P0 | **Два отдельных git-репозитория** | `mcp-knowledge/` (код сервера) ≠ `knowledge/` (Markdown SSOT). Разный lifecycle, cadence, backup-стратегия. `knowledge/` → bare-remote для `git push` (task 3.2). См. §1.5 | Фаза 0 |
| 29 | 🟠 P1 | **SMB — только backup-target + Ansible deploy** | Runtime — локальный диск. `backup.sh` копирует snapshots + `tar knowledge/` → SMB-шару. Развёртывание через **Ansible playbook** (повторяемость, видимость). См. §1.5 | Фаза 0/3 |

### Hybrid Search Architecture (v3.0) 🆕

> **Брейншторм:** Semantic RAG (v2.3) — safety net для контентного поиска, но слеп к структурной навигации и exhaustive tag search. Hybrid = semantic + hierarchy. FPF A.19.ECS score: **0.83** vs Semantic-only 0.74 vs Hierarchy-only 0.69.

| # | Приоритет | Решение | Детали | Покрытие |
|---|:---------:|---------|--------|:--------:|
| 30 | 🟠 P1 | **INDEX.gen.yaml generation** | Встроенный в mcp-server. Авто-генерация при reconciliation (старт) + при каждом write/update/delete. **Root** INDEX.gen.yaml ≤4 KB: `sections[]` + `top-20 tags` + `how_to_orient` + `completeness%`. **Per-section** `_INDEX.gen.yaml` ≤8 KB: `files[]` + `domain_tags` + `gap_analysis` (top-5 missing + suggested_tags) + adaptive L3 при >20 файлов. Секции — из структуры директорий `knowledge/`. **БЕЗ** внешнего `cascade.yaml`. Восстанавливается из Markdown SSOT (как Qdrant). См. задачу 1.11 | Фаза 1 |
| 31 | 🟠 P1 | **Новые MCP Tools (+2)** | `get_knowledge_map(domain?)` — возврат INDEX.gen.yaml / per-section `_INDEX.gen.yaml` (структурная карта, без GPU, p95 < 5 ms). `search_by_tags(tags[], match?)` — exhaustive поиск по тегам через **Qdrant payload filter** (без GPU, без embed, p95 < 50 ms). `match` = `all` (AND) \| `any` (OR). Итого **9 MCP Tools** вместо 7. См. задачи 2.14, 2.15 | Фаза 2 |
| 32 | 🟡 P2 | **Gap analysis** | **Proactive** валидация frontmatter при `write_knowledge` — WARN если обязательные поля (`domain`, `subject`, `tags`) отсутствуют или скудны. Per-section `_INDEX.gen.yaml` содержит `top-5 missing` + `suggested_tags`. `coverage%` (доля записей с заполненным frontmatter) в `get_knowledge_map()`. Интегрировано в INDEX generation (задача 1.11). Feeds `issues.jsonl` (Фаза 4) | Фаза 1/4 |

---

## 3. Структура проекта

```
mcp-knowledge/                          # РЕПА №1: код сервера (git → GitLab)
│                                       # 🆕 v2.3 (#28): ОТДЕЛЬНЫЙ репозиторий от knowledge/
├── docker-compose.yml                  # MVP: 2 сервиса (qdrant + mcp-server);
│                                       # embedding-svc опционально (закомментирован)
├── .env.example                        # MCP_READ_KEYS / MCP_WRITE_KEYS (мульти), backend, URL
├── Makefile                            # make dev / test / backup / reindex / dlq-replay (docker compose)
├── README.md
│
├── mcp_server/                         # Сервис: MCP-сервер + in-process embedding (Python 3.11+)
│   ├── Dockerfile                      # Стандартное имя образа (python:3.11-slim)
│   ├── pyproject.toml                  # fastmcp, fastapi, qdrant-client, pydantic, pyyaml,
│   │                                   #   sentence-transformers, onnxruntime, prometheus-client, gitpython
│   ├── src/mcp_server/
│   │   ├── __init__.py
│   │   ├── main.py                     # Точка входа: FastAPI + fastmcp app
│   │   ├── config.py                   # Settings (pydantic-settings, env vars; *_KEYS списки)
│   │   ├── auth.py                     # Мульти-ключи: MCP_READ_KEYS[], MCP_WRITE_KEYS[] (#18)
│   │   ├── health.py                   # /health endpoint
│   │   ├── metrics.py                  # 🆕 v2 (из Ф3→Ф2): Prometheus /metrics (#2.13)
│   │   ├── mcp/
│   │   │   ├── tools.py                # 9 MCP Tools (🆕 v3.0: +get_knowledge_map, +search_by_tags)
│   │   │   ├── resources.py            # MCP Resources
│   │   │   └── prompts.py              # MCP Prompts
│   │   ├── storage/
│   │   │   ├── markdown_store.py       # Markdown SSOT + git commit (#21)
│   │   │   ├── qdrant_client.py        # gRPC клиент Qdrant
│   │   │   └── schema.py               # Payload-схема Qdrant
│   │   ├── embedding/                  # 🆕 v2 (#17): IN-PROCESS эмбеддинги
│   │   │   ├── manager.py              # EMBEDDING_BACKEND=auto|cpu|gpu, авто-деградация
│   │   │   ├── gpu_backend.py          # BGE-M3 через sentence-transformers (CUDA/--gpus)
│   │   │   ├── cpu_backend.py          # BGE-M3 через ONNX Runtime
│   │   │   ├── tokenizer.py            # 🆕 v2 (#20): XLM-RoBERTa токенайзер для chunking
│   │   │   └── models_cache.py         # pre-download моделей для air-gap
│   │   ├── indexing/
│   │   │   ├── pipeline.py             # asyncio.Queue + worker(s)
│   │   │   ├── chunker.py              # Chunking по ## заголовкам (max 512 токенов XLM-R #20)
│   │   │   ├── reconcile.py            # 🆕 v2 (#19): сверка Markdown↔Qdrant при старте
│   │   │   ├── dlq.py                  # Dead Letter Queue + retry
│   │   │   ├── sync_barrier.py         # await для ?wait_for_index=true
│   │   │   └── knowledge_index.py      # 🆕 v3.0 (#30): INDEX.gen.yaml generation (root + per-section + gap analysis)
│   │   └── models.py                   # Pydantic-модели
│   └── tests/
│       ├── unit/                       # chunker, markdown_store, auth (мульти-ключи), tokenizer
│       ├── integration/                # pipeline → qdrant (testcontainers), reconcile
│       └── e2e/                        # MCP Tools через JSON-RPC — на РУССКОМ корпусе (#10)
│
├── embedding_svc/                      # ОПЦИОНАЛЬНО (#17): вынос при GPU на отдельном хосте
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── src/embedding_svc/              # (скелет; активируется при реальной потребности)
│
├── knowledge/                          # РЕПА №2: Markdown SSOT (🆕 v2.3 #28 — ОТДЕЛЬНЫЙ git-репо!)
│   ├── .git/                           #   #21 — git-аудит; git push → bare-remote (task 3.2)
│   ├── {domain}/{subject}/{project}/*.md
│   ├── INDEX.gen.yaml                  # 🆕 v3.0 (#30): root structural map (auto-gen, ≤4 KB)
│   ├── {domain}/_INDEX.gen.yaml        # 🆕 v3.0 (#30): per-section map (auto-gen, ≤8 KB)
│   └── .trash/                         # soft-delete
│
├── data/                               # 🆕 v2.3 (#27): ТОЛЬКО локальный диск (НЕ SMB!)
│   ├── qdrant/                         #   Volume Qdrant (mmap → локальный диск)
│   ├── qdrant/snapshots/               #   Бэкапы Qdrant
│   ├── dlq/                            #   Dead Letter Queue
│   ├── quality/issues.jsonl            #   🆕 Фаза 4: quality log (fcntl.flock!)
│   └── backups/                        #   🆕 v2.2: tar knowledge/ (task 3.2)
│
├── ansible/                            # 🆕 v2.3 (#29): IaC развёртывание (playbook + roles, Vault)
│   ├── playbook.yml                    #   site playbook (dirs → repos → compose → cron)
│   ├── inventory.yml                   #   целевой хост
│   └── templates/.env.j2              #   MCP_*_KEYS через Vault
└── scripts/
    ├── offline-deploy.sh               # Air-gap (docker save/load, pip wheelhouse)
    ├── backup.sh                       # 🆕 v2.2: snapshot Qdrant + бэкап SSOT (git push/tar) + ротация
    ├── reindex.sh                      # Полный reindex из Markdown
    └── seed_knowledge.py               # Загрузка начального корпуса (русский)
```

### 3.1 Схема Markdown-записи (YAML frontmatter)

```yaml
---
knowledge_id: "ru-python-async-patterns"     # уникальный ID (→ имя файла)
domain: "engineering"                         # первичная классификация
subject: "python"                             # вторичная
project: "backend"                            # опционально
cross_subjects: ["devops", "architecture"]    # кросс-теги (decision #4)
tags: ["asyncio", "best-practice"]            # свободные теги
chunk_size: 512                               # токенов/чанк XLM-RoBERTa (#13, #20)
version: 1                                    # optimistic locking (P2)
created_at: "2026-07-21T10:00:00+03:00"
updated_at: "2026-07-21T10:00:00+03:00"       # ← ключ reconciliation (#19)
---

# Асинхронные паттерны в Python

## Группы asyncio.Task                # ← граница чанка (XLM-RoBERTa-токенизация)
Контент секции...

## Корректное завершение              # ← граница чанка
Контент секции...
```

---

## 4. Фаза 0: Scaffolding и инфраструктура (Docker) — ✅ РЕАЛИЗОВАНО

> **Статус (2026-07-31):** ✅ Готово. См. [§0.2](#02-детализация-по-задачам-ф0ф1-реализованные). Ansible-роли (0.9) — структура есть, финальное наполнение в Ф3.
**Цель:** Подготовить проектный скелет, **Docker**-окружение и базовые health-checks, чтобы последующие фазы велись в воспроизводимой среде.

**Приоритет:** 🔴 BLOCKING (основа для всех остальных фаз)
**Трудозатраты:** ~12 ч *(v2.3: +4 ч — Ansible playbook #29; базовые 8 ч без изменений)*

### Задачи

| # | Задача | Детали | Файлы к созданию |
|---|--------|--------|------------------|
| 0.1 | Инициализация структуры (**2 репо #28**) | **Два отдельных git-репозитория:** `mcp-knowledge/` (код сервера) + `knowledge/` (Markdown SSOT — инициализируется `git init`). Каталоги `mcp_server/`, `knowledge/`, `data/`, `scripts/`, `ansible/`. См. §3 и §1.5 | дерево из §3 |
| 0.2 | `docker-compose.yml` | **2 сервиса** (qdrant, mcp-server); embedding-svc опционально (закомментирован). `healthcheck` + `depends_on: condition: service_healthy` + `restart: unless-stopped` (нативно в Docker). Запуск через `docker compose`. **🆕 v2.3 (#27):** volumes — bind mounts на локальный диск (НЕ SMB); см. §1.5 | `docker-compose.yml` |
| 0.3 | Dockerfile ×1 (+GPU-проверка) | `mcp_server/Dockerfile` (python:3.11-slim). Проверить GPU-доступ в Docker через **`--gpus all`** (nvidia-container-toolkit); задокументировать проброс в compose (`deploy.resources.reservations.devices`) | `mcp_server/Dockerfile` |
| 0.4 | Конфигурация (Settings) | `config.py` через pydantic-settings: `QDRANT_URL`, `MCP_READ_KEYS`/`MCP_WRITE_KEYS` (списки, #18), `EMBEDDING_BACKEND`, `GIT_AUDIT` (#21). **🆕 v2.2:** `WORKERS=1` — **инвариант**; валидация при старте: при `WORKERS != 1` → `ValueError` (in-memory состояние — `asyncio.Queue`, `sync_barrier`, in-process модель — не переживают >1 worker). Масштабирование — отдельное решение (внешняя очередь), **не** флаг запуска | `mcp_server/src/.../config.py` |
| 0.5 | `/health`-стабы | Минимальный health-эндпоинт на mcp-server: liveness (#10) | `health.py` |
| 0.6 | Makefile | `make dev` (up), `down`, `logs`, `test`, `lint`. Переменная `DOCKER_COMPOSE ?= docker compose`. **🆕 v2.2:** `make dlq-replay` — возврат задач из `data/dlq/` в очередь индексации (задача 2.11) | `Makefile` |
| 0.7 | `.env.example` | Шаблон env vars (`*_KEYS` через запятую, комментарии по ротации) | `.env.example` |
| 0.8 | CI-скелет | ruff + mypy + pytest на PR (базовые правила) | `.gitlab-ci.yml` |
| **0.9** | **🆕 v2.3 (#29): Ansible playbook** | `ansible/playbook.yml` + roles (`mcp_kb_dirs`, `mcp_kb_repos`, `mcp_kb_docker`, `mcp_kb_cron`). Создаёт `/opt/mcp-knowledge/` + bare-remote, клонирует репо, `docker compose up`, cron для backup/quality_scan. Secrets через **Ansible Vault** (`templates/.env.j2`). Покрывает §11.2 шаги 1–6 | `ansible/playbook.yml`, `ansible/inventory.yml`, `ansible/roles/`, `ansible/templates/.env.j2` |

### Критерии приёмки (ACCEPTANCE)

- ✅ `make dev` (= `docker compose up -d`) поднимает 2 сервиса, все переходят в `healthy` за ≤ 60 сек
- ✅ `curl localhost:8000/health` → `200 {"status":"healthy"}`
- ✅ `curl localhost:6333/healthz` (Qdrant native) → `200`
- ✅ `make lint` проходит без ошибок на пустых заготовках
- ✅ **GPU через `--gpus all` доступен** в контейнере mcp-server (проверка `nvidia-smi` при `EMBEDDING_BACKEND=gpu`), либо задокументирован fallback на CPU
- ✅ **🆕 v2.3 (#28):** `knowledge/` — отдельный git-репозиторий (`git rev-parse --git-dir` внутри `knowledge/` ≠ родительский `.git`)
- ✅ **🆕 v2.3 (#27):** Docker volumes — bind mounts на локальный диск (проверка: `mount | grep data/qdrant` → не сетевая ФС)
- ✅ **🆕 v2.3 (#29):** `ansible-playbook ansible/playbook.yml --check` (dry-run) проходит без ошибок

### Риски

| Риск | Вероятность | Mitigation |
|------|:-----------:|------------|
| GPU-драйверы/nvidia-container-toolkit недоступны в dev-окружении | Средняя | `EMBEDDING_BACKEND=cpu` по умолчанию в dev; GPU через `--gpus all` — опционально |
| Несовместимость версий Qdrant-клиента и сервера | Низкая | Пин до минорной версии после тестов |

---

## 5. Фаза 1: Ядро хранения и эмбеддингов (in-process) — ✅ РЕАЛИЗОВАНО (M1)

> **Статус (2026-07-31):** ✅ Готово (milestone M1). Все 11 задач выполнены: SSOT+git-aудит, Qdrant, BGE-M3 in-process, chunker XLM-R, async pipeline, INDEX.gen.yaml (#30/#32). Critic Gate §6-PHASE1 REVISE → P0/P1 исправлены.
**Цель:** Реализовать SSOT-хранилище Markdown (с git-аудитом), Qdrant-коллекцию, **in-process** embedding (GPU + CPU fallback), async-пайплайн с корректным токенайзер-чанкингом.

**Приоритет:** 🔴 HIGH (ядро всей системы)
**Трудозатраты:** ~30 ч *(v1: 24 ч; +2 ч — токенайзер XLM-R + git-аудит; in-process embedding нейтрален; 🆕 v3.0: +4 ч — INDEX.gen.yaml generation #30)*
**Решения:** #1, #2, #3, #4, #7, #8, #10 (health), #13, #17, #20, #21, **#30** (INDEX gen)

### Задачи

| # | Задача | Решение | Детали | Файлы |
|---|--------|:-------:|--------|-------|
| 1.1 | Markdown SSOT-хранилище + git-аудит | #2, #21 | Чтение/запись `.md` + парсинг YAML frontmatter. CRUD над `knowledge/{domain}/{subject}/{project}/`. Soft-delete → `knowledge/.trash/`. **После каждого write/update/delete — `git add && git commit`** (флаг `GIT_AUDIT=true`). **🆕 v2.2:** все git-операции (add/commit) обёрнуты в **`asyncio.Lock`** — два параллельных `write_knowledge` иначе дают гонку на `.git/index.lock`. Файл + git-коммит атомарны под локом. **Git-обслуживание:** `git gc --auto` встроен и срабатывает автоматически (~6700 loose objects); при >10K коммитов — `git gc --aggressive` через cron раз в месяц (опционально) | `storage/markdown_store.py` |
| 1.2 | Гибридная иерархия | #4 | Первичная классификация через путь директорий + `cross_subjects`/`cross_domains` в frontmatter | `storage/markdown_store.py`, `models.py` |
| 1.3 | Qdrant-коллекция | #1 | Создание коллекции `knowledge`: 1024d векторы, HNSW, payload-индексы по `domain`, `subject`, `project`, `tags`, `updated_at`. gRPC-клиент | `storage/qdrant_client.py`, `storage/schema.py` |
| 1.4 | Chunking (XLM-RoBERTa) | #13, #20 | Разбиение Markdown по `##` заголовкам, max **512 токенов BGE-M3 (XLM-RoBERTa)** на чанк (не приблизительно). Для русского текста коэффициент токен/слово выше — считать реальным токенайзером. Overlap=64–100 токенов. **🆕 v2.2:** 512 — выбор в пользу **качества retrieval** (precision/recall выше на коротких сфокусированных чанках), а не лимит BGE-M3 (модель держит контекст 8192) | `indexing/chunker.py`, `embedding/tokenizer.py` |
| 1.5 | In-process embedding: GPU backend | #3, #17 | BGE-M3 (1024d) через `sentence-transformers` на CUDA (через `--gpus all`, nvidia-container-toolkit). `embed(texts[]) → vectors[]` синхронный in-proc вызов. **🆕 v2.2:** синхронный `embed()` вызывается через **`loop.run_in_executor(thread_pool)`** — не блокирует event loop (критично на CPU-бэкенде, см. 1.6) | `embedding/gpu_backend.py`, `embedding/manager.py` |
| 1.6 | In-process embedding: CPU fallback | #8, #17 | BGE-M3 через **ONNX Runtime** (CPU). `EMBEDDING_BACKEND=auto` → пробует GPU, при отказе авто-деградация на CPU с WARN-логом. **🆕 v2.2:** синхронный `embed()` (600–1000 мс/чанк) — строго через **`loop.run_in_executor`**, иначе `search_knowledge` (тоже зовёт embed запроса) подвисает на CPU-бэкенде во время массового импорта | `embedding/cpu_backend.py`, `embedding/manager.py` |
| 1.7 | `/health` embedding-проверка | #10 | В `/health` mcp-server: модель загружена, backend активен (gpu/cpu), время отклика embed | `health.py` |
| 1.8 | Async-пайплайн индексации | #7 | `asyncio.Queue` + worker-корутины: Markdown-событие → chunk → embed (in-process) → Qdrant upsert. Backpressure (ограничение размера очереди, batching) | `indexing/pipeline.py` |
| 1.9 | Полный reindex | #7 | Обход `knowledge/**/*.md` → полная перестройка индекса. Сине-зелёное (P2): новый → swap → удалить старый | `indexing/pipeline.py` |
| 1.10 | Токенайзер-валидация (русский) | #20 | Unit-тесты: русский текст, проверка что чанк ≤ 512 XLM-R-токенов; сравнение с приблизительной оценкой | `tests/unit/test_tokenizer.py` |
| **1.11** | **🆕 v3.0/v3.1: INDEX.gen.yaml generation** | #30, #32 | `indexing/knowledge_index.py` (~250 строк). Build **root** INDEX.gen.yaml (≤4 KB: `sections[]` + `top-20 tags` + `how_to_orient` + `completeness%`) + **per-section** `_INDEX.gen.yaml` (≤8 KB: `files[]` + `domain_tags` + `gap_analysis` + adaptive L3 при >20 файлов). Секции — из структуры директорий `knowledge/`. Триггеры: (1) полная перестройка при reconciliation (старт, задача 2.9); (2) инкрементальное обновление затронутой секции при каждом write/update/delete. **Gap analysis** (#32): проверка frontmatter на обязательные поля (`domain`, `subject`, `tags`) → `top-5 missing` + `suggested_tags` + `coverage%`. **БЕЗ** внешнего `cascade.yaml`. **🆕 v3.1 P1-1 (cache-invalidation):** in-memory кэш распарсенного INDEX (`dict[str, dict]`) — после каждой перестройки (полной или инкрементальной) соответствующая запись **инвалидируется** (`del cache[section_key]`); `get_knowledge_map` (задача 2.14) читает из кэша, при промахе — ленивая загрузка с диска. **🆕 v3.1 P1-3 (INDEX size enforcement):** truncation-стратегия с жёсткими лимитами: root — `MAX_ROOT_TAGS=20` (top-N по frequency + счётчик «…and M more»); per-section — `MAX_SECTION_TAGS=30` (top-N + «…and M more»), `MAX_FILES_PER_SECTION=20` (при превышении → adaptive L3: группировка по `subject`/`project` подкатегориям); post-generation assert `size(root) ≤ 4096` и `size(section) ≤ 8192` — при нарушении WARN + автоматическое усечение. **🆕 v3.1 P1-4 (`suggested_tags` алгоритм):** (a) **frequency-based** — top-N (default 5) наиболее частых тегов в секции, которых **нет** у файла; (b) **path-based** — извлечение кандидатов-тегов из компонентов пути директории (`domain`, `subject`, `project` → нормализованные kebab-case). Объединение без дубликатов. **🆕 v3.1 P1-5 (structural change detection):** rename/move/delete директории → **полная перестройка root INDEX** + **обеих** затронутых per-section индексов (старое местоположение + новое); обнаружение через git diff `--name-status` (D/R/M) в reconciliation (задача 2.9) | `indexing/knowledge_index.py` |

### Контракт delete_entry (слепая зона B6)

- `delete_entry(id)`: Markdown → `knowledge/.trash/{id}.md` (soft-delete) + `git commit` (#21) + Qdrant `delete(payload.id==id)`.
- Восстановление: перемещение из `.trash/` + reindex.
- Физическое удаление `.trash/` — ручное (`make purge-trash`).

### Критерии приёмки

- ✅ `write_knowledge` создаёт `.md` с валидным frontmatter + **git-коммит** + ставит задачу в очередь
- ✅ Chunker разбивает документ на чанки ≤ 512 **XLM-RoBERTa-токенов** по границам `##` (проверено на русском тексте)
- ✅ `EMBEDDING_BACKEND=cpu` → запросы обрабатываются без GPU (latency зафиксирована)
- ✅ `EMBEDDING_BACKEND=auto` при недоступности GPU переключается на CPU с WARN-логом
- ✅ После `reindex()` Qdrant collection содержит векторы для каждого чанка каждого `.md`
- ✅ При потере Qdrant → `reindex()` полностью восстанавливает индекс из Markdown
- ✅ `/health` mcp-server возвращает `{"backend":"gpu\|cpu","model":"bge-m3","latency_ms":N}`
- ✅ **🆕 v2.2:** Unit-тест — два конкурентных `write_knowledge` не падают с гонкой на `.git/index.lock` (`asyncio.Lock` сериализует git-операции)
- ✅ **🆕 v2.2:** E2E — `search_knowledge` во время массового импорта на `EMBEDDING_BACKEND=cpu`: p95 ≤ 400 ms (синхронный `embed()` через `run_in_executor`, event loop не блокируется)
- ✅ **🆕 v3.0 (#30):** После `reindex()` генерируется root `INDEX.gen.yaml` (≤4 KB) + per-section `_INDEX.gen.yaml` (≤8 KB для каждой секции)
- ✅ **🆕 v3.0 (#30):** INDEX генерируется при reconciliation (старт) и инкрементально при каждом write/update/delete (затронутая секция обновляется)
- ✅ **🆕 v3.0 (#32):** Gap analysis: записи без `tags`/`domain`/`subject` → попадают в `top-5 missing` per-section INDEX; `coverage%` в root INDEX корректно отражает долю заполненного frontmatter
- ✅ **🆕 v3.0 (#30):** При потере INDEX.gen.yaml → полная перестройка из Markdown SSOT (как Qdrant recovery)
- ✅ **🆕 v3.0 (#30):** Adaptive L3: секция с >20 файлов → `_INDEX.gen.yaml` содержит сгруппированные подкатегории (L3 уровень)
- ✅ **🆕 v3.1 P1-3 (INDEX size enforcement):** root `INDEX.gen.yaml` всегда ≤ 4096 bytes, per-section `_INDEX.gen.yaml` всегда ≤ 8192 bytes (post-generation assert); при превышении тегов — top-N + «…and M more» счётчик; при превышении файлов — adaptive L3 группировка
- ✅ **🆕 v3.1 P1-4 (`suggested_tags` алгоритм):** для файла без тегов → `suggested_tags` содержит (a) top-5 частых тегов секции (frequency-based) + (b) теги из пути директории (path-based); без дубликатов
- ✅ **🆕 v3.1 P1-5 (structural change):** rename/move директории → root INDEX + **обе** затронутые per-section индексы перестроены; тест: `mv knowledge/engineering/python → knowledge/engineering/py` → оба `_INDEX.gen.yaml` (старый + новый) актуальны
- ✅ **🆕 v3.1 P1-1 (cache):** после каждой перестройки INDEX → соответствующая запись in-memory кэша инвалидирована; `get_knowledge_map` при промахе → ленивая загрузка с диска

### Метрики (EXPECTED) — CPU/GPU разделение (#2-ревизия)

| Метрика | GPU | CPU |
|---------|:---:|:---:|
| Время embed 1 чанка | < 100 ms | < 800 ms |
| Throughput индексации | ≥ 50 чанков/сек | ≥ 8 чанков/сек (batch ×8) |
| **Recovery из Markdown (10K записей)** | **< 30 мин** | **< 2 часов** |
| Overhead авто-деградации GPU→CPU | < 3 сек (detect + switch) | — |
| **🆕 v3.0: INDEX gen (10K записей)** | **< 30 сек** | **< 30 сек** |
| **🆕 v3.0: INDEX increment per write** | **< 100 ms** | **< 100 ms** |

> 🔶 **Правка v2 (#2):** метрика recovery разделена. Старое «10K < 30 мин» нереалистично для CPU: 10K × 5 чанков = 50K чанков × 800 мс ≈ 11 ч без батчинга, ~1.5 ч с батчингом ×8. Теперь: **GPU < 30 мин / CPU < 2 часов.**

### Риски

| Риск | Вероятность | Severity | Mitigation |
|------|:-----------:|:--------:|------------|
| BGE-M3 на ONNX даёт другие векторы, чем sentence-transformers | Средняя | 🟠 | Тест косинусного сходства GPU/CPU (порог ≥ 0.98); при расхождении — отдельная коллекция/перевекторизация |
| Приблизительный подсчёт токенов ломает границу 512 (#20) | Средняя | 🟡 | Считать токены **XLM-RoBERTa** реально; unit-тесты на русском тексте (задача 1.10) |
| Chunking ломает семантику секций | Средняя | 🟡 | Overlap=64–100 токенов на границах; сохранять заголовок секции в каждом чанке |
| Очередь переполняется при массовом импорте | Низкая | 🟡 | Backpressure: ограничить `asyncio.Queue`, batching embed (≥16 чанков) |
| Git-коммиты замедляют массовый импорт | Средняя | 🟡 | Батч-коммит (1 commit на N записей); `GIT_AUDIT=false` отключает |

---

## 6. Фаза 2: MCP-сервер, протокол и устойчивость — ❌ НЕ НАЧАТО (СЛЕДУЮЩАЯ)

> **Статус (2026-07-31):** ❌ Не реализовано. Нет `auth.py`, пакета `tools/` (9 MCP Tools), `resources.py`, `prompts.py`, `metrics.py`, `reconcile.py`. **Детальный план:** [`02-phase2-mcp-server.md`](02-phase2-mcp-server.md) (4 блока B→A→C→D, 15 задач, ~37 ч) + [`.board.md`](.board.md:1) (Critic Gate REVISE 0.58 → 6 фиксов применены). e2e-тест на русском — пустой docstring.
**Цель:** Реализовать полный MCP-интерфейс (**9 Tools** + Resources + Prompts), **мульти-ключевую** аутентификацию, sync-флаг (с CPU-ограничением), Dead Letter Queue, **reconciliation при старте**, базовый Prometheus-мониторинг и **🆕 v3.0: структурную навигацию** (`get_knowledge_map` + `search_by_tags`).

**Приоритет:** 🔴 HIGH (пользовательский контракт)
**Трудозатраты:** ~37 ч *(v1: 28 ч; +4 reconciliation #19, +3 Prometheus из Ф3, +1 sync-CPU доки; 🆕 v3.0: +3 ч — get_knowledge_map + search_by_tags #31)*
**Решения:** #5, #6, #7 (sync), #9, #14, #18, #19, **#30** (INDEX gen), **#31** (новые Tools)

### 6.1 Девять MCP Tools

| # | Tool | Сигнатура | Права | Решение |
|---|------|-----------|:-----:|:-------:|
| 1 | `search_knowledge` | `(query: str, filters?: {domain?, subject?, project?, tags?[]}, top_k?: int=5)` → `SearchResult[]` | read | #5 |
| 2 | `get_entry` | `(knowledge_id: str)` → `Entry` (полный текст + frontmatter) | read | #5 |
| 3 | `write_knowledge` | `(content, domain, subject, project?, cross_subjects?, tags?, wait_for_index?: bool=false)` → `{knowledge_id, indexed: bool, pending?: bool}` | write | #5, #9 |
| 4 | `update_entry` | `(knowledge_id, content?, metadata?)` → `Entry` | write | #5 |
| 5 | `delete_entry` | `(knowledge_id: str)` → `{deleted: true}` (soft-delete + Qdrant delete + git commit) | write | #5, B6, #21 |
| 6 | `list_domains` / `list_subjects` / `list_projects` | pagination | read | #5 |
| 7 | `reindex` | `()` → `{status, chunks_indexed}` (сине-зелёное) | write | #5 |
| **8** | **🆕 v3.0/v3.1 `get_knowledge_map`** | `(domain?: str)` → `KnowledgeMap` (INDEX.gen.yaml: sections + tags + completeness); **🆕 v3.1:** read-after-write consistent (cache-invalidation, P1-1) | read | #30, #31 |
| **9** | **🆕 v3.0/v3.1 `search_by_tags`** | `(tags: str[], match?: "all"\|"any" = "all", limit?: int = 500)` → `{results: Entry[], truncated: bool, total_count: int}` (exhaustive, Qdrant payload filter, без GPU; **🆕 v3.1 P1-2:** bounded results) | read | #31 |

### 6.2 Задачи

| # | Задача | Решение | Детали | Файлы |
|---|--------|:-------:|--------|-------|
| 2.1 | fastmcp интеграция | #5 | FastAPI + MCP JSON-RPC эндпоинт. Регистрация Tools/Resources/Prompts | `main.py`, `mcp/tools.py` |
| 2.2 | search_knowledge | #5 | Query → embed (in-process) → Qdrant `search` с payload-filter + top_k | `mcp/tools.py` |
| 2.3 | get_entry | #5 | Чтение полного Markdown из SSOT по `knowledge_id` | `mcp/tools.py` |
| 2.4 | write_knowledge + sync-флаг (CPU-aware) | #5, #9 | Запись `.md` + git commit (#21) + очередь. При `wait_for_index=true`: **GPU** — блок до upsert (≤5 сек таймаут); **CPU** — при риске >5 сек вернуть `{indexed:false, pending:true}` и приоритизировать в очереди | `mcp/tools.py`, `indexing/sync_barrier.py` |
| 2.5 | update_entry / delete_entry | #5, B6, #21 | Обновление frontmatter/контента + реиндексация + git commit; delete → soft-delete | `mcp/tools.py` |
| 2.6 | list_domains/subjects/projects | #5, B5 | Агрегация по payload + **pagination** + max-лимиты | `mcp/tools.py` |
| 2.7 | reindex | #5 | Запуск полного перестроения (сине-зелёное: новый индекс → swap) | `mcp/tools.py` |
| 2.8 | Мульти-ключевая аутентификация | #6, #18 | `MCP_READ_KEYS`/`MCP_WRITE_KEYS` — списки через запятую. Header `X-API-Key`. Ключ принимается, если входит в соответствующий список. **Graceful rotation:** добавить новый → перевести клиентов → убрать старый | `auth.py` |
| 2.9 | **🆕 Reconciliation при старте** | #19 | При старте mcp-server: обход `knowledge/**/*.md` → сравнение `updated_at` из frontmatter с payload Qdrant. Расхождения (новее в Markdown / отсутствует в Qdrant) → доиндексация. Лог: `{checked, reindexed, skipped}`. Покрывает потерю in-memory очереди при рестарте. **🆕 v2.2 (обратная сверка):** точки Qdrant без соответствующего `.md` (сироты от ручного удаления файлов вне MCP) → **delete** из Qdrant. Лог дополняется: `{checked, reindexed, skipped, deleted_orphans}` | `indexing/reconcile.py` |
| 2.10 | Sync-флаг барьер (CPU-aware) | #9 | `sync_barrier.py`: `asyncio.Event` + task_id; worker сигнализирует об upsert → резолв. На CPU при таймауте — `pending:true` + приоритет очереди | `indexing/sync_barrier.py` |
| 2.11 | Dead Letter Queue | #14 | При ошибке embed/upsert → retry (backoff 1с,4с,16с) ×3 → DLQ (`data/dlq/`) + алерт; метрика `dlq_size`. **🆕 v2.2:** путь возврата — `make dlq-replay` перекладывает задачи из `data/dlq/` обратно в очередь индексации (Makefile, задача 0.6) | `indexing/dlq.py` |
| 2.12 | MCP Resources + Prompts | #5 | Resources: `kb://{domain}/{subject}`; Prompts: «как структурировать знание», «best-practice для write_knowledge» | `mcp/resources.py`, `mcp/prompts.py` |
| **2.13** | **🆕 Prometheus-метрики** (#9-ревизии) | P1 | `/metrics`: `queue_size`, `dlq_size`, latency p50/p95/p99, `embed_backend`, `collection_size`, `reconcile_*`, **🆕 v3.0: `index_gen_*`, `knowledge_map_latency`, `tag_search_latency`**. Перенесено из Фазы 3 — нужно для отладки пайплайна уже на MVP | `metrics.py` |
| **2.14** | **🆕 v3.0/v3.1: `get_knowledge_map` tool** | #30, #31 | `(domain?: str)` → возврат root INDEX.gen.yaml (при `domain` опущен) или per-section `_INDEX.gen.yaml` (при `domain` указан). Структурная карта: `sections[]`, `top-20 tags`, `how_to_orient`, `completeness%`. **Без GPU** — чтение in-memory INDEX (p95 < 5 ms). Агент получает полную ориентацию в базе за 1 вызов вместо 3–4 `list_*`. **🆕 v3.1 P1-1 (read-after-write consistency):** in-memory кэш парсинга INDEX (см. задача 1.11) — `get_knowledge_map` всегда возвращает **свежие** данные, т.к. write/update/delete триггерят инкрементальное обновление + инвалидацию кэша затронутой секции **до** возврата из write-операции. Гарантия: `write_knowledge` → `get_knowledge_map` (без задержки) → новая запись видна. TTL на кэш = 300 сек как safety-net (случай ручного редактирования `.md` вне MCP) | `mcp/tools.py`, `indexing/knowledge_index.py` |
| **2.15** | **🆕 v3.0/v3.1: `search_by_tags` tool** | #31 | `(tags: str[], match?: "all"\|"any", limit?: int = 500)` → exhaustive поиск через **Qdrant payload filter** по полю `tags[]`. `match="all"` → AND (все теги), `match="any"` → OR (любой). **🆕 v3.1 P1-2 (bounded results):** параметр `limit` (default 500, hard max 1000) — возврат `{results: Entry[], truncated: bool, total_count: int}`. При `len(results) == limit` → `truncated=true` + WARN-лог: `search_by_tags hit limit=N, total_count=M; consider narrowing tags`. **Без GPU** — без embed, чистый metadata filter (p95 < 50 ms). Решает слепую зону SB1: `search_knowledge(top_k=5)` ≠ exhaustive | `mcp/tools.py` |

### 6.3 Модель аутентификации — мульти-ключи (#18)

```
Запрос ──▶ Header: X-API-Key: <ключ> ──▶ auth.verify_key(key, required_scope)
                                              │
              ┌───────────────────────────────┴───────────────────────────────┐
              ▼                                                                ▼
       key ∈ MCP_READ_KEYS[]                                       key ∈ MCP_WRITE_KEYS[]
       scope: READ (search/get/list)                               scope: READ + WRITE
              │                                                                │
       read-tools: ✅                                             все-tools: ✅
       write-tools: ❌ 403
```

> 🔶 **Правка v2 (#4):** одиночные `MCP_READ_KEY`/`MCP_WRITE_KEY` заменены на списки `*_KEYS`. Ротация без downtime: добавить новый ключ в список → перезапуск → перевести клиентов → убрать старый из списка → перезапуск.

### Критерии приёмки

- ✅ Все **9 Tools** отвечают по MCP JSON-RPC с валидными схемами входов/выходов
- ✅ **E2E на русском корпусе (#10):** `search_knowledge("асинхронные паттерны python", domain="engineering")` возвращает релевантные чанки со score
- ✅ `write_knowledge(..., wait_for_index=true)` на **GPU** → последующий `search_knowledge` находит запись (≤ 5 сек)
- ✅ `write_knowledge(..., wait_for_index=true)` на **CPU** для большого документа (>20 чанков) → `{indexed:false, pending:true}` + приоритет в очереди
- ✅ `write_knowledge(..., wait_for_index=false)` → немедленный возврат, индексация в фоне
- ✅ read-key отклоняет `write_knowledge` с `403 Forbidden`; **второй read-key** в списке также работает (мульти)
- ✅ **Reconciliation:** рестарт mcp-server при «потерянной» задаче (Markdown записан, Qdrant нет) → при старте доиндексируется, поиск находит запись
- ✅ MCP Resources корректно отображают дерево `kb://{domain}/{subject}`
- ✅ DLQ: при намеренной остановке embed (in-process: принудительный CPU-режим + throttle), 3 retry → задача в `data/dlq/` + алерт
- ✅ `list_*` срабатывает с pagination при > 1000 записей
- ✅ `/metrics` отдаёт Prometheus-формат (`queue_size`, `dlq_size`, …)
- ✅ **🆕 v2.2:** mcp-server запускается строго с **1 uvicorn worker** (`WORKERS=1`); инвариант задокументирован — in-memory состояние (`asyncio.Queue`, `sync_barrier`, in-process модель) не переживает >1 worker. Масштабирование — внешняя очередь (future), не флаг запуска
- ✅ **🆕 v2.2:** Reconciliation удаляет **сироты** — Qdrant-точки без `.md` (после ручного удаления файла) → `delete`, лог содержит `deleted_orphans`
- ✅ **🆕 v3.0:** `get_knowledge_map()` без аргументов → root INDEX.gen.yaml (sections + top-20 tags + completeness%); `get_knowledge_map(domain="engineering")` → per-section `_INDEX.gen.yaml`
- ✅ **🆕 v3.0:** `search_by_tags(["docker", "best-practice"], match="all")` → все записи с ОБА тегами (exhaustive, без top-K лимита); `match="any"` → OR
- ✅ **🆕 v3.0:** `search_by_tags` и `get_knowledge_map` работают на **CPU-only** (без GPU, без embed) — p95 < 50 ms и < 5 ms соответственно
- ✅ **🆕 v3.0:** `write_knowledge` без `tags` → WARN в логе (gap analysis #32); `get_knowledge_map()` → `coverage%` показывает долю записей с заполненным frontmatter
- ✅ **🆕 v3.0:** После `write_knowledge` → INDEX.gen.yaml обновляется (затронутая секция инкрементально перестроена)
- ✅ **🆕 v3.1 P1-1 (read-after-write):** `write_knowledge(...)` → немедленный `get_knowledge_map(domain=...)` → новая запись **видна** (без задержки; кэш инвалидирован синхронно с write)
- ✅ **🆕 v3.1 P1-2 (bounded search):** `search_by_tags(["common-tag"])` при >500 совпадениях → `truncated:true` + `total_count=N` + WARN-лог о достижении лимита; `limit=10` → ровно 10 результатов + `truncated:true`

### Метрики

| Метрика | Целевое значение |
|---------|------------------|
| Latency `search_knowledge` (p95) | < 150 ms (без embed) / < 300 ms (с embed) |
| Latency `get_entry` (p95) | < 30 ms |
| Sync-флаг overhead (GPU, p99) | ≤ 5 сек |
| Sync-флаг на CPU (>20 чанков) | `pending:true` (не блокирует >5 сек) |
| Reconciliation (10K записей) | < 5 мин (только сверка, без доиндексации) |
| DLQ попадание при healthy-системе | 0 |
| Покрытие тестами MCP Tools | ≥ 90% |
| **🆕 v3.0: Latency `get_knowledge_map` (p95)** | **< 5 ms** (in-memory read, без GPU) |
| **🆕 v3.0: Latency `search_by_tags` (p95)** | **< 50 ms** (Qdrant payload filter, без GPU) |
| **🆕 v3.0: INDEX gen overhead per write** | **< 100 ms** (инкрементальное обновление секции) |

### Риски

| Риск | Вероятность | Severity | Mitigation |
|------|:-----------:|:--------:|------------|
| Sync-флаг висит дольше 5 сек | Средняя | 🟠 | На **GPU**: жёсткий таймаут 5 сек → `pending:true`. На **CPU** (#6-ревизии): `pending:true` по умолчанию для больших документов + приоритет очереди |
| In-memory очередь теряется при рестарте | Высокая | 🔴 | **Reconciliation при старте (#19)**: сверка Markdown `updated_at` ↔ Qdrant payload |
| API-key утёк в логи | Низкая | 🔴 | Маскирование `X-API-Key`, только хеш в аудит-логе |
| DLQ переполняется при отказе embed | Средняя | 🟠 | Алерт при DLQ > 10; метрика `dlq_size` в Prometheus (#2.13) |
| Конкурентная запись в один `knowledge_id` | Низкая | 🟡 | P2 optimistic locking через `version`; для MVP — last-write-wins + WARN-лог |

---

## 7. Фаза 3: Production-готовность и Air-gap (Docker) — 🟡 ЧАСТИЧНО

> **Статус (2026-07-31):** 🟡 Скрипты готовы: [`backup.sh`](scripts/backup.sh:1) (Qdrant+SSOT), [`offline-deploy.sh`](scripts/offline-deploy.sh:1) (prepare/deploy/verify), [`reindex.sh`](scripts/reindex.sh:1). Остаток (зависит от Ф2): rate limiting (3.5), сине-зелёный reindex (3.9), conflict resolution (3.10), runbook ротации мульти-ключей (3.4), валидация pre-download моделей (3.7).
**Цель:** Довести систему до production-стандарта: резервное копирование, завершение ротации ключей, rate limiting, air-gap-развёртывание на Docker.

**Приоритет:** 🟠 MEDIUM (после работающего MVP)
**Трудозатраты:** ~17 ч *(v1: 20 ч; −3 Prometheus перенесён в Ф2)*
**Решения:** #10 (hardening), #11, #12, #15, #18 (завершение ротации) + P2

### Задачи

| # | Задача | Решение | Детали | Файлы |
|---|--------|:-------:|--------|-------|
| 3.1 | Health checks hardening (docker compose) | #10 | `/health` → проверка зависимостей (mcp-server → qdrant reachable). `docker-compose.yml`: `healthcheck` + `restart: unless-stopped` + `depends_on: condition: service_healthy` — работает в Docker нативно (без отдельной валидации) | `docker-compose.yml`, `health.py` |
| 3.2 | Бэкап (Qdrant + SSOT) | #11 | `POST /collections/knowledge/snapshots` → `data/qdrant/snapshots/` + ротация (хранить N последних). **🆕 v2.2:** SSOT (Markdown = источник правды) тоже бэкапится — `git push` на bare-remote (`origin`) **либо** `tar knowledge/` → `data/backups/knowledge-YYYYMMDD.tar.gz` + ротация. Скрипт переименован `backup_qdrant.sh` → **`backup.sh`** (теперь бэкапит и вычисляемый индекс, и SSOT). Cron через `scripts/backup.sh` | `scripts/backup.sh` |
| 3.3 | Восстановление из snapshot | #11 | `PUT /collections/knowledge/snapshots/{id}` → restore; runbook | `scripts/`, `README.md` |
| 3.4 | Завершение ротации ключей | #12, #18 | Runbook ротации мульти-ключей: добавить новый в `*_KEYS` → restart → перевести клиентов → убрать старый → restart. Аудит-лог хешей ключей | `auth.py`, `README.md` |
| 3.5 | Rate limiting (P2) | P2 | Token bucket: read 100 req/min, write 20 req/min на ключ. 429 при превышении | `auth.py` / middleware |
| 3.6 | Air-gap playbook (Docker) | #15 | `scripts/offline-deploy.sh`: `docker save` образов, `pip download` (wheelhouse), pre-download BGE-M3 (sentence-transformers + ONNX + tokenizer.json), checksum, `docker load` на целевом хосте | `scripts/offline-deploy.sh` |
| 3.7 | Pre-download моделей | #15 | Скрипт скачивания BGE-M3 (включая `tokenizer.json`, `vocab`) в `models_cache/`, volume | `embedding/models_cache.py` |
| 3.9 | Сине-зелёный reindex (P2) | P2 | `reindex()` → `knowledge_v2` → alias swap → удаление старой; поиск не прерывается | `indexing/pipeline.py` |
| 3.10 | Conflict resolution (P2) | P2 | `version` в frontmatter, optimistic locking при `update_entry` (409 Conflict) | `mcp/tools.py`, `models.py` |

### Критерии приёмки

- ✅ Падение embed (in-process: принудительный CPU-throttle/ошибка) → mcp-server `/health` деградирует (503), read-операции из Qdrant продолжают работать
- ✅ **docker compose:** `docker compose up -d` поднимает стек; `restart: unless-stopped` поднимает контейнер при падении; `docker compose logs` показывает логи
- ✅ `scripts/backup.sh` создаёт snapshot Qdrant **и** бэкапит SSOT (`git push`/`tar knowledge/`); restore восстанавливает коллекцию, Markdown-репозиторий восстанавливается из bare-remote/tar
- ✅ **Ротация мульти-ключей:** добавление нового ключа в `*_KEYS` + restart → старый И новый работают; удаление старого → только новый
- ✅ Rate limit: 101-й read-запрос/мин → 429
- ✅ Air-gap: `offline-deploy.sh` собирает артефакты; `docker load` + `pip install --no-index` на изолированном хосте → система запускается
- ✅ BGE-M3 модель доступна офлайн (не обращается в интернет при старте)
- ✅ Сине-зелёный reindex: `search_knowledge` отвечает во время перестроения

### Метрики

| Метрика | Целевое значение |
|---------|------------------|
| Время создания snapshot (10K записей) | < 60 сек |
| Время восстановления из snapshot | < 90 сек |
| Перезапуск при ротации ключей | 0 сек downtime (overlap мульти-ключей) |
| Размер air-gap-артефактов (образы + модели) | < 8 ГБ |
| Время холодного старта в air-gap (без интернет) | < 90 сек |

### Риски

| Риск | Вероятность | Severity | Mitigation |
|------|:-----------:|:--------:|------------|
| Air-gap: BGE-M3 требует онлайн-загрузку токенайзера | Средняя | 🟠 | Pre-download ВСЕХ артефактов (`tokenizer.json`, `vocab`); `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` |
| Snapshot Qdrant несовместим после апгрейда версии | Низкая | 🟠 | Пин версии Qdrant; при апгрейде — полный reindex из Markdown (SSOT) |

---

## 8. Сводная оценка трудозатрат

| Фаза | Объём | Ч/ч | Ч/д (8ч) | Зависимости | Δ от v1 |
|------|-------|:---:|:--------:|-------------|:-------:|
| **Фаза 0:** Scaffolding (Docker + **Ansible #29**) | **9 задач** | **12** | 1.5 | — | **+4 (v2.3 Ansible)** |
| **Фаза 1:** Ядро (in-process emb + **INDEX gen #30**) | **11 задач** | **31** | 3.9 | Фаза 0 | +7 (v2:+2, v3.0:+4, **v3.1:+1**) |
| **Фаза 2:** MCP + устойчивость (**+map/tags #31**) | **15 задач** | **38** | 4.8 | Фаза 1 | +10 (v2:+6, v3.0:+3, **v3.1:+1**) |
| **Фаза 3:** Production + Air-gap (Docker) | 9 задач | 17 | 2.1 | Фаза 2 | −3 |
| **Фаза 4:** Knowledge Quality ([04](04-phase4-knowledge-quality.md)) | 10 задач | 40 | 5.0 | Фазы 0–3 (#19,#21,#2.13) | — (v1.0) |
| Тестирование (unit + integration + E2E рус., Ф0–Ф3) | сквозное | 17 | 2.1 | распределяется | +1 |
| Документация (README, runbook, Ф0–Ф3) | сквозное | 7 | 0.9 | распределяется | +1 |
| **ИТОГО Ф0–Ф3** (MVP → Production M3) | | **122** | **≈ 15.3** | | **+20 vs v1 (102)** |
| **ИТОГО с Фазой 4** (базовый) | | **162** | **≈ 20.3** | | — |

> **Резерв на непредвиденное (contingency 20%):** Ф0–Ф3: +24 ч → **~146 ч / ~18 ч/д**; с Фазой 4: +32 ч → **~194 ч / ~24 ч/д**
>
> **🆕 v3.1 (+2 ч, post-Critic Gate):** уточнены детали существующих задач (без новых фаз): P1-1 cache-invalidation (задачи 1.11/2.14), P1-2 limit+truncated (задача 2.15), P1-3 INDEX size enforcement truncation (задача 1.11), P1-4 suggested_tags алгоритм (задача 1.11), P1-5 structural change INDEX update (задача 1.11). Ф1: 30→31 ч (+1), Ф2: 37→38 ч (+1), Ф0–Ф3: 120→122 ч. 4 новых риска R14–R17 (§10.4). Покрытие: §13.7.
>
> **🆕 v3.0 (+7 ч):** добавлены INDEX.gen.yaml generation (задача 1.11, #30, +4 ч), `get_knowledge_map` + `search_by_tags` tools (задачи 2.14/2.15, #31, +3 ч). Ф1: 26→30 ч, Ф2: 34→37 ч, Ф0–Ф3: 113→120 ч. Gap analysis (#32) интегрирован в задачу 1.11 (без отдельной оценки — часть INDEX generation).
>
> **🆕 v2.3 (+4 ч):** добавлен Ansible playbook (задача 0.9, #29) — повторяемое развёртывание на сервере (dirs → repos → compose → cron, Vault для secrets). Ф0: 8→12 ч, Ф0–Ф3: 109→113 ч.
>
> **Почему v2.1 (−10 ч vs v2.0):** внешний рецензент ошибочно предположил окружение Podman; оператор подтвердил стек команды — **Docker**. Убраны валидация Podman/Quadlet (−5 ч, бывшая задача 0.9) и Quadlet для production (−5 ч, бывшая задача 3.8). Docker поддерживает `healthcheck` + `restart: unless-stopped` + `depends_on: condition: service_healthy` и `--gpus all` нативно — отдельная валидация не требуется. Сохранены все остальные правки v2 (#17–#21, разделённые CPU/GPU-метрики, reconciliation, Prometheus в Фазе 2, кириллические тесты, все P0/P1/P2 из Critic Gate).
>
> **Рекомендуемая команда:** 1 backend-разработчик (полная загрузка) + эпизодический DevOps (Docker, air-gap, backup). При параллельной работе 2 разработчиков — ~9–10 ч/д календарных.

---

## 9. Критерии успеха проекта

### 9.1 Общие (Definition of Done)

- [ ] **2 сервиса** поднимаются `make dev` (`docker compose up -d`) и переходят в `healthy`
- [ ] Все **26 архитектурных решений** реализованы и подтверждены (см. §13 матрицу)
- [ ] Markdown SSOT → Qdrant recovery работает (демонстрация: удалить коллекцию → `reindex()` → поиск работает)
- [ ] **Reconciliation:** рестарт при «потерянной» задаче → доиндексация (#19)
- [ ] **9 MCP Tools** покрыты **E2E-тестами на русском корпусе** через JSON-RPC (#10)
- [ ] API-key read/write разделение + **мульти-ключи** работают (тест: read-key → write-tool = 403; второй ключ в списке принимается)
- [ ] CPU fallback демонстрируется (`EMBEDDING_BACKEND=cpu` → система работает)
- [ ] **Git-аудит:** после write/update/delete — коммит в истории знаний (#21)
- [ ] Air-gap playbook пройден на изолированном хосте end-to-end (Docker)
- [ ] **🆕 v3.0:** INDEX.gen.yaml генерируется при старте (root + per-section) и обновляется при каждом write/update/delete (#30)
- [ ] **🆕 v3.0:** `get_knowledge_map()` возвращает структурную карту (sections + tags + completeness) за < 5 ms (#31)
- [ ] **🆕 v3.0:** `search_by_tags(["docker"], match="all")` возвращает **все** записи с этим тегом (exhaustive, без GPU) (#31)
- [ ] **🆕 v3.0:** Gap analysis: `write_knowledge` без `tags` → WARN; `get_knowledge_map()` показывает `coverage%` (#32)
- [ ] **🆕 v3.1 P1-1:** `get_knowledge_map` read-after-write consistent — после `write_knowledge` новая запись видна без задержки (cache-invalidation синхронна с write)
- [ ] **🆕 v3.1 P1-2:** `search_by_tags` при >500 совпадениях → `truncated:true` + WARN; `limit` параметр работает
- [ ] **🆕 v3.1 P1-3:** INDEX.gen.yaml всегда в пределах лимитов (≤4KB root, ≤8KB per-section) — post-generation assert проходит
- [ ] **🆕 v3.1 P1-5:** rename/move директории → root INDEX + обе затронутые per-section перестроены

### 9.2 Пороговые метрики (SUCCESS THRESHOLD) — CPU/GPU

| Метрика | GPU | CPU |
|---------|:---:|:---:|
| Latency search (p95) | ≤ 300 ms | ≤ 400 ms |
| Latency get_entry (p95) | ≤ 30 ms | ≤ 30 ms |
| Sync-write доступность (p99) | ≤ 5 сек | `pending:true` (≤5 сек возврат) |
| **Recovery из Markdown (10K)** | **≤ 30 мин** | **≤ 2 часов** |
| Доступность при отказе GPU | 100% (через CPU fallback) | — |
| Покрытие тестами ядра | ≥ 90% | ≥ 90% |
| DLQ при healthy-системе | 0 задач | 0 задач |
| **🆕 v3.0: Latency `get_knowledge_map` (p95)** | **< 5 ms** | **< 5 ms** |
| **🆕 v3.0: Latency `search_by_tags` (p95)** | **< 50 ms** | **< 50 ms** |
| **🆕 v3.0: Recovery INDEX.gen.yaml (10K)** | **≤ 30 сек** | **≤ 30 сек** |

### 9.3 Граничные условия (BOUNDARY CONDITIONS)

- **Язык контента:** преимущественно **русский**; BGE-M3 мультиязычный. Токенизация chunker — XLM-RoBERTa (#20). E2E-тесты на русском (#10)
- **Объём:** целевой MVP — до 50K записей; Qdrant масштабируется до миллионов
- **Графика:** GPU опционален; система полностью функциональна на CPU (медленнее). Sync-режим ≤5 сек — **GPU-only гарантия**; на CPU — `pending`
- **Сеть:** air-gap — без обращения в интернет при runtime; онлайн нужен только для подготовки артефактов
- **Runtime:** **Docker** на Debian; dev и production — `docker compose` (`restart: unless-stopped` + `depends_on: condition: service_healthy`), GPU через `--gpus all` (nvidia-container-toolkit)

---

## 10. Реестр рисков и mitigation

### 10.1 Критические риски (severity 🔴)

| ID | Риск | Источник | Вероятность | Mitigation | Фаза |
|----|------|----------|:-----------:|------------|:----:|
| R1 | SPOF embedding → отказ GPU = отказ всей системы | Критика S1 | Низкая (после #8) | **CPU fallback ONNX** (#8), in-process авто-деградация (#17) | 1 |
| R2 | Async write→read окно 1-5 сек ломает LLM-агентов | Критика C1 | Средняя | **Sync-флаг** `wait_for_index=true` (GPU ≤5с; CPU → `pending`) (#9) | 2 |
| R3 | GPU-драйверы/CUDA/`--gpus` в air-gap = сложность развёртывания | Критика C2 | Высокая | **CPU fallback + offline playbook** (#8, #15); проверка `--gpus` (0.3) | 3 |
| R4 | Нет бэкапа Qdrant → reindex 100K = часы простоя | Критика B2 | Средняя | **Qdrant Snapshots** (#11) | 3 |
| R5 | BGE-M3 ONNX ≠ sentence-transformers по векторам | Низкая (R) | Средняя | Тест косинусного сходства ≥ 0.98; документация | 1 |
| **R6** | **In-memory очередь теряется при рестарте mcp-server** | **Рецензия v2 #3** | **Высокая** | **Reconciliation при старте (#19)**: сверка Markdown `updated_at` ↔ Qdrant | 2 |

### 10.2 Операционные риски (severity 🟠)

| ID | Риск | Mitigation | Фаза |
|----|------|------------|:----:|
| R8 | Падение сервиса незаметно | Health checks + `restart: unless-stopped` (#10) | 0/3 |
| R9 | Очередь переполняется при массовом импорте | Backpressure + batching | 1 |
| R10 | API-key утёк | Маскирование логов + мульти-ключевая ротация (#12, #18) | 2/3 |
| R11 | Конкурентная запись теряет данные | Optimistic locking `version` (P2) | 3 |
| R12 | DoS через search_knowledge | Rate limiting token bucket (P2) | 3 |
| **R13** | Приблизительный подсчёт токенов ломает границу 512 | Реальный токенайзер XLM-RoBERTa (#20) | 1 |

### 10.3 Матрица SPOF → Mitigation

| SPOF | Severity | Решение | Статус |
|------|:--------:|---------|:------:|
| S1: embedding (GPU) | 🔴 CRIT | CPU fallback ONNX (#8) + in-process (#17) | ✅ покрытие |
| S2: qdrant | 🟠 HIGH | Snapshots (#11) + Markdown SSOT recovery + Reconciliation (#19) | ✅ покрытие |
| S3: mcp-server | 🟡 MEDIUM | `restart: unless-stopped` + healthcheck (#10) | ✅ покрытие |
| S4: GPU физический | 🟠 HIGH | CPU fallback + мониторинг GPU health | ✅ покрытие |

### 10.4 Hybrid Search риски (v3.1 P1, Critic Gate PASS 0.84)

| ID | Риск | Источник | Вероятность | Severity | Mitigation | Фаза |
|----|------|----------|:-----------:|:--------:|------------|:----:|
| R14 | INDEX.gen.yaml corruption (битый YAML, partial write при crash) | Critic P1-6 | Низкая | 🔴 | Markdown SSOT = источник правды → `reindex()` полная перестройка (как Qdrant recovery, §1.3); assert `size ≤ 4KB/8KB` (задача 1.11 P1-3); кэш инвалидирован → ленивая загрузка с диска | 1 |
| R15 | `search_by_tags` DoS — unbounded запрос возвращает 100K+ записей, OOM | Critic P1-6 | Средняя | 🟠 | `limit=500` (hard max 1000) + `truncated:true` + WARN-лог (задача 2.15, P1-2); rate limiting token bucket (P2, задача 3.5) | 2/3 |
| R16 | INDEX size overflow на больших секциях (>100 файлов, >50 тегов) | Critic P1-6 | Средняя | 🟠 | Adaptive L3 truncation: `MAX_ROOT_TAGS=20`, `MAX_SECTION_TAGS=30`, `MAX_FILES_PER_SECTION=20` (задача 1.11, P1-3); top-N + счётчик «…and M more»; жёсткий assert `≤4KB/≤8KB` с auto-усечением | 1 |
| R17 | INDEX cache staleness — ручное редактирование `.md` без MCP → устаревший кэш | Critic P1-6 | Низкая | 🟠 | TTL=300 сек на кэш (safety-net) + write-invalidation при MCP write/update/delete (задача 1.11/2.14, P1-1); reconciliation при старте (задача 2.9) перестраивает INDEX полностью | 1/2 |

---

## 11. Air-gap Deployment Checklist (Docker)

> **Цель:** Развёртывание на изолированном хосте без доступа в интернет (decision #15).

### 11.1 Подготовка (на машине с интернетом)

- [ ] **1. Образы.** `docker pull` для всех образов (qdrant, mcp-server) → `docker save` в `artifacts/images/*.tar`
- [ ] **2. Python-зависимости.** `pip download -r requirements.txt -d artifacts/wheelhouse/` для mcp-server (+ опц. embedding_svc)
- [ ] **3. Модель BGE-M3.** Pre-download в `artifacts/models/bge-m3/`:
  - [ ] 3a. sentence-transformers версия (PyTorch-веса)
  - [ ] 3b. ONNX Runtime версия (CPU fallback)
  - [ ] 3c. `tokenizer.json`, `vocab.txt`, `config.json` (ВСЕ артефакты, XLM-R #20)
- [ ] **4. Контрольные суммы.** `sha256sum` → `artifacts/CHECKSUMS.sha256`
- [ ] **5. Валидация полноты.** Скрипт проверки: все ли образы/колёса/модели скачаны
- [ ] **6. Упаковка.** `tar -czf mcp-kb-airgap-bundle.tar.gz artifacts/ docker-compose.yml .env.example knowledge/ scripts/`

### 11.2 Развёртывание (на изолированном хосте, Debian)

- [ ] **7. Трансфер.** Перенос `mcp-kb-airgap-bundle.tar.gz` на целевой хост
- [ ] **8. Проверка целостности.** `sha256sum -c CHECKSUMS.sha256` — все OK
- [ ] **9. Загрузка образов.** `docker load -i artifacts/images/*.tar`
- [ ] **10. Установка зависимостей.** `pip install --no-index --find-links artifacts/wheelhouse/ -r requirements.txt`
- [ ] **11. Размещение моделей.** Распаковать `artifacts/models/bge-m3/` → `./models_cache/bge-m3/`
- [ ] **12. Env-конфигурация.** `.env.example` → `.env`:
  - [ ] `EMBEDDING_BACKEND=auto` (или `cpu`)
  - [ ] `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`
  - [ ] `QDRANT_URL`, `MCP_READ_KEYS`, `MCP_WRITE_KEYS`, `GIT_AUDIT=true`
- [ ] **13a. Запуск (dev/test).** `docker compose up -d`
- [ ] **13b. Запуск (production).** `docker compose up -d` с `restart: unless-stopped` + `depends_on: condition: service_healthy` (при желании — systemd-обёртка `Type=oneshot` / `Restart` над compose для интеграции с journald)
- [ ] **14. Проверка health.** Все сервисы → `healthy`
- [ ] **15. Дымовой тест.** `search_knowledge("тест")` → ответ
- [ ] **16. Загрузка корпуса.** `scripts/seed_knowledge.py` (русский корпус) или `git clone` Markdown-репозитория → `reindex()`

### 11.3 Скрипт-обёртка

```
scripts/offline-deploy.sh
  ├── prepare   # на машине с интернетом (шаги 1-6)
  ├── deploy    # на изолированном хосте (шаги 7-14)
  └── verify    # дымовой тест (шаг 15)
```

---

## 12. Roadmap (временная шкала)

> При условии 1 backend-разработчика (8 ч/д). Параллельная работа 2 разработчиков ≈ -40% календарных дней.

```
Неделя 1 (дни 1-5)
├─ День 1:   Фаза 0 — Scaffolding, docker compose, health-стабы           [8 ч] ✅
├─ День 2-3: Фаза 1 — Qdrant коллекция, Markdown SSOT + git-аудит (#21)   [10 ч]
├─ День 4:   Фаза 1 — In-process embedding (GPU+CPU ONNX) (#17)           [8 ч]
└─ День 5:   Фаза 1 — Chunker XLM-R (#20) + Async pipeline + тесты        [4 ч]
             + 🆕 v3.0: INDEX.gen.yaml generation (#30, задача 1.11)       [4 ч] ✅ M1

Неделя 2 (дни 6-11)
├─ День 6:   Фаза 2 — fastmcp + search/get/list Tools                     [8 ч]
├─ День 7:   Фаза 2 — write/update/delete/reindex + git (#21)             [8 ч]
├─ День 8:   Фаза 2 — Мульти-ключи (#18) + sync-флаг CPU-aware (#9)       [8 ч]
├─ День 9:   Фаза 2 — Reconciliation (#19) + DLQ (#14)                    [6 ч]
├─ День 10:  🆕 v3.0: get_knowledge_map + search_by_tags (#31, 2.14/2.15)  [3 ч]
│            + Resources/Prompts + Prometheus (#2.13)                     [4 ч]
└─ День 11:  Фаза 2 — E2E рус. корпус (9 tools, incl. v3.0)               [4 ч] ✅ M2: полный MCP MVP

Неделя 3 (дни 12-15) — реализация
├─ День 12:  Фаза 3 — Health hardening (docker compose) + бэкап (Qdrant+SSOT) [8 ч]
├─ День 13:  Фаза 3 — Ротация мульти-ключей + rate limiting (P2)           [6 ч]
├─ День 14:  Фаза 3 — Сине-зелёный reindex + conflict resolution (P2)      [4 ч]
└─ День 15:  Фаза 3 — Air-gap playbook (Docker) + pre-download + доки      [8 ч] ✅ M3: Production-ready

Контингент (дни 16-18) — резерв (~24 ч, ~144 ч с резервом)
├─ Буфер на v2.2/v3.0/v3.1-правки: run_in_executor, asyncio.Lock, INDEX gen, новые tools, P1-fixes (cache, truncation, structural change)
├─ Буфер на интеграцию, метрики, тесты
└─ При отсутствии проблем — раннее завершение / ранний старт Фазы 4
```

> **🆕 v3.0:** базовая оценка — **15 дней** (v2.3: 14 + 1 день на INDEX gen + новые tools); с контингентом 20% (≈24 ч / ≈3 ч/д) — **до 18 дней**. При удачной реализации M3 достижим на 15-й день.
>
> **🆕 v3.1:** post-Critic Gate правки (+2 ч, P1-1…P1-6) — уточнения в задачах 1.11/2.14/2.15 + 4 риска (§10.4). Доп. дня к roadmap не требуется (влезает в contingency).

### Вехи (Milestones)

| Веха | День | Критерий |
|------|:----:|----------|
| **M1: Ядро** | 5 | ✅ **ДОСТИГНУТ (2026-07-31)**. Markdown→Qdrant индексация; in-process GPU+CPU; chunker XLM-R; recovery из SSOT; git-аудит; **🆕 v3.0: INDEX.gen.yaml generation** |
| **M2: MCP MVP** | 11 | ⏳ **СЛЕДУЮЩИЙ.** План: [`02-phase2-mcp-server.md`](02-phase2-mcp-server.md). **9 Tools** + мульти-ключи + sync-флаг (CPU-aware) + reconciliation + DLQ + Prometheus; **🆕 v3.0: get_knowledge_map + search_by_tags**; E2E на русском |
| **M3: Production** | 15 (≤18) | ⏳ Ф3 частично (скрипты есть, hardening после Ф2). `restart: unless-stopped`, backup (Qdrant + SSOT), метрики, air-gap (Docker); **1 uvicorn worker** задокументирован; Definition of Done выполнен |

---

## 13. Матрица покрытия критики

> Демонстрация, что КАЖДАЯ рекомендация критика (P0/P1), слепая зона (B1-B10) и замечание внешней рецензии v2 (#1-#11) учтены в плане.

### 13.1 Рекомендации критика (v1)

| # | Приоритет | Рекомендация | Решение | Фаза | Статус |
|---|:---------:|--------------|:-------:|:----:|:------:|
| P0-1 | P0 | CPU fallback ONNX | #8 | 1 | ✅ |
| P0-2 | P0 | Sync-флаг wait_for_index | #9 | 2 | ✅ |
| P0-3 | P0 | Health-check эндпоинты | #10 | 0/3 | ✅ |
| P1-4 | P1 | Qdrant Snapshots бэкап | #11 | 3 | ✅ |
| P1-5 | P1 | API-key ротация | #12 | 3 | ✅ |
| P1-6 | P1 | Chunking-стратегия | #13 | 1 | ✅ |
| P1-7 | P1 | Dead Letter Queue | #14 | 2 | ✅ |
| P1-8 | P1 | Air-gap playbook | #15 | 3 | ✅ |
| P2-9 | P2 | Rate limiting | — | 3 | ✅ (задача 3.5) |
| P2-10 | P2 | Prometheus-метрики | — | **2** | ✅ (задача 2.13, перенесена из Ф3) |
| P2-11 | P2 | Conflict resolution (optimistic locking) | — | 3 | ✅ (задача 3.10) |
| P2-12 | P2 | HyDE query expansion | — | — | ⏭️ отложено (future) |
| P2-13 | P2 | BGE-Reranker | — | — | ⏭️ отложено (future) |
| P2-14 | P2 | Сине-зелёный reindex | — | 3 | ✅ (задача 3.9) |

### 13.2 Слепые зоны (Blind Spots)

| ID | Слепая зона | Покрытие | Где | Статус |
|----|-------------|----------|-----|:------:|
| B1 | CPU fallback для embedding | #8 → Фаза 1, задача 1.6 | 1 | ✅ |
| B2 | Бэкап Qdrant | #11 → Фаза 3, задача 3.2 | 3 | ✅ |
| B3 | Health-check / мониторинг | #10 → Фаза 0/3, задачи 0.5/3.1 + Prometheus 2.13 | 0/2/3 | ✅ |
| B4 | Conflict resolution | P2 → Фаза 3, задача 3.10 | 3 | ✅ |
| B5 | Размерные лимиты | Pagination в list_* → Фаза 2, задача 2.6 | 2 | ✅ |
| B6 | Семантика delete_entry | Контракт soft-delete + git commit → §5 + задача 2.5 | 1/2 | ✅ |
| B7 | reindex блокирует поиск | Сине-зелёное → Фаза 3, задача 3.9 | 3 | ✅ |
| B8 | Chunking-стратегия | #13, #20 → Фаза 1, задача 1.4 (XLM-RoBERTa) | 1 | ✅ |
| B9 | HyDE / re-rank | P2 → отложено (future) | — | ⏭️ |
| B10 | Rate limiting | P2 → Фаза 3, задача 3.5 | 3 | ✅ |

### 13.3 Противоречия → разрешение

| ID | Противоречие | Разрешение | Решение |
|----|--------------|------------|---------|
| C1 | Async индексация vs LLM-консистентность | Sync-флаг (GPU ≤5с; CPU → `pending`) + reconciliation при старте | #9, #19 |
| C2 | BGE-M3 GPU vs Air-gap простота | CPU fallback ONNX + offline playbook + in-process (#17) | #8, #15, #17 |
| C3 | API-ключи vs безопасность | **Мульти-ключевая** ротация через env vars + аудит-лог | #12, #18 |
| **C5** | **Отдельный embedding-svc vs простота MVP** | **In-process embedding для MVP; вынос при GPU на отдельном хосте** | **#17** |
| **C6** | **Semantic-only vs structural navigation** | **Hybrid: оба индекса** — semantic (Qdrant+BGE-M3) для content + hierarchy (INDEX.gen.yaml) для navigation. FPF 0.83 | **#30, #31** |

> **v2.1:** противоречие C4 («Docker-план vs Podman-окружение») снято — оператор подтвердил Docker-стек. Специальной адаптации runtime не требуется.

### 13.4 Внешняя рецензия v2 (оценка 8/10) 🔶

| # | Тип | Замечание рецензента | Решение/задача | Фаза | Статус |
|---|:---:|----------------------|:--------------:|:----:|:------:|
| **1** | 🔴 Критич. | Podman вместо Docker (compose, restart, GPU-CDI, save/load) | **Снято оператором:** стек команды — Docker; адаптация Podman не нужна (`healthcheck`/`restart`/`depends_on`/`--gpus`/`save`-`load` работают нативно); решение #16 удалено | 0/3 | ✅ |
| **2** | 🔴 Критич. | Recovery CPU-метрики нереалистичны | Метрики §5/§9.2 разделены: GPU <30 мин / CPU <2 ч | 1/9 | ✅ |
| **3** | 🔴 Критич. | In-memory очередь теряется при рестарте | #19, задача 2.9 (Reconciliation при старте) | 2 | ✅ |
| **4** | 🟠 Важн. | Ротация ключей — поддержка списка (graceful) | #18, задачи 2.8 + 3.4; §6.3 модель | 2/3 | ✅ |
| **5** | 🟠 Важн. | Chunking: 512 токенов токенайзером BGE-M3 | #20, задача 1.4 + 1.10 (XLM-RoBERTa) | 1 | ✅ |
| **6** | 🟠 Важн. | Sync-флаг ≤5 сек на CPU нереалистичен | Документировано: GPU-only гарантия; CPU → `pending`/приоритет; задача 2.4/2.10 | 2 | ✅ |
| **7** | 💡 Оптим. | Quadlet для air-gap production | **Снято:** Quadlet убран; production — `docker compose` (`restart: unless-stopped` + `depends_on`), air-gap — `docker save/load` | 3 | ✅ |
| **8** | 💡 Оптим. | Схлопнуть embedding-svc в mcp-server для MVP | #17, задачи 1.5/1.6 (in-process); §1.1 (2-сервисная топология) | 1 | ✅ |
| **9** | 💡 Оптим. | Prometheus в Фазу 2 | Задача 2.13 (перенесена из 3.5) | 2 | ✅ |
| **10** | 💡 Оптим. | Кириллические тесты | E2E на русском корпусе: §6 критерии, §9.1, §3 tests/e2e | 2 | ✅ |
| **11** | 💡 Оптим. | Git как аудит записей | #21, задачи 1.1/2.5 (`git commit` после write/upd/del) | 1/2 | ✅ |

### 13.5 Внешняя рецензия v2.2 (точечные правки в contingency) 🔶

> Без новых решений/фаз — правки в существующие задачи в рамках contingency 20%.

| # | Тип | Замечание рецензента | Решение/задача | Статус |
|---|:---:|----------------------|:--------------:|:------:|
| **v2.2-1** | 🔴 Критич. | Зафиксировать 1 uvicorn worker (in-memory состояние не переживает >1) | Задача 0.4 (валидация `WORKERS=1`) + критерии Ф2 | ✅ |
| **v2.2-2** | 🔴 Критич. | CPU-embed через `run_in_executor` (не блокировать event loop) | Задачи 1.5/1.6 + E2E-критерий Ф1 (p95 ≤ 400 ms) | ✅ |
| **v2.2-3** | 🔴 Критич. | `asyncio.Lock` вокруг git-операций (гонка `index.lock`) | Задача 1.1 + unit-тест конкурентной записи | ✅ |
| **v2.2-4** | 🟠 Важн. | Обратная сверка в reconciliation (Qdrant-сироты без `.md` → delete) | Задача 2.9 (`deleted_orphans`) | ✅ |
| **v2.2-5** | 🟠 Важн. | Бэкап Markdown-репозитория (SSOT), не только Qdrant | Задача 3.2 (`backup.sh`: snapshot + `git push`/tar) | ✅ |
| **v2.2-6** | 🟡 Желат. | `make dlq-replay` — возврат задач из DLQ в очередь | Задачи 2.11 + 0.6 (Makefile) | ✅ |
| **v2.2-7** | 💡 Космет. | stdio = локальный доверенный режим без auth | §1.1 (примечание о transport'ах) | ✅ |
| **v2.2-8** | 💡 Космет. | Roadmap 14 дней → 14–17 (честнее с contingency ~131 ч) | §12 (буфер дни 15-17) + веха M3 | ✅ |
| **v2.2-9** | 💡 Космет. | Обосновать chunk=512 (качество retrieval, не лимит BGE-M3) | Задача 1.4 + решение #13 | ✅ |

### 13.6 Hybrid Search Architecture v3.0 — слепые зоны (брейншторм) 🆕

> **Источник:** внутренний брейншторм (FPF A.19.ECS). FPF score: Hybrid **0.83** vs Semantic-only 0.74 vs Hierarchy-only 0.69. Каждая слепая зона текущего плана (v2.3) → mitigation в v3.0.

| ID | Слепая зона | Severity | Покрытие | Где | Статус |
|----|-------------|:--------:|----------|-----|:------:|
| SB1 | Exhaustive tag search невозможен (`search_knowledge(top_k=5)` ≠ all) | 🔴 | `search_by_tags(tags[], match?)` — Qdrant payload filter, без GPU, без top-K | Фаза 2, задача 2.15 | ✅ |
| SB2 | Нет структурной карты базы знаний | 🟠 | `get_knowledge_map(domain?)` + INDEX.gen.yaml (root + per-section) | Фаза 1 (1.11) + Фаза 2 (2.14) | ✅ |
| SB3 | CPU-only деградация замедляет навигацию | 🟠 | INDEX.gen.yaml — мгновенная навигация без GPU (p95 < 5 ms) | Фаза 1 (1.11) | ✅ |
| SB4 | Tag drift между агентами (разный словарь) | 🟡 | `suggested_tags` в gap analysis (#32); normalization — future enhancement | Фаза 1 (1.11) | ✅ (partial) |
| SB5 | Нет temporal/«что нового» поиска | 🟡 | INDEX содержит `updated_at` timestamps; `search_by_tags` может фильтровать по дате | Фаза 1 (1.11) | ✅ |
| SB6 | Нет proactive gap analysis | 🟢 | Gap analysis feeds `issues.jsonl` + `coverage%` в `get_knowledge_map()` | Фаза 1 (1.11) + Фаза 4 | ✅ |

### 13.7 Critic Gate v3.1 — P1-замечания (PASS 0.84) 🆕

> **Источник:** Critic Gate по плану v3.0. Оценка: **PASS (0.84)**. 6 P1-замечаний (без P0). Все интегрированы в существующие задачи — без новых фаз/решений.

| ID | Приоритет | Замечание Critic | Решение/задача | Фаза | Статус |
|----|:---------:|------------------|:--------------:|:----:|:------:|
| P1-1 | P1 | `get_knowledge_map` read-after-write consistency — кэш может быть устаревшим | In-memory кэш + write-invalidation (задача 1.11) + TTL=300с safety-net (задача 2.14) | 1/2 | ✅ |
| P1-2 | P1 | `search_by_tags` unbounded results — OOM/DoS риск | `limit=500` + `truncated:true` + WARN-лог (задача 2.15) + rate limiting (P2, 3.5) | 2/3 | ✅ |
| P1-3 | P1 | INDEX size enforcement (≤4KB/≤8KB) — нет truncation-стратегии | `MAX_ROOT_TAGS=20` + счётчик, `MAX_SECTION_TAGS=30` + adaptive L3, assert (задача 1.11) | 1 | ✅ |
| P1-4 | P1 | `suggested_tags` — алгоритм не специфицирован | Frequency-based (top-N тегов секции) + path-based (из пути директории) (задача 1.11) | 1 | ✅ |
| P1-5 | P1 | INDEX update при directory rename/move — не определено | Structural change detection → полная перестройка root + обеих per-section (задача 1.11) | 1 | ✅ |
| P1-6 | P1 | 4 риска не учтены (INDEX corruption, DoS, overflow, staleness) | R14–R17 (§10.4): recovery из SSOT, limit+rate, adaptive truncation, TTL+invalidation | 1/2/3 | ✅ |

---

## 14. Лог внешней рецензии v2

| Дата | Событие | Результат |
|------|---------|-----------|
| 2026-07-21 | Внешняя рецензия плана v1 | Оценка **8/10**; 3 критических, 3 важных, 5 оптимизационных замечаний |
| 2026-07-21 | Доработка до v2 (analyst) | Добавлены решения #16–#21; адаптация Podman/Quadlet; CPU/GPU метрики; reconciliation; мульти-ключи; токенайзер XLM-R; sync-CPU; in-process embedding; Prometheus→Ф2; кириллические тесты; git-аудит. Все 11 замечаний покрыты (§13.4). |
| 2026-07-21 | Корректировка до **v2.1** (analyst) | Внешний рецензент ошибочно предположил окружение Podman; **оператор подтвердил стек — Docker**. Удалены: решение #16 (Podman/Quadlet), задача 0.9 (валидация Podman), задача 3.8 (Quadlet), риск R7, противоречие C4, директория `quadlet/`, все упоминания Quadlet/CDI/podman-compose. Замены: `podman`→`docker`, `podman-compose`→`docker compose`, `Containerfile`→`Dockerfile`, CDI→`--gpus` (nvidia-container-toolkit). Трудозатраты: −10 ч → **~109 ч базовых / ~131 ч с резервом**. Roadmap сжат до **14 дней**. Сохранены все остальные правки v2 (#17–#21, метрики, reconciliation, Prometheus, кириллические тесты, P0/P1/P2). |

| 2026-07-21 | Доработка до **v2.2** (analyst) | Вторая внешняя рецензия — точечные правки в существующий contingency (~5 ч), без новых фаз/решений. 1) Инвариант **1 uvicorn worker** (задача 0.4 — валидация `WORKERS=1` + критерии Ф2) — масштабирование через внешнюю очередь; 2) CPU-embed через **`run_in_executor`** (задачи 1.5/1.6) + E2E «поиск под импортом на CPU, p95 ≤ 400 ms»; 3) **`asyncio.Lock`** вокруг git-операций против гонки `index.lock` (задача 1.1) + unit-тест конкурентной записи; 4) **обратная сверка** в reconciliation — Qdrant-сироты без `.md` → delete (задача 2.9); 5) **бэкап SSOT** (`git push`/tar `knowledge/`) + `backup_qdrant.sh`→`backup.sh` (задача 3.2); 6) **`make dlq-replay`** (задача 2.11 + Makefile). Косметика: stdio = локальный доверенный режим без auth (§1.1); Roadmap **14 → 14–17 дней** (§12); chunk=512 — качество retrieval (BGE-M3 держит 8192). Трудозатраты и структура фаз **не изменены** — правки влезают в contingency 20%. Покрытие: §13.5. |

| 2026-07-29 | Доработка до **v3.0** (analyst) | **Hybrid Search Architecture** (брейншторм, FPF 0.83). Semantic RAG (v2.3) — safety net для контентного поиска, но слеп к структурной навигации и exhaustive tag search. Добавлено: INDEX.gen.yaml generation (#30, задача 1.11, ~4 ч) — root ≤4 KB + per-section ≤8 KB, gap analysis, adaptive L3; `get_knowledge_map` + `search_by_tags` (#31, задачи 2.14/2.15, ~3 ч) — структурная навигация без GPU + exhaustive tag search; Gap analysis (#32) — proactive frontmatter validation в `write_knowledge` + `coverage%` + `suggested_tags`. Трёхинструментная поисковая модель: агент сам выбирает tool'ы (последовательно или параллельно). MCP Tools: 7→**9**. Трудозатраты: +7 ч → Ф0–Ф3 **120 ч** (с резервом ~144 ч). Roadmap: 14→**15 дней**. Покрытие: §13.6 (SB1–SB6). |

| 2026-07-29 | Доработка до **v3.1** (analyst) | **Post-Critic Gate (PASS 0.84).** 6 P1-замечаний интегрированы в существующие задачи (без новых фаз/решений): P1-1 cache-invalidation для `get_knowledge_map` (in-memory кэш + write-invalidation + TTL, задачи 1.11/2.14); P1-2 `search_by_tags` bounded results (`limit=500` + `truncated:true` + WARN, задача 2.15); P1-3 INDEX size enforcement truncation-стратегия (`MAX_ROOT_TAGS`, `MAX_SECTION_TAGS`, adaptive L3, assert, задача 1.11); P1-4 `suggested_tags` алгоритм (frequency-based + path-based, задача 1.11); P1-5 INDEX update при structural change (rename/move → полная перестройка, задача 1.11); P1-6 четыре новых риска R14–R17 (§10.4). Трудозатраты: +2 ч → Ф0–Ф3 **122 ч** (с резервом ~146 ч). Покрытие: §13.7. |

---

## 📌 Сводка для исполнителя

- **Старт:** Фаза 0 (Scaffolding) — **Docker** + health-checks + **🆕 v2.3: 2 отдельных git-репо (#28) + Ansible playbook (#29)**
- **Ядро:** Фаза 1 — Markdown SSOT (**git-аудит**) + Qdrant + BGE-M3 **in-process** (GPU/CPU) + chunker **XLM-RoBERTa** + async pipeline + **🆕 v3.0: INDEX.gen.yaml generation (#30)** + **🆕 v3.1: cache-invalidation, size enforcement, suggested_tags, structural change (P1-1/3/4/5)**
- **Интерфейс:** Фаза 2 — **9 MCP Tools** + Resources + Prompts + **мульти-ключи** + sync-флаг (**CPU-aware**) + **reconciliation** + DLQ + **Prometheus** + **🆕 v3.0: `get_knowledge_map` + `search_by_tags` (#31)** + **🆕 v3.1: read-after-write consistency + bounded results (P1-1/2)**
- **Production:** Фаза 3 — `restart: unless-stopped` + **backup (Qdrant + SSOT `backup.sh`)** + ротация мульти-ключей + метрики + air-gap (Docker) + rate-limit + `make dlq-replay`
- **Контракт:** Markdown = SSOT (git-versioned), Qdrant + INDEX.gen.yaml = **два вычисляемых индекса**, `reindex()` = восстановление, reconciliation при старте = консистентность
- **🆕 v2.3 Storage (#27/#28/#29):** runtime — **локальный диск** (НЕ SMB!); **2 репо** — `mcp-knowledge/` (код) + `knowledge/` (SSOT, отдельный git); SMB — только backup-target; развёртывание через **Ansible** (Vault для secrets). См. §1.5
- **🆕 v3.0/v3.1 Hybrid Search (#30/#31/#32):** INDEX.gen.yaml = **второй вычисляемый индекс** (структурная карта, без GPU, p95 < 5 ms); трёхинструментная поисковая модель (`get_knowledge_map` + `search_by_tags` + `search_knowledge`); gap analysis в `write_knowledge` (proactive). **🆕 v3.1 P1:** cache-invalidation + bounded search + truncation + suggested_tags + structural change recovery. FPF 0.83. Трудозатраты: **+9 ч** (v3.0:+7, v3.1:+2) → Ф0–Ф3 **122 ч**
- **Всего:** ~**122 ч** Ф0–Ф3 (с резервом **~146 ч**), ~15 ч/д, **~15–18 дней** до Production-ready (M3: 15 — базовая оценка, ≤18 — с контингентом); **Фаза 4** (опц., follow-on после M3) — ~40 ч / ~5 дней (см. [`04-phase4-knowledge-quality.md`](04-phase4-knowledge-quality.md))
- **Runtime:** **Docker** на Debian; dev и production — `docker compose` (`restart: unless-stopped` + `depends_on: condition: service_healthy`), GPU через `--gpus all` (nvidia-container-toolkit)

> **FPF-методология:** C.30 (Grounded Architecture — план на основе данных критики + внешней рецензии), A.22 (Structure Views — декомпозиция на фазы/задачи), A.19.ECS (оценка выбрана на предыдущем шаге), A.10 (Evidence Graph — матрица покрытия §13, включая §13.4 и §13.5 внешних рецензий).
