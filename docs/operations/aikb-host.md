# aikb — узел mcp-knowledge (air-gap прод)

Карта узла `aikb`: слои стека, сервисы, порты, модели, эксплуатация. Дополняет
`airgap-first-install.md` (процедура обновления) и `skill('mcp-knowledge-prod-ops')`.

## Хост
| | |
|---|---|
| Роль | air-gap прод-узел (сообщество) |
| ОС / ядро | Linux 6.1.0-53-amd64 (Debian 12) |
| CPU / RAM | 24 vCPU / 123.5 GiB |
| Диск | /dev/md0 3.6T (исп. ~5%) |
| GPU | 2×NVIDIA RTX 5080 (16 GB each, драйвер 615.71.09) |
| Docker | 29.8.1 (API 1.56) |
| Интернет | **есть** (нужен для домывки моделей/образов; air-gap — политика, не физика) |
| Доступ | только вложенный jump: `ssh <jump-user>@<jump-host> "ssh aikb …"` (у jump-пользователя нет docker; root-действия — оператор) |

## Слои стека (всё через make-таргеты)
| Слой | Сервисы | Таргет | Порты |
|------|---------|--------|-------|
| **L1 knowledge** | mcp-server, kb-console, kb-console-tls, qdrant, ollama(контейнер), kb-converter | `make deploy` | 8085↔**8443** (TLS), MCP↔**8444** (TLS) |
| **L2 gateway** | litellm | `make gateway-up` | internal :4000 |
| **L3 ws-infra** | ws-redis | `make ws-up` | internal |
| **L4 ws-консоль** | workspace (чат) + ws-console-tls | `make ws-console-up` | loopback **8095**↔**8445** (TLS) |
| **WS-слой** (L2+L3+L4) | — | `make ws-stack-up` | — |
| **Всё** (L1+WS) | — | `make stack-up` | — |

**Air-gap:** `make ws-stack-up AIRGAP=1` → `-f compose.airgap.yml` + `--no-build` (I12). В `ansible/playbooks/update.yml` — авто после L1 при `ws_layer_enabled: true`.

## Сервисы (10) · `compose`-файлы
`docker-compose.prod.yml` (L1) · `compose.gateway.yml` (litellm) · `compose.workspace.yml` (ws-redis, workspace) · `compose.airgap.yml` (overlay, `--no-build`).
Контейнеры: `mcp-knowledge-server`, `kb-console(-tls)`, `mcp-knowledge-ollama`, `mcp-qdrant-dev`, `mcp-knowledge-converter`, `mcp-knowledge-litellm`, `mcp-knowledge-ws-redis`, `mcp-knowledge-workspace`, `mcp-knowledge-ws-tls`.

## ⚠️ ДВА ollama (важно)
| | Стор | Версия | Модели |
|---|---|---|---|
| **host** (systemd `ollama.service`) | `*:11434` | 0.33.3 | мощные: `qwen3:30b-a3b-instruct-2507-q4_K_M`, `qwen3:32b-q4_K_M`, `qwen3-32b-ctx64`, `t-pro-it-2.1-q5`, `deepseek-r1:32b`, `r1-32b-ctx32`, `qwen3:4b` |
| **контейнер** `mcp-knowledge-ollama` | internal :11434 (host loopback :11435) | **0.20.2 (pinned)** | свой стор `./data/ollama/models`; эмбеддинги (`mxbai-embed-large`); мощных нет |

- Контейнер pinned **0.20.2** — индекс Qdrant построен эмбеддером этой версии (host 0.33.3 даёт другие векторы). **Не апгрейдить** без переиндексации.
- Контура верстака `litellm local` направлен на **host-ollama** (`host.docker.internal:11434`, `extra_hosts: host-gateway`) — там мощные модели.
- `ollama list` в шелле хоста показывает **host**-набор; `docker exec …-ollama ollama list` — контейнерный.

## Модели верстака (env-настраиваемо)
- `WS_LOCAL_MODEL` — модель контура `local` (default `qwen3:30b-a3b-instruct-2507-q4_K_M`).
- `WS_LOCAL_OLLAMA_BASE` — адрес ollama контура local (prod: `host.docker.internal:11434`; dev: `mcp-knowledge-ollama:11434`).
- Оба — **не секреты**; источник — `stack.settings`/`.env.j2` (Ф5: единый стек-конфиг).

## Env и секреты (prod)
`.env` на узле **генерируется ansible** из `ansible/templates/.env.j2` + **vault** (`deploy.yml --tags env`). Ручная правка `.env` эфемерна.
- **Секреты → vault:** `vault_litellm_master_key`, `vault_ws_mcp_key`, `vault_ws_mcp_import_key` (+ существующие `vault_console_password`, `vault_mcp_*`).
- **Не секреты → vars/шаблон:** `ws_local_model`, `ws_local_ollama_base`, порты/подсети.
- Ключи верстака чеканятся **на узле**: `docker exec mcp-knowledge-server python -m mcp_server.cli token create --level read|import --zone both`.

## Firewall (UFW, host-prepare)
LAN-порты: `["8443","8444","8445"]`. `11434`/`11435` — **internal**; убедиться, что host-ollama `*:11434` **не** доступен из LAN.

## Эксплуатация
- Диагностика: `sudo -n make -C /opt/mcp-knowledge/mcp-knowledge prod-diag` → лог `/var/log/mcp-knowledge/diag/prod-diag-latest.log` (0644). Ожидаемо **21 passed / 0 failed / 3 warn**.
- **Единый конфиг стека (Ф5):** `make stack-config` (эффективный конфиг + источник каждого ключа) / `make stack-config-set ARGS="ws.local_model=… --apply"` (правка `stack.settings.yaml`).
- ⚠️ **Ручные make-таргеты WS-слоя на узле — только с env (R11):** `WS_LOCAL_OLLAMA_BASE=host.docker.internal:11434 make gateway-render` — иначе file-слой `stack.settings.yaml` отдаст dev-алиас `mcp-knowledge-ollama:11434`. Штатный путь — ansible-apply (`update.yml` R8b несёт env).

- WARN-и по ошибке/дизайну: `D10` (LiteLLM :4000 не публикуется — internal), `D13` (`:8700` calib-api не слушает — fail-soft), `D16` (zone-gate — поведенческая проба вне HTTP).
- Обновление: `airgap-first-install.md`; секреты/несекретное — см. «Env и секреты».
- ⚠️ **litellm:** читает `litellm.config.yaml` ТОЛЬКО на старте; при смене конфига `gateway-up` делает `--force-recreate` (по sha256 в `.gateway-config.sha`).
- ⚠️ **litellm fail-closed:** без `LITELLM_MASTER_KEY` шлюз НЕ стартует (краш-луп).

---
**v1.0** | 2026-10-10 | Карта узла aikb: слои/таргеты, 10 сервисов, два-ollama, env/vault, модели, firewall, prod-diag. | trace_id: arch-2026-10-10-ws-airgap-layers
