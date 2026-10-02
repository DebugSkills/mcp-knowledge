# Air-gap: первичная установка mcp-knowledge (038)

> Сборка бандла → перенос → первичная установка на изолированный хост.
> Единый источник — этот документ: `make airgap-runbook` печатает его целиком.

## Инвариант контурной изоляции (оператор, 2026-10-02)

Деплой/обновление переносят между контурами **ТОЛЬКО код и модели**. Документы (корпус
знаний) и индексы (данные Qdrant) **НИКОГДА** не переносятся и не затираются — у dev и prod
**свои документы**.

| Переносится | Никогда не переносится |
|-------------|------------------------|
| код (git bundle `repo.git` / ff-merge клона) | корпус (`…/knowledge`, структура `universal/`) |
| docker-образы (`docker load`) | индекс `data/qdrant` |
| ollama-модели (`DATA_ROOT/ollama/models`) | документы пользователей |

**Как проверить:**
- Пакет: `offline-update.sh inspect --check <пакет>` — в `manifest.json` только `images` (образы)
  и `models` (модели); нет путей с `knowledge/`, `corpus`, `qdrant`.
- Применение: `offline-update.sh apply-stage` / `ansible update-local` пишут только в клон кода,
  docker, models-dir, staging — runtime-защита `guard_write_path()` / `is_corpus_or_index_path()`
  (маркеры `knowledge`, `universal/`, `data/qdrant`) останавливает прогон при указании пути в корпус/индекс.
- Стражи: `tests/test_airgap_package_content.py` (состав пакета, статика путей),
  `tests/test_no_infra_leaks.py` (секретность), `tests/test_airgap_runbook.py` (маркеры ранбука).
- Guard bootstrap-reindex (`ansible/playbooks/deploy.yml`) опрашивает Qdrant напрямую (`GET /collections`)
  и делает reindex только если зональные коллекции (`knowledge_public*`/`knowledge_private*`, включая
  алиасы) реально отсутствуют; при недоступном Qdrant — fail-safe (данные не трогаем).

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

Флаги air-gap (заданы в `host_vars/aikb.yml`):
- `mcp_kb_docker__build: false` — образы приходят из бандла (`docker load`); сборка требует pypi/DNS и в контуре падает;
- `mcp_kb_docker__bootstrap_reindex: false` — bootstrap уже выполнен 2026-10-02 (Э6-Э8). Для **свежего** узла включите
  `true` в `host_vars/aikb.yml`, после первого успешного reindex — выключите обратно. Guard опрашивает Qdrant напрямую
  (`GET /collections`) и делает разовый `python -m mcp_server.cli reindex` только если зональные коллекции отсутствуют;
  при недоступном Qdrant — fail-safe (пропуск, данные не трогаем).

Ожидаемо: `Health: mcp-server /health` = **200**, cron-секция применена, `PLAY RECAP … failed=0`.

**После bootstrap (свежий узел):** выключить `mcp_kb_docker__bootstrap_reindex` (`true → false`) в
`ansible/inventory/host_vars/aikb.yml` и закоммитить — оставленный `true` на живом узле несёт риск
незапрошенного полного reindex при повторном `deploy.yml` (аудит контурной изоляции 2026-10-02, Н1/Н2).

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

---

## ✅ Прогон 2026-10-02 (aikb): первое офлайн-обновление — Э8 закрыт

Первый реальный прогон обновления изолированного узла новым образом, **без переиндексации**.

**Что обновляли:** образ `mcp-knowledge-mcp-server` (фикс blue-green алиаса: `_alias_pairs`, атомарный `swap_alias`, `has_points`, правило «target ≠ active») + код-клон.

**Пакет:** подмножество канонного формата (схема как у `offline-update.sh pack`): `manifest.json` (1 образ) + `repo.git` (`git bundle --all`) + `images/mcp-knowledge-mcp-server_latest.tar.gz` + `CHECKSUMS.sha256`; цель `1c68623`; 303 МиБ. Проверка `offline-update.sh verify` → OK; после переноса sha256 пакета на узле совпала байт-в-байт.

**Порядок на узле (root):** `airgap-bundle-unpack.sh --bundle <pkg.tar.gz>` → `tar -xzf <pkg.tar.gz>` → `offline-update.sh apply-stage <DIR> --clone … --models-dir …` → `docker compose up -d --force-recreate mcp-server`.

**Приёмка:** `/health` = `healthy`; `points=41546` (public 37543 + private 4003); алиасы `knowledge_*→*_v2`; `reconcile: checked=8407, reindexed=0, skipped=8407` — **данные не переиндексировались**; `data/qdrant` и корпус не изменялись.

**Грабли прогона (проверено на практике):**
- Если базовый `python:3.11-slim` удалён из локального стора, а LAN-зеркало реестра отдаёт 5xx — `bundle-pack` падает на резолве базового образа. Лечение: `gunzip -c python-3.11-slim.tar.gz | docker load` перед сборкой (база лежит в старых бандлах).
- Перезапущенный/упавший `pack` может перезаписать тег своего образа сборкой из **чистого клона** (`.pack-src` = HEAD без локальных правок). Перед сборкой пакета убедиться, что нужный коммит **закоммичен** (`make push` не коммитит — только `git push`!), иначе в пакет уедет старый код.
- Перенос (pipe) resumable по частям, но **нельзя запускать два `ship` одновременно**: оба пишут один `.ship-part/<name>.part-*` → sha части не сходится. Запускать одним persistent-процессом; при обрыве — повторить ту же команду.
- Идемпотентности у `ship` нет: если файл уже лежит в `--dest` с тем же sha, повтор всё равно передаёт заново (~8 мин на 303 МиБ при ≈0,62 МиБ/с).
- Переиндексация при обновлении **не нужна**: `apply-stage` делает ff-merge кода + `docker load`; модели копируются только при расхождении digest (в логе `SKIP модель`).

---

## 🔧 Находки обновления 2026-10-02 (aikb): Н9 / Н10 / Н11 / Н12, O24

> Дополнение по следам пакета `mcp-kb-update-20261002T143909Z` (образ — фикс blue-green
> `673ccd3`, контракт путей изоляции — `40840c7`, DATA_ROOT-фикс — `ea8ea04`).
> Узкоспецифичные шимы узла (симлинк DATA_ROOT, exclude клона) — в приватном файле.

### Н11 — сверка образа в `apply-stage`: один образ, два ID в разных image store ✅

После `docker load` `apply-stage` сравнивал `.Id` с `manifest.images[].id` и падал
(«после load .Id образа … != manifest …»). Это **не другой образ**, а разные ID одного:

| Store | IMAGE ID | Что это |
|-------|----------|---------|
| overlay2 (машина сборки; манифест пакета) | **config-digest** | `blobs/sha256/<config>` = `manifest.json.Config`; пример `sha256:31d74e19…` |
| containerd image store (узел) | **digest OCI-манифеста** | `index.json.manifests[0].digest`; пример `sha256:539358cc…` |

`blobs/sha256/<manifest-digest>` → `config.digest` = тот же config-digest ⇒ один образ.
Поток обрывался ПОСЛЕ успешного load и ДО git-merge (клон оставался на прежнем HEAD).
Фикс: `pack` пишет `images[].digest` (из `index.json`), `apply-stage` принимает любой из
двух ID (`image_id_acceptable`, backward-compatible с пакетами старого формата);
регресс-тест `tests/test_airgap_image_id_store_agnostic.py`.

### Н12 — `guard_write_path` возвращал rc=1: ложный STOP `apply-stage` без вывода ✅

Функция заканчивалась на `is_corpus_or_index_path "$p" && die …` → при «не корпус»
последняя команда давала rc=1 ⇒ под `set -e` `apply-stage` падал СРАЗУ (пустые
stdout/stderr), не дойдя до проверок. Внесено контрактом путей `40840c7`, найдено
тестом Н11. Фикс: явный `if is_…; then die …; fi; return 0`.

### Ручной добор (только для узла со СТАРЫМ скриптом)

`docker load` уже прошёл — остаётся довести клон:

```bash
git -C /opt/mcp-knowledge/mcp-knowledge fetch /var/tmp/update-bundle/<PAKET>/repo.git main
git -C /opt/mcp-knowledge/mcp-knowledge merge --ff-only FETCH_HEAD
```

### Н9 — `DATA_ROOT` для `backup.sh` при запуске через ansible

Симптом: `ERROR: Snapshot file not found: <клон>/data/qdrant/snapshots/…` в preflight-таске
бэкапа `update.yml`. Причина: `scripts/backup.sh` берёт `DATA_ROOT="${DATA_ROOT:-$PROJECT_DIR/data}"`
(`backup.sh:15`), а таска не передавала `environment.DATA_ROOT` — данные живут ВНЕ клона
(`data_root`, напр. `/opt/mcp-knowledge/data`). Исправлено в `ea8ea04`: в таске появился
`environment: DATA_ROOT: "{{ data_root }}"`. Тот же контракт — у `deploy.yml` (cron-env)
и `ansible/playbooks/backup.yml`.

### Н10 — тихий выход полного `backup.sh` (открытый пункт; трасса code-2026-10-02-bibliography)

Симптом: полный `bash scripts/backup.sh` завершается **RC=1 молча** сразу после блока
Qdrant-снапшотов: stdout обрывается на «OK: Snapshot validated …», stderr пуст, до
SSOT-шага не доходит. Причина-класс: `set -euo pipefail` + диспетчер вида
`[ "$NO_QDRANT" = false ] && create_qdrant_snapshot` (`backup.sh:755`) — не-0 из функции
делает не-0 весь AND-список и убивает скрипт. Проверено: `backup.sh --no-qdrant` → RC=0
(SSOT уходит в tar-fallback при отсутствии remote `backup` — штатно для air-gap;
console/secrets/errors-тары создаются в `$DATA_ROOT/backups`). Правильный паттерн — как у
console-state: фиксировать RC шага и отдавать его в `exit` в конце, а не прерывать прогон.
Файл правит другая трасса — здесь только фиксация.

### Dirty-гейт `update.yml` — защита, не баг

Preflight обновления требует **чистый код-клон**; локальные правки → падение. Правильная
реакция — НЕ stash (вернёт ту же «грязь»):
- mode-only (` M scripts/…` — смена 100644→100755 при распаковке) →
  `git -C <клон> config core.fileMode false`;
- локальные артефакты вроде симлинка `data` (паттерн `.gitignore` `data/` игнорирует
  только каталоги, а симлинк — файл, без exclude гейт видит `?? data`) →
  `echo data >> <клон>/.git/info/exclude` (метаданные клона, не содержимое репо).
Конкретные команды шимов узла — в приватном файле.

### O24 — доставка ansible-части на узел (открытый пункт)

Ansible-клон оператора на aikb — ручная копия, **cut off от GitHub**: фиксы плейбуков до
узла через git НЕ доходят (факты узла — в приватном файле). Варианты (решить):
- **(а)** класть ansible-часть в офлайн-пакет;
- **(б)** запускать плейбуки из app-клона, который пакет обновляет:
  `ansible-playbook /opt/mcp-knowledge/mcp-knowledge/ansible/playbooks/update.yml -i /root/mcp-knowledge/ansible/inventory …`
