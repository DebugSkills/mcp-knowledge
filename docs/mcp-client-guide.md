# 🔌 MCP Client Guide — подключение AI-агентов к mcp-knowledge

> **trace_id:** `code-2026-08-08-912` | **Фаза:** 13.21 | **Дата:** 2026-08-08
> **Аудитория:** AI-агенты (Kilo Code, Claude Desktop, Cline), DevOps-инженеры.
> **Связанные документы:** `README.md`, `mcp-stdio/bridge.py`, глобальный конфиг `~/.config/kilo/kilo.jsonc`

---

## 1. TL;DR — три шага к подключению

1. **Запустите mcp-knowledge сервер** (локально или на хосте):
   ```bash
   docker compose up -d mcp-server
   # → http://localhost:8000 (POST /mcp, GET /health)
   ```

2. **Добавьте секцию `mcp-knowledge` в глобальный конфиг Kilo** (`~/.config/kilo/kilo.jsonc`) — сервер станет доступен **во всех проектах пользователя** (не только в mcp-knowledge):
   - Тип: `local` process
   - Команда: `python3 /kvm/mcp-knowledge/mcp-knowledge/mcp-stdio/bridge.py` (абсолютный путь!)
   - Env: `MCP_SERVER_URL`, `MCP_API_KEY`

3. **Проверьте подключение:**
   - Kilo: `/mcps` → статус `connected`; tools list → 20 инструментов
   - Ручная: `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp-stdio/bridge.py` → JSON с 20 tools

---

## 2. Архитектура — почему нужен stdio-мост

mcp-knowledge сервер реализует **JSON-RPC 2.0 поверх HTTP** (POST /mcp). Он **НЕ** использует стандартные MCP-транспорты (stdio, SSE, Streamable HTTP) — это осознанное решение:
сервер создавался как REST-совместимый JSON-RPC, а не как библиотека MCP.

Прямое подключение Kilo/Claude/Cline к серверу невозможно без транспорта stdio (Kilo) или SSE (Claude). Поэтому используется **тонкий stdio-мост** — standalone Python-скрипт `mcp-stdio/bridge.py`, который транслирует транспорт:

```
┌──────────────┐  newline-delimited JSON-RPC  ┌──────────────────┐  POST /mcp (HTTP)  ┌──────────────────┐
│  MCP-клиент  │ ──────── stdin/stdout ────── │ mcp-stdio/       │ ─────────────────→ │ mcp-knowledge    │
│  (Kilo Code, │                              │ bridge.py        │ ←───────────────── │ server           │
│   Claude,    │                              │                  │   JSON response    │ (FastAPI, :8000) │
│   Cline)     │                              │                  │                    │                  │
└──────────────┘                              └──────────────────┘                    └──────────────────┘
```

- **Клиент** запускает `bridge.py` как локальный процесс (через `type:"local"`)
- **Мост** читает JSON-RPC из stdin (newline-delimited), POST'ит на `/mcp`, пишет ответ в stdout
- **Сервер** работает как обычно (HTTP + X-API-Key auth)
- **0 правок прод-кода сервера** — мост полностью отдельный компонент

---

## 3. Конфигурация Kilo Code

### 3.1 Глобальный конфиг (рекомендуется — доступен во ВСЕХ проектах)

Секция `mcp-knowledge` добавляется в **`~/.config/kilo/kilo.jsonc`** — сервер подключается в каждом проекте пользователя автоматически:

```jsonc
"mcp": {
  // ... другие серверы (context7, playwright, ...)

  "mcp-knowledge": {
    "type": "local",
    "command": [
      "python3",
      "/kvm/mcp-knowledge/mcp-knowledge/mcp-stdio/bridge.py"   // АБСОЛЮТНЫЙ путь — работает из любого проекта
    ],
    "environment": {
      "MCP_SERVER_URL": "http://localhost:8000",
      "MCP_API_KEY": "<read-key из mcp-knowledge/.env → MCP_READ_KEYS[0]>"
    },
    "timeout": 60000,                            // мс (импорт книг может идти долго)
    "enabled": true
  }
}
```

**Важно (глобальный конфиг):**
- `command` — **абсолютный** путь к `bridge.py` (относительный путь работает только внутри проекта mcp-knowledge)
- После добавления — `chmod 600 ~/.config/kilo/kilo.jsonc` (ключ в открытом виде, защита от других пользователей)
- Перезапустите Kilo — MCP-серверы подхватываются при старте сессии
- Не дублируйте секцию в проектном `.kilo/kilo.jsonc` (SSOT — только глобальный конфиг)

### 3.2 Проектный конфиг (для одного проекта)

Добавьте в `.kilo/kilo.jsonc` этого проекта в секцию `"mcp"`:

```jsonc
"mcp": {
  // ... другие серверы (context7, playwright, ...)

  "mcp-knowledge": {
    "type": "local",
    "command": [
      "python3",
      "mcp-stdio/bridge.py"                      // относительный путь — только внутри проекта mcp-knowledge
    ],
    "environment": {
      "MCP_SERVER_URL": "http://localhost:8000",
      "MCP_API_KEY": "sk-..."                    // опционально: если на сервере включена auth
    },
    "timeout": 60000,                            // мс (импорт книг может идти долго)
    "enabled": true
  }
}
```

**Важно:**
- `type: "local"` — Kilo запускает процесс и общается через stdin/stdout
- `command` — путь к `bridge.py` относительно корня проекта (или абсолютный)
- `environment` — переменные окружения, видимые процессу моста (имя поля `environment`, НЕ `env`)
- Tool permissions Kilo генерирует как `mcp-knowledge_<tool_name>`, например `mcp-knowledge_search_knowledge`
- Для разрешения всех tools: используйте glob `mcp-knowledge_*`

**Зависимости моста:** только Python 3.11+ (stdlib: `sys`, `json`, `urllib.request`). Никаких `pip install`.

---

## 4. Конфигурация Claude Desktop

Файл `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "mcp-knowledge": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp-stdio/bridge.py"],
      "env": {
        "MCP_SERVER_URL": "http://localhost:8000",
        "MCP_API_KEY": "sk-..."
      }
    }
  }
}
```

Примечание: Claude Desktop использует `env` (не `environment` как Kilo). Перезапустите Claude после изменения конфига.

---

## 5. Конфигурация Cline (VS Code)

В Cline MCP settings (`~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` или через UI Settings → MCP Servers):

```json
{
  "mcpServers": {
    "mcp-knowledge": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp-stdio/bridge.py"],
      "env": {
        "MCP_SERVER_URL": "http://localhost:8000",
        "MCP_API_KEY": "sk-..."
      },
      "disabled": false
    }
  }
}
```

---

## 6. Права доступа (read / import / write)

Сервер поддерживает трёхуровневую систему ключей (X-API-Key header):

| Уровень | Переменная `.env` | Доступ |
|---------|-------------------|--------|
| **read** | `MCP_READ_KEYS=["..."]` | Поиск, чтение, browse, ресурсы, промпты, `analyze_content`, `list_collections` |
| **import** | `MCP_IMPORT_KEYS=["..."]` | Read + `import_content` (импорт книг без права delete/reindex/write) |
| **write** | `MCP_WRITE_KEYS=["..."]` | Полный доступ: read + import + `write_knowledge`, `update_entry`, `delete_entry`, `reindex` |

**Рекомендация:**
- AI-агенту, который только читает БЗ → **read-ключ**
- kb-console для импорта → **import-ключ** (не write — безопаснее)
- Администратору для реиндексации → **write-ключ**

Если ключи не заданы (пустые массивы `[]`) — auth отключена, доступ открыт.

---

## 7. Таблица 20 инструментов

| # | Tool | Уровень | Назначение |
|:--|------|:-------:|-----------|
| 1 | `search_knowledge` | read | Семантический поиск (Ollama embed) |
| 2 | `search_by_tags` | read | Поиск по тегам (payload-фильтр Qdrant) |
| 3 | `get_entry` | read | Получить полную запись (frontmatter + Markdown) |
| 4 | `get_knowledge_map` | read | Структурная карта: domains → subjects → IDs |
| 5 | `list_collections` | read | Список книг/коллекций с метаданными |
| 6 | `write_knowledge` | write | Создать: Markdown SSOT → chunk → embed → Qdrant |
| 7 | `update_entry` | write | Обновить с optimistic locking (version check) |
| 8 | `delete_entry` | write | Удалить: SSOT + Qdrant + Git commit |
| 9 | `list_domains` | read | Список доменов (пагинация) |
| 10 | `list_subjects` | read | Список тем в домене |
| 11 | `list_projects` | read | Список проектов |
| 12 | `reindex` | write | Перестроить индекс: все .md → Qdrant (blue-green) |
| 13 | `review_queue` | read | Топ устаревших записей (staleness_score DESC) |
| 14 | `review_queue_books` | read | Топ устаревших КНИГ (агрегат по parent, доля устаревших секций) |
| 15 | `list_quality_issues` | read | Проблемы: дубликаты, edit-wars, битые ссылки |
| 16 | `resolve_quality_issue` | write | Разрешить: merge/deprecate/restore/resolve/ignore (cascade) |
| 17 | `run_quality_scan` | write | Периодический quality scan (для cron, фоновая задача с lock) |
| 18 | `cancel_quality_scan` | write | Отменить активный scan, освободить lock |
| 19 | `import_content` | import | Декомпозиция + batch запись: content → book collection. **PDF (Фаза 13.21):** `content_type="pdf"` + `pdf_path` (путь с сервера после POST /upload) или base64-контент → асинхронная очередь импортов (ответ `{import_id, status: started|queued}`, прогресс в GET /imports/{id}/progress, лог в /imports/{id}/log, отмена POST /imports/{id}/cancel). Лимиты: ≤2000 страниц, ≤100 МБ (base64 ~96 МБ), OCR для сканов, encrypted → ошибка |
| 20 | `analyze_content` | read | AI-анализ контента (Ollama LLM + TF-IDF fallback) |

---

## 7.1 MCP-протокол: ping и notifications (Фаза 13.21)

Сервер реализует JSON-RPC 2.0 over HTTP с MCP-совместимыми методами:

| Метод | Ответ | Примечание |
|-------|-------|------------|
| `initialize` | `{"result": {protocolVersion, serverInfo, capabilities}}` | Handshake, protocolVersion `2024-11-05` |
| `ping` | `{"result": {}}` (пустой объект) | Keepalive — используется MCP-клиентами |
| `notifications/initialized` | **HTTP 204** (без тела) | Notification (без `id`) — клиент шлёт после initialize |
| `tools/list` | `{"result": {"tools": [...20 инструментов]}}` | Schemas для автогенерации permission |
| `tools/call` | `{"result": {"content": [...]}}` | Вызов инструмента |

**Правила:**
- Notification-методы (`notifications/*`) принимаются **без поля `id`** (`_validate_jsonrpc`, Фаза 13.21) — строго по JSON-RPC 2.0 §4.1
- Неизвестные `notifications/*` → HTTP 204 (не -32601)
- Обычные методы БЕЗ `id` → ошибка -32600 (валидация сохранена)
- Bridge: HTTP 204 → **не пишет ответ в stdout** (клиент не ждёт ответа на notification)

**Лимит запроса:** `MCP_MAX_REQUEST_SIZE` (default 128 МБ, настраивается в `config.py` Settings) — замена старого хардкода 64 МБ. Книги до ~100 МБ импортируются без изменения конфига. Лимит клиента kb-console (`MAX_FILE_SIZE` 50 МБ) согласован формулой `MAX_FILE_SIZE ≤ MCP_MAX_REQUEST_SIZE − 20%` (буфер на JSON-overhead).

**Big-book safety (Фаза 13.21):** при импорте книг >500 секций `quality_checks` автоматически отключается (вдвое быстрее импорт); батчинг эмбеддингов — `CLUSTER_BATCH_SIZE=64`.

---

## 8. Проверка подключения

### 8.1 В Kilo Code

1. Откройте сессию Kilo
2. Выполните `/mcps` — отобразится список MCP-серверов
3. `mcp-knowledge` должен быть в статусе `connected`
4. Выполните `/mcp-tools mcp-knowledge` → список из 20 инструментов

### 8.2 Ручная проверка (без Kilo)

```bash
# Проверка здоровья сервера
python3 mcp-stdio/bridge.py --health

# Список инструментов
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp-stdio/bridge.py

# Инициализация
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"test"},"capabilities":{}}}' | python3 mcp-stdio/bridge.py

# Поиск
echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"docker"}}}' | python3 mcp-stdio/bridge.py
```

### 8.3 Через curl (напрямую к серверу)

```bash
# Если сервер доступен по HTTP:
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${MCP_API_KEY}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

---

## 9. Troubleshooting

### 9.1 «Сервер недоступен» / Bridge не запускается

**Симптом:** `[bridge] Сервер недоступен: Connection refused`

**Причины и решения:**
```bash
# Проверьте что сервер запущен
curl -sf http://localhost:8000/health/live && echo "OK" || echo "сервер не отвечает"

# Проверьте MCP_SERVER_URL
echo $MCP_SERVER_URL  # должно быть http://localhost:8000 (или IP сервера)

# Проверьте что python3 доступен
python3 --version  # должно быть 3.11+
```

### 9.2 Ошибка аутентификации (401/403)

**Симптом:** `JSON-RPC error (-32002): Authentication failed` или `Forbidden`

**Причины:**
- `MCP_API_KEY` не задан, но на сервере включена auth
- Ключ не входит в `MCP_READ_KEYS` / `MCP_IMPORT_KEYS` / `MCP_WRITE_KEYS` на сервере
- Read-ключ пытается выполнить write-операцию (например, `write_knowledge`)

**Решение:**
```bash
# Проверьте какой ключ используется
echo $MCP_API_KEY

# На сервере проверьте .env:
grep MCP_READ_KEYS .env
grep MCP_IMPORT_KEYS .env
grep MCP_WRITE_KEYS .env

# Для write-операций нужен write-ключ; для import_content — import-ключ
```

### 9.3 Rate limit (429)

**Симптом:** `JSON-RPC error (-32003): Rate limit exceeded`

По умолчанию: read-ключ — 100 запросов/мин, write-ключ — 20 запросов/мин. Если AI-агент шлёт слишком много запросов:
- Увеличьте `RATE_LIMIT_READ_PER_MIN` / `RATE_LIMIT_WRITE_PER_MIN` в `.env` сервера
- Или используйте batch-запросы (JSON-RPC batch — несколько методов в одном HTTP-запросе)

### 9.4 Degraded (503)

**Симптом:** `GET /health` возвращает `"status":"degraded"`

Проверьте логи сервера: `docker compose logs mcp-server --tail 50`. Возможные причины:
- Qdrant не отвечает → `docker compose ps qdrant`
- Embedder не загрузился → проверьте Ollama: `curl http://localhost:11434/api/tags`
- Pipeline worker упал → перезапустите сервер: `docker compose restart mcp-server`

### 9.5 Пустой tools list

**Симптом:** `tools/list` возвращает `{"tools":[]}` или `{"tools":[...]}` с неполным списком

Это штатная ситуация для `tools/list` без авторизации — неподписанные запросы возвращают пустой/редуцированный список. Убедитесь что `MCP_API_KEY` задан и корректен.

### 9.6 Мост выводит мусор в консоль

Мост пишет диагностику в **stderr** (не stdout). В конфиге Kilo это нормально — Kilo читает только stdout для JSON-RPC. Если вы запускаете мост вручную и видите строки `[bridge]` — они идут в stderr, перенаправляйте: `2>/dev/null`.

### 9.7 Bridge пишет ошибку «Некорректный JSON-ответ» при notifications

**Симптом:** после `initialize` клиент шлёт `notifications/initialized`, а bridge в stderr пишет `-32000 Некорректный JSON-ответ от сервера`.

**Причина:** старая версия bridge пыталась парсить пустое тело HTTP 204 (сервер отвечает 204 на notification — тела нет).

**Решение:** обновите `mcp-stdio/bridge.py` (Фаза 13.21): HTTP 204 → `_post_json` возвращает `None` → ответ в stdout НЕ пишется. Проверка: `git log -1 --oneline mcp-stdio/bridge.py` → должен быть `5191f53`.

---

## 10. Ресурсы kb:// и промпты

### 10.1 Ресурсы (resources)

Сервер предоставляет 3 ресурса в схеме `kb://`:

| URI | Содержимое |
|-----|-----------|
| `kb://` | Список всех доменов знаний |
| `kb://{domain}` | Список subjects в домене |
| `kb://{domain}/{subject}` | Список knowledge_ids в subject |

Вызов через MCP: `resources/list` → `resources/read` с параметром `uri`.

### 10.2 Промпты (prompts)

Сервер предоставляет 3 промпта для AI-агентов:

| Prompt | Назначение |
|--------|-----------|
| `how-to-structure-knowledge` | Рекомендации по структурированию знаний в Markdown (SSOT, frontmatter, теги) |
| `best-practice-write` | Best-practice для `write_knowledge`: SSOT, YAML frontmatter, теги, кросс-ссылки, anti-паттерны |
| `periodic_quality_cleanup` | Пошаговая инструкция для AI-агента: `review_queue` → `list_quality_issues` → `resolve_quality_issue` |

Вызов: `prompts/list` → `prompts/get` с параметром `name`.

---

## 11. Переменные окружения моста

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `MCP_SERVER_URL` | `http://localhost:8000` | Базовый URL сервера (POST /mcp, GET /health) |
| `MCP_API_KEY` | `""` (пусто) | API-ключ для X-API-Key header (пусто = без auth) |

---

## 12. Ссылки

- **README проекта:** [`../README.md`](../README.md) — архитектура, быстрый старт, все фазы
- **Исходный код моста:** [`../mcp-stdio/bridge.py`](../mcp-stdio/bridge.py) — ~290 строк stdlib Python (v1.1: HTTP 204 → без ответа)
- **Руководство kb-console:** [`../kb-console/USER_GUIDE.md`](../kb-console/USER_GUIDE.md)
- **Key rotation runbook:** [`key-rotation.md`](key-rotation.md) — процедура смены ключей
- **Air-gap deployment:** [`air-gap-validation.md`](air-gap-validation.md) — деплой на изолированные хосты

---

*Актуально на 2026-08-08. 20 MCP Tools, 3 промпта, 3 ресурса kb://, stdio-мост v1.1 (HTTP 204 → без ответа), ping/notifications по MCP spec, MCP_MAX_REQUEST_SIZE 128 МБ.*
