# Ansible: подготовка хоста, деплой и перенос mcp-knowledge

Ansible-обвязка стека mcp-knowledge: подготовка хоста (docker + NVIDIA GPU), fresh-install деплой и перенос стека между хостами.

> Операционный runbook (пошаговые команды, переменные, firewall/порты, обновление/откат) ведётся **приватно** и в публичный репозиторий не входит.

## Три сценария

- **Сценарий A — fresh-install из git-клона** — деплой на хост с интернетом, M0–M9 (`playbooks/deploy.yml`).
- **Сценарий B — подготовка хоста** — docker + драйвер NVIDIA + container toolkit (`playbooks/host-prepare.yml`).
- **Сценарий C — перенос old→new** — перенос стека без интернета на new, pack/bulk/stop/cutover (`playbooks/transfer-*.yml`).

Make-команды (`deps/test/lint/check/run/run-tag/vault-*`) — см. [`Makefile`](Makefile).

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
│   ├── deploy.yml               # Сценарий A: этапы (теги) dirs → repos → docker → cron
│   ├── transfer-pack.yml        # Сценарий C: docker save на СТАРОМ хосте
│   ├── transfer-bulk.yml        # Сценарий C: перенос bulk-данных на НОВЫЙ хост
│   └── transfer-cutover.yml     # Сценарий C: stop old → финальный rsync → запуск new
├── templates/
│   ├── .env.j2                  # окружение контейнеров (секреты — из vault)
│   ├── docker.list.j2           # apt-репозиторий docker (host-prepare)
│   └── http-proxy.conf.j2       # systemd proxy drop-in для docker-сервиса (host-prepare)
├── requirements.txt             # pip-зависимости (ansible, ansible-lint)
└── requirements.yml             # galaxy-коллекции
```
