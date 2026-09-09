# Contributing — mcp-knowledge

Руководство для сообщества DebugSkills и всех, кто планирует дорабатывать проект.
Здесь — **как работать с репозиторием**, **какие правила и конвенции действуют** и
**что проверяется по безопасности** в публичном коде.

---

## О проекте

`mcp-knowledge` — семантическая база знаний для AI-агентов по протоколу **MCP**
(Model Context Protocol). Хранение: Markdown SSOT → chunk → Ollama embed
(`mxbai-embed-large`, 1024d) → Qdrant vector search. **20 MCP Tools**, air-gap
совместимость, production-ready (health, rate-limit, blue-green reindex, quality system).

### Компоненты

| Компонент | Что это | Точка входа |
|---|---|---|
| `mcp_server/` | FastAPI + MCP Handler, 20 tools, auth, rate-limit, quality | `mcp_server/src/mcp_server/` |
| `kb-console/` | Веб-консоль (NiceGUI, :8085) — статус, книги, импорт, поиск | `kb-console/src/kb_console/` |
| `mcp-stdio/` | stdio-мост для подключения AI-агентов (Kilo/Claude/Cline) | `mcp-stdio/bridge.py` |
| `ansible/` | Роли/шаблоны для прод-развёртывания | `ansible/` |
| `scripts/` | Служебные скрипты (offline-deploy, бэкапы) | `scripts/` |
| `docs/` | Документация (air-gap, key-rotation, restore, client-guide) | `docs/` |

Пайплайн: `Markdown SSOT → chunk → embed → Qdrant upsert → INDEX update`, поверх
всё фиксируется в git (audit). Auth — multi-key (`read`/`import`/`write`, `X-API-Key`).

---

## Быстрый старт

```bash
# 1) Python-окружение
python3 -m venv .venv && source .venv/bin/activate
pip install -e mcp_server/ && pip install -e kb-console/   # kb-console для E2E S20

# 2) Конфиг (никогда не коммитить реальный .env!)
cp .env.example .env
#    .env.example содержит ТОЛЬКО плейсхолдеры changeme-*.
#    Сгенерируйте свои ключи:  openssl rand -hex 32

# 3) Запуск (dev: mcp-server host-network; Qdrant и Ollama — контейнеры compose)
docker compose up -d mcp-server
docker compose up -d kb-console      # → http://localhost:8085
```

Полезные цели Makefile: `make dev`, `make logs`, `make test`, `make lint`,
`make bundle`, `make deploy`, `make e2e`, `make prod-verify`, `make console-test`.

---

## Как вносить вклад

### Рабочий процесс

1. **Опишите изменение** в issue/обсуждении, прежде чем начинать крупную работу.
2. **Создайте ветку** от актуального `main` (нейминг по смыслу: `fix/...`, `feat/...`, `docs/...`).
3. **Пишите код по конвенциям** (см. ниже).
4. **Добавьте/обновите тесты** — изменения без зелёных тестов не принимаются.
5. **Проверьте** локально весь затронутый набор тестов.
6. **Создайте PR** на `main`, опишите, что и почему изменилось.

### Стиль коммитов

Формат `type(scope): описание` — `type`: `feat` / `fix` / `docs` / `chore` / `build` / `refactor`.

```text
feat(kb-console): карточки очереди импорта + delete на /books
fix: MCP notification → 204 с пустым телом (Content-Length)
docs(13.21): синхронизация документации по PDF-импорту
```

Коммиты на русском или английском — одинаково допустимо; главное — **единый стиль** и понятное описание.

### Конвенции кода

- **`.trash/`** — только для одноразовых/временных артефактов (скрипты, эксперименты, черновики). НЕ кладите одноразовое в `scripts/` или корень.
- **Переиспользование > копипаст** — перед добавлением дубликата ищите существующий компонент (DRY-by-Detection).
- **Секреты — только через env** (`.env`, `MCP_*_KEYS`, `docker-compose` `environment:`). Никаких хардкод-ключей в коде.
- Соблюдайте линт (`make lint`) и не ломайте существующую логику.

### Тестирование

Минимальный набор перед отправкой PR:

```bash
make e2e-slow                        # E2E S1-S20 (нужны Qdrant :6333 + Ollama :11435)
.venv/bin/python -m pytest mcp_server/tests -q   # полный suite (~629)
make console-test                    # unit + smoke kb-console (~66)
.venv/bin/python -m pytest mcp-stdio/tests -q    # stdio-мост (~19)
```

Любая новая функциональность должна сопровождаться тестами, попадающими в один из этих наборов.

---

## Безопасность

### Что проверяется при работе с кодом

- **Секреты не коммитятся.** Реальные ключи живут только в `.env` / переменных окружения.
- **`.gitignore` покрывает** локальные и служебные файлы: `.env`, `.env.example` (исключение — только шаблон), `.board.md`, `.boardData.md`, `.kilo/`, `.roo/`, `.knowledge`, `plans`, `.backup/`, `.trash/`, `NOTES.md`, дампы и бэкапы.
- **Перед пушем проверяйте** индекс: `git status`, `git ls-files`, и что случайно не попали `*.log`, `*.pem`, дампы БД, токены.
- **История git тоже чистая.** Если секрет попал в историю — используйте `git-filter-repo` и форс-пуш (с согласованием, т.к. переписывание истории влияет на всех).

### Результаты аудита безопасности (2026-08-11)

Полная проверка публичного репозитория на утечку чувствительных данных:

- ✅ **Чисто:** `.env`, `.boardData.md`, `.knowledge`, `.board.md` — не отслеживаются и **никогда** не были в git-истории.
- ✅ **`.env.example` / `.env.prod.example`** содержат только плейсхолдеры (`changeme-*`), реальных ключей нет.
- ✅ **Приватные ключи** (RSA/EC/SSH/PGP), SSH-конфиги — отсутствуют.
- ✅ **Реальные секреты** (JWT, `ghp_`, `sk-`, `AKIA`, `AIza`) — не найдены ни в одном из файлов и коммитов.
- ✅ **Connection strings с паролями**, хардкод кредов в compose/ansible, внутренние IP, персональные email — не обнаружены.
- ⚠️ **Найдено и устранено:** `NOTES.md` был публичным (отслеживался git) и отсутствовал в `.gitignore` → добавлен в `.gitignore`, вычищен из всей истории (`git-filter-repo`), чистка опубликована.

**Вывод:** публичный код утечек чувствительных данных не содержит. Требование к доработчикам — сохранить это состояние.

### Рекомендации для контрибьюторов

- **Перед коммитом:** прогоните локальный сканер секретов, например:
  ```bash
  gitleaks detect --source . -v          # или: detect-secrets scan
  ```
- **Не добавляйте** в отслеживаемые файлы: `.env`, дампы, логи, ключи, `NOTES.md`, личные заметки.
- **Обновляйте `.gitignore`** при появлении новых локальных артефактов.
- Если заметили потенциальную утечку — поднимите **issue с меткой `security`** до публикации.

---

Подготовлено для сообщества DebugSkills.
**Чуян Андрей**
