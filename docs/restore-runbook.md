# Restore Runbook — MCP Knowledge Server

> **Версия:** 1.1 | **Дата:** 2026-09-29 | **Область:** Production Recovery
> **Связанные документы:** [`scripts/backup.sh`](../scripts/backup.sh), [`docker-compose.yml`](../docker-compose.yml), [`docs/air-gap-validation.md`](air-gap-validation.md) §7 (offline update)

---

## Предварительные требования

Перед началом восстановления убедитесь:

| № | Требование | Проверка |
|:--|:-----------|:---------|
| 1 | Доступ к серверу (SSH / console) | `ssh <host>` |
| 2 | Docker и docker compose установлены | `docker compose version` |
| 3 | Qdrant запущен (порт 6333) | `curl -s http://localhost:6333/healthz` → `ok` |
| 4 | Доступна резервная копия SSOT | `ls knowledge/.git` или `ls data/backups/knowledge-*.tar.gz` |
| 5 | Доступен Qdrant snapshot | `ls data/qdrant/snapshots/knowledge/ | head -5` |
| 6 | Достаточно дискового пространства | `df -h /` — min 2× размер snapshot |

**Инструменты восстановления:**
- Скрипт бэкапа: `scripts/backup.sh`
- REST API Qdrant: `http://localhost:6333`
- Docker Compose: управление контейнерами `mcp-qdrant`, `mcp-knowledge-server`

---

## Сценарий 1: Восстановление Qdrant из snapshot

**Применимость:** потеря/повреждение векторного индекса, сбой коллекции, миграция на новый хост.
SSOT-данные (Markdown в `knowledge/`) **не затронуты**.

> **2026-08-09:** `scripts/backup.sh` снапшотит все коллекции mcp-knowledge,
> **кроме `ws-*`** (решение владельца: Svyazi подтвердил, что `ws-*` — чужие,
> см. `docs/qdrant-ws-collections-message.md` в feature/Svyazi).
> Файлы снапшотов пишутся в bind-mount `data/qdrant/snapshots/<collection>/` (env
> `QDRANT__STORAGE__SNAPSHOTS_PATH=/qdrant/storage/snapshots` в docker-compose.yml).
> Раньше Qdrant писал снапшоты в `/qdrant/snapshots` (слой контейнера) — они терялись
> при пересоздании контейнера и не попадали в rsync-бэкап.

### Шаги

#### 1.1. Остановить mcp-server
```bash
docker compose stop mcp-server
```
> **Почему:** во время восстановления Qdrant-коллекции запросы к ней могут давать некорректные результаты или ошибки.

#### 1.2. Найти актуальный snapshot
```bash
# Список доступных snapshots
curl -s http://localhost:6333/collections/knowledge/snapshots \
  | python3 -m json.tool

# Или локально через файловую систему:
ls -lt data/qdrant/snapshots/knowledge/ | head -5
```
Выберите наиболее свежий snapshot по дате в имени (`backup-YYYYMMDD-HHMMSS`).

#### 1.3. Восстановить коллекцию из snapshot
```bash
SNAPSHOT_NAME="backup-20260803-030000"  # замените на актуальное имя

curl -s -X PUT \
  "http://localhost:6333/collections/knowledge/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d '{"location": "file:///qdrant/storage/snapshots/knowledge/'"${SNAPSHOT_NAME}"'"}'
```

> **Важно:** Qdrant обрабатывает восстановление **асинхронно**. Подождите 5–10 секунд после ответа API.

#### 1.4. Проверить восстановленную коллекцию
```bash
# Проверить статус коллекции
curl -s http://localhost:6333/collections/knowledge | python3 -m json.tool

# Ожидаемые значения:
#   "status": "green"
#   "points_count": > 0
```

#### 1.5. Запустить mcp-server
```bash
docker compose start mcp-server
```

#### 1.6. Финальная проверка
```bash
# Health check — убедиться, что сервер работает
curl -s http://localhost:8000/health | python3 -m json.tool

# Проверить, что точки доступны через search
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${MCP_READ_KEY}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"test","limit":1}}}' \
  | python3 -m json.tool
```

### Критерии успеха
- `GET /collections/knowledge` → `status: "green"`, `points_count > 0`
- `GET /health` → `qdrant.connected: true`, `qdrant.points > 0`
- `search_knowledge` возвращает результаты (не пустой массив)

---

## Сценарий 2: Восстановление SSOT (Markdown) из git

**Применимость:** потеря/повреждение файлов Markdown, откат ошибочных изменений, возврат к известному состоянию.
Qdrant-коллекция **не затронута**, но потребуется реиндексация.

### Шаги

#### 2.1. Перейти в директорию SSOT и восстановить из git
```bash
cd knowledge/

# Проверить доступные бэкапы (remote "backup" должен быть настроен)
git remote -v

# Если remote "backup" доступен — восстановить из него
git fetch backup
git reset --hard backup/main

# Альтернатива — восстановление из tar-бэкапа:
# cd ..
# tar -xzf data/backups/knowledge-YYYYMMDD-HHMMSS.tar.gz
```

#### 2.2. Если восстанавливали из tar — переинициализировать git (опционально)
```bash
cd knowledge/
git init
git add -A
git commit -m "restore: восстановление из tar-бэкапа $(date -I)"
```

#### 2.3. Остановить и запустить mcp-server (рестарт для подхвата изменений)
```bash
cd /kvm/mcp-knowledge/mcp-knowledge
docker compose restart mcp-server
```

#### 2.4. Проверить файлы SSOT
```bash
# Количество Markdown-файлов
find knowledge/ -name "*.md" | wc -l

# Свежесть данных
ls -lt knowledge/ | head -10
```

#### 2.5. Проверить health
```bash
curl -s http://localhost:8000/health | python3 -m json.tool
# Ожидается: qdrant.points > 0, ssot.files > 0
```

### Критерии успеха
- Markdown-файлы восстановлены: `find knowledge/ -name "*.md" | wc -l` > 0
- `GET /health` → `ssot.files > 0`
- Если Qdrant содержит точки — `search_knowledge` возвращает результаты

---

## Сценарий 3: Полное восстановление (Qdrant + SSOT)

**Применимость:** полная потеря данных, переезд на новый сервер, восстановление после сбоя диска.
Восстанавливаются **оба** источника данных.

### Шаги

#### 3.1. Восстановить SSOT (Сценарий 2, шаги 2.1–2.4)
```bash
# 1. Восстановить Markdown из git или tar
cd knowledge/
git fetch backup && git reset --hard backup/main

# 2. Рестарт mcp-server
cd /kvm/mcp-knowledge/mcp-knowledge
docker compose restart mcp-server
```

#### 3.2. Восстановить Qdrant (Сценарий 1, шаги 1.1–1.5)
```bash
# 1. Остановить mcp-server
docker compose stop mcp-server

# 2. Восстановить коллекцию из snapshot
SNAPSHOT_NAME="backup-20260803-030000"
curl -s -X PUT \
  "http://localhost:6333/collections/knowledge/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d '{"location": "file:///qdrant/storage/snapshots/knowledge/'"${SNAPSHOT_NAME}"'"}'

# 3. Запустить mcp-server
docker compose start mcp-server
```

#### 3.3. Альтернатива шагу 3.2 — полный reindex из SSOT
Если snapshot недоступен, но SSOT восстановлен:
```bash
# Запустить reindex через MCP tool
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${MCP_WRITE_KEY}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"reindex","arguments":{}}}' \
  | python3 -m json.tool
```
> **Время выполнения:** ~1 мин на 100 файлов. Зависит от размера SSOT и бэкенда эмбеддинга.

#### 3.4. Полный health check
```bash
echo "=== Full Health Check ==="

# 1. Qdrant health
echo -n "Qdrant: "
curl -s http://localhost:6333/healthz

# 2. Qdrant collection
echo ""
echo -n "Collection: "
curl -s http://localhost:6333/collections/knowledge | python3 -c "import sys,json; d=json.load(sys.stdin)['result']; print(f'status={d[\"status\"]} points={d[\"points_count\"]}')"

# 3. mcp-server liveness
echo ""
echo -n "Liveness: "
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health/live

# 4. mcp-server readiness
echo ""
echo "Readiness:"
curl -s http://localhost:8000/health | python3 -m json.tool

# 5. Search test
echo ""
echo "Search test:"
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${MCP_READ_KEY}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"test","limit":3}}}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'total={r[\"result\"][\"total_count\"]}')" 2>/dev/null || echo "FAIL"
```

### Критерии успеха (все проверки должны быть green)
| Проверка | Команда | Ожидаемый результат |
|:---------|:--------|:--------------------|
| Qdrant healthz | `curl localhost:6333/healthz` | `ok` |
| Коллекция существует | `GET /collections/knowledge` | `status: "green"` |
| Points count > 0 | `GET /collections/knowledge` | `points_count > 0` |
| Liveness (200) | `GET /health/live` | HTTP 200 |
| Readiness (200 или 503) | `GET /health` | HTTP 200 (не 503) |
| Qdrant подключён | `GET /health` | `qdrant.connected: true` |
| Search работает | tool `search_knowledge` | `total > 0` |
| SSOT файлы | `find knowledge/ -name "*.md" \| wc -l` | > 0 |

---

## Процедура тестирования бэкапов (`--test-restore`)

Скрипт `scripts/backup.sh` поддерживает флаг `--test-restore` для проверки полного цикла восстановления **без прерывания работы**:

```bash
./scripts/backup.sh --test-restore
```

**Что делает тест:**
1. Создаёт snapshot коллекции `knowledge`
2. Валидирует snapshot (существует, размер > 0)
3. Восстанавливает snapshot во временную коллекцию `knowledge_restore_test`
4. Сравнивает количество точек между оригиналом и восстановленной
5. Удаляет временную коллекцию

**Рекомендуемая периодичность:** еженедельно (в cron после основного бэкапа):
```cron
# Еженедельный restore test (воскресенье, 4:00)
0 4 * * 0 cd /kvm/mcp-knowledge/mcp-knowledge && ./scripts/backup.sh --test-restore >> /var/log/mcp-restore-test.log 2>&1
```

---

## Устранение неисправностей (Troubleshooting)

### Ошибка: "Snapshot file not found"
```
ERROR: Snapshot file not found: data/qdrant/snapshots/knowledge/backup-20260803-030000
```
**Причина:** Snapshot не был создан, или смонтирован не тот volume.
**Решение:**
1. Проверьте список snapshots через API: `curl http://localhost:6333/collections/knowledge/snapshots`
2. Проверьте монтирование volume: `ls data/qdrant/snapshots/knowledge/`
3. Создайте новый snapshot: `curl -X POST http://localhost:6333/collections/knowledge/snapshots`

### Ошибка: "Collection 'knowledge' not found"
**Причина:** Коллекция удалена или не была создана.
**Решение:**
1. Проверьте список коллекций: `curl http://localhost:6333/collections`
2. Если коллекции нет — восстановите из snapshot (Сценарий 1) или запустите полный reindex (Сценарий 3, шаг 3.3)

### Ошибка: "Snapshot file is empty" (размер 0)
**Причина:** Qdrant не завершил создание snapshot, или коллекция пуста.
**Решение:**
1. Проверьте статус коллекции: `curl http://localhost:6333/collections/knowledge`
2. Если `points_count: 0` — коллекция пуста. Восстановите из другого snapshot или запустите reindex.
3. Если `points_count > 0` — повторите создание snapshot.

### Ошибка: mcp-server возвращает 503 на `/health`
```json
{"status": "degraded", "checks": {"qdrant": {"status": "error", "message": "connection refused"}}}
```
**Причина:** Qdrant не запущен или не отвечает.
**Решение:**
1. Проверьте контейнер: `docker compose ps qdrant`
2. Если остановлен — запустите: `docker compose start qdrant`
3. Проверьте health: `curl http://localhost:6333/healthz`
4. После восстановления Qdrant — рестарт mcp-server: `docker compose restart mcp-server`

### Ошибка: git push backup failed (при создании бэкапа)
```
WARN: git push backup failed. Falling back to tar.
```
**Причина:** Не настроен git remote `backup` (bare-репозиторий для бэкапов).
**Решение:** Это штатный fallback — бэкап будет создан через tar. Для настройки git-бэкапа:
```bash
cd knowledge/
git remote add backup /path/to/bare/backup.git
# или
git remote add backup ssh://backup-host/path/to/repo.git
```

### Ошибка: точки в Qdrant есть, но search возвращает пустой результат
**Причина:** Эмбеддинг-модель не загружена или несоответствие версий.
**Решение:**
1. Проверьте логи: `docker compose logs mcp-server | grep -i embed`
2. Проверьте `/health` → `embed.ready: true`
3. Если модель не загружается — проверьте `models_cache/` и переменные окружения в `.env`

---

## Профилактика и мониторинг

### Ежедневно (автоматически, cron)
```cron
# Бэкап в 3:00
0 3 * * * cd /kvm/mcp-knowledge/mcp-knowledge && ./scripts/backup.sh >> /var/log/mcp-backup.log 2>&1
```

### Еженедельно (автоматически, cron)
```cron
# Restore test в 4:00 по воскресеньям
0 4 * * 0 cd /kvm/mcp-knowledge/mcp-knowledge && ./scripts/backup.sh --test-restore >> /var/log/mcp-restore-test.log 2>&1
```

### Мониторинг (проверять вручную или через алерты)
| Что проверять | Метрика | Порог |
|:--------------|:--------|:------|
| Размер snapshot | Файл в `data/qdrant/snapshots/` | size > 0 |
| Свежесть бэкапа | `ls -lt data/backups/ | head -1` | < 25 часов |
| Успешность test-restore | Лог `/var/log/mcp-restore-test.log` | Содержит `PASSED` |
| Количество точек | `GET /collections/knowledge` | `points_count > 0` |
| Свободное место | `df -h /` | > 20% |

---

## Контрольный список при инциденте

- [ ] Определить сценарий (1, 2 или 3)
- [ ] Проверить наличие бэкапов (snapshot + SSOT)
- [ ] Остановить mcp-server (если требуется сценарием)
- [ ] Выполнить шаги восстановления по инструкции
- [ ] Проверить все критерии успеха
- [ ] Запустить mcp-server
- [ ] Проверить `/health` и `search_knowledge`
- [ ] Задокументировать инцидент (дата, причина, сценарий, время восстановления)

---

## §11. Offline update и откат

**Применимость:** обновление **уже работающей** установки в изолированном контуре из offline-пакета (air-gap), а также быстрый откат, если обновление прошло неудачно. Это не замена Сценариям 1-3: §11 возвращает **код и образы**, **данные не трогает** (Qdrant/SSOT/console живут в `{{ data_root }}` вне git-клона). Если откат невозможен/недостаточен — Сценарий 3 (полное восстановление).

### Процедура (кратко)

```bash
# 1) Машина с интернетом — собрать полный пакет
make update-bundle [ARGS="--with-models"]
# 2) Носитель — проверка целостности пакета
make update-bundle-verify DIR=/media/.../mcp-kb-update-<ISO>.tar.gz
# 3) Изолированный хост — dry-run (план, 0 мутаций), затем применение
make -C ansible update-local-check BUNDLE=/media/.../mcp-kb-update-<ISO>.tar.gz
make prod-update-local            BUNDLE=/media/.../mcp-kb-update-<ISO>.tar.gz
```

Применение **идемпотентно**: неизменённый коммит → `merge` пропускается, образы с совпавшим `.Id` → `load` пропускается, модели с совпавшим digest → копирование пропускается; контейнеры не перезапускаются. Перед загрузкой изменённых образов сохраняются теги `:prev` (точка отката).

### Критерии успеха

- health ×4 зелёный: `:8000/health`, `:6333/healthz`, `:11435/api/tags`, `:8085/` (200/301/302/401);
- `git -C <clone_dir> rev-parse HEAD` == `manifest.target_commit`;
- `docker image inspect -f '{{.Id}}'` двух своих образов == Id из `manifest.json`;
- логи без `error`/`traceback`; повторный `update-local` того же пакета → 0 `changed`.

### Откат

```bash
# 1) вернуть образы из rollback-тегов
docker tag mcp-knowledge-mcp-server:prev mcp-knowledge-mcp-server:latest
docker tag kb-console:prev kb-console:prod
# 2) вернуть код на prev-HEAD (деструктивная ручная операция отката)
git -C <clone_dir> reset --hard <prev-HEAD>
# 3) поднять стек на откате — ТОЛЬКО up
make -C ansible run-tag PLAYBOOK=playbooks/update.yml ROLE=up HOST=<host>
```

Проверка после отката: health ×4 + `rev-parse HEAD` == prev-HEAD + `docker image inspect -f '{{.Id}}'` == Id тега `:prev`.

> ⚠️ **После отката НЕ повторяйте apply** (`make prod-update-local` с тем же пакетом): плейбук увидит `HEAD(prev) != target` → `merge` вернёт target → `load` снова накатит новые образы — **откат отменится сам собой**. Идемпотентность apply — про «повтор без изменений», а не про «повтор после ручного отката». Если плейбук остановился **после `load`, до `up`** — не запускайте `up` вручную: сначала шаги 1-3 отката (новый образ без миграций = риск).

**Крайняя мера:** `make prod-restore SCOPE=all RESTORE_CONFIRM=yes` — Сценарий 3.
