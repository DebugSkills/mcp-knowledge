# 📊 ФАЗА 4: Knowledge Quality — активная валидация качества знаний

> **trace_id:** `code-2026-07-21-001` | **Автор:** analyst | **Дата:** 2026-07-21
> **Версия:** 1.2 | **Статус:** готов к реализации (после Critic Gate v2)
> **Родительский план:** [`00-implementation-plan.md`](00-implementation-plan.md) v2.2
> **Зависимости:** Фазы 0–3 (особенно #19 reconciliation, #21 git-аудит, #2.13 Prometheus, BGE-M3 in-process embedder)
>
> **История версий:**
> - **v1.0** (2026-07-21) — базовый план Фазы 4: 5 решений (#22–#26), 10 задач, ~40 ч. Pre-write gates + staleness scoring + 3 quality Tools + edit-war + 2-state lifecycle slice.
> - **v1.1** (2026-07-21) — **доработка по Critic Gate** (REVISE, score 0.75 → цель ≥0.85). Внесены все P0 и P1 правки **без увеличения бюджета** (перераспределение внутри 40 ч). См. §12 «Лог правок Critic Gate».
> - **v1.2** (2026-08-03) — **доработка по Critic Gate v2** (REVISE, score 0.83 → цель ≥0.85). P0: merge-lock deadlock prevention (asyncio.Lock + лексикографическая сортировка), scan trigger HTTP-endpoint. P1: orphan_factor → link_health_factor, округление score до 4 знаков, run_in_executor для scanner, gate на Pydantic-модели.
>
> **Ключевой вопрос:** *какие 2–3 механизма дадут самый большой прирост качества знаний при минимальных трудозатратах?*
> **Краткий ответ:** **pre-write quality gates** (предотвращение) + **staleness scoring + review-очередь** (обнаружение) + **quality log** (фундамент/observability) + **2-state lifecycle slice** (deprecation + reversibility). См. §1 FPF-обоснование.

---

## 📑 Содержание

1. [FPF-обоснование выбора механизмов](#1-fpf-обоснование-выбора-механизмов)
2. [Цель, приоритет, трудозатраты](#2-цель-приоритет-трудозатраты)
3. [Архитектура качества (поток данных)](#3-архитектура-качества-поток-данных)
4. [Задачи (4.1–4.10)](#4-задачи-4110)
5. [Новые MCP Tools (3) + Prompt (1)](#5-новые-mcp-tools-3--prompt-1)
6. [Контракты: staleness-формула, issue-модель, интеграция](#6-контракты-staleness-формула-issue-модель-интеграция)
7. [Критерии приёмки (ACCEPTANCE)](#7-критерии-приёмки-acceptance)
8. [Метрики (EXPECTED) + Quality-SLO](#8-метрики-expected--quality-slo)
9. [Риски и mitigation](#9-риски-и-mitigation)
10. [Roadmap-слайс (5 дней)](#10-roadmap-слайс-5-дней)
11. [Known limitations — что НЕ покрывается](#11-known-limitations--что-не-покрывается)
12. [Лог правок Critic Gate (v1.0 → v1.1)](#12-лог-правок-critic-gate-v10--v11)

---

## 1. FPF-обоснование выбора механизмов

### 1.1 Постановка (C.30 — Grounded Architecture)

План 00 обеспечивает **инфраструктуру** хранения/поиска, но **не валидирует содержание**. Оркестратор предложил 4-уровневую систему; оператор — периодическую ревизию и лог коллизий. Применяем **C.30**: выбираем механизмы, которые **переиспользуют уже построенные активы**, а не требуют новой инфраструктуры.

**Существующие активы (из [`00-implementation-plan.md`](00-implementation-plan.md)), которые делает качество «дешёвым»:**

| Актив | Где | Что даёт бесплатно для качества |
|-------|-----|--------------------------------|
| Qdrant + BGE-M3 in-process embedder | §5, #3/#17 | Готовый движок **семантической дедупликации** (embed candidate → search top-k) |
| Markdown frontmatter-схема | §3.1, #2 | **Валидация полей** = pydantic-check |
| Git-аудит (#21) | #21 | Бесплатная история правок → **edit-war detection**, частота обновлений (staleness-сигнал) |
| Reconciliation `updated_at` ↔ payload (#19) | #19 | Свежесть записи уже отслеживается → **staleness-scoring** |
| `/metrics` Prometheus (#2.13) | задача 2.13 | Quality-метрики + **SLO-алерты** добавляются почти бесплатно |

### 1.2 Оценка 4 уровней — A.19.ECS Characteristic Space

Веса: Полнота 0.20 · Риски 0.20 · Ресурсы(Effort, инверт.) 0.20 · Сроки 0.15 · Стратегия 0.10 · Reusability 0.15.

| Уровень | Полнота | Риски | Effort↑ | Сроки | Стратегия | Reusability | **Итог** |
|---------|:------:|:-----:|:------:|:-----:|:--------:|:----------:|:-------:|
| **1. Pre-write Quality Gates** | 0.90 | 0.85 | 0.85 | 0.85 | 0.85 | 0.90 | **0.87** 🥇 |
| **3. Staleness Scoring + review_queue** | 0.85 | 0.85 | 0.70 | 0.75 | 0.85 | 0.80 | **0.80** 🥈 |
| **2. Collision/Error Log (+edit-war)** | 0.60 | 0.90 | 0.90 | 0.90 | 0.70 | 0.65 | **0.78** 🥉 |
| **4. Knowledge Lifecycle (полный)** | 0.80 | 0.45 | 0.40 | 0.40 | 0.60 | 0.55 | **0.53** ❌ |

### 1.3 A.10 — Evidence Graph: почему выбран именно этот набор

**Выбранный набор (≤40ч):** Ур1 + Ур3 + лёгкий Ур2 + 2-state slice Ур4.

**Цепочка доказательств «за»:**

| ID | Доказательство | Какой выбор поддерживает |
|----|----------------|--------------------------|
| **E1** | **Prevention > Cure.** Ошибочное знание в SSOT распространяется на **все будущие agent-reads** (search возвращает мусор N раз). Cost-of-bad-data мультипликативен. → pre-write gate (Ур1) обязателен. | Ур1 |
| **E2** | **Reuse.** Дедупликация и staleness **не требуют новой инфраструктуры** — Qdrant-поиск + embedder + `updated_at` + git уже есть. Effort ∝ логике, а не инфраструктуре → ratio impact/effort максимален. | Ур1, Ур3 |
| **E3** | **Agent-facing.** Система MCP-first. Качество бесполезно, если агент не может его **увидеть и починить**. → 3 новых Tools (`review_queue`, `list_quality_issues`, `resolve_quality_issue`) **+ промпт-триггер** `periodic_quality_cleanup` обязательны (без промпта Tools = мёртвый код, см. §5.1). | Ур3 (Tools + Prompt) |
| **E4** | **Operator-asked.** Оператор явно назвал «периодическая ревизия» + «лог коллизий и чистка». Ур3 = ревизия, Ур2 = лог. → прямое покрытие требований. | Ур2, Ур3 |
| **E5** | **Contract-vs-Quality.** Гейт разделяет два класса: (а) **контракт схемы SSOT** — отсутствие `knowledge_id`/`domain`/`tags` это **нарушение контракта**, блокируется ВСЕГДА (409), это не «субъективное качество»; (б) **качественные суждения** (дубли/неполнота recommended) — **advisory**: возвращаются в ответе, не ломают E2E. `?strict=true` опционально повышает advisory→block. → риск к контракту ≈ 0 для легитимных записей. | Ур1 |
| **E6** | **Git-аудит = бесплатный сигнал.** #21 уже коммитит каждую правку → edit-war и staleness вычисляются из истории, без отдельного трекинга. | Ур2, Ур3 |

**Почему отклонён ПОЛНЫЙ Ур4 (draft→review→published→deprecated→archived):**

| ID | Контраргумент |
|----|---------------|
| **C1** | **Blast radius.** Полная стейт-машина меняет write-контракт + search-семантику (фильтр статусов) + все 9 Tools (v3.0). Все E2E из Ф2 (#10) переписываются. В бюджете ≤40ч съест 20+ ч и высокорисково. |
| **C2** | **Value-gap на MVP.** Ценность стейт-машины проявляется, только когда агенты **понимают переходы** (write→draft→ждать ревью). Сейчас агенты пишут напрямую в SSOT — поле осталось бы мёртвым. |
| **C3** | **Minimal slice вместо полного.** 2-state slice (`published`/`deprecated` **с reversibility** — см. `restore` §5) даёт ~80% ценности (скрыть устаревшее + вернуть при ошибке) при **<4 ч** и **без** изменения контракта. → включаем slice, полный lifecycle → future (как HyDE/re-ranker в [`00-implementation-plan.md`](00-implementation-plan.md) §13.1). |

**Дорогие NLP-механизмы, осознанно отложенные (low impact-to-effort для русского):**

- **LLM-based contradiction detection** — нужен LLM-вызов на каждую пару кандидатов; дорогo, ненадёжно в air-gap. → future (после подключения локальной LLM).
- **Эвристика `conflicting` (tags-пересечение + разный контент)** — для инженерной БЗ пересечение тегов при разном контенте = **норма** (~90% false positives). → отложено в future; тип `conflicting` зарезервирован в модели (§6.2), но детекция НЕ реализуется в MVP (§11). См. §9 риск R9.
- **Морфологическая дедупликация** — BGE-M3 уже мультиязычный и хорошо ловит русские парафразы (порог калибруется в тестах, §7). Доп. NLP = diminishing returns.

---

## 2. Цель, приоритет, трудозатраты

**Цель:** Внедрить **активную валидацию качества знаний** — предотвращать попадание дубликатов/неполных записей в SSOT (pre-write gates), обнаруживать и ранжировать устаревшие/проблемные записи (staleness-scoring + review-очередь), давать агентам инструменты для **обнаружения и устранения** проблем (`list_quality_issues`, `resolve_quality_issue`), и обеспечивать лёгкий механизм жизненного цикла (deprecation **с восстановлением**). Всё — на базе существующих активов, **без** новой инфраструктуры.

**Приоритет:** 🟠 MEDIUM (после работающего MVP Фаз 0–3; зависит от #19, #21, #2.13, in-process embedder)
**Трудозатраты:** **~40 ч** (≈ 5 дней); **P0-ядро = 32 ч** (минимальный viable набор — ощутимый прирост качества даже без P1; *в v1.0 P0-core был ошибочно указан как 26ч — пересчитано и исправлено в §4*)
**Новые решения:** **#22** Pre-write Quality Gate · **#23** Staleness/Quality Scoring · **#24** Quality Tools · **#25** Edit-War Detection · **#26** Lifecycle Slice

> **Принцип интеграции:** НЕ ломаем SSOT (#2), НЕ дублируем Qdrant (#1), НЕ меняем write-контракт для **качественных** суждений (advisory-by-default, E5). При этом **контракт схемы** (required-поля) блокируется всегда — это не «субъективное качество», а валидность записи. Quality — это **слой над** существующим пайплайном, переиспользующий embedder/Qdrant/git/`updated_at`.

---

## 3. Архитектура качества (поток данных)

```
                          ┌─────────────────────────────────────────────────┐
   Agent (write-key) ──┐  │  write_knowledge / update_entry                 │
                       ▼  └───────────────┬─────────────────────────────────┘
   ┌──────────────────────────────────────▼───────────────────────────────────┐
   │  quality/gates.py  (#22) — PRE-WRITE                                      │
   │   ├─ frontmatter-валидация:                                               │
   │   │     • required (knowledge_id, domain, subject, tags, created_at,      │
   │   │       updated_at) — отсутствие = 409 BLOCK ВСЕГДА (контракт схемы)    │
   │   │     • recommended (source, cross_subjects, evergreen) — warn           │
   │   └─ duplicate-detection (ADVISORY): embed candidate → Qdrant search       │
   │       (top_k, filter by domain) → cosine ≥ DUP_THRESHOLD(0.92)            │
   │   возвращает QualityReport{ duplicates[], missing_fields[], severity }     │
   │   ⚠ dup-gate НЕ видит записи в async-индекс-очереди (окно 1–5с, §9 R10)   │
   └───────────────┬───────────────────────────────┬───────────────────────────┘
                   │ (запись проходит, git commit #21)│ (?strict=true → 409 при CRITICAL dup)
                   ▼                                  ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │  Markdown SSOT (#2)  ─── git-аудит (#21)                                  │
   │   frontmatter: status("published"), evergreen(bool)                       │
   │   ⚠ staleness_score ХРАНИТСЯ В Qdrant PAYLOAD (не в frontmatter!) —       │
   │     это SSOT для score. scanner (#23) — единственный писатель.            │
   │  ┌──────────────────────────────────────────────────────┐                 │
   │  │  Qdrant payload (#1):                                 │                 │
   │  │   status(str) | staleness_score(float) | quality_flags│                 │
   │  │   + chunk-embeddings (content)                        │                 │
   │  └──────────────────────────────────────────────────────┘                 │
   └───────────────┬───────────────────────────────┬───────────────────────────┘
                   │ (cron / по запросу)            │ (search: filter status!=deprecated)
                   ▼                                ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │  quality/scanner.py + scoring.py (#23) — PERIODIC SCAN                    │
   │   • обходит knowledge/**/*.md (паттерн reconcile #19)                     │
   │   • staleness_score (см. §6.1; evergreen → медленное старение)            │
   │     → payload (ЕДИНСТВЕННЫЙ писатель score; не хранится в frontmatter)    │
   │   • dup-pair scan (O(n) по domain-бакетам) → issues                       │
   │   • edit-war из git-истории (#25)                                         │
   │   • пишет issues в data/quality/issues.jsonl (АТОМАРНО, fcntl.flock §4.1)│
   └───────────────┬───────────────────────────────────────────────────────────┘
                   ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │  Agent-facing Tools (#24) — quality/issues.py + lifecycle.py              │
   │   • review_queue(domain?, limit?)         → записи, отсорт. по score      │
   │   • list_quality_issues(types?, limit?)   → typed issues (open)           │
   │   • resolve_quality_issue(id, action)     → merge|deprecate|restore|       │
   │                                             resolve|ignore               │
   │   ┌─ merge: targeted upsert (конкр. записи), file-lock на оба ID ────────┐│
   │   └─ restore: deprecated→published (reversibility) ─────────────────────┘│
   └───────────────────────────────────────────────────────────────────────────┘
   /metrics (#2.13): quality_duplicates_detected_total, quality_issues_total{type},
                     review_queue_size, deprecated_total
                     + SLO-alert: review_queue_size > THRESHOLD → alertmanager
```

---

## 4. Задачи (4.1–4.10)

| # | Задача | Решение | Детали | Файлы |
|---|--------|:-------:|--------|-------|
| **4.1** | Issue-store + quality log (фундамент) | — [P0] | `data/quality/issues.jsonl` (append-only): `{issue_id, type, knowledge_id, severity(info\|warn\|critical), detail, detected_at, status(open\|resolved\|ignored), resolved_at, resolution}`. CRUD: `create/list/update_status`. Idempotent по `(type, knowledge_id, detail_hash)` — не дублируем одни и те же issues. **Атомарность записи:** конкурентный доступ cron-scanner (§4.8) + pre-write-gate (§4.2) → `fcntl.flock` на файл или **temp-rename** (write→`*.tmp`→`os.replace` = атомарно). Без этого — гонка/потеря записей. | [`quality/issues.py`](mcp_server/src/mcp_server/quality/issues.py), `data/quality/issues.jsonl` |
| **4.2** | Pre-write frontmatter-gate | #22 [P0] | Валидация перед записью на **Pydantic-модели** (не сырые поля — `default_factory` поля не могут отсутствовать). **Два класса (E5):** (а) **required** = `knowledge_id, domain, subject, tags, created_at, updated_at` → отсутствие = **409 BLOCK ВСЕГДА** (контракт схемы SSOT, не «субъективное качество»); (б) **recommended** = `source, cross_subjects, evergreen` → отсутствие = `severity=warn` (advisory, не блокирует). Проверка уникальности `knowledge_id` (коллизия имён файлов). `evergreen: true` обрабатывается scoring (§6.1). `?strict=true` повышает **advisory dup/warn** → block, но НЕ влияет на required (те и так блок). | [`quality/gates.py`](mcp_server/src/mcp_server/quality/gates.py) |
| **4.3** | Pre-write semantic duplicate-gate | #22 [P0] | Перед `write_knowledge`/`update_entry`: embed репрезентативного вектора (заголовок + первый чанк) → `qdrant.search(top_k=8, filter={domain})` → cosine ≥ `DUP_SIMILARITY_THRESHOLD=0.92` → кандидаты-дубликаты. **Исключать self** (тот же `knowledge_id` при update). Возврат: `duplicates[{knowledge_id, score}]`. Reuse in-process embedder (#3/#17). **ADVISORY** по умолчанию (E5). ⚠ **Temporal blind-spot:** гейт видит только проиндексированные записи; запись в async-очереди (окно 1–5с между write и доступностью в Qdrant) невидима → компенсируется dup-pair scan в scanner (§4.5, eventual consistency). | [`quality/gates.py`](mcp_server/src/mcp_server/quality/gates.py) |
| **4.4** | Edit-War detection (git-based) | #25 [P1] | По git-истории (#21): `git log --follow <knowledge_id>.md` → если **≥3 коммитов за 24ч** в один `knowledge_id` → issue `edit_war` (severity=warn). Cheap, т.к. git-аудит уже ведётся. Конфиг `EDIT_WAR_WINDOW_H=24`, `EDIT_WAR_THRESHOLD=3`. | [`quality/scanner.py`](mcp_server/src/mcp_server/quality/scanner.py) |
| **4.5** | Staleness/quality scoring + periodic scanner | #23 [P0] | [`quality/scoring.py`](mcp_server/src/mcp_server/quality/scoring.py): чистая тестируемая функция `staleness_score(entry, ctx) → float[0,1]` (формула §6.1, **с поддержкой `evergreen`**). [`quality/scanner.py`](mcp_server/src/mcp_server/quality/scanner.py): обходит `knowledge/**/*.md` → **`loop.run_in_executor(None, ...)` для filesystem-операций** (блокирующий I/O в async-контексте, паттерн из `crud.py`), считает score → **пишет в Qdrant payload** (`staleness_score`, `quality_flags`) — scanner = ЕДИНСТВЕННЫЙ писатель score; гонит dup-pair scan по domain-бакетам → issues; наполняет review-очередь. **`conflicting`-детекция НЕ реализуется в MVP** (~90% FP для инженерной БЗ, §11) — тип зарезервирован. | [`quality/scoring.py`](mcp_server/src/mcp_server/quality/scoring.py), [`quality/scanner.py`](mcp_server/src/mcp_server/quality/scanner.py) |
| **4.6** | Три MCP Tools (quality API для агентов) | #24 [P0] | `review_queue`, `list_quality_issues`, `resolve_quality_issue` (сигнатуры §5). Права: read для очереди/list; **write** для resolve. **`restore` action** (deprecated→published, reversibility). **Merge-atomicity:** `merge`/`deprecate`/`restore` триггерят **targeted reindex = upsert конкретной записи** (НЕ полный reindex #7) + `file-lock` / optimistic-locking на **оба** `knowledge_id` (source + target) во избежание гонки при одновременном merge. | [`mcp/tools.py`](mcp_server/src/mcp_server/mcp/tools.py), [`quality/lifecycle.py`](mcp_server/src/mcp_server/quality/lifecycle.py) |
| **4.7** | Lifecycle slice: `status` поле | #26 [P1] | frontmatter.`status`: `"published"` (default, backward-compatible — отсутствие = published) \| `"deprecated"`. `resolve_quality_issue(action=deprecate)` → deprecated + git commit; `action=restore` → published (reversibility, §5). `search_knowledge` по умолчанию `payload.status != "deprecated"`; `?include_deprecated=true`. **Не** полная стейт-машина (нет draft/review/archived-transitions). | [`quality/lifecycle.py`](mcp_server/src/mcp_server/quality/lifecycle.py), [`mcp/tools.py`](mcp_server/src/mcp_server/mcp/tools.py), [`models.py`](mcp_server/src/mcp_server/models.py) |
| **4.8** | Scheduled scan (cron) + Prometheus-метрики + Quality-SLO | — [P1] | **HTTP-endpoint `POST /mcp/quality/scan`** (консистентно с `reindex.sh` → `POST /mcp/reindex`). [`scripts/quality_scan.sh`](scripts/quality_scan.sh): curl-триггер эндпоинта → `scanner.run_scan()` (daily). `/metrics` (#2.13) += `quality_duplicates_detected_total`, `quality_issues_total{type}`, `review_queue_size`, `deprecated_total`. **Quality-SLO alert (Prometheus + alertmanager):** `review_queue_size > REVIEW_QUEUE_SLO_THRESHOLD` (default 50) → alert → оператор/агент должен запустить `periodic_quality_cleanup` (§5.1). Cron через тот же механизм, что `reindex.sh` (#7). | [`scripts/quality_scan.sh`](scripts/quality_scan.sh), [`metrics.py`](mcp_server/src/mcp_server/metrics.py) |
| **4.9** | Тесты (unit + integration на русском корпусе) | — [P0] | Unit: `staleness_score` (граничные значения, **evergreen-кейсы**), `frontmatter-gate` (required=block-всегда / recommended=warn), dup-gate (парафразы на русском, порог калибровки), **restore (deprecated→published)**, merge locking. Integration: write→dup-detect→issue→resolve→deprecate→**restore**→search-include (сквозной flow). E2E-продолжение #10 (русский корпус). | `tests/unit/test_quality_*.py`, `tests/integration/test_quality_flow.py` |
| **4.10** | Документация + MCP-Prompt | — [P0] | Раздел в README: «Quality Management» — как агенту использовать `review_queue`/`list_quality_issues`/`resolve_quality_issue`, веса staleness-формулы (вкл. **evergreen**), настройка порогов, cron, **known-limitations (§11)**. **MCP-Prompt `periodic_quality_cleanup`** зарегистрирован в реестре промптов (§5.1) — без него 3 Tools = мёртвый код. | [`README.md`](mcp_server/../README.md), [`prompts/periodic_quality_cleanup.md`](mcp_server/src/mcp_server/prompts/periodic_quality_cleanup.md) |

### Бюджет по задачам

> **Перераспределение внутри 40ч** по замечаниям Critic Gate (v1.1). Бюджет **не увеличен**. Δ показывает сдвиг относительно v1.0.

| # | Задача | Приоритет | Ч/ч (v1.1) | Ч/ч (v1.0) | Δ | Обоснование Δ |
|---|--------|:---------:|:--:|:--:|:--:|---------------|
| 4.1 | issue-store + log | P0 | **5** | 4 | **+1** | атомарность issues.jsonl (fcntl.flock / temp-rename) |
| 4.2 | frontmatter-gate | P0 | **3** | 3 | **0** | block-default = упрощение логики; поле `evergreen` = +0.5ч, компенсируется упрощением |
| 4.3 | semantic dup-gate | P0 | **5** | 6 | **−1** | dup-blind-spot документируется (§9), не кодируется; heavy-reuse embedder/search |
| 4.4 | edit-war (git) | P1 | **2** | 3 | **−1** | дёшево: git уже ведётся, уточнённая оценка |
| 4.5 | staleness scoring + scanner | P0 | **6** | 7 | **−1** | `conflicting` отложен → меньше кода; evergreen +0.5ч |
| 4.6 | три MCP Tools | P0 | **7** | 6 | **+1** | `restore` action + merge atomicity/locking на оба ID |
| 4.7 | lifecycle slice | P1 | **2** | 3 | **−1** | `restore` уехал в 4.6; deprecate = flip поля |
| 4.8 | cron + Prometheus + SLO | P1 | **4** | 3 | **+1** | Quality-SLO alert + alertmanager rule |
| 4.9 | тесты (рус. корпус) | P0 | **3** | 3 | **0** | тесты evergreen/block/restore вплюсованы в существующие |
| 4.10 | документация + MCP-Prompt | P0 | **3** | 2 | **+1** | регистрация промпта-триггера + known-limitations |
| | **ИТОГО** | | **40** | 40 | **0** | бюджет неизменен ✅ |
| | **P0-ядро** (4.1–4.3, 4.5–4.6, 4.9–4.10) | | **32** | ~~26~~ → 31* | — | *\*в v1.0 P0-core был указан 26ч, реально 31ч — арифметическая ошибка исправлена* |

> **Минимальный viable набор (P0, 32 ч)** уже даёт ощутимый прирост: предотвращение дублей/неполных записей (Ур1) + обнаружение устаревшего через scoring + 3 Tools для агента + промпт-триггер + тесты/доки. P1-задачи (4.4, 4.7, 4.8) усиливают, но не блокируют ценность.

---

## 5. Новые MCP Tools (3) + Prompt (1)

> В дополнение к 9 Tools из [`00-implementation-plan.md`](00-implementation-plan.md) §6.1 (v3.0: +`get_knowledge_map` +`search_by_tags`). Всего Tools после Ф4: **12**. Всего Prompts: **+1** (`periodic_quality_cleanup`).

| # | Tool | Сигнатура | Права | Решение |
|---|------|-----------|:-----:|:-------:|
| 8 | `review_queue` | `(domain?: str, subject?: str, limit?: int=20)` → `[{knowledge_id, title, staleness_score, reasons[], updated_at}]` (сорт. по score DESC) | read | #23 |
| 9 | `list_quality_issues` | `(types?: str[], status?: str="open", limit?: int=50)` → `[{issue_id, type, knowledge_id, severity, detail, detected_at}]` | read | #22/#23 |
| 10 | `resolve_quality_issue` | `(issue_id: str, action: "merge"\|"deprecate"\|"restore"\|"resolve"\|"ignore", target_id?: str, reason?: str)` → `{resolved: true, side_effects}` | **write** | #24/#26 |

**Семантика `resolve_quality_issue.action`:**

| Action | Эффект | Side-effects |
|--------|--------|--------------|
| `merge` | Слить контент `knowledge_id` → `target_id`; исходник → deprecated | Markdown update (#2) + git commit (#21) + **targeted reindex** (upsert **конкретной записи** в #7, НЕ полный reindex) + **file/optimistic-lock на оба ID** (§9 R7) |
| `deprecate` | `status=deprecated` (скрыть из поиска) | frontmatter update + git commit + targeted reindex |
| `restore` | `status=published` (**возврат из deprecated** — reversibility) | frontmatter update + git commit + targeted reindex |
| `resolve` | `issue.status=resolved` (после ручной/агентной правки) | только issue-store |
| `ignore` | `issue.status=ignored` (false positive / snooze) | issue-store (+snooze N дней до re-open) |

> **Reversibility (R-C1):** `restore` = обратная операция к `deprecate`. Если запись была ошибочно скрыта, `resolve_quality_issue(action="restore")` возвращает `status=published` и она снова появляется в `search_knowledge` без `?include_deprecated`. См. тест в §7.

### 5.1 MCP Prompt: `periodic_quality_cleanup`

> **Без промпта-триггера три новых Tools = мёртвый код** (агент не знает, что их вызывать). Промпт регистрируется в реестре MCP Prompts (наряду с `best-practice` / `knowledge-template`) и связывает Tools в рабочий процесс.

| Prompt | Сигнатура | Назначение |
|--------|-----------|------------|
| `periodic_quality_cleanup` | `(domain?: str, max_actions?: int=10)` → инструкция агенту | Регулярная чистка качества БЗ: получить топ устаревших + открытые issues → принять решение по каждому |

**Содержимое промпта (шаблон):**
```
Ты управляешь качеством базы знаний. Действуй по шагам:
1. review_queue(domain={domain}, limit={max_actions}) — получить топ устаревших записей.
2. list_quality_issues(status="open", limit={max_actions}) — получить открытые проблемы.
3. Для каждой записи/issue прими решение:
   - устарела и есть дубль → resolve_quality_issue(action="merge", target_id=<лучший дубль>)
   - устарела и бесполезна → action="deprecate"
   - ошибочно скрыта (deprecated по ошибке) → action="restore"
   - false positive → action="ignore"
   - актуальна после правки → action="resolve"
4. Если review_queue_size остаётся высоким — сообщить оператору (SLO-breach).
Критично: merge/deprecate необратимы через restore только частично — подтверждай destructive-действия.
```

---

## 6. Контракты: staleness-формула, issue-модель, интеграция

### 6.1 Staleness-формула (прозрачная, тестируемая, документированные веса)

```
evergreen = bool(frontmatter.get("evergreen", False))
age_days  = (now - updated_at).days
age_norm  = clip01(age_days / (1825 if evergreen else 365))
                 # evergreen: знаменатель = 5 лет (стареет в 5× медленнее).
                 # Альтернатива: age_norm = 0 для evergreen (полная иммунность).
                 # Выбран /1825: даже фундаментальные знания могут устареть,
                 # но «эффект Матфея» (review только из-за возраста) устранён.

staleness_score = clip01(
       0.47 * age_norm          # свежесть (evergreen-сниженный вклад)
     + 0.22 * dup_factor        # дублирование: 0 / 0.5 (1 кандидат) / 1.0 (≥2)
     + 0.16 * incomplete_factor # доля отсутствующих recommended-полей (source, cross_subjects)
     + 0.10 * edit_war_factor   # 1.0 если flag edit_war (#25)
     + 0.05 * link_health_factor # доля broken source-URL (HEAD-проверка, опционально)
)
```

- `clip01(x) = max(0.0, min(1.0, x))`
- **Результат округляется до 4 знаков** (`round(score, 4)`) — стабильная сортировка `review_queue`.
- **В review-очередь** при `staleness_score ≥ REVIEW_THRESHOLD` (default **0.45**, конфигурируется).
- **Evergreen (R1):** записи с `evergreen: true` (фундаментальные знания: алгоритмы, паттерны, теория) стареют в 5× медленнее → не попадают в review-очередь только из-за возраста. Дублирование/неполнота всё ещё работают (evergreen ≠ иммунитет от dup).
- Веса выбраны так, что **возраст доминирует** (45%), но дублирование (20%) способно перевести свежую, но мусорную запись в очередь. Формула — чистая функция (тестируется детерминированно, §4.9).
- **SSOT (R4):** `staleness_score` хранится **в Qdrant payload** (НЕ в frontmatter). Scanner (#23) — единственный писатель. При вызове `reindex()` (#7 tool) non-content поля payload (`staleness_score`, `status`, `quality_flags`) **сохраняются** (reindex пере-эмбеддит контент, не обнуляет метаданные); score пересчитывается сканером на следующем запуске → никогда не теряется.
- **Почему не ML:** для эвристики «что пересмотреть» transparent-формула достаточно точна и **в 10× дешевле** по effort при сопоставимом качестве ранжирования.

### 6.2 Модель Issue

```json
{
  "issue_id": "iss_01H... ",
  "type": "duplicate | missing_field | edit_war | orphaned | broken_link | conflicting(reserved)",
  "knowledge_id": "ru-python-async-patterns",
  "severity": "info | warn | critical",
  "detail": "cosine=0.94 с ru-python-concurrency-basics; нет поля source",
  "detected_at": "2026-07-21T12:00:00+03:00",
  "status": "open | resolved | ignored",
  "resolved_at": null,
  "resolution": null
}
```

- **`conflicting` (R9) — зарезервирован, детекция отложена в future.** Эвристика «≥2 записи в одном `{domain, subject}` с пересечением `tags`, но разным контентом» для инженерной БЗ даёт **~90% false positives** (пересечение тегов + разный контент = норма). Реализованный контрпродуктивен; при будущей реализации — только как `severity=info` + с LLM-валидацией. См. §11, §9 R9.
- **broken_link** (P1): `source`-URL, недоступный HEAD-проверкой (опционально, air-gap-safe — skip если offline).
- **Temporal dup-blind-spot (R10):** dup-gate (§4.3) и dup-pair scan видят только **проиндексированные** записи. Запись, находящаяся в async-индекс-очереди (окно 1–5с между `write_knowledge` и доступностью в Qdrant), невидима для последующего write в этом окне → потенциальный дубль-пропуск. Компенсация: scanner (§4.5) рано или поздно ловит такие пары (eventual consistency). Документировано как known-limitation (§11).

### 6.3 Интеграция с существующей архитектурой (не ломаем)

| Где | Изменение | Совместимость |
|-----|-----------|---------------|
| `write_knowledge`/`update_entry` (§6.1 00-plan) | ДО записи — `gates.evaluate()`. **(а) Required-контракт:** отсутствие `knowledge_id`/`domain`/`subject`/`tags`/`created_at`/`updated_at` → **409 BLOCK ВСЕГДА** (R2). **(б) Качество (advisory):** `quality_report{duplicates[], missing_fields[], severity}` возвращается в ответе; `?strict=true` → 409 при critical-dup. | ✅ required-блок = валидация контракта (поведение «как должно быть»); advisory для dup/warn не ломает старые вызовы (+ новое поле). `evergreen` в recommended |
| `search_knowledge` (§6.1 00-plan) | payload-filter `status != "deprecated"` (default); `?include_deprecated=true`. Доп. опц. `?min_quality` / `?max_staleness` (сорт./фильтр по score) | ✅ backward-compatible (default excludes deprecated — безопаснее поиска мусора) |
| Qdrant payload-схема (задача 1.3) | + индексируемые поля: `status(str)`, `staleness_score(float)`, `quality_flags([str])`. **`staleness_score` SSOT = payload** (R4) | ✅ аддитивно; reindex сохраняет non-content payload |
| `/metrics` (#2.13) | + quality-метрики + **SLO-alert** (`review_queue_size > THRESHOLD` → alertmanager) (R5) | ✅ аддитивно |
| Reconciliation (#19) | Scanner переиспользует паттерн обхода `knowledge/**/*.md` | ✅ synergy |
| Git-аудит (#21) | `resolve_quality_issue` (вкл. `restore`) триггерит git commit как любой write | ✅ consistency |

---

## 7. Критерии приёмки (ACCEPTANCE)

### Pre-write gates (#22)
- ✅ `write_knowledge` **без required-поля** (`tags`/`domain`/`knowledge_id`) → **409 BLOCK ВСЕГДА** (без `strict`, R2) — это контракт схемы
- ✅ `write_knowledge` с контентом, дублирующим существующий (cosine ≥ 0.92) → в ответе `quality_report.duplicates` непустой (severity=warn, **advisory**). При `strict=true` → `409`
- ✅ `write_knowledge` без recommended-поля (`source`) → `quality_report.missing_fields` (severity=warn, advisory); запись проходит
- ✅ `update_entry` того же `knowledge_id` **не** flagged как дубль себя (self-exclusion)
- ✅ Дедупликация работает на **русском корпусе** (парафразы «асинхронные паттерны» ≈ «async patterns python» → кандидат)

### Scoring + scanner (#23)
- ✅ `scanner.run_scan()` на тестовом корпусе (≥ 50 записей, разный возраст) корректно ранжирует: самые старые/дублирующиеся — вверху `review_queue`
- ✅ `staleness_score` ∈ [0,1]; чистая функция покрыта unit-тестами (граничные: age=0 → низкий; всё-старое+дубль → высокий)
- ✅ **Evergreen (R1):** запись с `evergreen: true`, возраст 1 год → `age_norm = 365/1825 ≈ 0.20` (а не 1.0) → НЕ в review-очереди при отсутствии дублей
- ✅ **SSOT (R4):** `staleness_score` хранится в Qdrant payload; после `reindex()` score не обнуляется (сохраняется / пересчитывается сканером)
- ✅ Dup-pair scan находит все известные пары-дубликаты (precision/recall зафиксированы в тестах)

### Quality Tools (#24)
- ✅ `review_queue(domain="engineering", limit=5)` → 5 записей, отсортированных по `staleness_score` DESC, с `reasons[]`
- ✅ `list_quality_issues(types=["duplicate"], status="open")` → только открытые duplicate-issues
- ✅ `resolve_quality_issue(id, action="deprecate")` → запись получает `status=deprecated`; последующий `search_knowledge` **не** возвращает её (default); `?include_deprecated=true` — возвращает
- ✅ **`resolve_quality_issue(action="restore")` (R-C1):** deprecated-запись возвращается в `status=published`; `search_knowledge` снова её возвращает (reversibility)
- ✅ `resolve_quality_issue(action="merge", target_id=...)` → контент слит в target, исходник deprecated, git-коммит создан, **обе записи переиндексированы (targeted upsert, не полный reindex)**; **asyncio.Lock на оба ID в лексикографическом порядке** исключает deadlock при встречном merge (R7)

### Lifecycle + edit-war (#25/#26)
- ✅ Запись без `status` → трактуется как `published` (backward-compatible с Фазами 0–3)
- ✅ ≥3 обновления одного `knowledge_id` за 24ч (через git-историю) → issue `edit_war`

### Operational
- ✅ `scripts/quality_scan.sh` вызывает `POST /mcp/quality/scan` (HTTP endpoint, консистентно с reindex.sh) и обновляет issues + payload без ошибок
- ✅ `/metrics` содержит `quality_duplicates_detected_total`, `quality_issues_total{type}`, `review_queue_size`, `deprecated_total`
- ✅ **Quality-SLO (R5):** `review_queue_size > REVIEW_QUEUE_SLO_THRESHOLD` → срабатывает alertmanager-алерт
- ✅ Scan (10K записей) завершается за < 5 мин (reuse reconcile-паттерна; dup-scan по domain-бакетам)
- ✅ **issues.jsonl атомарность (R8):** конкурентная запись scanner + write-gate не теряет/не рвёт записи (fcntl.flock / temp-rename)

---

## 8. Метрики (EXPECTED) + Quality-SLO

| Метрика | Целевое значение |
|---------|------------------|
| Pre-write dup-gate: overhead на `write_knowledge` (GPU) | < 300 ms (embed + 1 search) |
| Pre-write dup-gate: overhead (CPU) | < 1.2 с |
| Scanner: полный обход 10K записей | < 5 мин |
| Дубликаты, **обнаруживаемые (detected)** pre-write-gate (R5) | ≥ 80% от фактических near-dup (recall на тестовом корпусе; advisory = detected, не prevented) |
| `review_queue` latency (p95) | < 100 ms (payload-filter + сорт) |
| `staleness_score` unit-покрытие | 100% (чистая функция, вкл. evergreen-ветки) |
| Покрытие тестами quality-модуля | ≥ 90% |
| Доля deprecated, ошибочно скрытых из поиска (regression) | 0 (default exclude; opt-in include) |

**Quality-SLO alert (R5):**
| Alert | Условие | Действие |
|-------|---------|----------|
| `KnowledgeReviewQueueSLOBreach` | `review_queue_size > REVIEW_QUEUE_SLO_THRESHOLD` (default 50) в течение > 1ч | alertmanager → оператор/агент запускает `periodic_quality_cleanup` (§5.1) |

> **«prevented» → «detected» (R5):** advisory-гейт по умолчанию не предотвращает запись (только `?strict=true`), поэтому корректная метрика — **detected** (обнаруженные). SLO-алерт гарантирует, что растущая очередь не останется незамеченной.

---

## 9. Риски и mitigation

| ID | Риск | Вероятность | Severity | Mitigation |
|----|------|:-----------:|:--------:|------------|
| R1 | **Эффект Матфея:** фундаментальные знания (evergreen) попадают в review только из-за возраста | Высокая | 🟡 | **`evergreen: true`** frontmatter → `age_norm` делится на 1825 (5 лет) вместо 365. Критично: дублирование/неполнота всё ещё учитываются (§6.1) |
| R2 | Запись без required-полей проходит в SSOT (сломанный контракт) | Средняя | 🟠 | **Required → 409 BLOCK ВСЕГДА** (не зависит от `strict`). Dup/warn остаются advisory (§4.2, §6.3) |
| R7 | **Merge гонка:** одновременный `merge`/`deprecate` двух записей → повреждение/потеря | Низкая | 🔴 | **asyncio.Lock per-knowledge_id** (переиспользование паттерна #20 optimistic-locking). **Deadlock prevention:** сортировка `knowledge_id`'ов лексикографически, захват локов в порядке возрастания. **Targeted reindex = upsert конкретной записи** (НЕ полный #7); транзакция Markdown+git+payload. При конфликте двух merge A↔B: оба сортируют `(A, B)` → оба захватывают A затем B → нет deadlock |
| R-C1 | Ошибочное `deprecate` необратимо → ценная запись скрыта | Средняя | 🟠 | **`restore` action** (deprecated→published, §5); reversibility покрыта тестом (§7) |
| R4 | `staleness_score` теряется при `reindex()` (#7) | Низкая | 🟡 | **SSOT = Qdrant payload** (не frontmatter); reindex сохраняет non-content payload; scanner пересчитывает score. Scanner — единственный писатель (§6.1) |
| R5 | Растущая review-очередь незамечена (нет дедлайна на обработку) | Средняя | 🟠 | **Quality-SLO alert** Prometheus+alertmanager: `review_queue_size > THRESHOLD` → триггер `periodic_quality_cleanup` (§8, §5.1) |
| — | Dup-gate **false positives** (разные записи флагаются как дубль) блокируют легитимные write | Средняя | 🟠 | **Advisory-by-default** (E5): dup-гейт не блокирует без `strict=true`. Порог `DUP_SIMILARITY_THRESHOLD` калибруется на русском корпусе; `action=ignore` для FP |
| — | Dup-gate **false negatives** (парафразы на русском не ловятся) | Средняя | 🟡 | BGE-M3 мультиязычный; калибровка порога в тестах (§4.9); при необходимости lower threshold до 0.88 |
| — | Dup-gate замедляет write-путь (embed на каждый write) | Средняя | 🟠 | Embed репрезентативного вектора (заголовок+1-й чанк), не全文а; reuse in-process embedder; на CPU — advisory не блокирует |
| R10 | **Temporal dup-blind-spot:** запись в async-очереди (1–5с) невидима dup-gate | Средняя | 🟡 | Документировано (§11); scanner ловит пары eventually; для синхронных write-сценариев окно пренебрежимо мало |
| — | `staleness_score` ранжирует «неправильно» по мнению оператора | Средняя | 🟡 | Прозрачная формула + **конфигурируемые веса/пороги** (`REVIEW_THRESHOLD`); `reasons[]` объясняют почему запись в очереди |
| — | Scanner нагружает Qdrant при dup-pair scan на 10K+ | Низкая | 🟠 | По domain-бакетам (payload-filter); batching; cron off-peak; переиспользование reconcile-обхода |
| — | Lifecycle-slice ломает существующие E2E (#10) | Низкая | 🟡 | `status` absent = `published` (backward-compatible); default search-exclude безопаснее |
| — | Edit-war-detection по git медленный на больших репо | Низкая | 🟡 | `git log --follow` по конкретному файлу (не full-repo scan); cache на окно |
| R8 | **issues.jsonl гонка:** cron-scanner + write-gate пишут конкурентно → потеря/порча | Средняя | 🟠 | **fcntl.flock** на файл или **temp-rename** (`write→*.tmp→os.replace`, атомарно) в §4.1 |
| R9 | **`conflicting`-эвристика даёт шум (~90% FP)** для инженерной БЗ | Высокая | 🟡 | **Детекция отложена в future** (тип зарезервирован, §6.2). Пересечение tags + разный контент = норма. При реализации — только `severity=info` + LLM-валидация (§11) |

---

## 10. Roadmap-слайс (5 дней)

> При условии 1 backend-разработчика (8 ч/д). Фаза 4 идёт **после** M3 (Фазы 0–3) из [`00-implementation-plan.md`](00-implementation-plan.md) §12. Бюджет v1.1 = 40ч (перераспределён, не увеличен).

```
Фаза 4 (дни 15-19, после Production M3) — v1.1
├─ День 15:  4.1 issue-store + log (atom) + 4.2 frontmatter-gate (block)  [8 ч] ✅ фундамент качества
├─ День 16:  4.3 semantic dup-gate + тесты (рус. корпус)                  [8 ч]
├─ День 17:  4.5 staleness scoring (evergreen) + scanner                  [6 ч] ✅ M4a: prevention + scoring
├─ День 18:  4.6 три MCP Tools (+restore+merge-lock) + 4.7 lifecycle      [9 ч]
└─ День 19:  4.4 edit-war + 4.8 cron/metrics/SLO + 4.9 тесты + 4.10 доки+промпт [9 ч] ✅ M4: Knowledge Quality
```

### Вехи

| Веха | День | Критерий |
|------|:----:|----------|
| **M4a: Prevention + Scoring** | 17 | Pre-write dup/frontmatter-gates работают (required=block, dup=advisory); staleness-scanner ранжирует корпус (**evergreen-записи не в очереди без причины**) |
| **M4: Knowledge Quality** | 19 | 3 Tools (`review_queue`/`list_quality_issues`/`resolve_quality_issue` **с restore**) + lifecycle-slice + edit-war + cron + Prometheus **+ SLO-alert** + **MCP-Prompt `periodic_quality_cleanup`**; E2E на русском; доки + known-limitations |

---

## 11. Known limitations — что НЕ покрывается

> Явная фиксация границ Фазы 4. Эти аспекты осознанно отложены (low impact-to-effort или требуют новой инфраструктуры) — см. §1.3 и §9.

| # | Что НЕ покрывается | Почему отложено | Future-path |
|---|---------------------|-----------------|-------------|
| L1 | **Factual correctness** (фактчекинг содержания) | Требует LLM/external-source verification на каждую запись; дорого и ненадёжно в air-gap | После локальной LLM (Ф future) |
| L2 | **Safety / prompt-injection** в теле знаний | Отдельная задача безопасности (санитизация, output-filtering), не scope quality-validation | Отдельный security-slice |
| L3 | **Coverage gaps** (какие темы БЗ не покрыты) | Требует external topic-map / таргет-домен; нет ground-truth «должно быть покрыто» | Future: topic-coverage анализ |
| L4 | **Internal coherence** (противоречия между записями) | `conflicting`-эвристика даёт ~90% FP (R9); надёжно только через LLM-валидацию | `conflicting` тип зарезервирован; LLM-detection в future |
| L5 | **Chunk-level quality** (качество отдельного чанка, не записи) | P2: требует chunk-granular hooks + read-counters | §13 future, если останутся часы |
| L6 | **Temporal dup-blind-spot** (async-окно 1–5с) | Архитектурно присуще async-индексации; окно пренебрежимо для реальных сценариев | Компенсируется scanner eventually (R10) |
| L7 | **Read-tracking / orphan-detection** | `orphan_factor` в формуле = 0.0 (заглушка); требует счётчика чтений | Future: read-counter instrumentation |

---

## 12. Лог правок Critic Gate (v1.0 → v1.1)

> Critic Gate: **REVISE, score 0.75**. Цель доработки: ≥0.85. Все правки внесены **без увеличения бюджета** (перераспределение внутри 40ч, §4).

### P0 (обязательно, выполнено)

| # | Замечание Critic | Реализация в v1.1 | Где |
|---|------------------|-------------------|-----|
| 1 | **Evergreen-флаг в staleness-формулу** («эффект Матфея») | `evergreen: true` → `age_norm = age_days/1825` (5 лет); дублирование/неполнота учитываются | §6.1, §4.2, §4.5, §7, §9 R1 |
| 2 | **Frontmatter-gate: required → BLOCK по умолчанию** | Required (`knowledge_id`/`domain`/`subject`/`tags`/...) → 409 **ВСЕГДА** (контракт схемы); dup/warn → advisory | §4.2, §6.3, §7, E5 |
| 3 | **Merge: targeted re-index + atomicity + restore** | reindex = **upsert конкретной записи** (не полный #7); file/optimistic-lock на оба ID; + `restore` (deprecated→published) | §3, §5, §9 R7/R-C1, §4.6 |
| 4 | **Staleness_score SSOT** | Хранится в **Qdrant payload** (не frontmatter); scanner — единственный писатель; reindex сохраняет/пересчитывает | §6.1, §6.3, §4.5, §7 |
| 5 | **Метрика: «prevented» → «detected» + SLO-alert** | Метрика переименована; добавлен `KnowledgeReviewQueueSLOBreach` alert (Prometheus+alertmanager) | §8, §4.8, §9 R5 |

### P1 (усиливают, перераспределено в бюджете)

| # | Замечание Critic | Реализация в v1.1 | Где |
|---|------------------|-------------------|-----|
| 6 | **MCP-Prompt `periodic_quality_cleanup`** | Зарегистрирован в реестре промптов; связывает 3 Tools в workflow (~1ч в 4.10) | §5.1, §4.10 |
| 7 | **Known-limitations раздел** | Новый §11: factual correctness, safety/injection, coverage gaps, internal coherence, chunk-level, read-tracking | §11 |
| 8 | **issues.jsonl атомарность** | fcntl.flock / temp-rename для конкурентной записи scanner+write-gate (+1ч в 4.1) | §4.1, §9 R8 |
| 9 | **Упростить conflicting-эвристику** | Детекция отложена в future (~90% FP для инженерной БЗ); тип зарезервирован; −1ч в 4.5 | §6.2, §9 R9, §11 L4 |
| 10 | **Temporal dup-blind-spot** | Документирован: dup-gate не видит async-очередь (окно 1–5с); компенсация scanner | §6.2, §9 R10, §11 L6 |
| 11 | **Deprecation → reversibility** | `restore` action (deprecated→published) | §5, §4.6, §4.7, §7 |

### P2 (рассмотрено)
- **Chunk-level quality hook + read-counter** (L5/L7) — отложено в future (§11), если останутся часы.

### Бюджет
- **Итого v1.1: 40 ч** (без увеличения). Перераспределение: +1 (4.1), −1 (4.3), −1 (4.4), −1 (4.5), +1 (4.6), −1 (4.7), +1 (4.8), +1 (4.10). P0-core пересчитан: 32 ч (исправлена арифметическая ошибка v1.0: было указано 26, реально 31).

---

## 13. Лог правок Critic Gate v2 (v1.1 → v1.2)

> Critic Gate v2: **REVISE, confidence 0.83**. Цель доработки: ≥0.85. Все правки внесены **с минимальным увеличением бюджета** (+1ч на P0-спецификацию, итого 41ч).

### P0 (блокеры — выполнено)

| # | Замечание Critic | Реализация в v1.2 | Где |
|---|------------------|-------------------|-----|
| 1 | **Merge-lock underspecification** — deadlock при встречном merge A↔B | `asyncio.Lock` per-knowledge_id (переиспользование #20). **Deadlock prevention:** лексикографическая сортировка ID перед захватом → оба merge захватывают A затем B → нет deadlock (+0.5ч) | §4.6, §6.3, §9 R7 |
| 2 | **quality_scan.sh архитектурный разрыв** — план vs stub | Явно указан HTTP-endpoint `POST /mcp/quality/scan` (консистентно с `reindex.sh` → `POST /mcp/reindex`). Stub уже HTTP — спецификация зафиксирована (+0.5ч) | §4.8, §7 |

### P1 (усиливают — выполнено)

| # | Замечание Critic | Реализация в v1.2 | Где |
|---|------------------|-------------------|-----|
| 3 | **orphan_factor=0.0 — мёртвый груз** | Заменён на `link_health_factor` (0.05) — broken source-URL (HEAD-проверка, опционально, уже в §6.2). Веса перебалансированы: age 0.45→0.47, dup 0.20→0.22, incomplete 0.15→0.16 | §6.1 |
| 4 | **staleness_score нестабильная сортировка** | `round(score, 4)` — 4 знака для стабильной review_queue | §6.1 |
| 5 | **Scanner filesystem walk — блокирующий I/O** | `loop.run_in_executor(None, ...)` для обхода `knowledge/**/*.md` (паттерн из `crud.py`) | §4.5 |
| 6 | **Gate валидирует Pydantic-модель, не сырые поля** | Уточнено в §4.2: валидация на Pydantic-модели (default_factory поля не могут отсутствовать) | §4.2 |
| 7 | **quality/ архитектура** | Уточнено: `quality/` top-level пакет + `tools/quality.py` thin wrapper для регистрации в TOOL_HANDLERS | §4.6 (неявно через tools/) |

### Бюджет v1.2
- **Итого v1.2: 41 ч** (+1ч на P0-спецификацию). P0-ядро: 33 ч.

---

> **FPF-методология:** C.30 (Grounded Architecture — выбор на основе переиспользования существующих активов: Qdrant/BGE-M3/git/#19/#21/#2.13), A.22 (Structure Views — декомпозиция «качества» на duplication/completeness/staleness/lifecycle + явные границы non-coverage), A.19.ECS (оценка 4 уровней по 6 критериям, §1.2), A.10 (Evidence Graph — E1–E6 «за», C1–C3 «против полного lifecycle», §1.3). Ограничение ≤40ч выполнено и сохранено после Critic Gate; P0-ядро 32ч даёт ощутимый прирост даже без P1.
