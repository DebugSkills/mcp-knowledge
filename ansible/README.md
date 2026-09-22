# Ansible: подготовка хоста, деплой и перенос mcp-knowledge

Ansible-обвязка стека mcp-knowledge: подготовка хоста (docker + NVIDIA GPU), fresh-install деплой и перенос стека между хостами.

> Операционный runbook (пошаговые команды, переменные, firewall/порты, обновление/откат) ведётся **приватно** и в публичный репозиторий не входит.

## Три сценария

- **Сценарий A — fresh-install из git-клона** — деплой на хост с интернетом, M0–M9 (`playbooks/deploy.yml`).
- **Сценарий B — подготовка хоста** — docker + драйвер NVIDIA + container toolkit (`playbooks/host-prepare.yml`).
- **Сценарий C — перенос old→new** — ⚠️ **DEPRECATED** (code-2026-09-22-002): layout данных вынесен в `DATA_ROOT` (`{{ deploy_root }}/data`, см. `deploy.yml` и `group_vars/all.yml`), пути transfer-плейбуков рассчитаны на старый layout (data внутри клона). Не использовать до переработки; файлы сохранены как справка.

## Make-таргеты (`ansible/Makefile`)

| Таргет | Плейбук | Что делает |
|---|---|---|
| `run` / `run-tag` | deploy / host-prepare / transfer | деплой по сценарию (transfer — DEPRECATED, см. выше); `run-tag PLAYBOOK=… ROLE=<тег> HOST=<хост>` — один тег |
| `update` | `update.yml` | идемпотентный апдейт КОДА: preflight (диск/dirty-гейт/**mount-гейт** DATA_ROOT/лёгкий бэкап) → `git pull --ff-only` только `mcp-knowledge` → build → `up -d --wait` → health ×4 → **миграции релиза** (`scripts/(backfill\|migrate)_*.py` через `docker run --env-file`, с pause-подтверждением) → rollback-hint. `update_source=bundle` — заглушка (air-gap → `scripts/offline-deploy.sh`) |
| `update-check` | `update.yml` | dry-run апдейта (`--check --diff`) — смотреть ПЕРЕД `update` |
| `logs` / `events` | `ops.yml` | read-only, **БЕЗ vault-промпта**: `logs [S=<сервис>] [N=<строк>]`; `events [M=<строк>]` — маркеры `[START\|RECONCILE\|REINDEX\|EMBED\|MCP\|IMPORT\|ANALYZE]` за 24ч (без `[AUTH]`) |
| `health` / `metrics` / `stats` | `ops.yml` | health-пробы ×4 (8000/6333/11435/8085); Prometheus `:8000/metrics`; `docker stats` |
| `backup` | `backup.yml` | полный бэкап: qdrant-snapshots + weekly-4 (вс) + SSOT (bare/tar) + console-state + secrets-tar (0600) |
| `backup-verify` | `backup.yml` | `backup.sh --verify`: qdrant test-restore + sha256 таров + console untar-drill → «RESTORE TEST PASSED/FAILED» |
| `restore` | `backup.yml` | **ДЕСТРУКТИВНЫЙ, интерактивный**: `make restore SCOPE=qdrant\|ssot\|console\|secrets\|all RESTORE_CONFIRM=yes [RESTORE_SNAPSHOT=<name>]` — план перезаписи → confirm-гейт + pause → recover/clone/untar → рестарт + health |
| `vault-pass-file` | — | `make <target> VAULT_PASS_FILE=~/.vault-pass.txt` — `--vault-password-file` вместо интерактивного `--ask-vault-pass` |

Из корня клона те же операции — passthrough: `make prod-update`, `prod-update-check`, `prod-logs [S=…] [N=…]`, `prod-events [M=…]`, `prod-health`, `prod-metrics`, `prod-stats`, `prod-backup`, `prod-backup-verify`, `prod-restore [SCOPE=…] [RESTORE_CONFIRM=yes] [RESTORE_SNAPSHOT=…]`.

## Структура каталога `ansible/`

```
ansible/
├── ansible.cfg                  # inventory, forks
├── Makefile                     # deps/test/lint/check/run/run-tag/vault-*
├── inventory/
│   ├── hosts.yml                # группы mcp_kb_old / mcp_kb_new (целевой хост)
│   ├── group_vars/
│   │   ├── all.yml              # переменные (в т.ч. бывшие дефолты ролей): deploy_root, compose_file, transfer_images
│   │   └── all/vault.yml        # СЕКРЕТЫ (ansible-vault, в .gitignore; образец — vault.yml.example)
│   └── host_vars/aikb.yml       # параметры хоста (локальный bootstrap)
├── playbooks/                   # самодостаточные плейбуки (single-file, без ролей)
│   ├── host-prepare.yml         # Сценарий B: docker + GPU на новом хосте
│   ├── deploy.yml               # Сценарий A: этапы (теги) dirs → repos → docker → cron (DATA_ROOT-layout)
│   ├── update.yml               # идемпотентный апдейт кода: preflight → pull → build → up → health → миграции
│   ├── ops.yml                  # read-only эксплуатация: logs/events/health/metrics/stats (без vault)
│   ├── backup.yml               # backup / verify / restore (restore — деструктивный, confirm-гейт)
│   ├── transfer-pack.yml        # Сценарий C (DEPRECATED): docker save на СТАРОМ хосте
│   ├── transfer-bulk.yml        # Сценарий C (DEPRECATED): перенос bulk-данных на НОВЫЙ хост
│   └── transfer-cutover.yml     # Сценарий C (DEPRECATED): stop old → финальный rsync → запуск new
├── templates/
│   ├── .env.j2                  # окружение контейнеров (секреты — из vault)
│   ├── docker.list.j2           # apt-репозиторий docker (host-prepare)
│   └── http-proxy.conf.j2       # systemd proxy drop-in для docker-сервиса (host-prepare)
├── requirements.txt             # pip-зависимости (ansible, ansible-lint)
└── requirements.yml             # galaxy-коллекции
```
