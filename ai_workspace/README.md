# ai_workspace — control-plane AI-верстака (Ф3.1–Ф3.2)

Пакет серверной логики AI-верстака сообщества (plan arch-2026-10-05-ai-workspace):
Scheduler (Redis+Lua, WFQ+aging) + Mode engine. **Ф3.1** — каркас + durable
job-store; **Ф3.2** — планировщик очередей вызовов (`scheduler/`).

## Что здесь
- `orchestrator/job.py` — job-store `ws:job:{id}` (HASH): `JobState` (8 статусов),
  `ALLOWED_TRANSITIONS` + `validate_transition` (чистая функция), `JobStore`
  (`create`/`get`/`transition`), CAS по `version` (hget+hset в ОДНОМ EVALSHA),
  epoch-fencing (I4: `epoch < current` → `StaleEpoch`, без записи),
  `compute_effect_id = H(job, node, effect)` — без attempt (идемпотентность I4).
- `redis_client.py` — тонкая фабрика ws-redis-клиента (`WS_REDIS_URL`).
- `scheduler/policy.py` — чистая политика WFQ 3×3 + aging (offline-тестируема):
  `BASE`/`MULT`/`T_STARVE`, `weight`, `select_order`, `pick_best`,
  `virtual_finish`.
- `scheduler/queue.lua` — **SSOT атомарных Lua-скриптов** (секции `-- @script`):
  `enqueue` (vft = max(V,last)+cost/w; ZADD в ДВА индекса), `dequeue` (правило
  pick_best + ДВУХ-ИНДЕКСНОЕ снятие q+starve одной Lua), `complete`
  (ws:vt += cost_actual/w).
- `scheduler/queue.py` — обёртка `Queue`: `enqueue`/`dequeue`/`complete`/
  `size`, `make_call(job, step, attempt)`; `now` инъектируется (clock callable)
  — Lua время сам не читает, детерминированные тесты.

## Scheduler (Ф3.2)
- **9 логических очередей** = 3 приоритета аккаунта (high/med/low) × 3 класса
  вызова (interactive/batch/background); живут метаданными в ДВУХ ZSET полки:
  `ws:q:{shelf}` (score = vft) и `ws:starve:{shelf}` (score = starve-дедлайн).
- **Вес** `w(p,c) = base[c]*mult[p]`: base={interactive:100, batch:10,
  background:1}, mult={high:4, med:2, low:1}; `vft = max(V,last)+cost_est/w`
  (вставка без гонки: vt и last-VFT — отдельные ключи, обновление одной Lua).
- **Aging-пол — абсолютное право** (инвариант I2): starve_deadline = now +
  T_starve[class] (60s / 30m / 2h); просроченный побеждает любого. `dequeue`
  берёт просроченных ПРЯМО из starve-индекса (FIFO по дедлайну), минуя окно
  WFQ `ZRANGE 0 31` — иначе background с большим vft был бы невидим для пола;
  затем добор до limit по argmin vft.
- **Снятие из двух индексов — одной Lua** (`ZREM q` + `ZREM starve` атомарно):
  иначе гонка «half-removed». `complete` двигает vt отдельным скриптом —
  между снятием и завершением живёт воркер.
- Float в ARGV/score — строкой `%.17g` (round-trip без усечения Redis).

## Ключи `ws:*` (спека Scheduler §2)
| Ключ | Тип | Владелец |
|---|---|---|
| `ws:job:{id}` | HASH | job-store (Ф3.1) |
| `ws:q:{shelf}` / `ws:starve:{shelf}` | ZSET | scheduler (Ф3.2) |
| `ws:vt:{shelf}` / `ws:vftlast:{shelf}:{p}:{c}` | float | scheduler (Ф3.2) |
| `ws:slots:*`, `ws:lease:*`, `ws:gate:*`, `ws:pos:*` | — | Ф3.3+ |

## Тесты
- unit (без Redis): `make ws-test` (все зелёные; integration авто-skip).
- integration (живой Redis): `make ws-up-test` → `make ws-test-integration`
  (Redis `127.0.0.1:6390` — test-only overlay `compose.workspace.test.yml`,
  loopback; авто-skip, если `WS_REDIS_URL` не задан или PING не прошёл).
- Ф3.2: parity-тест — порядок dequeue (Lua) == эталон `policy.pick_best`
  (N=60 случайных вызовов, seed), обе ветки правила (WFQ + просроченные).

## DBD-Scan-решение: redis_client.py
Паттерн клиента **ПОВТОРЕН** из `kb-console/src/kb_console/core/redis_client.py`
(Ф2 #5b: ленивый import, fail-closed env, `decode_responses=True`), НЕ импортирован:
направление зависимости — kb-console (клиентское приложение) зависит от
ai_workspace (control-plane), не наоборот. Осознанный ограниченный дубль (~50 строк);
консолидация — kb-console переключить на `ai_workspace.redis_client` в Ф3.x.

## НЕ входит (Ф3.3+)
Slot-семафор `ws:slots:*` (K_local/M_ext) + `XADD` event-commit +
`PUBLISH ws:kick` (Ф3.3); requeue/lease/sweeper с СОХРАНЁМ starve-дедлайна
(иначе preempt-livelock, Ф3.4); Mode engine, admission (Ф3.5+).
