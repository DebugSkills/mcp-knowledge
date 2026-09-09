---
name: kb-mcp-preflight
description: >-
  Полный цикл проверок перед глубокой аналитической задачей или работой с базой
  знаний MCP-сервером (mcp-knowledge): health + reconcile + зоны + качество →
  бутстрап контекста → сбалансированная загрузка. Пре-флайт read-only, фиксация
  результата в .board.md. Для ВСЕХ ролей, чья задача опирается на базу знаний.
version: "1.0"
token_budget: ~120
modeSlugs:
  - universal-orchestrator
  - analyst
  - critic
  - role-architect
  - media-strategist
  - content-manager
  - content-builder
  - code-implementer
  - code-debugger
target_roles:
  - Orchestrator
  - Analyst
  - Critic
  - RoleArchitect
  - MediaStrategist
  - ContentManager
  - CodeImplementer
  - CodeDebugger
vendor_adaptation: ready
related_skills:
  - global-knowledge-base
  - orchestrator-core
  - project-navigation
  - global-verification
  - global-error-handling
related_docs:
  - docs/mcp-client-guide.md
  - README.md
skill_triggers:
  - mcp-knowledge
  - работа с базой знаний
  - поиск знаний
  - search_knowledge
  - глубокая аналитика
  - пре-флайт
  - health
  - зоны доступа
  - bootstrап контекста
triggers:
  - mcp-knowledge
  - база знаний
  - knowledge
  - search
  - пре-флайт
  - preflight
  - health
  - zones
  - reconcile
  - reindex
  - контекст
  - бутстрап
status: ready
revised: "2026-08-19"
---

# 🎯 KB MCP PRE-FLIGHT — полный цикл перед работой с базой знаний

## ⚡ QUICK REFERENCE
| Ситуация | Действие |
|----------|----------|
| Просьба «поработать с базой знаний» / MCP-сервером | Пройти цикл **S1→S6** (read-only), затем обычный workflow |
| Глубокая аналитическая задача (анализ/критика/архитектура) | S1 (health) + S4 (качество) перед загрузкой доменных скиллов |
| Первый `search_knowledge` в сессии | Сначала S1-S3: знать здоровье и топологию БД |
| `/health` = degraded | Decision Tree ниже → диагностика, НЕ начинать глубокую работу вслепую |
| `reconcile.state` ≠ done | Учесть: индекс может быть неполным; проверить после завершения |
| `embedding.loaded` = false | Поиск будет пуст/ошибочен → диагностировать Ollama ДО бутстрапа |

## 🎬 КОГДА ЗАПУСКАТЬ
1. **Пользователь просит работать с базой знаний MCP-сервером** (поиск, анализ контента, импорт, курирование).
2. **Глубокая аналитическая задача**, которая опирается на знания (Analyst/Critic/RoleArchitect/MediaStrategist).
3. **Первый запрос к БД в сессии** — даже если задача не «про базу», пре-флайт даёт карту данных для точных запросов.
4. **После долгой паузы / рестарта стека** — состояние индекса могло измениться (reconcile, импорт, миграция).

## 🔄 ПОЛНЫЙ ЦИКЛ (6 шагов, read-only)

| # | Шаг | Инструмент | Приемлемо | Провал → |
|---|-----|-----------|-----------|----------|
| S1 | **Health** | `curl -sf http://localhost:8000/health` (или `python3 mcp-stdio/bridge.py --health`) | `status=healthy`, `reconcile.state=done`, `embedding.loaded=true`, обе зоны существуют | Decision Tree / 🚨 Ошибки |
| S2 | **Топология** | `list_domains` + `list_subjects(domain=…)` (или `get_knowledge_map`) | Словарь доменов/предметов для точных запросов | Зона/домен не найден → искать по-другому |
| S3 | **Зоны** | `/health → checks.qdrant.zones` (+ `list_collections`) | Знать public/private распределение и книги-коллекции | Ожидаемый контент не в своей зоне → уточнить зону запроса |
| S4 | **Качество** | `list_quality_issues` (open, по типам) + `review_queue_books` top-5 | Дубли/устаревшее → учитывать при отборе кандидатов | Массовые dup-issues → отсеять кандидатов, не доверять поиску 1:1 |
| S5 | **Бутстрап контекста** | `search_knowledge` (топ 5-10, зона по задаче) → `get_entry` на топ-кандидатов; внутри книг — `find_fragment` | 3-5 прочитанных записей = достаточный контекст | Записей мало → расширить запрос, сменить домен/теги |
| S6 | **Фиксация** | Записать пре-флайт в `.board.md` (checkpoint) | Сводка: health, зоны, топология, качество, отобранные записи | — |

**Результат цикла:** карта данных (здоровье, зоны, домены, качество) + 3-5 релевантных записей в контексте — прежде чем начать содержательную работу.

## 🧭 DECISION TREE (после S1)
| Состояние `/health` | Действие |
|--------------------|----------|
| `healthy`, reconcile done, embedding loaded | Продолжать цикл S2-S6 |
| `reconcile.state` = `pending`/`reindexing` | Подождать завершения (индекс обновляется), затем повторный S1 |
| `embedding.loaded` = false | Проверить ollama-контейнер проекта (`curl http://localhost:11435/api/tags`, `docker compose ps ollama`), логи `[EMBED]`; без embedder поиск не работает |
| `status` = `degraded` | Диагностика: `curl /health/live` (сервер жив?), `docker compose ps qdrant`, `docker compose logs mcp-server --tail 50`; починить или сообщить оператору |
| Сервер недоступен (connection refused) | НЕ начинать глубокую работу; предложить `make dev` / `make deploy` |

## ⚖️ БАЛАНС: что НЕ делать
- **Пре-флайт — read-only.** Не вызывать `run_quality_scan`, `reindex`, `bulk_*` без явного запроса оператора (тяжёлые, пишут в прод).
- **Один health на сессию** (кэшировать результат S1). Rate limits: read 100/мин, write 20/мин, subscriber 45/мин.
- **Целевой поиск, не веер.** 1-2 точных запроса (домен+зона+теги) вместо 10 расплывчатых; топ-3-5 `get_entry`, а не все результаты.
- **Не тянуть записи в контекст без отбора.** Кандидат с dup-issue/высоким staleness — пометить, читать только при необходимости.
- **Deprecated исключены из поиска по умолчанию** — при сомнении включить `include_deprecated=true` точечно, не глобально.
- **Не дублировать реестры:** пре-флайт БД ≠ поиск паттернов `global-knowledge-base` (exp-index.gen.yaml). Оба нужны, но это разные источники.

## 🚨 ОШИБКИ / degraded
- **401/403** → ключ/уровень не соответствует операции (read для чтения, import для импорта, write для мутаций). Проверить `MCP_API_KEY`.
- **429 rate limit** → пауза между вызовами; batch-запросы вместо веера.
- **-32003 / таймаут** → крупные книги (тысячи секций) обрабатываются долго; дождаться или отменить через `cancel_import`.
- **Частичный контент** → проверить `reconcile.orphans` / `dlq` в `/health`; `GET /health` вернёт цифры в `checks`.
- **Неразрешимая проблема** → зафиксировать в `.board.md` §5 (операционная) и эскалировать оператору, а не «додумывать» данные.

## 🔗 СВЯЗАННОЕ
| Документ | Связь |
|----------|-------|
| `docs/mcp-client-guide.md` (в корне проекта) | 31 инструмент, права (subscriber/read/import/write), troubleshooting |
| skill: `global-knowledge-base` | Реестр опыта L1-L3 (отдельный от БД-контента источник) |
| skill: `orchestrator-core` | §Диагностика: маркеры логов `[RECONCILE] [REINDEX] [EMBED]`, `make dev-follow-server` |
| skill: `project-navigation` | Навигация по файлам проекта (для .knowledge-контента) |
| skill: `global-verification` | Финальная проверка результата перед завершением |

---
**v1.0** | 2026-08-19 | Пре-флайт перед глубокой аналитикой / работой с mcp-knowledge: 6-шаговый read-only цикл (health → топология → зоны → качество → бутстрап → фиксация), Decision Tree по `/health`, правила баланса (rate limits, один health на сессию, целевой поиск). Бюджет: ~120 строк
