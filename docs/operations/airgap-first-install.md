# Air-gap: первичная установка mcp-knowledge (038)

> Сборка бандла → перенос → первичная установка на изолированный хост.
> Единый источник — этот документ: `make airgap-runbook` печатает его целиком.

## Роли и хосты

| Хост | Роль | Заметка |
|------|------|---------|
| `lup` | интернет-машина (сборка + перенос) | репо кода: `/kvm/mcp-knowledge/mcp-knowledge` |
| `aikb` | целевой изолированный хост | доступа в интернет нет / выборочный |

**SSH на aikb — ТОЛЬКО вложенный ключевой канал** (классический `ssh -W %h:%p` не работает):

```bash
JUMP=<jump-user>@<jump-host>   # конкретика — в приватном файле, см. «Приватные данные»
ssh -o BatchMode=yes "$JUMP" 'ssh -o BatchMode=yes aikb <cmd>'
```

## Измеренные факты среды (проверено)

| Факт | Значение | Следствие |
|------|----------|-----------|
| Канал lup→aikb | **0.62 MiB/s** | параллельные потоки не помогают; обрывы реальны → докачка обязательна |
| Docker Hub с aikb | 129 KiB/s | образы везти бандлом, НЕ качать с узла |
| `registry.ollama.ai` | бывает TLS timeout | модели тоже в бандле, не тянуть на узле |
| rsync на aikb | **не установлен** | режим `--host` вернёт rc 3 → поставить `apt-get install -y rsync` |
| `DATA_ROOT` | `/opt/mcp-knowledge/data` | `host_vars/aikb.yml:37` |
| Модели | bind-mount `${DATA_ROOT}/ollama/models` | `docker-compose.yml:71` |

## Шаг 1 — сборка бандла (lup)

```bash
cd /kvm/mcp-knowledge/mcp-knowledge
tmux new -s pack
make bundle-pack ARGS="--out /kvm/update-bundles"
```

7 шагов `[N/7]` (каждый — баннер + тайминг + tee-лог): `[1/7]` preflight → `[2/7]` чистая
копия (`git clone --local`, грязное дерево не мешает) → `[3/7]` сборка пакета
(образы + git bundle + модели) → `[4/7]` carrier-образ моделей → `[5/7]` база python →
`[6/7]` верификация → `[7/7]` сводка.

- Длительность ≈ **15–25 мин** (сборка образов — основное время).
- Успех = `[6/7]` `inspect --check` OK + **три артефакта**:
  - `mcp-kb-update-<ISO>.tar.gz` (~9 ГБ) — код + образы
  - `mcp-kb-models-<ISO>.tar.gz` (~5.3 ГБ) — carrier-образ моделей
  - `python-3.11-slim.tar.gz` — база python для `docker compose build`
- **Обрыв (Ctrl+C) безопасен**: временный клон `.pack-src` перезаписывается (`--force`)
  и подчищается `trap EXIT INT TERM` — просто повторить `make bundle-pack`.
- **НЕ запускать второй pack параллельно** (один клон `.pack-src` на `--out`).

## Шаг 2 — перенос на узел (lup)

Все команды — из корня репо. Три варианта:

**(а) USB:**

```bash
make bundle-ship-usb USB=/media/<user>/<USB>
```

Прогресс `--info=progress2` → sha256-сверка каждого файла → `sync` → напоминание `umount`.

> ⚠️ Бандл **не содержит корпус** и не должен: документы в каждом контуре свои — переносятся только код и модели.

**(б) Сеть БЕЗ rsync (рабочий вариант — каталожный pipe):**

```bash
make bundle-ship-net ARGS="--pipe-via $JUMP"
```

Чанки `--chunk 900M`; **докачка на уровне частей** — состояние в
`/var/tmp/update-bundle/.ship-part/` на узле (создаётся preflight'ом), повторный запуск
скипает переданное, финальная sha256 целого файла перед удалением частей.
Текущий набор (5.3 ГБ) ≈ **2.4 ч** при канале 0.62 MiB/s — точную оценку печатает сам preflight.
Превью без передачи (список файлов, размеры, ETA): `ARGS="--pipe-via $JUMP --dry-run"`.

**(в) Сеть С rsync (после установки rsync на узле):**

```bash
make bundle-ship-net ARGS="--host $JUMP --rsync-path 'ssh -o BatchMode=yes aikb rsync'"
```

При отсутствии rsync на узле скрипт вернёт **rc 3** и подскажет apt-команду
(`ssh … 'ssh … apt-get install -y rsync'`).

Печать чек-листа без передачи: `make bundle-ship-net ARGS="--checklist-only"` /
`make bundle-ship-usb USB=… ARGS="--checklist-only"`.

## Шаг 3 — предусловия (aikb, root)

1. Клон кода: `/opt/mcp-knowledge/mcp-knowledge` (публичный HTTPS).
2. **Корпус — СВОЙ в каждом контуре** (правило оператора, 2026-10-01): документы НЕ переносятся между контурами,
   между ними едут только **код и модели**. На узле — собственный git-репозиторий `/opt/mcp-knowledge/knowledge`,
   ветка `main`, **≥1 коммит** (иначе mcp-server не стартует), наполняется своим содержимым.
3. Vault (Э4): значения в `inventory/group_vars/all/vault.yml` (связка console/cron).

## Шаг 4 — распаковка (aikb, root)

```bash
./scripts/airgap-bundle-unpack.sh --bundle /var/tmp/update-bundle/mcp-kb-update-<ISO>.tar.gz \
    --models-image /var/tmp/update-bundle/mcp-kb-models-<ISO>.tar.gz \
    --python-base /var/tmp/update-bundle/python-3.11-slim.tar.gz \
    --data-root /opt/mcp-knowledge/data
```

- `docker load` идемпотентен (skip по `.Id`), модели → `$DATA_ROOT/ollama/models`.
- ⚠️ **Модели ставятся ДО первого старта стека** — иначе `/health` = **503**
  (`mcp_server/tests/unit/test_health.py:7`, embedder not ready), а канонный
  `update-local` на пустом сторе даёт deadlock: health-гейт
  (`ansible/playbooks/update.yml:428-436`) стоит ДО блока моделей (`:478-516`).
- Проверка стора:
  `ls /opt/mcp-knowledge/data/ollama/models/manifests/registry.ollama.ai/library/`
  → `mxbai-embed-large  qwen2.5`.

  **Обязательно после распаковки** (иначе `/health` = 503 без внятной причины):
  ```bash
  docker exec mcp-knowledge-ollama ollama list        # ожидаем mxbai-embed-large:latest 669 MB
  ```
  Если список пуст, хотя манифест на месте — сравните digest'ы из манифеста с `ls blobs/`: чаще всего не хватает
  **config-блоба** (у `mxbai-embed-large` это `sha256:38badd94…`, 408 Б) — его теряет сборка пакета; `ollama` молча
  пропускает модель без config.

## Шаг 5 — деплой (aikb, root)

```bash
cd /root/mcp-knowledge/ansible
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --ask-vault-pass --skip-tags repos
```

Флаги air-gap (уже заданы в `host_vars/aikb.yml`, 2026-10-01):
- `mcp_kb_docker__build: false` — образы приходят из бандла (`docker load`); сборка требует pypi/DNS и в контуре падает;
- `mcp_kb_docker__bootstrap_reindex: true` — плейбук сам делает разовый `python -m mcp_server.cli reindex`, если коллекций
  Qdrant нет (иначе `/health` = degraded 404 → health-гейт падает).

Ожидаемо: `Health: mcp-server /health` = **200**, cron-секция применена, `PLAY RECAP … failed=0`.

## Шаг 6 — приёмка KB (aikb)

```bash
docker logs -f mcp-knowledge-server | grep -E '\[START\]|\[REINDEX\]|\[EMBED\]|\[RECONCILE\]'
```

**«KB работает» = все индикаторы:**
- `status=healthy` ∧ `checks.embedding.loaded=true` ∧ `checks.qdrant.points>0`
- `reconcile.mode=full, state=done`
- непустой smoke-поиск в консоли `https://<console_lan_ip>:8443`

**Свежий узел: коллекции Qdrant создаст только явный полный reindex** (startup-reconcile коллекции НЕ создаёт —
`indexing/reconcile.py`, `ensure_collection` живёт в `cli.py`). Запускать один раз, ~15-30 мин на 8407 записей:
```bash
docker exec mcp-knowledge-server python -m mcp_server.cli reindex     # в tmux
```
Признак готовности: `checks.qdrant.ok=true` ∧ `points>0`; поле `reconcile` в health относится к startup-прогону
и может остаться `pending/none` — ориентируйтесь на `points`.
При пустом/неполном индексе — запустить явный reindex (см. выше) и дождаться полного счёта чанков (ориентир для текущего корпуса: public ≈37.5 тыс., private ≈4 тыс.); поле `reconcile` в `/health` относится к startup-прогону.

## Шаг 7 — дальнейшие обновления

```bash
make -C ansible update-local BUNDLE=/media/…/mcp-kb-update-<ISO>.tar.gz
```

Стор моделей уже не пуст ⇒ deadlock (Шаг 4) не возникает.

## Диагностика и грабли

Логи: `/kvm/update-bundles/bundle-pack-<TS>.log`, `pack.log`, `ship-<TS>.log`, `ship-net.log`.

| Симптом | Причина / лечение |
|---------|-------------------|
| `--to … уже существует` | лечится автоматически (`--force` перезаписывает `.pack-src`) |
| `Нет правила для сборки цели` | запущено НЕ из корня репо → `cd /kvm/mcp-knowledge/mcp-knowledge` |
| `split: невозможно открыть …/mcp-kb-airgap-bundle.tar.gz` | маску `*.tar.gz` перехватил легаси-файл из CWD (старый `make bundle`). Исправлено в скрипте (`set -f` + запрет легаси-имени), легаси-бандл убран в `.trash/` |
| `split: невозможно открыть …/mcp-kb-update-*.tar.gz` | файла нет в `--src` → сначала `make bundle-pack` |
| сборка 0 Б / `No such file or directory` на узле | не создан `$DEST/.ship-part` или редирект исполнился не на узле — обновить `scripts/airgap-bundle-ship.sh` (preflight создаёт каталог, сборка через `node_ssh_cmd`) |
| `ollama list` пуст, манифест на месте | не хватает config-блоба (pack его теряет, O18): сверить digest'ы манифеста с `ls blobs/` и дослать файл |
| `нет пары mcp-kb-update-* + mcp-kb-models-*` | в `--src` не бандл offline-update → пересобрать `make bundle-pack` |
| Обрыв сборки в голом терминале | запускать в `tmux` (Шаг 1) |

| `no such service: mcp-knowledge-server` | в compose сервис называется **`mcp-server`** (`mcp-knowledge-server` — имя контейнера): `docker compose up -d --force-recreate mcp-server` |
| `container mcp-knowledge-server is unhealthy` + `Application startup failed` | ручной патч в контейнере сломан: пересоздать сервис из образа (`--force-recreate mcp-server`) и наложить патч файлом (см. ниже) |
| `/health` = degraded, 404 `Collection knowledge_* doesn't exist` после рестарта | инцидент 2026-10-01: blue-green терял алиас (`get_aliases()` отдаёт pydantic-ответ, ошибка глушилась, `force_recreate` удалял живую коллекцию). Исправлено в коде (`_alias_pairs`, атомарный `swap_alias`, правило «не целимся в активную», `has_points`). До прихода исправленного образа: `docker cp /var/tmp/update-bundle/fix_alias.py mcp-knowledge-server:/tmp/ && docker exec mcp-knowledge-server python3 /tmp/fix_alias.py && docker restart mcp-knowledge-server` |
| Алиас восстановить руками | `curl -s -X POST localhost:6333/collections/aliases -H 'Content-Type: application/json' -d '{"actions":[{"create_alias":{"collection_name":"knowledge_public_v1","alias_name":"knowledge_public"}}]}'` |

Запреты: НЕ тянуть образы/модели с aikb (канал 129 KiB/s); НЕ запускать `make lint` на
aikb (`ansible-compat` требует core ≥2.16); НЕ повторять Э5-плейбук без
`--skip-tags toolkit` (дефект ключа NVIDIA, O9).

## Приватные данные

Реальные адреса jump-хоста, логин, прокси и LAN-адреса — **НЕ в открытом репо**.
Они лежат в `docs/operations/airgap-first-install.private.md` (gitignored);
шаблон — `docs/operations/airgap-first-install.private.md.example` рядом
(плейсхолдеры `<jump-user>@<jump-host>`, `<remote-user>`, `<proxy-host:port>`,
`<lan-cidr>`). Скопировать шаблон в `.private.md` и заполнить своими значениями.

## Провенанс

- `scripts/airgap-bundle-pack.sh` (+ `scripts/airgap-clean-src.sh`) — сборка.
- `scripts/airgap-bundle-ship.sh` — перенос (USB / pipe / rsync).
- `scripts/airgap-bundle-unpack.sh` — распаковка на узле.
- Критика распаковки — `.boardData.md §6.4`; открытые пункты доски O13/O14.
