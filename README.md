# mcp-knowledge

MCP Knowledge Server — семантическая база знаний для AI-агентов по протоколу MCP (Model Context Protocol). Открытый проект сообщества DebugSkills.

## Quality Management (Фаза 4)

Встроенная система контроля качества знаний предотвращает деградацию базы:

| Механизм | Описание |
|----------|----------|
| **Pre-write gates** | Валидация frontmatter (required/recommended поля) и семантическая проверка дублей (cosine ≥0.92) перед записью |
| **Staleness scoring** | 5-факторная формула: возраст (evergreen-адаптивный), дублирование, неполнота, edit-wars, битые ссылки |
| **Review queue** | Автоматическая очередь записей требующих ревизии (score ≥0.45) |
| **Edit-war detection** | Git-based: ≥3 правок за 24ч → флаг конфликта |
| **Lifecycle** | 2-state модель: published ↔ deprecated (reversible через restore) |
| **Quality-SLO** | `review_queue_size > 50` → Prometheus/alertmanager alert |

### MCP Quality Tools

| # | Tool | Назначение |
|---|------|-----------|
| 12 | `review_queue` | Топ устаревших записей (сорт. по staleness_score DESC) |
| 13 | `list_quality_issues` | Список проблем: дубликаты, edit-wars, битые ссылки |
| 14 | `resolve_quality_issue` | Разрешить: merge/deprecate/restore/resolve/ignore |
| 15 | `run_quality_scan` | Запустить периодический scan (для cron) |

### MCP Quality Prompt

- **`periodic_quality_cleanup`** — пошаговая инструкция для AI-агента: review_queue → list_issues → resolve

### Метрики

| Prometheus metric | Тип |
|-------------------|-----|
| `quality_duplicates_detected_total` | Counter |
| `quality_issues_total{type}` | Counter |
| `review_queue_size` | Gauge |
| `deprecated_total` | Gauge |

### Конфигурация

| Переменная | Default | Описание |
|-----------|---------|----------|
| `REVIEW_THRESHOLD` | 0.45 | Порог staleness_score для review-очереди |
| `DUP_SIMILARITY_THRESHOLD` | 0.92 | Cosine-порог для дублей |
| `EDIT_WAR_WINDOW_H` | 24 | Окно анализа edit-war (часы) |
| `EDIT_WAR_THRESHOLD` | 3 | Мин. число коммитов для edit-war |
| `QUALITY_SCAN_TIMEOUT` | 600 | Таймаут quality_scan.sh (сек) |

### Known Limitations

- **Factual correctness:** не проверяется (требует LLM/external-source verification)
- **Coverage gaps:** не детектируются непокрытые темы (требует external topic-map)
- **Conflicting entries:** `conflicting` тип зарезервирован, детекция отложена (~90% FP)
- **Temporal dup-blind-spot:** async-окно 1-5с между write и доступностью в Qdrant (компенсируется periodic scan)

---

*Фаза 4 завершена. 15 MCP Tools, 4 Quality Prompts, 133 теста.*
