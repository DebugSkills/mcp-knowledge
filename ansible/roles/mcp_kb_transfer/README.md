# mcp_kb_transfer — перенос стека mcp-knowledge между хостами

Роль оркестрирует перенос docker-стека mcp-knowledge (mcp-server, kb-console, qdrant, ollama)
со старого хоста (`mcp_kb_old`) на новый (`mcp_kb_new`) через 4 последовательных этапа.

## Этапы (stage)

| Stage | Хост | Что делает | Idempotent |
|-------|------|-----------|------------|
| `pack` | **old** | `docker save` образов → `images.tar` + sha256 + манифесты (docker images, ollama list) в staging | да (`creates`) |
| `bulk` | **new** | df-проверка места → перенос staging + `docker load` (маркер `.images-loaded`) → bulk-rsync кода/данных **без** `--delete` | да, повторяемый |
| `stop` | **old** | `docker compose stop` — замораживает данные для финального rsync | частично (повторный stop — no-op) |
| `cutover` | **new** | финальный rsync **с** `--delete` → `docker compose up -d --wait` → health-гейты (mcp-server, qdrant, ollama-модели, kb-console, GPU) | запускать 1 раз |

## Порядок запуска

```text
pack (old)  →  bulk (new)  →  stop (old)  →  cutover (new)
```

Пример playbook (по одному на этап; `stage` задаётся через vars):

```yaml
# transfer-pack.yml
- name: pack на старом хосте
  hosts: mcp_kb_old
  become: true
  roles:
    - role: mcp_kb_transfer
      vars:
        mcp_kb_transfer__stage: pack
```

Аналогично: `bulk`/`cutover` → `hosts: mcp_kb_new`, `stop` → `hosts: mcp_kb_old`.

Запуск (через Makefile-обвязку):

```bash
make -C ansible test PLAYBOOK=playbooks/transfer-pack.yml   # syntax-check
make -C ansible check PLAYBOOK=playbooks/transfer-bulk.yml  # dry-run
make -C ansible run PLAYBOOK=playbooks/transfer-bulk.yml    # ТОЛЬКО оператор
```

## Переменные

| Переменная | Default | Описание |
|-----------|---------|----------|
| `mcp_kb_transfer__stage` | `pack` | Этап: `pack` \| `bulk` \| `stop` \| `cutover` |
| `mcp_kb_transfer__old_root` | `/kvm/mcp-knowledge` | Корень стека на старом хосте |
| `mcp_kb_transfer__new_root` | `/opt/mcp-knowledge` | Корень стека на новом хосте |
| `mcp_kb_transfer__staging` | `/opt/mcp-kb-transfer` | staging: images.tar, манифесты (на обоих хостах) |
| `mcp_kb_transfer__repo_name` | `mcp-knowledge` | Имя каталога репозитория внутри root |
| `mcp_kb_transfer__compose_file` | `docker-compose.yml` | Compose-файл (относительно root/repo) |
| `mcp_kb_transfer__images` | 4 образа | Список образов для `docker save`/`docker load` |
| `mcp_kb_transfer__drop_vl_model` | `false` | Зарезервировано (VL-модель не влезает в 8GB VRAM); задачами пока не используется |
| `mcp_kb_transfer__min_free_gb` | `30` | Минимум свободного места на `/opt` (этап bulk) |
| `mcp_kb_transfer__excludes` | см. defaults | Exclude-паттерны rsync (.venv, .trash, node_modules, …) |

## Требования

- inventory: группы `mcp_kb_old`, `mcp_kb_new` (уже в `inventory/hosts.yml`).
- `rsync` на **обоих** хостах; SSH-доступ old → new.
  Паттерн переноса: задача в play на `mcp_kb_new`, rsync выполняется на old
  (`delegate_to: groups['mcp_kb_old'][0]`, `mode: push`): src на old, dest — new.
- collection `ansible.posix` (`requirements.yml`), docker + compose plugin на обоих хостах.
- Health-порты на new: `8000` mcp-server, `6333` qdrant, `11435` ollama, `8085` kb-console.

## Предупреждения

- ⚠️ **`/etc/docker/daemon.json` НЕ копировать** — это host-specific конфиг Docker
  (data-root, GPU runtime, registry-зеркала). На новом хосте настраивается отдельно
  (роль `mcp_kb_host_prepare`), rsync репозитория его и не трогает.
- ⚠️ **`.env` содержит секреты** — переносится только rsync over SSH (в bulk/cutover).
  Роль проверяет наличие `.env` после bulk и предупреждает, если его нет.
- `images.tar` не перегенерируется при изменении `mcp_kb_transfer__images`
  (guard `creates`). Для re-pack очистите staging на old.
- bulk — повторяемый (без `--delete`): стек на old работает во время переноса.
  cutover — только после `stop` на old: финальный rsync с `--delete`.
- После cutover выполните search-smoke вручную (M5): поиск «MkDocs настройка»
  должен дать топ `engineering/mkdocs` со score ≈0.88.

---
**v1.0** | 2026-09-09 | Начальная версия: 4 stage (pack/bulk/stop/cutover)
