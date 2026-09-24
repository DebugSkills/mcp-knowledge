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
| script:verify-deploy.sh | post-deploy гейт (make push / make verify-deploy, 2026-09-24) | sink: — | gap: ручной; FAIL → exit≠0 и явное сообщение оператору из make push; при автоматизации через cron_wrap.sh |
| script:reindex.sh | ручной запуск | sink: — | gap: ручной; запускать через cron_wrap.sh (RUNBOOK §Error-наблюдаемость) |
| script:offline-deploy.sh | air-gap-утилита, ручной запуск | sink: — | gap: ручной (bundle-контур вне прода) |
| script:seed_knowledge.py | разовый сид, ручной | sink: — | gap: разовый; через cron_wrap.sh при использовании |
| script:backfill_sequence_payload.py | миграция payload, ручной | sink: — | gap: ручной; через cron_wrap.sh (конвенция update-флоу) |
| script:errors_collect.py | сам коллектор (cron */5) | sink: — | gap: self — анти-рекурсия (P2-2); stderr → mcp-errors-collect.log с ротацией |
| script:errors_report.py | weekly-отчёт (ручной/make) | sink: — | gap: self — свои ошибки в reports/tg-errors.log + stderr, не в sink |
| script:errors_notify.py | общий TG-sender (016: weekly+алерты) | sink: — | gap: self — ошибки доставки → reports/tg-errors.log (маскировка <token>/<proxy>), не в sink |
| script:errors_alert.py | немедленные алерты new-P0/burst (016, cron */5) | sink: — | gap: self — анти-шторм/skip-решения → reports/tg-errors.log + stdout, не в sink |
| script:errors_notify_import.sh | sudo-helper notify.json 0600 из /etc/backup-status.env (016, руками оператора) | sink: — | gap: self — имя отсутствующей переменной в stderr (без значений, R5), не в sink |
| script:errors_prune.py | prune (ручной/make, dry-run-first) | sink: — | gap: self — вывод только в stdout/stderr |
| script:errors_guard.py | write-side гвард (cap/burst) + suppression-CLI | sink: — | gap: self — сам не источник; решения оператора → sink/suppression.json + audit.jsonl |

## Известные дыры (честный бэклог, канон §9)

| source_id | источник | sink: путь-механизм | статус |
|---|---|---|---|
| ollama:context_truncation | тихая обрезка контекста ollama | sink: — | gap: ловится только если ollama пишет распознаваемую строку в stdout; проверить на проде после 1 недели наблюдения |
| manual:prod-update | make prod-update / ansible-фейлы / offsite-rsync (j) | sink: — | gap: ручные операции; запускать через cron_wrap.sh (документировано в RUNBOOK) |
| marker:ERRORS_QUERY | mcp-server, 1 сайт (errors_query, audit обращений) | sink: docker logs mcp-knowledge-server → errors_collect.py (source=docker_logs) | covered |
| class:errors_query_audit | [ERRORS_QUERY]-строки тула | sink: classify_routine → expected=True (P3-baseline) | covered |

## Шторм-гард write-side (code-2026-09-23-008, спека §7 .boardData.md)

Гвард между сбором и записью в raw (`errors_collect.py:main` → `errors_guard.apply_write_guard`).
Порядок «сначала фикс источника, потом гвард» (§11.3 канона) — источник 401-класса
устранён трассой 007; гвард закрывает оставшиеся пробелы §13.3 (а/б/в).

**Лимиты (config `guard.*`, kill-switch `guard.enabled=false`):**

| Механизм | Лимит | Что делает |
|---|---|---|
| cap/sampling | 5 событий/60 с на сигнатуру (минутные ведра ts-based) | сверх — НЕ пишется в raw: `suppressed_pending` переносится в следующее разрешённое событие (`suppressed_count`+`sampled=true`), `suppressed_total/suppressed_daily` — в агрегат немедленно; `count_total/daily` = полный поток |
| burst-детектор | ≥50/цикл(5 мин) ИЛИ ×10 к среднему за 12 циклов (~1 ч) | маркер `[GUARD]` (P1, `priority_hint=burst`) + жертве `burst/burst_ts/burst_count_5m`; эскалация P2/P3→P1 sticky в окне 7d (декей), re-arm после спада с кулдауном 24 цикла; БЕЗ немедленных алертов (M7 weekly-only) |
| suppression-лист | ключ = ТОЛЬКО точная сигнатура | файл `sink/suppression.json` (НЕ рендерится ansible); `until` опционален |

**Иммунитет-матрица (никогда не глушится автоматикой):** 4xx-с-актором
(user-impact), traceback (не глушится и листом — абсолютно), host/health/
docker_events (свой дедуп), сам маркер `[GUARD]`. 4xx-с-актором глушится
ТОЛЬКО явной записью оператора с reason+audit. Диапазоны кодов/regex/
подстроки — запрещены архитектурно (ключ = exact match).

**Процедура suppression (оператор):** `make errors-guard-add SIG='<точная
сигнатура>' REASON='зачем' [UNTIL=YYYY-MM-DD]` → `suppression.json` + запись в
`sink/audit.jsonl` (кто/когда/зачем). Снять: `errors-guard-remove SIG=…`;
посмотреть: `errors-guard-list`. Верификация фикса при активном гварде
(§7.5): «источник устранён» = `suppressed_total>0` И `count_7d==0` И
`suppressed_7d==0` (глушилась и замолчала) — видно в weekly-строке suppressed.

**Видимость:** `errors_query` — поля `suppressed_total/suppressed_7d/burst/
burst_ts` + `suppressed_count/sampled` в примерах; weekly — suppressed-строка
(топ-3) и подсекция «Burst-инциденты за 7d»; view — колонка `sup=`.

## Поле endpoint (code-2026-09-23-009, спека §7 .boardData.md)

Диагностическое поле `endpoint` (route-шаблон request-target) в событии и
`endpoints={ep: count}` (cap 20 + `__others__`) в агрегате сигнатуры — БЕЗ
смены формулы сигнатуры E2: 401-сигнатуры разных эндпоинтов по-прежнему одна
строка, но с разбивкой «сколько на какой маршрут». Вычисляется только из
уже замаскированного сообщения (порядок mask→trunc→extract — секреты и query
в поле не попадают); id-сегменты (digits/UUID/hex≥16) → `<id>`; абсолютный
URL → только path (`http://host:8420/x?y=1` → `/x`). Пример: сигнатура
`docker_logs|401|INFO: … "GET <path> HTTP/<n>.<n>" <n> Unauthorized` →
`endpoints == {"/imports/active": 3, "/data-version": 2}`.

**Зона действия:** разбивка покрывает uvicorn access-логи mcp-server
(кавычечный формат `"METHOD target HTTP/x.x" NNN`); `[REQ]`-строки kb-console
(`app.py:33` — формат без статуса/кавычек) endpoint не получают — всегда
`endpoint=None`, их разбивка не требуется и не обещается (после фикса 007
устранены в источнике).

**Видимость:** `errors_query` — поле `endpoints` (top-20, count desc);
view — суффикс сигнатуры `ep=/a×3(+2)` (топ-3, `(+N)` = сумма вне топ-3);
weekly P3-baseline — суффикс ` · ep: /a×3, /b×2` (топ-3, только если непуст).

## Parity замороженных артефактов (code-2026-09-23-010, спека §7 .boardData.md)

Часть конвенций Error→Rule физически живёт в ДВУХ копиях: collector
(`scripts/errors_collect.py`, сбор) ↔ tool (`tools/errors_query.py`, агентский
API). Дубликат обязателен (scripts/ не копируется в Docker-образ), поэтому
расхождение копий ловится parity-тестами на хосте (G4 preflight):
`tests/test_masking_parity.py` (маскирование, 006) и
`tests/test_frozen_parity.py` (010; блоки A–F, 22 теста, RED×7-мутации).
Дрейф любой из сторон = красный тест — «тихая деградация» диагностики
(агент и сбор трактуют P0–P3/окна по-разному) невозможна.

**Шесть двухкопийных артефактов (предмет parity):**

| Артефакт | Collector | Tool | Механизм проверки |
|---|---|---|---|
| Словарь P0–P3 | литералы лестницы `update_aggregates` (неявные) | `PRIO_RANK` | AST-литералы == ключам (A) |
| Формат сигнатуры `source\|key\|normalized` | `make_signature` | разбор в `_filter_aggregates` | round-trip через обе функции (B) |
| Trend up/down/flat | рост-условие лестницы | `_trend` | матрица + live-прогон (C) |
| 7d-окно (включительная граница) | week_ago-фильтр `update_aggregates` | `_suppressed_7d` | синтетика + live-прогон (D) |
| endpoints cap 20 | `ENDPOINTS_KEEP` | срез в `_render_aggregate` | рендер + scoped-регекс (E) |
| Маркер `ERRORS_QUERY` | classify_routine → expected (P3-baseline) | аудит-фильтры (`include_audit=false`) | parse + фильтр (B5) |

**Граница скоупа:** JSON-контракты полей агрегатов/событий
(`sampled`, `suppressed_count`, `burst_ts`, `endpoints`) — двухкопийные имена,
НО вне parity-трассы: покрыты mcp_server-юнитами и shape-тестами 009.
`P0_HINTS`, `BASELINE_4XX`, `deep_normalize` — одиночные копии (живут только
в collector, заморожены `test_errors_lib.py`), в parity не входят.

## Quality-scan: статусы, отмена и auto-clear (code-2026-09-24-011, спека §7 .boardData.md)

Статус `cancelled` — самостоятельное терминальное состояние скана качества,
НЕ ошибка: `cancelled ≠ error` в `scan_state.json`, progress-трекере
(`progress.cancel()` — force-persist, log-level warning) и kb-console
(терминальное множество `{done, error, cancelled}`). Отмена скана
пользователем (`cancel_quality_scan`) срабатывает ≤10 с (замер 0.88 с:
cancel-чек между батчами scoring / внутри чанков 5.5 / каждые 500 итераций
dup-пары) и возвращает частичные метрики — они валидны для отчёта.

Зависший после рестарта сервера `running`-скан при старте помечается
`cancelled` («scan interrupted by server restart») с сохранением
авто-ресюма 13.27: refresh-сканы < 2 ч подхватываются автоматически.

Шаг 5.5 (auto-clear OK-записей) — снапшот O(N+M): один read стора
(`list_open_issue_ids_grouped`), сверка в памяти, один bulk-RMW
(`bulk_update_status`, чанки `AUTO_CLEAR_CHUNK_SIZE=2000`). Инцидент стенда
(40–60 мин CPU: per-kid проходы стора 0.28 с × 8406) невозможен по
конструкции: пер-запись RMW больше нет нигде — шаги 5/5.4 пишут issues
батчем `ISSUES_BATCH_SIZE=500`, dup-пары флашатся per-domain-бакетом одним
`create_issues_batch` (+ флаш перед выходом при отмене). Лог 5.5 агрегирован:
`auto-clear: n/m entries, k collected` — одна строка на шаг вместо 8406.

**Видимость:** `scan_state.json` → `status: cancelled` + `reason`;
`progress.get()` / kb-console «Качество» → бейдж Cancelled; AC-замеры —
`TestAcceptanceMeasures` (AC1 <5 с на 8406×32930: 0.95 с; AC3 ≤10 с).

---
Обслуживание: новый источник → `make test-errors` КРАСНЫЙ → добавить строку
(механизм сбора или честный gap с причиной) → ЗЕЛЁНЫЙ. Live-проверка на проде:
`make prod-errors-sources` (gap>0 → non-zero exit).
