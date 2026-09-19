# Ansible: подготовка хоста, деплой и перенос mcp-knowledge

Ansible-обвязка стека mcp-knowledge: подготовка хоста (docker + NVIDIA GPU), fresh-install деплой и перенос стека между хостами.

> Операционный runbook (пошаговые команды, переменные, firewall/порты, обновление/откат) ведётся **приватно** и в публичный репозиторий не входит.

## Три сценария

- **Сценарий A — fresh-install из git-клона** — деплой на хост с интернетом, M0–M9 (`playbooks/deploy.yml`).
- **Сценарий B — подготовка хоста** — docker + драйвер NVIDIA + container toolkit (`playbooks/host-prepare.yml`).
- **Сценарий C — перенос old→new** — перенос стека без интернета на new, pack/bulk/stop/cutover (`playbooks/transfer-*.yml`).

Make-команды (`deps/test/lint/check/run/vault-*`) — см. [`Makefile`](Makefile).

## Структура каталога `ansible/`

```
ansible/
├── ansible.cfg                  # inventory, roles_path, forks
├── Makefile                     # deps/test/lint/check/run/run-role/vault-*
├── inventory/
│   ├── hosts.yml                # группы mcp_kb_old / mcp_kb_new (целевой хост)
│   ├── group_vars/
│   │   ├── all.yml              # несекретные переменные (deploy_root, compose_file, transfer_images)
│   │   └── all/vault.yml        # СЕКРЕТЫ (ansible-vault, в .gitignore; образец — vault.yml.example)
│   └── host_vars/aikb.yml       # параметры хоста (локальный bootstrap) + переменные ролей
├── playbooks/
│   ├── host-prepare.yml         # Сценарий B: docker + GPU на новом хосте
│   ├── deploy.yml               # Сценарий A: dirs → repos → docker → cron
│   ├── transfer-pack.yml        # Сценарий C: docker save на СТАРОМ хосте
│   ├── transfer-bulk.yml        # Сценарий C: перенос bulk-данных на НОВЫЙ хост
│   └── transfer-cutover.yml     # Сценарий C: stop old → финальный rsync → запуск new
├── roles/
│   ├── mcp_kb_host_prepare/     # драйвер NVIDIA, docker, proxy, verify
│   ├── mcp_kb_dirs/             # каталоги deploy_root ВНЕ клона (data/, models_cache) + bare-remote
│   ├── mcp_kb_repos/            # клонирование: mcp-knowledge (HTTPS) + knowledge (SSH, deploy-key)
│   ├── mcp_kb_docker/           # .env из шаблона, compose build, up --wait, health-пробы
│   ├── mcp_kb_cron/             # backup + quality scan из клона + crontab-env MCP_WRITE_KEY
│   └── mcp_kb_transfer/         # stages: pack | bulk | stop | cutover
├── templates/.env.j2            # окружение контейнеров (секреты — из vault)
├── requirements.txt             # pip-зависимости (ansible, ansible-lint)
└── requirements.yml             # galaxy-коллекции
```

## README ролей

- [`roles/mcp_kb_host_prepare/README.md`](roles/mcp_kb_host_prepare/README.md)
- [`roles/mcp_kb_transfer/README.md`](roles/mcp_kb_transfer/README.md)
