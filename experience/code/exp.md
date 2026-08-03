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
