# 📊 ФАЗА 5: Content Preprocessor Pipeline — модульный импорт крупных текстов

> **trace_id:** `code-2026-08-03-162` | **Автор:** analyst | **Дата:** 2026-08-03 (обновление v1.1)
> **Версия:** 1.1 | **Статус:** готов к реализации (после Critic Gate v2 — PASS 0.85)
> **Родительский план:** [`00-implementation-plan.md`](00-implementation-plan.md) **v3.1** (расширение, НЕ замена)
> **Зависимости:** Фазы 0–4 (особенно #2 Markdown SSOT, #3/#17 in-process BGE-M3 embedder, #19 reconciliation, #20 XLM-RoBERTa токенайзер, #21 git-аудит, #22 quality gates из Фазы 4 — реализованы)
>
> **История версий:**
> - **v1.0** (2026-07-29) — базовый план Фазы 5: 4 решения (#33–#36), 6 задач (5.1–5.6), ~30 ч. Модульная архитектура `ContentPreprocessor` (ABC) + registry; `BookPreprocessor` — первая реализация. Гибридное семантическое разбиение (structural → embedding clustering → recursive split). Parent-child коллекции. Best-effort batch + orphan cleanup (переиспользует reconciliation #19). Архитектурные решения приняты на брейншторме оператором.
> - **v1.1** (2026-08-03) — **обновление по результатам codebase audit + Critic Gate v2** (trace_id `code-2026-08-03-162`). План написан до реализации Ф4 и содержал 22 расхождения с реальным кодом (5 critical, 9 medium, 8 low) + P0-блокер schema.py payload pathway. Исправлено: пути (`mcp/tools.py` → `tools/` пакет, tool #13 → **#16**), 4 parent-child поля модели, `"orphaned"` в IssueType, `orphaned_detected` в ReconcileResult, P0 schema.py payload wiring, зависимости (nltk/yake/sklearn), бюджет **30h → 34.5h**. A.19.ECS: 0.697 → 0.876 (консенсус Critic: 0.867). См. §13 «Лог правок v1.1».
>
> **Ключевой вопрос:** *как импортировать большие неструктурированные тексты (книги, будущие PDF, дампы документации) в SSOT, сохранив качество retrieval и не сломав single-record write-контракт?*
> **Краткий ответ:** отдельный MCP Tool `import_content` + модульные препроцессоры (`content_type → preprocessor`) + гибридное разбиение на семантические секции + parent-child связывание (`collection` root) + best-effort batch с orphan cleanup через reconciliation. См. §1 FPF-обоснование.

---

## 📑 Содержание

1. [FPF-обоснование выбора стратегии](#1-fpf-обоснование-выбора-стратегии)
2. [Цель, приоритет, трудозатраты, новые решения](#2-цель-приоритет-трудозатраты-новые-решения)
3. [Архитектура (поток данных) + модульная структура](#3-архитектура-поток-данных--модульная-структура)
4. [Задачи (5.1–5.6)](#4-задачи-5156)
5. [Новый MCP Tool: `import_content`](#5-новый-mcp-tool-import_content)
6. [Контракты: frontmatter, splitting, linking, batch](#6-контракты-frontmatter-splitting-linking-batch)
7. [Критерии приёмки (ACCEPTANCE)](#7-критерии-приёмки-acceptance)
8. [Метрики (EXPECTED)](#8-метрики-expected)
9. [Риски и mitigation](#9-риски-и-mitigation)
10. [Roadmap-слайс (4 дня)](#10-roadmap-слайс-4-дня)
11. [Known limitations — что НЕ покрывается](#11-known-limitations--что-не-покрывается)
12. [Интеграция с существующей архитектурой (не ломаем)](#12-интеграция-с-существующей-архитектурой-не-ломаем)
13. [Лог правок v1.1 (codebase audit + Critic Gate v2)](#13-лог-правок-v11-codebase-audit--critic-gate-v2)

---

## 1. FPF-обоснование выбора стратегии

### 1.1 Постановка (C.30 — Grounded Architecture)

План [`00-implementation-plan.md`](00-implementation-plan.md) обеспечивает хранение/поиск **атомарных записей**: `write_knowledge` пишет один Markdown-файл, который разбивается на чанки ≤ 512 XLM-RoBERTa-токенов по границам `##` (#13, #20). Этот контракт оптимален для агентных записей (1 факт/паттерн = 1 запись), но **не приспособлен для крупных текстов**: книга в 100K+ токенов не может быть одной записью (chunks-взрыв, бесполезный `knowledge_id`, отсутствие навигации).

Применяем **C.30**: выбираем стратегию, которая **переиспользует уже построенные активы**, а не строит новую инфраструктуру.

**Существующие активы (из [`00-implementation-plan.md`](00-implementation-plan.md) + [`04-phase4`](04-phase4-knowledge-quality.md)), которые делают импорт «дешёвым»:**

| Актив | Где | Что даёт бесплатно для импорта |
|-------|-----|-------------------------------|
| In-process BGE-M3 embedder (#3/#17) | §5, Фаза 1 | Готовый движок **paragraph embedding** для семантической кластеризации секций (без внешних API, air-gap) |
| XLM-RoBERTa токенайзер (#20) | задача 1.4 | Точный подсчёт токенов для **recursive split** (гарантия ≤ 512 на чанк) |
| Markdown SSOT + frontmatter (#2) | §3.1 | Каждая секция = обычная `.md`-запись; **никаких новых форматов хранения** |
| Async indexing pipeline (#7) | задача 1.8 | Декомпозиция только готовит секции; индексация идёт через **существующий** pipeline (chunk → embed → Qdrant) |
| Git-аудит (#21) | задача 1.1 | Импорт = серия git-коммитов; бесплатная история, batch-коммит (1 commit на N секций) |
| Reconciliation (#19) | задача 2.9 | Обход `knowledge/**/*.md` уже есть → **orphan detection** добавляется как ещё одна проверка в том же проходе |
| Quality gates (#22) / issues.jsonl (Ф4) | §4.2/§4.1 | Orphans и неполные коллекции → тип issue `orphaned` в существующем логе |
| Qdrant payload filtering | задача 1.3 | `parent_knowledge_id`, `content_type` → индексируемые поля; `search_by_tags`/`get_knowledge_map` работают с коллекциями без изменений |

> **Следствие C.30:** импорт — это **слой подготовки (preprocessor)** перед существующим write-path, а не новый движок хранения. Декомпозиция → N обычных `write_knowledge`-операций.

### 1.2 Оценка стратегий разбиения — A.19.ECS Characteristic Space

Веса (как в [`04-phase4`](04-phase4-knowledge-quality.md) §1.2): Полнота 0.20 · Риски 0.20 · Ресурсы(Effort, инверт.) 0.20 · Сроки 0.15 · Стратегия 0.10 · Reusability 0.15.

| Стратегия | Полнота | Риски | Effort↑ | Сроки | Стратегия | Reusability | **Итог** |
|-----------|:------:|:-----:|:------:|:-----:|:--------:|:----------:|:-------:|
| **A. Fixed-size chunking** (naive split по 512 токенов) | 0.40 | 0.60 | 0.95 | 0.95 | 0.40 | 0.60 | **0.66** ❌ |
| **B. Embedding-clustering-only** (без structural) | 0.75 | 0.65 | 0.55 | 0.55 | 0.70 | 0.65 | **0.64** ❌ |
| **C. Structural-only** (только по #/##/###) | 0.65 | 0.80 | 0.90 | 0.85 | 0.65 | 0.70 | **0.77** 🥈 |
| **D. Hybrid** (structural → cluster fallback → recursive) | 0.90 | 0.80 | 0.65 | 0.70 | 0.90 | 0.85 | **0.79** 🥇 |

> **Почему не C (structural-only), хотя он дешевле:** plain-text дампы и PDF-извлечения **без заголовков** — нормальный сценарий (L2). C падает на них: одна секция = весь документ. D даёт fallback на embedding clustering, который сохраняет семантическую когерентность даже без структуры.

### 1.3 A.10 — Evidence Graph: почему выбран именно этот набор

**Выбранный набор (≤34.5 ч):** отдельный `import_content` tool + модульные препроцессоры + гибридное разбиение (D) + parent-child коллекции + best-effort batch.

**Цепочка доказательств «за»:**

| ID | Доказательство | Какой выбор поддерживает |
|----|----------------|--------------------------|
| **E1** | **Structure-first.** Книги и документация **изначально структурированы** (главы/разделы/заголовки). Игнорирование структуры (A) рвёт семантические единицы → retrieval-качество падает. Structural parsing дёшев и высокоточен, когда структура есть. | D (stage 1) |
| **E2** | **Fallback для неструктурированных.** Не весь контент имеет заголовки (plain-text дампы, OCR-вывод). Pure structural (C) на них не работает. Fallback на paragraph embedding + cosine clustering сохраняет семантическую когерентность секций. | D (stage 2) |
| **E3** | **Reuse.** BGE-M3 in-process embedder (#3/#17) уже есть → embedding clustering = «бесплатная» инфраструктура, **без новой модели/API**. Air-gap-совместимо (ONNX fallback). | D (stage 2), #34 |
| **E4** | **Defense-in-depth.** Recursive split гарантирует жёсткий инвариант (≤ 512 XLM-R-токенов/чанк, #13/#20) **независимо** от качества кластеризации. Даже oversized-секция будет разбита. | D (stage 3) |
| **E5** | **Contract separation.** `import_content` ≠ `write_knowledge`: write — single-record атомарный контракт; import — операция декомпозиции (N записей). Перегрузка write сломала бы quality gates (#22) и sync-семантику (#9). Отдельный tool сохраняет оба контракта чистыми. | #33 |
| **E6** | **Best-effort > transactional.** Reuse reconciliation (#19) + issues.jsonl (Ф4) для orphan cleanup — **без новой инфраструктуры консистентности**. Partial_success-контракт сохраняет работу (см. C1). | #36 |

**Почему отклонены альтернативы:**

| ID | Альтернатива | Контраргумент |
|----|--------------|---------------|
| **C1** | **Транзакционный batch** (all-or-nothing: 1 секция упала → rollback всех) | **Blast radius:** книга на 200 секций, где 1 секция падает на embed → rollback 199 хороших записей. Дорогой ре-импорт. Best-effort сохраняет работу; orphan cleanup (#19) лечит частичное состояние. |
| **C2** | **LLM-based decomposition** (умное суммаризацию глав, «smart splitting») | **Air-gap:** LLM недоступен офлайн; дорогой per-call. TF-IDF/YAKE для извлечения тегов — локальные, unsupervised, достаточно для advisory-тегов. LLM → future (после локальной LLM, как HyDE в [`00`](00-implementation-plan.md) §13.1). |
| **C3** | **Полный PDF-parsing сейчас** | PDF — отдельный `content_type`, требующий layout-анализа/OCR (`PyMuPDF`/`pdfplumber` + fallback Tesseract). Out of scope для первой реализации (`BookPreprocessor`). Архитектура registry **поддерживает** добавление `PdfPreprocessor` позже **без** изменения tool-контракта (§11). |

---

## 2. Цель, приоритет, трудозатраты, новые решения

**Цель:** Внедрить **модульную систему импорта крупных неструктурированных текстов** (книги, будущие PDF, дампы документации) в SSOT: декомпозиция на семантические секции, авто-gen frontmatter (вкл. теги через TF-IDF/YAKE), parent-child связывание в коллекции, и best-effort batch-загрузка с orphan cleanup. Всё — на базе существующих активов, **без** новой инфраструктуры хранения.

**Приоритет:** 🟠 MEDIUM (после работающего MVP Фаз 0–3; **не блокирует** MVP). Зависит от #2, #3/#17, #7, #19, #20, #21, #22 (Ф4 — желательно, но orphan-issue можно логировать и без quality-gate).
**Трудозатраты:** **~34.5 ч** (≈ 4.5 дня); **P0-ядро = 27 ч** (рабочий `import_content` + `BookPreprocessor` + гибридное разбиение + тесты; без orphan-detection в reconciliation и без batch-recovery контракта). *v1.0: 30h → v1.1: 34.5h (+4.5h на адаптацию к реальному коду: paths, model fields, schema.py payload, deps).*
**Новые решения:** **#33** `import_content` MCP Tool · **#34** Hybrid Semantic Splitting · **#35** Parent-Child Collection Linking · **#36** Best-Effort Batch + Orphan Cleanup

| # | Приоритет | Решение | Детали | Покрытие |
|---|:---------:|---------|--------|:--------:|
| 33 | 🟠 P1 | **`import_content` MCP Tool** (отдельный от `write_knowledge`) | `import_content(content, content_type, domain, subject, project?, title?, tags?, ...)` → декомпозиция через препроцессор → N write-операций. Reuse write-path (markdown_store + git #21 + pipeline #7). **НЕ** перегружает single-record write-контракт (#5) и quality gates (#22). Права: **write** | Фаза 5 |
| 34 | 🟠 P1 | **Hybrid Semantic Splitting** | 3-стадийная стратегия: (1) **structural parsing** по заголовкам `#`/`##`/`###`; (2) **fallback** — при отсутствии структуры ИЛИ oversized-секции: paragraph embedding (BGE-M3, in-process #3/#17) + cosine clustering (порог кластеризации); (3) **recursive split** — любая секция > `max_chunk_tokens` (512 XLM-R #20) → разбиение по границам предложений. Air-gap-совместимо (ONNX fallback) | Фаза 5 |
| 35 | 🟡 P2 | **Parent-Child Collection Linking** | Новые frontmatter-поля: `parent_knowledge_id`, `sequence_number`, `content_type`. Root-запись: `content_type: "collection"` — «оглавление» (список children: `{knowledge_id, title, sequence_number}`). Дети: `parent_knowledge_id=<root_id>`, `sequence_number=N`, `content_type=<исходный тип>`. Навигация: `get_entry(root_id)` возвращает TOC | Фаза 5 |
| 36 | 🟡 P2 | **Best-Effort Batch + Orphan Cleanup** | Best-effort: partial failure **не откатывает** успешные секции. Возврат: `{collection_id, imported: N, failed: M, failed_sections[], partial_success: bool}`. Orphan cleanup встроен в **reconciliation (#19)**: ребёнок без родителя / неполная коллекция → issue `orphaned` (reuse issues.jsonl Ф4) + опциональный auto-cleanup | Фаза 5 |

> **Принцип интеграции:** НЕ ломаем SSOT (#2) — новые frontmatter-поля **аддитивны** (backward-compatible, absent = обычная single-запись). НЕ дублируем pipeline (#7). НЕ меняем write-контракт (#5). Import = **слой подготовки** над существующим write-path.

---

## 3. Архитектура (поток данных) + модульная структура

```
   Agent (write-key)
      │  import_content(content, content_type="book", domain, subject, title?, tags?, ...)
      ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  content/registry.py — CONTENT PREPROCESSOR REGISTRY (#33)                │
   │   content_type → ContentPreprocessor (instance)                           │
   │   ┌─ "book"  → BookPreprocessor ────────────────────────────────────┐     │
   │   │  validate(content, meta) → ValidationResult                      │     │
   │   │  decompose(content, meta) → list[Section]                        │     │
   │   └──────────────────────────────────────────────────────────────────┘     │
   │   (future: "pdf" → PdfPreprocessor, "docs" → DocsPreprocessor ...)        │
   └──────────────┬───────────────────────────────────────────────────────────┘
                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  BookPreprocessor.decompose()  (#34)                                      │
   │   STAGE 1 — structural parse:  split by # / ## / ###  → Section[]         │
   │        ┌─ если секций ≥ MIN_SECTIONS и каждая ≤ max_chunk_tokens → DONE  │
   │        ▼  (иначе fallback)                                                │
   │   STAGE 2 — embedding clustering (fallback):                              │
   │        paragraph embedding (BGE-M3 in-process) →                          │
   │        cosine similarity matrix → agglomerative clustering                │
   │        (threshold CLUSTER_COSINE=0.75) → group смежных похожих абзацев   │
   │        ▼  (oversized чанк?)                                               │
   │   STAGE 3 — recursive split:  секция > max_chunk_tokens (XLM-R #20)       │
   │        → разбиение по границам предложений до ≤ max_chunk_tokens         │
   │   + keywords.py:  TF-IDF / YAKE → top-N тегов на секцию (advisory)        │
   │   + auto-frontmatter:  knowledge_id slug, title (ближайший заголовок),    │
   │                        domain/subject (унаследованы), timestamps          │
   └──────────────┬───────────────────────────────────────────────────────────┘
                  ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  content/linking.py — COLLECTION MODEL (#35)                              │
   │   • ROOT:  content_type="collection" (TOC: children[])                    │
   │   • CHILD: parent_knowledge_id=<root_id>, sequence_number=N,              │
   │            content_type=<исходный тип>                                     │
   └──────────────┬───────────────────────────────────────────────────────────┘
                  ▼   (best-effort batch, #36)
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  WRITE PATH — REUSE Ф0–Ф3 (НЕ дублируем!)                                 │
   │   per-section:  markdown_store.write() + git commit (#21, batch N/commit) │
   │                → indexing pipeline (#7): chunk → embed (in-proc) → Qdrant │
   │   • quality gates (#22, Ф4): advisory — дубли/missing в ответе            │
   │   • partial_success: упавшая секция → failed_sections[], остальные LIVE   │
   │   возврат: {collection_id, imported, failed, failed_sections,            │
   │             partial_success, indexed}                                      │
   └──────────────┬───────────────────────────────────────────────────────────┘
                  ▼
   Reconciliation (#19, augmented):
      • child с parent_knowledge_id, где parent отсутствует  → issue "orphaned"
      • collection-root с incomplete children                 → WARN + cleanup option
      • (логируется в data/quality/issues.jsonl — Ф4, fcntl.flock)
```

### 3.1 Модульная структура (новые файлы)

```
mcp_server/src/mcp_server/
├── content/                             # 🆕 Фаза 5: Content Preprocessor Pipeline
│   ├── __init__.py
│   ├── preprocessor.py                  # ContentPreprocessor (ABC) + ValidationResult / Section models
│   ├── registry.py                      # content_type → preprocessor (registry + lookup)
│   ├── book_preprocessor.py             # BookPreprocessor (первая реализация)
│   ├── splitting.py                     # Hybrid splitting: structural + clustering + recursive
│   ├── keywords.py                      # TF-IDF / YAKE keyword extraction → tags
│   └── linking.py                       # Parent-child collection model (root TOC + children)
├── tools/
│   ├── content.py                       # 🆕 import_content tool handler (#16 в реестре)
│   └── __init__.py                      # TOOLS + TOOL_HANDLERS (реальный паттерн регистрации)
└── indexing/
    └── reconcile.py                     # + orphan detection (augmented #19)
```

### 3.2 Абстракция препроцессора (контракт)

```python
class ContentPreprocessor(ABC):
    """Базовый контракт для всех content_type-препроцессоров."""

    content_type: str  # "book" | "pdf" | "docs" | ...

    @abstractmethod
    def validate(self, content: str, metadata: ImportMeta) -> ValidationResult:
        """Проверка пригодности контента (non-empty, размер, кодировка)."""

    @abstractmethod
    def decompose(self, content: str, metadata: ImportMeta) -> list[Section]:
        """Декомпозиция контента в упорядоченный список семантических секций."""

# Section = семантическая единица (→ одна knowledge-запись):
#   title: str            # ближайший заголовок / авто-сгенерированный
#   body: str             # Markdown-тело секции
#   sequence_number: int  # порядок в коллекции
#   tags: list[str]       # auto (TF-IDF/YAKE) ∪ inherited (из import params)
#   meta: dict            # доп. поля для frontmatter
```

> **Registry** (`content_type → preprocessor`) — точка расширения. Новый `content_type` (pdf, docs) = новый класс + 1 строка регистрации. **Без** изменения `import_content` tool или write-path. Это сознательный OCP (Open-Closed Principle).

---

## 4. Задачи (5.1–5.6)

| # | Задача | Решение | Детали | Файлы |
|---|--------|:-------:|--------|-------|
| **5.1** | Preprocessor interface + registry + `import_content` tool | #33 [P0] | `ContentPreprocessor` ABC (`validate()` + `decompose()` → `list[Section]`). `content/registry.py`: dict `content_type → preprocessor`, lookup с fallback на `ValueError("unknown content_type")` + список доступных типов в сообщении. MCP Tool `import_content` (§5): валидация → `registry.get(content_type).decompose()` → linking (#35) → batch write (reuse markdown_store + pipeline #7). Права: **write**. **Reuse:** markdown_store (#2), git (#21), pipeline (#7) — без дублей. **Регистрация:** `tools/content.py` + TOOLS/TOOL_HANDLERS в `tools/__init__.py` (tool **#16**). | [`content/preprocessor.py`](mcp_server/src/mcp_server/content/preprocessor.py), [`content/registry.py`](mcp_server/src/mcp_server/content/registry.py), [`tools/content.py`](mcp_server/src/mcp_server/tools/content.py), [`tools/__init__.py`](mcp_server/src/mcp_server/tools/__init__.py) |
| **5.2** | `BookPreprocessor`: structural parsing + auto-frontmatter + keyword extraction | #33/#35 [P0] | Реализация для `content_type="book"`. **Structural parse** (stage 1): regex/MD-парсинг заголовков `#`/`##`/`###` → секции (заголовок + тело до след. заголовка). **Auto-frontmatter:** `knowledge_id` = `{domain}-{subject}-{slug(title)}-{seq}` (slug kebab-case, транслит кириллицы); `title` = ближайший заголовок (или авто «Раздел N»); `domain`/`subject`/`tags` унаследованы из import params; `created_at`/`updated_at` = now. **Keyword extraction** (`keywords.py`, §6.4): TF-IDF (корпус = все секции книги) **и/или** YAKE → top-N (default 5) ключевых слов → candidate tags (kebab-case, стоп-слова RU+EN, дедуп с inherited). | [`content/book_preprocessor.py`](mcp_server/src/mcp_server/content/book_preprocessor.py), [`content/keywords.py`](mcp_server/src/mcp_server/content/keywords.py) |
| **5.3** | Hybrid semantic splitting: embedding clustering + recursive split | #34 [P0] | [`content/splitting.py`](mcp_server/src/mcp_server/content/splitting.py). **Stage 2 (fallback):** если structural-parse дал < `MIN_SECTIONS` ИЛИ есть oversized-секция → paragraph embedding (reuse in-process BGE-M3 #3/#17, через `run_in_executor` #1.6 — не блокировать event loop) → cosine similarity matrix相邻них абзацев → agglomerative clustering (порог `CLUSTER_COSINE`, default 0.75, калибруется в тестах) → группировка смежных похожих абзацев в чанки. **Stage 3 (recursive):** любой чанк > `max_chunk_tokens` (512 XLM-R #20) → recursive split по границам предложений (sent_tokenize, RU+EN) до ≤ лимита. **Гарантия:** после всех стадий **каждая секция ≤ max_chunk_tokens**. | [`content/splitting.py`](mcp_server/src/mcp_server/content/splitting.py) |
| **5.4** | Parent-child linking + orphan detection в reconciliation | #35/#36 [P1] | [`content/linking.py`](mcp_server/src/mcp_server/content/linking.py): создание root-collection записи (`content_type: "collection"`, TOC `children[]`) + проставление `parent_knowledge_id`/`sequence_number` на детях. **Orphan detection** — augment [`indexing/reconcile.py`](mcp_server/src/mcp_server/indexing/reconcile.py) (#19): в существующем проходе `knowledge/**/*.md` доп. проверка — (а) запись с `parent_knowledge_id`, где parent-файл отсутствует → issue `orphaned`; (б) `content_type: collection` с incomplete children (child missing/deleted) → WARN. Issues → `data/quality/issues.jsonl` (Ф4, fcntl.flock §4.1). Лог reconcile дополняется: `{..., orphaned_detected}`. | [`content/linking.py`](mcp_server/src/mcp_server/content/linking.py), [`indexing/reconcile.py`](mcp_server/src/mcp_server/indexing/reconcile.py) |
| **5.5** | Batch best-effort: partial_success контракт + orphan cleanup | #36 [P1] | `import_content` обрабатывает секции в цикле (batch write). **Partial failure:** секция падает (embed/store error) → catch → запись в `failed_sections[{sequence_number, title, error}]`, остальные продолжают. **Контракт возврата** (§5): `{collection_id, imported, failed, failed_sections[], partial_success, indexed}`. **Batch git-commit** (#21): 1 коммит на N секций (default `IMPORT_BATCH_COMMIT=10`), не 1 коммит/секция (производительность массового импорта). **Orphan cleanup action:** `?cleanup_orphans=true` (опц.) — при partial_success/interrupt удалить осиротевших детей через `delete_entry` (soft-delete + Qdrant + git). Default: только логировать (безопаснее). | [`tools/content.py`](mcp_server/src/mcp_server/tools/content.py), [`content/linking.py`](mcp_server/src/mcp_server/content/linking.py) |
| **5.6** | Тесты: structural book, plain text без заголовков, oversized секции, batch interrupt recovery | — [P0] | Unit: `splitting` (structural → секции; clustering → группы; recursive → ≤512 токенов), `keywords` (TF-IDF/YAKE на русском, стоп-слова, top-N), `linking` (root TOC + children sequence), `registry` (lookup/fallback). Integration: `import_content(book)` end-to-end → N записей в SSOT + Qdrant + git-коммиты; `get_entry(collection_id)` → TOC с детьми; orphan: удалить root-файл вручную → reconcile → issue `orphaned`. E2E-кейсы: (1) **структурная книга** (ясные #/##) — чистая декомпозиция; (2) **plain text без заголовков** — fallback на clustering; (3) **oversized секции** (>512 токенов) — recursive split; (4) **batch interrupt** (симуляция падения 1 секции) — `partial_success:true`, остальные LIVE, orphan cleanup. | `tests/unit/test_content_*.py`, `tests/integration/test_import_flow.py`, `tests/e2e/test_import_content.py` |

### Бюджет по задачам

| # | Задача | Приоритет | Ч/ч (v1.1) | Ч/ч (v1.0) | Δ | Обоснование Δ |
|---|--------|:---------:|:--:|:--:|:--:|-------------|
| 5.1 | Preprocessor interface + registry + `import_content` tool | P0 | **4.5** | 4 | +0.5 | адаптация к `tools/` пакету + tool #16 регистрация |
| 5.2 | `BookPreprocessor`: structural parse + auto-frontmatter + keywords | P0 | **7** | 6 | +1.0 | deps-check (nltk/yake fallback) + keywords TF-IDF |
| 5.3 | Hybrid splitting: clustering + recursive | P0 | **8.5** | 8 | +0.5 | embed_sync API + Ollama fallback |
| 5.4 | Parent-child linking + orphan detection | P1 | **5** | 4 | +1.0 | 4 поля модели + IssueType orphaned + orphaned_detected + schema.py payload |
| 5.5 | Batch best-effort + orphan cleanup | P1 | **3** | 3 | 0 | без изменений |
| 5.6 | Тесты (4 E2E-кейса + unit) | P0 | **6.5** | 5 | +1.5 | schema.py payload wiring + async fixtures + deps mocks |
| | **ИТОГО** | | **34.5** | 30 | **+4.5** | адаптация к реальному коду Ф0–Ф4 |
| | **P0-ядро** (5.1, 5.2, 5.3, 5.6) | | **27** | 23 | +4 | минимальный viable: рабочий импорт + разбиение + тесты, без orphan-detection/batch-recovery |

> **Минимальный viable набор (P0, 27 ч)** уже даёт рабочий `import_content`: структурная книга → N записей с auto-frontmatter + тегами + гибридное разбиение (вкл. fallback для plain text). P1-задачи (5.4, 5.5) добавляют коллекции/навигацию и устойчивость к partial failure, но не блокируют базовую ценность. *Бюджет пересчитан в v1.1 (+4.5h) на адаптацию к реальному коду Ф0–Ф4 (пути, model fields, schema.py payload, deps).*

---

## 5. Новый MCP Tool: `import_content`

> В дополнение к 15 Tools (11 base v3.0 + 4 quality из [`04-phase4`](04-phase4-knowledge-quality.md) §5). Всего Tools после Ф5: **16**. *v1.0: 12 → 13 (неверно — план считал 9 base tools; реально 11 base + 4 quality = 15).*

| # | Tool | Сигнатура | Права | Решение |
|---|------|-----------|:-----:|:-------:|
| 16 | `import_content` | `(content: str, content_type: str, domain: str, subject: str, project?: str, title?: str, tags?: str[], cross_subjects?: str[], max_chunk_tokens?: int=512, wait_for_index?: bool=false, cleanup_orphans?: bool=false)` → `{collection_id, imported: int, failed: int, failed_sections: [{sequence_number, title, error}], partial_success: bool, indexed: bool, pending?: bool, quality_report?: dict}` | **write** | #33–#36 |

**Семантика параметров:**

| Параметр | Назначение | Default |
|----------|-----------|---------|
| `content` | Исходный текст (Markdown/plain). Может содержать `#`/`##` заголовки | — (required) |
| `content_type` | Тип контента → выбор препроцессора через registry. v1.0: `"book"` | — (required) |
| `domain`, `subject`, `project?` | Классификация (унаследована всеми секциями-детьми) | — (required domain/subject) |
| `title?` | Заголовок root-коллекции (иначе — авто из первого заголовка/`content_type`) | auto |
| `tags?`, `cross_subjects?` | Inherited теги (∪ с auto-tags каждой секции) | `[]` |
| `max_chunk_tokens` | Лимит токенов на секцию (XLM-R #20); oversized → recursive split | `512` |
| `wait_for_index` | Sync-флаг (как #9): GPU — блок ≤5с; CPU — `pending:true` | `false` |
| `cleanup_orphans` | При partial failure: удалить осиротевших детей (soft-delete) | `false` (только лог) |

**Контракт возврата (partial_success):**

```json
{
  "collection_id": "engineering-python-clean-code-collection",
  "imported": 18,
  "failed": 1,
  "failed_sections": [
    {"sequence_number": 7, "title": "Глава 7: Обработка ошибок", "error": "embed_timeout"}
  ],
  "partial_success": true,
  "indexed": true,
  "pending": false
}
```

> **Best-effort (E6/C1):** `failed > 0` → `partial_success: true`, но **17 успешных секций остаются в SSOT** (не rollback). Оператор/агент видит `failed_sections[]` и может ре-импортировать проблемные секции или запустить cleanup.

---

## 6. Контракты: frontmatter, splitting, linking, batch

### 6.1 Новые frontmatter-поля (аддитивны, backward-compatible)

```yaml
---
# ... существующие поля (#2): knowledge_id, domain, subject, project, tags, ...
parent_knowledge_id: "engineering-python-clean-code-collection"   # 🆕 Фаза 5: null для root
sequence_number: 3                                                 # 🆕 Фаза 5: порядок в коллекции
content_type: "book"                                               # 🆕 Фаза 5: "book" | "pdf" | "collection"
created_at: "2026-07-29T12:00:00+03:00"
updated_at: "2026-07-29T12:00:00+03:00"
---

# Глава 3: Функции
Тело секции...
```

> **Backward-compatibility:** отсутствие `parent_knowledge_id`/`sequence_number`/`content_type` = обычная single-запись (Фазы 0–4). Новые поля **не ломают** reconciliation (#19), search, quality gates (#22), INDEX.gen.yaml (#30) — все игнорируют неизвестные/ отсутствующие поля. Qdrant payload получает новые индексируемые поля (`parent_knowledge_id`, `content_type`) — аддитивно (задача 1.3).

### 6.2 Root-collection запись («оглавление»)

```yaml
---
knowledge_id: "engineering-python-clean-code-collection"
domain: "engineering"
subject: "python"
content_type: "collection"            # 🆕 root = TOC
parent_knowledge_id: null             # 🆕 root не имеет родителя
sequence_number: null
tags: ["clean-code", "book"]
title: "Clean Code (Р. Мартин)"
children:                             # 🆕 только для collection
  - {knowledge_id: "engineering-python-clean-code-ch01", title: "Глава 1: Чистый код",  sequence_number: 1}
  - {knowledge_id: "engineering-python-clean-code-ch02", title: "Глава 2: Имена",       sequence_number: 2}
  - {knowledge_id: "engineering-python-clean-code-ch03", title: "Глава 3: Функции",     sequence_number: 3}
created_at: "2026-07-29T12:00:00+03:00"
updated_at: "2026-07-29T12:00:00+03:00"
---

# Clean Code (коллекция)
Импортированная книга. Оглавление — в frontmatter.children.
```

> **Навигация:** `get_entry(collection_id)` (tool #2) возвращает root с TOC → агент видит структуру и может `get_entry(child_id)` для конкретной главы. **Без** новых read-tools.

### 6.3 Hybrid Splitting — контракт (3 стадии)

| Стадия | Условие запуска | Алгоритм | Результат |
|--------|-----------------|----------|-----------|
| **1. Structural** | Всегда (первая попытка) | Парсинг `#`/`##`/`###` → секции (заголовок + тело) | `list[Section]` |
| **2. Clustering** (fallback) | structural дал < `MIN_SECTIONS` (default 2) **ИЛИ** есть oversized-секция | paragraph embed (BGE-M3 #3/#17) → cosine matrix смежных абзацев → agglomerative clustering (`CLUSTER_COSINE=0.75`) → группы | укрупнённые/переразбитые `Section[]` |
| **3. Recursive** | любая секция > `max_chunk_tokens` | split по границам предложений (sent_tokenize RU+EN) до ≤ `max_chunk_tokens` | **гарантия** ≤512 токенов |

```
MAX_CHUNK_TOKENS  = 512          # XLM-RoBERTa (#13/#20), совпадает с chunker Ф1
MIN_SECTIONS      = 2            # если structural дал <2 → fallback clustering
CLUSTER_COSINE    = 0.75         # порог cosine для группировки абзацев (калибруется §7/§8)
IMPORT_BATCH_COMMIT = 10         # секций на 1 git-коммит (#21, производительность)
```

> **Гарантия (E4):** после stage 3 **каждая** секция ≤ `max_chunk_tokens` — независимо от качества кластеризации. Defense-in-depth: даже если clustering плохой, recursive split спасёт инвариант. Дальнейшее chunking внутри секции делает **существующий** `indexing/chunker.py` (#13) — import **не дублирует** chunker, а только готовит секции ≤ лимта.

### 6.4 Keyword extraction → tags (TF-IDF / YAKE)

| Метод | Алгоритм | Air-gap | Когда |
|-------|----------|:-------:|-------|
| **YAKE** (primary) | Unsupervised, статистические фичи (casing, position, frequency, term-in-sentence). Multi-word keyphrases. Чистый Python (`pip install yake`) | ✅ | Default — лучше для keyphrases, не требует корпуса |
| **TF-IDF** (fallback) | Корпус = все секции импортируемого документа; high TF-IDF → candidate tags. Стоп-слова RU+EN | ✅ | Если YAKE недоступен (зависимость не установлена) |
| **Frequency-based** (last resort) | Top-N частых токенов минус стоп-слова | ✅ | Если ни YAKE, ни scikit-learn (TF-IDF) недоступны |

- **Нормализация:** kebab-case, lowercase, транслит кириллицы → латиница (slug), дедуп с inherited `tags` из import params.
- **top-N** (default 5) candidate tags на секцию; **advisory** — не блокируют импорт.
- **Reuse:** теги попадают в стандартное frontmatter-поле `tags` → `search_by_tags` (#31), INDEX.gen.yaml (#30), quality gates (#22) работают **без изменений**.

### 6.5 Orphan cleanup (reconciliation #19, augmented)

| Сценарий | Обнаружение | Действие |
|----------|-------------|----------|
| **Child без parent** | запись с `parent_knowledge_id=X`, но `X.md` отсутствует (удалён вручную / прерванный импорт) | issue `orphaned` (severity=warn) → `issues.jsonl` (Ф4). При `cleanup_orphans=true` → soft-delete child |
| **Collection с incomplete children** | root `content_type: collection`, но часть `children[].knowledge_id` отсутствует | WARN в reconcile-логе; issue `orphaned` на root. TOC остаётся, но помечен incomplete |
| **Parent без children** | root `content_type: collection`, но `children` пуст/все удалены | issue `orphaned` (root); опц. auto-deprecate (Ф4 lifecycle) |

> Orphan-detection — **ещё одна проверка** в существующем проходе `reconcile.run_reconciliation()` (#19). Лог дополняется: `{checked, reindexed, skipped, deleted_orphans, orphaned_detected}`. **Никакого** отдельного cron-сканера (reuse reconcile + scanner Ф4).

---

## 7. Критерии приёмки (ACCEPTANCE)

### Preprocessor + registry + tool (#33)
- ✅ `import_content(content, content_type="book", domain, subject)` → валидная декомпозиция → N `.md`-записей в SSOT + git-коммиты (#21)
- ✅ `import_content(..., content_type="unknown")` → `400` со списком доступных типов (`["book"]`)
- ✅ `import_content(content="", ...)` → `validate()` отклоняет пустой контент (`400`)
- ✅ Все секции-дети наследуют `domain`/`subject` из import params; `knowledge_id`-slug корректен (kebab-case, уникален в пределах коллекции)

### BookPreprocessor + auto-frontmatter (#33/#35)
- ✅ Книга с чёткими `#`/`##` заголовками → structural parse даёт по одной секции на заголовок; `title` = текст заголовка
- ✅ Auto-`tags`: каждая секция имеет ≥1 тег из TF-IDF/YAKE (если контент не пуст); теги kebab-case, без стоп-слов, дедуп с inherited
- ✅ Root-collection запись (`content_type: "collection"`) содержит `children[]` с корректными `knowledge_id`/`title`/`sequence_number`

### Hybrid splitting (#34)
- ✅ **Структурная книга** (ясные заголовки): stage 1 достаточно, clustering **не** запускается (нет oversized)
- ✅ **Plain text без заголовков**: stage 1 даёт < `MIN_SECTIONS` → fallback на clustering → ≥2 семантических секций
- ✅ **Oversized секция** (> `max_chunk_tokens`): recursive split → **все** результирующие секции ≤ 512 XLM-R-токенов (проверка реальным токенайзером #20)
- ✅ Embedding в clustering идёт через `run_in_executor` (#1.6) — event loop **не** блокируется во время импорта; `search_knowledge` остаётся отзывчивым

### Parent-child linking + orphan detection (#35/#36)
- ✅ `get_entry(collection_id)` (tool #2) возвращает root с TOC `children[]` — агент видит структуру коллекции
- ✅ Каждый child имеет `parent_knowledge_id=<root_id>` и корректный `sequence_number` (1..N)
- ✅ **Orphan:** удалить root-файл вручную → `reindex`/restart → reconcile (#19) → issue `orphaned` в `issues.jsonl` (Ф4); лог содержит `orphaned_detected`
- ✅ **Incomplete collection:** удалить 1 child → reconcile → WARN + issue на root

### Batch best-effort (#36)
- ✅ **Interrupt recovery:** симуляция падения 1 секции (mock embed error) → `partial_success: true`, `failed: 1`, `failed_sections[{...}]`, остальные `imported` секции **LIVE** в SSOT + Qdrant
- ✅ `?cleanup_orphans=true` при partial failure → осиротевшие дети soft-deleted (`knowledge/.trash/`) + git commit
- ✅ Batch git-commit: при импорте 25 секций с `IMPORT_BATCH_COMMIT=10` → ровно 3 git-коммита (не 25)
- ✅ `?wait_for_index=true` на **GPU** → `indexed: true` (≤5с таймаут); на **CPU** для большой книги → `pending: true` (как #9)

### Operational
- ✅ Декомпозиция тестовой книги (~100K токенов) завершается без OOM; embed-batching (reuse #1.6) удерживает память
- ✅ `/metrics` (#2.13) содержит `import_content_total`, `import_sections_total`, `import_failed_sections_total`, `orphans_detected_total`
- ✅ Покрытие тестами content-модуля ≥ 90%

---

## 8. Метрики (EXPECTED)

| Метрика | GPU | CPU |
|---------|:---:|:---:|
| Декомпозиция книги (~100K токенов, end-to-end) | < 60 сек | < 5 мин |
| Embedding clustering: throughput абзацев | ≥ 50/сек | ≥ 8/сек (batch ×8) |
| Recursive split: гарантия ≤ max_chunk_tokens | 100% секций | 100% секций |
| `import_content` на структурной книге (без clustering) | < 20 сек | < 90 сек |
| Keyword extraction (YAKE): recall vs manual tags (русский) | ≥ 70% (advisory) | ≥ 70% (advisory) |
| Orphan detection coverage (на reconcile pass) | 100% | 100% |
| Batch interrupt: сохранённых секций при 1 failure | N−1 (best-effort) | N−1 (best-effort) |
| `get_entry(collection_id)` latency (p95) | < 30 ms | < 30 ms |
| Покрытие тестами content-модуля | ≥ 90% | ≥ 90% |

> **Recall тегов — advisory (E5/C2):** авто-теги из TF-IDF/YAKE не претендуют на полноту (~70% vs ручной разметки). Это advisory-обогащение; оператор/агент может дополнить `tags` через `update_entry`. Сравнимо с `suggested_tags` в INDEX.gen.yaml (#30, P1-4).

---

## 9. Риски и mitigation

| ID | Риск | Вероятность | Severity | Mitigation |
|----|------|:-----------:|:--------:|------------|
| R1 | **Cosine clustering даёт плохие границы** (порог 0.75 не оптимален для русского) | Средняя | 🟡 | `CLUSTER_COSINE` калибруется в тестах (§7/§8); recursive split (stage 3) гарантирует ≤512 токенов **независимо** от качества кластеризации (E4). Порог — конфигурируемый |
| R2 | **Oversized секция** не разбивается корректно (нет границ предложений) | Низкая | 🟠 | Recursive split с fallback на фиксированный split по токенам (последнее средство); unit-тест на 10K-токенной секции без точек |
| R3 | **Partial failure оставляет «мусор»** (осиротевшие дети без cleanup) | Средняя | 🟠 | Orphan detection в reconcile (#19, §6.5) + `?cleanup_orphans=true` (§5). Default — логирование в issues.jsonl (Ф4), не silent |
| R4 | **Импорт большой книги → OOM** (embedding всех абзацев в память) | Низкая | 🔴 | Batching embed (reuse #1.6, ≥16 абзацев/батч); streaming-декомпозиция (секции обрабатываются последовательно, не все в памяти); cosine matrix — только смежные абзацы (windowed), не full NxN |
| R5 | **YAKE/TF-IDF недоступны в air-gap** (зависимость не в wheelhouse) | Средняя | 🟡 | Frequency-based last-resort fallback (§6.4); pre-download `yake`/`scikit-learn` в air-gap-артефакты (#15, §3.6). Теги — advisory, импорт не падает без них |
| R6 | **Batch git-commit замедляет массовый импорт** (#21) | Средняя | 🟡 | `IMPORT_BATCH_COMMIT=10` (1 коммит на N секций, не 1/секция); `GIT_AUDIT=false` отключает полностью. См. mitigation Ф1 R |
| R7 | **`knowledge_id`-slug коллизия** (две секции с одинаковым title) | Средняя | 🟡 | Slug дополнен `sequence_number` (`...-ch03`); проверка уникальности перед write; при коллизии — авто-суффикс `-2`, `-3` |
| R8 | **Импорт ломает существующие E2E** (#10) | Низкая | 🟡 | Новые frontmatter-поля аддитивны (absent = single-запись, backward-compatible); `import_content` — отдельный tool, write-контракт (#5) не изменён |
| R9 | **Clustering блокирует event loop** на CPU | Средняя | 🟠 | `embed()` строго через `run_in_executor` (#1.6); `search_knowledge` остаётся отзывчивым во время импорта (как в Ф1 E2E p95 ≤ 400 ms) |
| R10 | **Root TOC рассинхрон с детьми** (child удалён, TOC не обновлён) | Средняя | 🟡 | Orphan detection (§6.5) помечает incomplete collection; опц. auto-rebuild TOC при reconcile (future) |

---

## 10. Roadmap-слайс (4 дня)

> При условии 1 backend-разработчика (8 ч/д). Фаза 5 идёт **после** M3 (Фазы 0–3) из [`00-implementation-plan.md`](00-implementation-plan.md) §12. **Не блокирует MVP.** Может идти параллельно с Фазой 4 при 2 разработчиках (нет общих файлов, кроме augment reconcile #19 — координировать задачу 5.4).

```
Фаза 5 (дни 20-24, после Production M3) — v1.1   (~34.5 ч / ~4.5 дня при 8 ч/д)
├─ День 20:  5.1 Preprocessor ABC + registry + import_content tool [4.5 ч]
│            + 5.2 structural parse + auto-frontmatter (часть)     [3.5 ч] ✅ фундамент           [8 ч]
├─ День 21:  5.2 keywords (TF-IDF) + auto-tags (завершение)        [3.5 ч]
│            + 5.3 hybrid splitting: clustering (часть)            [4.5 ч] ✅ M5a: рабочий импорт  [8 ч]
├─ День 22:  5.3 recursive split (завершение)                      [4 ч]
│            + 5.4 linking + orphan detection (model+IssueType)    [4 ч] ✅ M5b: hybrid split    [8 ч]
├─ День 23:  5.4 schema.py payload + reconcile augment (заверш.)   [1 ч]
│            + 5.5 batch best-effort + cleanup                     [3 ч]
│            + 5.6 тесты (часть)                                   [4 ч] ✅ M5: Content Import   [8 ч]
└─ День 24:  5.6 тесты (завершение) + deps/fallback верификация    [2.5 ч]
             + буфер (deps, air-gap, docs)                         [5.5 ч]                       [8 ч]
```

### Вехи

| Веха | День | Критерий |
|------|:----:|----------|
| **M5a: Structural Import** | 21 | `import_content(content_type="book")` на структурной книге → N записей с auto-frontmatter + auto-tags (TF-IDF/YAKE); root-collection с TOC |
| **M5b: Hybrid Splitting** | 22 | Plain text без заголовков → clustering fallback; oversized секции → recursive split ≤512 токенов; `run_in_executor` (event loop не блокируется) |
| **M5: Content Import** | 23 | Parent-child linking + orphan detection в reconcile (#19) + best-effort batch (partial_success) + cleanup + 4 E2E-кейса (structural/plain/oversized/interrupt); доки |

---

## 11. Known limitations — что НЕ покрывается

> Явная фиксация границ Фазы 5. Эти аспекты осознанно отложены (low impact-to-effort, требуют новой инфраструктуры, или ждут локальную LLM) — см. §1.3 и §9.

| # | Что НЕ покрывается | Почему отложено | Future-path |
|---|---------------------|-----------------|-------------|
| L1 | **PDF parsing** (`content_type="pdf"`) | Требует layout-анализа/OCR (`PyMuPDF`/`pdfplumber` + Tesseract fallback); отдельный `PdfPreprocessor` | Архитектура registry (#33) готова — добавить класс без изменения tool (§3.2) |
| L2 | **LLM-based decomposition** (smart summarization, chapter-aware splitting) | Air-gap: LLM недоступен офлайн; дорогой per-call (C2) | После локальной LLM (Ф future, как HyDE в [`00`](00-implementation-plan.md) §13.1) |
| L3 | **OCR для сканов** | Отдельная инфраструктура (Tesseract/EasyOCR); `content_type="scan"` | Future: `ScanPreprocessor` + OCR pipeline |
| L4 | **Cross-document deduplication** (книга частично дублирует существующую БЗ) | Dup-gate (#22) работает per-section на момент write; cross-doc scan — O(n×m) | Future: batch-dedup через Qdrant bulk-search |
| L5 | **Semantic re-ranking секций** (переупорядочить по важности) | Требует relevance-model; текущий `sequence_number` = структурный порядок (как в источнике) | Future: post-import re-rank |
| L6 | **Incremental re-import** (докинуть новые главы в существующую коллекцию) | `import_content` создаёт новую коллекцию; merge в существующую — отдельный контракт | Future: `import_content(parent_collection_id=...)` |
| L7 | **TOC auto-rebuild при изменении детей** | Orphan detection помечает incomplete (R10), но не перестраивает TOC автоматически | Future: auto-rebuild в reconcile |
| L8 | **HTML/Markdown-fence санитизация** (внедрённый код/скрипты в импортируемом контенте) | Отдельная security-задача (как L2 в Ф4) | Отдельный security-slice |

---

## 12. Интеграция с существующей архитектурой (не ломаем)

| Где | Изменение | Совместимость |
|-----|-----------|---------------|
| `write_knowledge`/`update_entry` (#5) | **Без изменений.** `import_content` — отдельный tool; вызывает markdown_store/pipeline под капотом | ✅ single-record контракт не тронут |
| Markdown SSOT (#2) | + 4 аддитивных frontmatter-поля (`parent_knowledge_id`, `sequence_number`, `content_type`, `children`); absent = обычная запись (Optional, default None) | ✅ backward-compatible |
| `KnowledgeFrontmatter` (models.py) | + `parent_knowledge_id: Optional[str]`, `sequence_number: Optional[int]`, `content_type: Optional[str]`, `children: Optional[list[dict]]` (после поля `source`) | ✅ аддитивно; `exclude_none=True` в model_dump (markdown_store.py:233) — существующие записи не затрагиваются |
| Qdrant payload-схема (задача 1.3) | + индексируемые поля `parent_knowledge_id(str)`, `content_type(str)` | ✅ аддитивно; `search_by_tags`/`get_knowledge_map` работают без изменений |
| **`storage/schema.py` (P0-фикс v1.1)** | **`build_payload_point()` (schema.py:82-111) использует hardcoded payload — новые поля НЕ попадут в Qdrant автоматически.** Обновить: `PAYLOAD_SCHEMA` (26-37), `PAYLOAD_INDEXES` (40-47), `build_payload_point()` payload dict, callers (pipeline.py:359,434; cli.py:124) | ✅ без этого поля Ф5 будут в YAML, но не в Qdrant (SSOT-Qdrant inconsistency) |
| Async indexing pipeline (#7) | **Без изменений.** Import готовит секции → та же очередь (chunk → embed → Qdrant) | ✅ reuse |
| In-process embedder (#3/#17) | Reuse в splitting (clustering) через `run_in_executor` (#1.6). API: `EmbeddingManager.embed_sync(texts) → list[list[float]]` (manager.py:75). Fallback air-gap: `OllamaEmbedder` (quality/embedder.py) | ✅ synergy (E3) |
| Reconciliation (#19) | + orphan detection (ещё одна проверка в существующем проходе) + поле `orphaned_detected` в `ReconcileResult` (reconcile.py:28-45) | ✅ augment, не rewrite |
| Quality gates (#22, Ф4) | per-section advisory (дубли/missing) — работают на каждой секции импорта. API: `evaluate_frontmatter()` (gates.py:118), `check_duplicates()` (dup_gate.py:77) | ✅ reuse |
| Issues.jsonl (Ф4 §4.1) | + тип `orphaned` — **НЕ зарезервирован в Ф4 §6.2** (реальный IssueType: duplicate\|missing_field\|edit_war\|broken_link\|conflicting). Добавить в Literal (issues.py:72), schema enum (tools/__init__.py:126), docstring | ⚠️ v1.0 утверждал неверно; v1.1 фиксирует |
| Git-аудит (#21) | Batch-commit (1 на N секций) под `asyncio.Lock` (задача 1.1) | ✅ consistency |
| `/metrics` (#2.13) | + `import_content_*`, `orphans_detected_total` | ✅ аддитивно |
| INDEX.gen.yaml (#30) | Коллекции/дети — обычные `.md`; INDEX их индексирует (теги `content_type` доступны) | ✅ без изменений |
| `get_entry`/`search_knowledge`/`search_by_tags` | Root TOC через `get_entry`; дети ищутся как обычные записи | ✅ без новых read-tools |

---

## 13. Лог правок v1.1 (codebase audit + Critic Gate v2)

> **trace_id:** `code-2026-08-03-162` | **Дата:** 2026-08-03 | **A.19.ECS:** 0.697 → 0.876 (консенсус Critic: 0.867, PASS 0.85)

**22 расхождения план ↔ реальный код устранены (5 critical, 9 medium, 8 low):**

| # | Правка | Severity |
|---|--------|:--------:|
| 1 | `mcp/tools.py` → `tools/content.py` + `tools/__init__.py` (TOOLS+TOOL_HANDLERS) | 🔴 |
| 2 | Tool count: план 12→13, реально 15→16 | 🟠 |
| 3 | 4 parent-child поля модели (`parent_knowledge_id`, `sequence_number`, `content_type`, `children`) | 🔴 |
| 4 | `"orphaned"` в IssueType — НЕ был зарезервирован (v1.0 неверно утверждал) | 🔴 |
| 5 | `orphaned_detected` в ReconcileResult (reconcile.py:28-45) | 🟠 |
| 6 | **P0: `schema.py:build_payload_point()` hardcoded** — новые поля не попадут в Qdrant; обновить PAYLOAD_SCHEMA/INDEXES/fn/callers | 🔴 |
| 7 | Embedder API: `EmbeddingManager.embed_sync()` (не `.encode()`) | 🟠 |
| 8 | OllamaEmbedder (air-gap, quality/embedder.py) — не упомянут в v1.0 | 🟡 |
| 9 | `status`/`evergreen`/`source` — уже есть в модели (Ф4), v1.0 предполагал отсутствие | 🟡 |
| 10 | Токенайзер: `embedding/tokenizer.py` (не `indexing/chunker.py`) | 🟡 |
| 11 | Prompts: 3 (не 2) — Ф4 добавил `periodic_quality_cleanup` | 🟡 |
| 12 | `create_issue_async()` — orphan detection из async reconcile должен вызывать async-обёртку | 🟡 |
| 13-22 | 10 остальных правок (пути, сигнатуры, docstrings, counts) | 🟡 |

**Зависимости (проверены в pyproject.toml):** `scikit-learn` ✅ (transitive через sentence-transformers); `nltk` ❌, `yake` ❌ — **fallback**: свой simple cosine-clustering вместо sklearn (или vendor), simple split по ". " вместо nltk.sent_tokenize, TF-IDF через sklearn. Добавить в deps при реализации (или документировать fallback).

**Бюджет:** 30h → **34.5h** (+4.5h: paths 0.5h, model fields 1h, schema.py payload 0.5h, deps/fallback 0.5h, async fixtures 0.5h, quality_report +1.5h в тестах).

---

> **FPF-методология:** C.30 (Grounded Architecture — переиспользование BGE-M3/XLM-R/SSOT/#7/#19/#21/Ф4 вместо новой инфраструктуры), A.22 (Structure Views — декомпозиция «импорта» на validate/decompose/split/link/batch + явные границы non-coverage), A.19.ECS (оценка 4 стратегий разбиения по 6 критериям, §1.2), A.10 (Evidence Graph — E1–E6 «за», C1–C3 «против» транзакций/LLM/PDF, §1.3). Бюджет v1.1: 34.5 ч; P0-ядро 27 ч даёт рабочий `import_content` даже без коллекций/orphan-detection. Фаза 5 идёт после MVP, не блокирует; может параллелиться с Ф4.
