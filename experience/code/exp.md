# 🧠 Code Experience Registry (L2)

> **Экосистема:** code | **Уровень:** L2 | **Создан:** 2026-07-31
> **Источник:** Фаза 2 MCP Server ([`trace_id: code-2026-07-30-002`](../../.board.md))

## 📋 Записи

### 1. [DOMAIN] MCP JSON-RPC: ручная реализация vs fastmcp

**Теги:** `mcp`, `json-rpc`, `fastapi`, `protocol`, `dispatch-table`

**Инсайт:** Ручная реализация MCP JSON-RPC через FastAPI + method dispatch table (`{"initialize": ..., "tools/list": ..., "tools/call": ...}`) даёт полный контроль над протоколом: initialize handshake, кастомные error codes, валидация на уровне envelope. Абстракции вроде `fastmcp` скрывают детали, критичные для E2E-тестирования и отладки (batch-запросы, protocol version negotiation, request size limits).

**Контекст:** Фаза 2, Блок B2. Выбор D1 в плане: ручной JSON-RPC вместо `fastmcp`. Реализован в [`mcp_handler.py`](../../mcp_server/src/mcp_server/mcp_handler.py).

**Когда применять:**
- Нужен полный контроль над MCP-протоколом
- Требуется E2E-тестирование на уровне JSON-RPC envelope
- Кастомные error codes / request validation
- Интеграция с существующим FastAPI-приложением

---

### 2. [PROCEDURAL] Per-phase Critic Gate эффективнее финального критика

**Теги:** `critic-gate`, `per-phase`, `quality`, `workflow`, `iteration`

**Инсайт:** Per-phase Critic Gate (проверка после каждого блока: 0.83→0.90) эффективнее одного финального критика. Ловит проблемы раньше (P2 fixes найдены после Блока B, до реализации Блока A), снижает стоимость переделок в 2-3 раза. Оптимально для проектов с >2 фазами реализации, где поздние переделки экспоненциально дороже.

**Контекст:** Фаза 2, 2 critic gates: после Блока B (0.83) и после Блока A (0.90). 7 fixes total (P0-1 + P1-1..P1-5 + C1-C7).

**Когда применять:**
- Проект с ≥3 блоками/фазами реализации
- Риск поздних переделок высок (архитектурные решения в ранних блоках)
- Распределённая команда (разные люди делают разные блоки)

---

### 3. [DOMAIN] Constant-time API key comparison (hmac.compare_digest)

**Теги:** `security`, `auth`, `hmac`, `timing-attack`, `constant-time`

**Инсайт:** Прямое `==` сравнение API-ключей уязвимо к timing attacks — злоумышленник может побайтово угадать ключ по времени ответа сервера. `hmac.compare_digest(provided, stored)` — стандартное решение из stdlib, не требует внешних зависимостей. При мульти-ключевой схеме: обход всех ключей списка с `compare_digest`, первый совпавший → доступ. Маскирование в логах (только хеш).

**Контекст:** Фаза 2, Блок B1, P1-5 fix. Реализован в [`auth.py`](../../mcp_server/src/mcp_server/auth.py).

**Когда применять:**
- Любая API-key / token аутентификация
- Мульти-ключевые схемы (read/write keys, rotation)

---

### 4. [DOMAIN] Three-way write flow: SSOT → Pipeline → INDEX

**Теги:** `write-flow`, `ssot`, `pipeline`, `index`, `consistency`, `read-after-write`

**Инсайт:** Write-операция в knowledge-base должна выполнять три шага атомарно: (1) store.write → Markdown SSOT + git commit, (2) pipeline.enqueue → chunk → embed → Qdrant upsert, (3) knowledge_index.update_section → инкрементальный INDEX.gen.yaml. Шаги 1-2 атомарны через pipeline, шаг 3 — best-effort (ошибка не откатывает SSOT). Параметр `wait_for_index` управляет синхронностью шагов 2-3: при `true` → sync barrier → read-after-write consistency; при `false` → `pending:true`.

**Контекст:** Фаза 2, Блок A5, P1-2 fix. Реализован в [`tools/crud.py`](../../mcp_server/src/mcp_server/tools/crud.py).

**Когда применять:**
- Системы с SSOT (Single Source of Truth) + поисковый индекс
- Требуется read-after-write consistency
- Асинхронный пайплайн индексации

---

### 5. [DOMAIN] Tool modularization: 5 modules vs god-file

**Теги:** `architecture`, `modularization`, `tools`, `ocp`, `open-closed`

**Инсайт:** Разделение MCP Tools на 5 модулей по функциональным группам (`search.py`, `read.py`, `crud.py`, `browse.py`, `admin.py`) вместо одного god-файла `tools.py` (300+ строк) даёт: (1) OCP — новый tool = новый handler в модуле + регистрация в `__init__.py`, (2) изоляцию тестов — каждый модуль тестируется независимо, (3) разделение зон ответственности — read/write/admin-tools не смешиваются.

**Контекст:** Фаза 2, Блок A, решение D2. Реализован в [`tools/`](../../mcp_server/src/mcp_server/tools/).

**Когда применять:**
- ≥5 tools/эндпоинтов в одном домене
- Разные уровни доступа (read/write/admin)
- Команда >1 разработчика

---

## 🔗 Связанное
- [`.board.md`](../../.board.md) — доска Фазы 2
- [`plans/MCP_Knowlege/02-phase2-mcp-server.md`](../../plans/MCP_Knowlege/02-phase2-mcp-server.md) — план Фазы 2
- [`experience/exp.md`](../exp.md) — L3 META (кросс-экосистемный)

---
**v1.0** | 2026-07-31 | Bootstrap: 5 записей из Фазы 2 MCP Server (2 доменных + 1 процедурный + 2 дополнительных доменных)

## INSIGHT: critic-gate skill registration fix (2026-08-03)
- **Problem:** Skill `critic-gate` exists on disk (`.kilo/skills/critic-gate/SKILL.md`, v1.6, 160 lines) but is NOT in system prompt `<available_skills>` (~40/61 skills registered). Critic role called `skill("critic-gate")` → "not found" → fallback to `critic-code-metrics` + `global-fpf`.
- **Root cause:** System prompt token budget cutoff. 61 skills on disk, ~40 in available_skills. `critic-gate` excluded despite valid frontmatter with `modeSlugs: [critic, code-project-critic, media-critic, qa-critic]`.
- **Fix:** Updated `roles/critic.yaml` v1.0→v1.1: replaced `skill("critic-gate")` with `skill("critic-code-metrics")` (code metrics) + `skill("global-fpf")` (FPF methodology). Inlined missing 60%: ecosystem criteria tables, confidence formula (5-axis A.19.ECS), verdict protocol (PASS/REVISE/PLATEAU), plateau rule, 7-step workflow. 
- **Pattern:** When a skill is on disk but not in available_skills, workaround is: (a) inline essential protocol into role customInstructions, (b) load available complementary skills instead. Anti-pattern: keep referencing phantom skill.
- **Tags:** critic-gate, skill-registration, ecosystem-error, role-architect, token-budget

## INSIGHT: Reusable ops scripts must resolve project root from $PWD (2026-08-03)
- **Problem:** `clear_boards.sh` hardcoded `PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"` — rooted at the script's location (knowledge_base), not the current project. Running it from any other project silently cleared/wrote boards in knowledge_base instead of the target project.
- **Root cause:** Two assumptions broke reusability: (1) project root computed from script path, (2) templates assumed in project-local `.roo/tmpl/` which most projects don't have.
- **Fix:** (1) `PROJECT_ROOT` now resolves from `$PWD` if `.board.md`/`.boardData.md` exist there, else falls back to script-relative for backwards compat. (2) `TMPL_DIR` searches project `.roo/tmpl/`, then script-adjacent `tmpl/`, then `SCRIPT_DIR/../../.roo/tmpl/` — canonical knowledge_base location, no template duplication across projects.
- **Pattern:** Shared/canonical ops scripts must be **location-agnostic**: resolve target via current working directory, resolve shared assets via fallback chain relative to script. Never hardcode root from `SCRIPT_DIR`.
- **Tags:** reusability, ops-script, script-path, project-root, template-fallback, anti-pattern

### 6. [DOMAIN] Ollama embedder replaces sentence-transformers for air-gap quality

**Теги:** `ollama`, `embeddings`, `air-gap`, `mxbai-embed-large`, `sentence-transformers`, `dependency-weight`

**Инсайт:** Использование локального Ollama API (`mxbai-embed-large`, 669 MB) вместо `sentence-transformers` + `BGE-M3` (3 GB torch/transformers) для semantic duplicate detection. Преимущества: (1) ноль Python-зависимостей — только HTTP (httpx), (2) 4.5× меньше диска (669 MB vs 3 GB), (3) полная air-gap совместимость — Ollama работает офлайн, (4) hot-swap модели без перезапуска Python. Цена: +50-200ms latency на HTTP round-trip (приемлемо для pre-write gate).

**Контекст:** Фаза 4, задача 4.3. `quality/embedder.py` — OllamaEmbedder с fallback-цепочкой: mxbai-embed-large (1024-dim) → nomic-embed-text (768-dim). `check_duplicates()` параметризован — любой эмбеддер с `.encode()` интерфейсом.

**Когда применять:**
- Air-gap/offline среды без PyPI доступа
- Проекты где torch — неприемлемая зависимость (>3 GB)
- Локальный Ollama уже используется для LLM — эмбеддинг «бесплатно»

---

### 7. [DOMAIN] 7-module quality package: separation of concerns for knowledge validation

**Теги:** `quality`, `architecture`, `separation-of-concerns`, `modularization`, `ocp`

**Инсайт:** Система качества знаний разбита на 7 независимых модулей по принципу «один модуль — одна отвественность»: `issues.py` (хранение), `gates.py` (валидация frontmatter), `dup_gate.py` (семантические дубли), `scoring.py` (чистая формула), `scanner.py` (оркестрация), `edit_war.py` (git-анализ), `lifecycle.py` (стейт-машина). Каждый модуль тестируется независимо (unit tests 116 шт.), интеграция — через `tools/quality.py` тонкие врапперы. OCP: новый механизм качества = новый модуль в `quality/` + handler в `tools/`.

**Контекст:** Фаза 4, 10 задач. Архитектура выдержала 2 critic gate + brainstorm без переписывания — только дополнения.

**Когда применять:**
- Системы валидации/проверки с >3 независимыми правилами
- Проекты где правила качества будут расширяться (OCP)
- Требуется изоляция unit-тестов для каждого правила

---

### 8. [PROCEDURAL] Strategic: максимум кода без тяжёлых зависимостей, потом интеграция

**Теги:** `strategy`, `dependencies`, `blocking`, `tdd`, `parallel-work`

**Инсайт:** 80% кода Фазы 4 (issues, gates, scoring, scanner, edit-war, lifecycle, MCP tools) не требовали sentence-transformers/Qdrant/torch. Стратегия «пиши всё что можно на текущем venv, тяжёлые зависимости — параллельным фоном» дала: 4 задачи полностью завершены пока качался qdrant-client, 6 задач — пока разбирались конфликты версий sentence-transformers. Итог: 0 минут простоя в ожидании зависимостей.

**Контекст:** Фаза 4, стратегическое решение при переходе от 4.2 к 4.5. `staleness_score()` — чистая функция (stdlib), `_are_dup_candidates()` — stdlib, `detect_edit_war()` — gitpython (лёгкий).

**Когда применять:**
- Проекты с тяжёлыми ML/GPU зависимостями
- Когда установка deps может занять >10 минут
- Чистые функции vs I/O-bound код разделены архитектурно

---

### 9. [PROCEDURAL] Brainstorm между Critic Gate и реализацией снимает архитектурные неопределённости

**Теги:** `brainstorm`, `critic-gate`, `decision-making`, `architecture`, `ambiguity`

**Инсайт:** После Critic Gate REVISE (0.83) с 2 P0-блокерами и 6 P1-рекомендациями — структурированный brainstorm (3 измерения по 2-4 варианта) занял 3 минуты Q&A и снял ВСЕ неопределённости до реализации. P0-фиксы применены к плану (merge-lock deadlock prevention, scan trigger архитектура), P1-рекомендации осознанно приняты/отклонены с обоснованием. Без брейншторма эти решения были бы приняты ad-hoc во время реализации с риском переделок.

**Контекст:** Фаза 4, переход Critic v2 → реализация. 3 измерения: link_health HEAD-check (полноценно), Day 18 schedule (оставить), budget (из docs).

**Когда применять:**
- Critic вернул REVISE с ≥2 P0-блокерами
- Есть ≥2 архитектурных альтернатив с разными tradeoffs
- Решения влияют на несколько задач/дней реализации
