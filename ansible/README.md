# Ansible: подготовка хоста, деплой и перенос mcp-knowledge

Три сценария использования:

1. **Подготовка нового хоста** — docker + NVIDIA driver + container toolkit (`playbooks/host-prepare.yml`).
2. **Fresh-install из git-клона (хост с интернетом)** — основной сценарий: каталоги → git-клон → compose build/up → cron (`playbooks/deploy.yml`). Пошаговый runbook **M0–M9** ниже.
3. **Перенос работающего стека old → new** (без интернета на new) — `docker save` образов + rsync данных, окно простоя только на cutover (`playbooks/transfer-*.yml`). Runbook в конце («Runbook переноса old→new»).

Топология (миграция code-2026-09-08-001): mcp-server на host-сети, Qdrant `localhost:6333`, ollama-контейнер на `127.0.0.1:11435`, kb-console `8085`.

Контейнеры и порты (имена из `docker-compose.yml`):

| Контейнер | Порт | Назначение |
|---|---|---|
| `mcp-knowledge-server` | 8000 | MCP-сервер (host-сеть) |
| `kb-console` | 8085 | NiceGUI-консоль (host-сеть) |
| `mcp-qdrant-dev` | 6333 | векторная БД |
| `mcp-knowledge-ollama` | 11435 (loopback) | эмбеддинги + chat-модель |

## Структура каталога

```
ansible/
├── ansible.cfg                  # inventory, roles_path, forks
├── Makefile                     # deps/test/lint/check/run/run-role/vault-*
├── inventory/
│   ├── hosts.yml                # группы mcp_kb_old / mcp_kb_new (вписать реальные IP)
│   └── group_vars/
│       ├── all.yml              # несекретные переменные (deploy_root, compose_file, transfer_images)
│       └── all/vault.yml        # СЕКРЕТЫ (ansible-vault, в .gitignore; образец — vault.yml.example)
├── playbooks/
│   ├── host-prepare.yml         # docker + GPU на новом хосте
│   ├── deploy.yml               # fresh-install: dirs → repos → docker → cron
│   ├── transfer-pack.yml        # docker save на СТАРОМ хосте
│   ├── transfer-bulk.yml        # перенос bulk-данных на НОВЫЙ хост
│   └── transfer-cutover.yml     # stop old → финальный rsync → запуск new
├── roles/
│   ├── mcp_kb_host_prepare/     # драйвер NVIDIA, docker, proxy, verify
│   ├── mcp_kb_dirs/             # каталоги deploy_root ВНЕ клона (data/, models_cache) + bare-remote
│   ├── mcp_kb_repos/            # клонирование: mcp-knowledge (HTTPS, публичный) + knowledge (SSH, deploy-key)
│   ├── mcp_kb_docker/           # .env из шаблона, compose build, up --wait, health-пробы
│   ├── mcp_kb_cron/             # backup + quality scan из клона + crontab-env MCP_WRITE_KEY
│   └── mcp_kb_transfer/         # stages: pack | bulk | stop | cutover
├── templates/.env.j2            # окружение контейнеров (секреты — из vault)
├── requirements.txt             # pip-зависимости (ansible, ansible-lint)
└── requirements.yml             # galaxy-коллекции
```

## Make-команды

Запуск из `ansible/`. `PLAYBOOK` — путь относительно `ansible/` (дефолт `playbooks/host-prepare.yml`).

| Команда | Что делает |
|---|---|
| `make deps` | pip-пакеты (`--user --break-system-packages`, Debian 12 PEP 668) + galaxy-коллекции |
| `make test PLAYBOOK=...` | `--syntax-check` |
| `make lint` | `ansible-lint roles/ playbooks/` |
| `make check PLAYBOOK=...` | dry-run: `--check --diff` + vault-pass |
| `make run PLAYBOOK=...` | реальный прогон (запросит vault-pass) |
| `make run-role PLAYBOOK=... ROLE=<tag> HOST=<host>` | один тег роли на одном хосте |
| `make vault-create` / `make vault-edit` | создать / редактировать `inventory/group_vars/all/vault.yml` |

## Runbook: Fresh-install из git-клона (хост с интернетом) — M0–M9

### Предпосылки

- **Debian 12**, доступ в интернет, SSH с контроллера (ansible-пользователь), python3 + pip.
- **GPU NVIDIA ≥ 8 GB VRAM** — для ollama (`mxbai-embed-large` ~0.7 ГБ + `qwen2.5:7b` ~4.7 ГБ; одновременно в VRAM грузится одна модель — `OLLAMA_MAX_LOADED_MODELS=1`).
  ⚠️ **CPU-only не работает из коробки**: `docker-compose.yml` требует nvidia-runtime у сервиса `ollama` **безусловно** (`deploy.resources.reservations.devices`) — без GPU `docker compose up` упадёт с «could not select device driver "nvidia"». Обходной путь: в клоне закомментировать блок `deploy:` у сервиса `ollama` (7 строк: `deploy:` / `resources:` / `reservations:` / `devices:` / `driver: nvidia` / `count: 1` / `capabilities: [gpu]`) — ollama автоматически уйдёт на CPU (медленно: эмбеддинги ~секунды, `qwen2.5:7b` на CPU очень медленный; `mem_limit: 16g` это допускает). Отдельного CPU-override compose-файла в репо нет (follow-up).
- **Read-only deploy-key для приватного knowledge-репо.** Фактический origin репо знаний: `git@github.com:AndreyChuyan/knowledge.git` (**приватный**; `git@github.com:DebugSkills/knowledge.git` не существует — «Repository not found»). Репо кода `https://github.com/DebugSkills/mcp-knowledge.git` публичный — анонимный HTTPS-clone, ключ не нужен.

  Подготовка deploy-key на целевом хосте — под пользователем, от которого идёт clone (ansible `become`, по умолчанию `root`):

  ```bash
  ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -C "mcp-kb-deploy"
  cat /root/.ssh/id_ed25519.pub
  # → GitHub: репозиторий AndreyChuyan/knowledge → Settings → Deploy keys → Add deploy key
  #   вставить публичный ключ; «Allow write access» НЕ включать (ключ read-only)
  cat >> /root/.ssh/config <<'EOF'
  Host github.com
      IdentityFile /root/.ssh/id_ed25519
      IdentitiesOnly yes
  EOF
  ssh -T git@github.com
  # ожидание: «Hi AndreyChuyan/knowledge! You've successfully authenticated...»
  # (GitHub закрывает SSH-сессию — ненулевой exit-код здесь НОРМА)
  ```

### Шаги

| # | Шаг | Команда |
|---|-----|---------|
| M0 | Заполнить `inventory/hosts.yml`: хост `mcp-knowledge-new` (группа `mcp_kb_new`), реальный IP, `ansible_user` | — |
| M1 | Зависимости ansible на контроллере | `cd ansible && make deps` |
| M2 | Создать vault по образцу `vault.yml.example`: 4 ключа (`vault_mcp_read_keys`, `vault_mcp_write_keys`, `vault_mcp_import_keys`, `vault_mcp_console_key`) + при необходимости `vault_cron_write_key` | `make vault-create` |
| M3 | Подготовка хоста: docker + NVIDIA driver + container toolkit. **Reboot**, если ставился драйвер | `make run PLAYBOOK=playbooks/host-prepare.yml` |
| M4 | Деплой: dirs → repos → docker (`.env`, `compose build`, `up -d --wait`, health-пробы) → cron | `make run PLAYBOOK=playbooks/deploy.yml` |
| M5 | Скачать модели в ollama-контейнер (~5.5 ГБ суммарно) | `docker exec mcp-knowledge-ollama ollama pull mxbai-embed-large && docker exec mcp-knowledge-ollama ollama pull qwen2.5:7b` |
| M6 | Bootstrap индекса + проверка `points_count > 0` | `MCP_WRITE_KEY=<ключ> bash /opt/mcp-knowledge/mcp-knowledge/scripts/reindex.sh` |
| M7 | Финальные проверки (health, поиск-smoke, порты) | см. ниже |
| M8 | Обновление стека при доработках | см. ниже |
| M9 | Откат | см. ниже |

**M2 (vault).** `vault_cron_write_key` — одиночный write-ключ для cron-задачи quality scan (один из элементов JSON-списка `vault_mcp_write_keys`). Если его не задать, crontab-переменная `MCP_WRITE_KEY` будет пустой, `quality_scan.sh` откатится на `dev-write-key-001` и на проде получит 401 (бэкап не страдает — он ходит в Qdrant напрямую, без ключа).

**M4 (что клонируется).** Роль `mcp_kb_repos` клонирует: код — `https://github.com/DebugSkills/mcp-knowledge.git` (публичный, HTTPS, без ключа) → `{{ deploy_root }}/mcp-knowledge`; знания — `git@github.com:AndreyChuyan/knowledge.git` (приватный, SSH deploy-key) → `{{ deploy_root }}/knowledge`. Если health-проба mcp-server в M4 не дождалась 200 (модели ещё не скачаны — embedding degraded, `/health` отдаёт 503), это ожидаемо: выполните M5 и повторите M4 (прогон идемпотентен) либо `docker restart mcp-knowledge-server`.

**M4 (каталоги данных).** `data/qdrant` и `data/ollama/models` создаёт Docker при первом `up` (bind-mount), `data/backups` — `backup.sh` (`mkdir -p`); роль `mcp_kb_dirs` их намеренно НЕ создаёт — иначе `git clone` в `mcp_kb_repos` упадёт на непустом каталоге.

**M5 (модели).** Проверка: `curl -s http://localhost:11435/api/tags` — в списке `mxbai-embed-large` и `qwen2.5:7b`.

**M6 (bootstrap индекса).** До этого шага Qdrant пуст — поиск не работает. Ключ возьмите из vault (`vault_mcp_write_keys` — JSON-список, подойдёт любой элемент):

```bash
MCP_WRITE_KEY=<ключ из vault_mcp_write_keys> bash /opt/mcp-knowledge/mcp-knowledge/scripts/reindex.sh
# альтернатива: docker restart mcp-knowledge-server (reconcile индекса при старте)
curl -s http://localhost:6333/collections/knowledge | python3 -m json.tool
# → result.points_count > 0
```

**M7 (проверки).**

```bash
curl -s http://localhost:8000/health                                  # mcp-server: 200
# поиск-smoke: открыть http://<host>:8085/ (kb-console), вкладка поиска, любой запрос → есть результаты
ss -ltnp | grep -E '8000|8085|6333|11435'                             # 11435 — только на 127.0.0.1
```

**M8 (обновление стека при доработках).** Деплой-роль пиннит репо (`mcp_kb_repos__update: false`) — обновления вручную:

```bash
cd /opt/mcp-knowledge/mcp-knowledge
git pull
docker compose build
docker compose up -d --wait
# при изменениях эмбеддера/индекса — перестроить:
MCP_WRITE_KEY=<ключ из vault> bash scripts/reindex.sh
```

`.env` при `git pull` не перетирается (генерируется только ролью `mcp_kb_docker`).

**M9 (откат).**

```bash
cd /opt/mcp-knowledge/mcp-knowledge
docker compose down
git reset --hard <tag-или-commit>        # или git checkout <tag>
docker compose build && docker compose up -d --wait
```

Данные в `data/` (qdrant, модели ollama, dlq, quality, tokens, backups) при down/reset **не трогаются**.

## Firewall и порты

Готовой ufw/iptables-роли в репозитории НЕТ — управление файрволом остаётся за оператором хоста. Матрица портов стека (по `docker-compose.yml`):

| Порт | Сервис | Привязка | Наружу? | Рекомендация |
|---|---|---|---|---|
| 8000 | mcp-server | host-сеть → `0.0.0.0:8000` | **да, нужен** | MCP-клиенты подключаются извне; закрыть от интернета файрволом (allow-list IP) |
| 8085 | kb-console | host-сеть → `0.0.0.0:8085` | да, UI | при необходимости ограничить по IP (там MCP-ключ в UI, но без авторизации страниц) |
| 6333 | qdrant | `ports: "6333:6333"` → **`0.0.0.0`** | ⚠️ опубликован на всех интерфейсах | mcp-server ходит на `localhost:6333` → допустимо перевести в compose на `127.0.0.1:6333:6333` ЛИБО закрыть файрволом (`ufw deny 6333`). gRPC 6334 не публикуется |
| 11435 | ollama | `ports: "127.0.0.1:11435:11434"` | **нет** ✓ | уже loopback-only, менять не нужно |

Проверка на хосте после деплоя: `ss -ltnp | grep -E '8000|8085|6333|11435'` — 11435 должен слушать только на `127.0.0.1`.

## Runbook переноса old→new (M0–M6)

Сценарий для переноса работающего стека без интернета (docker save/load + rsync). Если новый хост с интернетом — используйте fresh-install runbook выше.

| # | Шаг | Команда |
|---|-----|---------|
| M0 | Заполнить `inventory/hosts.yml` (реальные IP old/new), создать vault: `make vault-create` по образцу `inventory/group_vars/all/vault.yml.example` | — |
| M1 | Подготовка нового хоста: `make run PLAYBOOK=playbooks/host-prepare.yml`. **Reboot**, если ставился NVIDIA-драйвер | `make run PLAYBOOK=playbooks/host-prepare.yml` |
| M2 | Упаковка стека на старом хосте (docker save образов, без простоя) | `make run PLAYBOOK=playbooks/transfer-pack.yml` |
| M3 | Bulk-перенос на новый хост (rsync данных + docker load) — без простоя | `make run PLAYBOOK=playbooks/transfer-bulk.yml` |
| M4 | Cutover-окно: stop old → финальный rsync → запуск new. **Простой = минуты** | `make run PLAYBOOK=playbooks/transfer-cutover.yml` |
| M5 | Проверки: `curl http://<new-ip>:8000/health` + search-smoke вручную (поиск через MCP/консоль) | вручную |
| M6 | Старый хост — точка отката, данные **НЕ удалять** | — |

## Откат

- **Fresh-install:** см. шаг M9 выше (`docker compose down` → `git reset --hard <tag>` → повторный build/up; данные в `data/` не трогаются).
- **Перенос:** обратим до демонтажа старого хоста — вернуть подключения/DNS на old и поднять стек там (`docker compose up -d`). Старый хост и staging-каталог (`/opt/mcp-kb-transfer` на новом) содержат полные копии образов и данных — не удалять до подтверждённой стабильной работы (M6).
- `.env` перегенерируется из шаблона + vault при повторном прогоне `mcp_kb_docker`.

## ⚠️ Предупреждения

- **`daemon.json` НЕ копировать** со старого хоста — прокси/драйв-специфику настраивает `mcp_kb_host_prepare` под новый хост.
- **`.env` содержит секреты** (MCP-ключи из vault) — передаётся только по SSH через ansible (mode `0600`), никогда не коммитится.
- **Репо знаний — приватное** (`git@github.com:AndreyChuyan/knowledge.git`): клон по SSH требует read-only deploy-key на целевом хосте (см. «Предпосылки»). Репо кода публичное (HTTPS, без ключа).
- **`vault_cron_write_key`** — одиночный write-ключ для cron quality scan; без него scan-задача получит 401 на проде (см. M2).
- **GPU ≥ 8 GB VRAM** для `qwen2.5:7b`; VL-модель (`qwen2.5vl:7b`) в 8 GB не влезает — не тянуть. CPU-only — только с ручной правкой compose (см. «Предпосылки»).
- Свободное место на новом хосте: ≥ `min_free_gb` (30 GB) — проверяется ролью transfer на этапе bulk; для fresh-install ориентируйтесь на тот же запас (модели ~5.5 ГБ + данные qdrant).
