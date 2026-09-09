# Ansible: подготовка хоста, перенос и деплой mcp-knowledge

Три сценария использования:

1. **Подготовка нового хоста** — docker + NVIDIA driver + container toolkit (`playbooks/host-prepare.yml`).
2. **Перенос работающего стека old → new** — `docker save` образов + rsync данных, окно простоя только на cutover (`playbooks/transfer-*.yml`).
3. **Fresh-install на чистый хост** — каталоги, git-репо, compose up, cron (`playbooks/deploy.yml`).

Топология после переноса (миграция code-2026-09-08-001): mcp-server на host-сети, Qdrant `localhost:6333`, ollama-контейнер на порту `11435`, kb-console `8085`.

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
│   ├── host-prepare.yml         # M1: docker + GPU на новом хосте
│   ├── deploy.yml               # fresh-install: dirs → repos → docker → cron
│   ├── transfer-pack.yml        # M2: docker save на СТАРОМ хосте
│   ├── transfer-bulk.yml        # M3: перенос bulk-данных на НОВЫЙ хост
│   └── transfer-cutover.yml     # M4: stop old → финальный rsync → запуск new
├── roles/
│   ├── mcp_kb_host_prepare/     # драйвер NVIDIA, docker, proxy, verify
│   ├── mcp_kb_dirs/             # структура /opt/mcp-knowledge + bare-remote
│   ├── mcp_kb_repos/            # клонирование mcp-knowledge + knowledge
│   ├── mcp_kb_docker/           # .env из шаблона, compose up --wait, health-пробы
│   ├── mcp_kb_cron/             # backup + quality scan по расписанию
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

## Runbook переноса (M0–M6)

| # | Шаг | Команда |
|---|-----|---------|
| M0 | Заполнить `inventory/hosts.yml` (реальные IP old/new), создать vault: `make vault-create` по образцу `inventory/group_vars/all/vault.yml.example` | — |
| M1 | Подготовка нового хоста: `make run PLAYBOOK=playbooks/host-prepare.yml`. **Reboot**, если ставился NVIDIA-драйвер | `make run PLAYBOOK=playbooks/host-prepare.yml` |
| M2 | Упаковка стека на старом хосте (docker save образов, без простоя) | `make run PLAYBOOK=playbooks/transfer-pack.yml` |
| M3 | Bulk-перенос на новый хост (rsync данных + docker load) — без простоя | `make run PLAYBOOK=playbooks/transfer-bulk.yml` |
| M4 | Cutover-окно: stop old → финальный rsync → запуск new. **Простой = минуты** | `make run PLAYBOOK=playbooks/transfer-cutover.yml` |
| M5 | Проверки: `curl http://<new-ip>:8000/health` + search-smoke вручную (поиск через MCP/консоль) | вручную |
| M6 | Старый хост — точка отката, данные **НЕ удалять** | — |

## Fresh-install (deploy.yml)

Для развёртывания на новом хосте «с нуля» (без переноса данных), при готовом docker/GPU (после `host-prepare.yml`):

```bash
make run PLAYBOOK=playbooks/deploy.yml
```

Порядок ролей: `mcp_kb_dirs` → `mcp_kb_repos` → `mcp_kb_docker` (рендер `.env` из vault + `docker compose -f docker-compose.yml up -d --wait` + health-пробы 8000/6333/11435) → `mcp_kb_cron`.

## Firewall и порты

Готовой ufw/iptables-роли в репозитории НЕТ — управление файрволом остаётся за оператором хоста. Матрица портов стека (по `docker-compose.yml`):

| Порт | Сервис | Привязка | Наружу? | Рекомендация |
|---|---|---|---|---|
| 8000 | mcp-server | host-сеть → `0.0.0.0:8000` | **да, нужен** | MCP-клиенты подключаются извне; закрыть от интернета файрволом (allow-list IP) |
| 8085 | kb-console | host-сеть → `0.0.0.0:8085` | да, UI | при необходимости ограничить по IP (там MCP-ключ в UI, но без авторизации страниц) |
| 6333 | qdrant | `ports: "6333:6333"` → **`0.0.0.0`** | ⚠️ опубликован на всех интерфейсах | mcp-server ходит на `localhost:6333` → допустимо перевести в compose на `127.0.0.1:6333:6333` ЛИБО закрыть файрволом (`ufw deny 6333`). gRPC 6334 не публикуется |
| 11435 | ollama | `ports: "127.0.0.1:11435:11434"` | **нет** ✓ | уже loopback-only, менять не нужно |

Проверка на хосте после деплоя: `ss -ltnp | grep -E '8000|8085|6333|11435'` — 11435 должен слушать только на `127.0.0.1`.

## Откат

- Cutover обратим до демонтажа старого хоста: вернуть подключения/DNS на old и поднять стек там (`docker compose up -d`).
- Старый хост и staging-каталог (`/opt/mcp-kb-transfer` на новом) содержат полные копии образов и данных — не удалять до подтверждённой стабильной работы (M6).
- `.env` перегенерируется из шаблона + vault при повторном прогоне `mcp_kb_docker`.

## ⚠️ Предупреждения

- **`daemon.json` НЕ копировать** со старого хоста — прокси/драйв-специфику настраивает `mcp_kb_host_prepare` под новый хост.
- **`.env` содержит секреты** (MCP-ключи из vault) — передаётся только по SSH через ansible (mode `0600`), никогда не коммитится.
- **GPU ≥ 8 GB VRAM** для `qwen2.5:7b`; VL-модель (`qwen2.5vl:7b`) в 8 GB не влезает — в `transfer_images` не включена, на новый хост не тянуть.
- Свободное место на новом хосте: ≥ `min_free_gb` (30 GB) — проверяется ролью transfer на этапе bulk.
