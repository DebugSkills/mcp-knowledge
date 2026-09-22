# Реестр охвата источников ошибок (E5) — code-2026-09-22-003

> Канон: `.knowledge/docs/observability/self-improvement-loop.md` §3 E5, §7.1.
> Правило: **каждый источник ошибок обязан иметь путь в sink** — новый маркер /
> сервис / cron / скрипт без строки ниже = КРАСНЫЙ `make test-errors`
> (`tests/test_error_sources.py`). Gap-статус допустим (реестр = бэклог дыр
> охвата), но требует явной причины.

Формат строки (фиксирован, парсится тестом):

```
| <source_id> | <источник> | sink: <путь-механизм> | covered |
| <source_id> | <источник> | sink: <путь-механизм> | gap: <причина> |
```

Сборщик: `scripts/errors_collect.py` (host-cron */5) → sink `$DATA_ROOT/logs/errors/`.
Прод-деплой использует `docker-compose.yml` (group_vars `compose_file`) — имена
контейнеров совпадают с dev; `docker-compose.prod.yml` — только air-gap-бандл
(`scripts/offline-deploy.sh`), см. gap-строки `service:*-prod` (P2-7).

## Маркеры событий (mcp_server/src + kb-console/src)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| marker:IMPORT | mcp-server, 24 сайтов | sink: docker logs mcp-knowledge-server → errors_collect.py (source=docker_logs) | covered |
| marker:RECONCILE | mcp-server, 19 сайтов | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:START | mcp-server, 10 сайтов | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:REINDEX | mcp-server, 9 сайтов | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:DELETE | mcp-server, 5 сайтов | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:REVIEW | mcp-server, 4 сайта | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:REPLACE | mcp-server, 4 сайта | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:MCP | mcp-server, 4 сайта (mcp_handler) | sink: docker logs mcp-knowledge-server → errors_collect.py + hang-детектор | covered |
| marker:CONVERT | mcp-server, 4 сайта | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:LIFECYCLE | mcp-server, 3 сайта | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:ANALYZE | mcp-server, 3 сайта | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:UPLOAD | mcp-server, 1 сайт | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:EXTRACT_PDF | mcp-server, 1 сайт | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:EMBED | mcp-server, 1 сайт | sink: docker logs mcp-knowledge-server → errors_collect.py | covered |
| marker:REQ | kb-console app.py (каждый запрос) | sink: docker logs kb-console → errors_collect.py | covered |

## Классы строк (не маркеры)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| class:levels | WARN/ERROR/CRITICAL в stdout 4 контейнеров | sink: errors_collect.py parse_docker_log_events (level-фильтр) | covered |
| class:traceback | traceback-блоки (Traceback… → строка исключения) | sink: errors_collect.py (hint=traceback → P0) | covered |
| class:access_4xx_5xx | uvicorn-access 4xx/5xx (2xx/3xx не пишутся — поток проб) | sink: errors_collect.py ACCESS_RE | covered |
| class:mcp_hang | `[MCP] tool=X start` без `ok` за окно 30 мин (инцидент 2026-08-06) | sink: errors_collect.py detect_hangs (hint=hang → P0) | covered |
| class:auth | [AUTH]-класс (захватывается, baseline P3 — пересмотр Q3 trace 002) | sink: errors_collect.py capture-first; отображение P3 | covered |
| class:cron_exit | `[CRON] job=<name> exit=<N> dur=<s>` от cron_wrap.sh | sink: errors_collect.py collect_cron_logs (exit≠0 → P0) | covered |
| class:health_probe | health-пробы коллектора (4×/5 мин, P3-baseline) | sink: errors_collect.py collect_health | covered |

## Контейнеры (docker logs + docker events)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| service:qdrant | docker-compose.yml `qdrant` (container_name mcp-qdrant-dev) | sink: errors_collect.py docker logs + docker events | covered |
| service:ollama | docker-compose.yml `ollama` (mcp-knowledge-ollama) | sink: errors_collect.py docker logs + docker events | covered |
| service:mcp-server | docker-compose.yml `mcp-server` (mcp-knowledge-server) | sink: errors_collect.py docker logs + docker events | covered |
| service:kb-console | docker-compose.yml `kb-console` (kb-console) | sink: errors_collect.py docker logs + docker events | covered |
| service:qdrant-prod | docker-compose.prod.yml (mcp-qdrant-prod) | sink: — (air-gap-бандл offline-deploy.sh; вне основного деплоя) | gap: air-gap-контур; при переходе на offline-deploy добавить имя в config.json контейнеров (P2-7) |
| service:ollama-prod | docker-compose.prod.yml (mcp-knowledge-ollama-prod) | sink: — (air-gap-бандл) | gap: air-gap-контур; аналогично qdrant-prod (P2-7) |
| source:docker_events | docker events die/oom/restart/health_status | sink: errors_collect.py collect_docker_events | covered |

## Host-пороги

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| host:df | df(data_root) ≥85% (≥95% → P0 disk_critical) | sink: errors_collect.py collect_host (дедуп пересечения + снапшот ≤1/ч) | covered |
| host:ram | RAM avail <10% | sink: errors_collect.py collect_host | covered |
| host:load15 | load15 > 2×nproc | sink: errors_collect.py collect_host | covered |
| host:vram | nvidia-smi VRAM ≥95% | sink: errors_collect.py collect_host (nvidia-smi отсутствует → skip, P2-5) | covered |
| host:xid | NVIDIA Xid-ошибки драйвера (dmesg -T \| grep -i xid) | sink: — | gap: v1 не реализован (нужен root dmesg); покрыт частично VRAM-порогом; кандидат в бэклог |

## Health-эндпоинты

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| health:mcp | http://localhost:8000/health (reconcile.state=error, embedding.loaded=false) | sink: errors_collect.py collect_health (изменение + снапшот 1/ч) | covered |
| health:qdrant | http://localhost:6333/healthz | sink: errors_collect.py collect_health | covered |
| health:ollama | http://localhost:11435/api/tags | sink: errors_collect.py collect_health | covered |
| health:console | http://localhost:8085/ (200/401 = жив) | sink: errors_collect.py collect_health | covered |

## Cron-задачи (ansible.builtin.cron: deploy.yml + errors.yml)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| cron:MCP Knowledge — daily backup | deploy.yml cron 3:00 → /var/log/mcp-backup.log | sink: errors_collect.py collect_cron_logs + cron_wrap.sh [CRON]-строка | covered |
| cron:MCP Knowledge — weekly quality scan | deploy.yml cron Пн 4:00 → /var/log/mcp-quality.log | sink: errors_collect.py collect_cron_logs + cron_wrap.sh | covered |
| cron:MCP Knowledge — errors collector | errors.yml cron */5 → /var/log/mcp-errors-collect.log | sink: собственный stderr → ротация в errors_collect.py (последние 2000 строк при >10 МБ, P2-6) | gap: self — анти-рекурсия: собственный лог НЕ читается коллектором (P2-2) |

## Host-скрипты (scripts/*)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| script:backup.sh | cron daily 3:00 (deploy.yml) | sink: cron_log /var/log/mcp-backup.log + WARN/ERROR-строки + [CRON] wrapper | covered |
| script:quality_scan.sh | cron weekly Пн 4:00 (deploy.yml) | sink: cron_log /var/log/mcp-quality.log + [CRON] wrapper | covered |
| script:cron_wrap.sh | обёртка ручных/cron запусков | sink: [CRON]-строки → collect_cron_logs | covered |
| script:preflight.sh | pre-push гейт (004); запускать через cron_wrap.sh при желании | sink: — | gap: self — сам гейт НЕ собирает ошибки (P2-2); unit-гейты G4/G5 покрывают E5 |
| script:reindex.sh | ручной запуск | sink: — | gap: ручной; запускать через cron_wrap.sh (RUNBOOK §Error-наблюдаемость) |
| script:offline-deploy.sh | air-gap-утилита, ручной запуск | sink: — | gap: ручной (bundle-контур вне прода) |
| script:seed_knowledge.py | разовый сид, ручной | sink: — | gap: разовый; через cron_wrap.sh при использовании |
| script:backfill_sequence_payload.py | миграция payload, ручной | sink: — | gap: ручной; через cron_wrap.sh (конвенция update-флоу) |
| script:errors_collect.py | сам коллектор (cron */5) | sink: — | gap: self — анти-рекурсия (P2-2); stderr → mcp-errors-collect.log с ротацией |
| script:errors_report.py | weekly-отчёт (ручной/make) | sink: — | gap: self — свои ошибки в reports/tg-errors.log + stderr, не в sink |
| script:errors_prune.py | prune (ручной/make, dry-run-first) | sink: — | gap: self — вывод только в stdout/stderr |

## Известные дыры (честный бэклог, канон §9)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| ollama:context_truncation | тихая обрезка контекста ollama | sink: — | gap: ловится только если ollama пишет распознаваемую строку в stdout; проверить на проде после 1 недели наблюдения |
| manual:prod-update | make prod-update / ansible-фейлы / offsite-rsync (j) | sink: — | gap: ручные операции; запускать через cron_wrap.sh (документировано в RUNBOOK) |
| marker:ERRORS_QUERY | mcp-server, 1 сайт (errors_query, audit обращений) | sink: docker logs mcp-knowledge-server → errors_collect.py (source=docker_logs) | covered |
| class:errors_query_audit | [ERRORS_QUERY]-строки тула | sink: classify_routine → expected=True (P3-baseline) | covered |

---
Обслуживание: новый источник → `make test-errors` КРАСНЫЙ → добавить строку
(механизм сбора или честный gap с причиной) → ЗЕЛЁНЫЙ. Live-проверка на проде:
`make prod-errors-sources` (gap>0 → non-zero exit).
