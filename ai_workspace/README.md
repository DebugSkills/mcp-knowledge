# ai_workspace — control-plane AI-верстака (Ф3.1–Ф3.4)

Пакет серверной логики AI-верстака сообщества (plan arch-2026-10-05-ai-workspace):
Scheduler (Redis+Lua, WFQ+aging) + Mode engine. **Ф3.1** — каркас + durable
job-store; **Ф3.2** — планировщик очередей вызовов (`scheduler/`); **Ф3.3** —
слот-семафор + события; **Ф3.4** — requeue/preempt/свипер.

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
  `enqueue` (vft = max(V,last)+cost/w; ZADD в ДВА индекса + per-call HASH),
  `dequeue` (правило pick_best + ДВУХ-ИНДЕКСНОЕ снятие q+starve одной Lua),
  `complete` (ws:vt += cost_actual/w), `REQUEUE` (Ф3.4: возврат с сохранением
  vft+starve, epoch-fencing, attempt++).
- `scheduler/queue.py` — обёртка `Queue`: `enqueue`/`dequeue`/`requeue`/
  `preempt`/`call_record`/`complete`/`size`, `make_call(job, step, attempt)`;
  `now` инъектируется (clock callable) — Lua время сам не читает,
  детерминированные тесты.

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
| `ws:call:{shelf}:{call}` | HASH | scheduler (Ф3.4) |
| `ws:slots:*`, `ws:lease:*`, `ws:events:*` | SET/TTL/Stream | scheduler (Ф3.3) |
| `ws:gate:*`, `ws:pos:*` | — | Ф3.5+ |

## Тесты
- unit (без Redis): `make ws-test` (все зелёные; integration авто-skip).
- integration (живой Redis): `make ws-up-test` → `make ws-test-integration`
  (Redis `127.0.0.1:6390` — test-only overlay `compose.workspace.test.yml`,
  loopback; авто-skip, если `WS_REDIS_URL` не задан или PING не прошёл).
- Ф3.2: parity-тест — порядок dequeue (Lua) == эталон `policy.pick_best`
  (N=60 случайных вызовов, seed), обе ветки правила (WFQ + просроченные).
- Ф3.4: `test_requeue_preempt.py` — сохранение обоих индексов, anti-livelock
  (3 preempt-цикла, дедлайн стабилен), epoch-fencing без записей, кредит ==
  `vft − cost_done/w`, сервируемость, sweeper (мёртвый lease → слот → requeue).

## DBD-Scan-решение: redis_client.py
Паттерн клиента **ПОВТОРЕН** из `kb-console/src/kb_console/core/redis_client.py`
(Ф2 #5b: ленивый import, fail-closed env, `decode_responses=True`), НЕ импортирован:
направление зависимости — kb-console (клиентское приложение) зависит от
ai_workspace (control-plane), не наоборот. Осознанный ограниченный дубль (~50 строк);
консолидация — kb-console переключить на `ai_workspace.redis_client` в Ф3.x.

## НЕ входит (Ф3.5+)
Mode engine, admission (Ф3.5+); human-gate `ws:gate:*` + дашборд спящих;
полка `gpu` (Ф3.8).

## Слоты + события (Ф3.3, `scheduler/slots.lua` + `queue.py:dequeue_and_acquire`)

- **Ключи:** `ws:slots:{shelf}` (SET занятых), `ws:lease:{shelf}:{call}` (TTL 90 c),
  `ws:events:{shelf}` (Redis Stream, поле `event` = JSON).
- **Инвариант I1 «отказ, не очередь»:** попытка сверх `k` → `acquire() -> False`
  БЕЗ побочных эффектов (holders/lease/event не меняются). Ожидание живёт только
  в `ws:q:{shelf}`. `k == max_parallel_requests` полки (сверка с шлюзом — Ф1/I1).
- **Инварианты I2/I3:** `dequeue_and_acquire(k_max=…)` — ОДНА Lua: снятие из двух
  индексов + `SADD`/`SET lease` + `XADD`; слотов нет → `[]` и очередь не тронута.
- **I12:** `XADD MAXLEN ~ stream_maxlen` (обрезка приблизительная, по макро-узлам).
- **События:** `acquired` / `released` / `lease_expired`; потребитель — Mode engine
  (Ф3.5) через `XGROUP CREATE` + `XREADGROUP` + `XACK` (совместимость покрыта тестом).
- **Lua-хелперы:** `lua/_common.lua` предваряется к секциям `slots.lua`
  (только `local`, иначе Redis 7 «readonly table script»). `queue.lua` остаётся
  самодостаточным (секционные хелперы, Ф3.2).
- **Тесты:** `test_slots_lua.py` (K-инвариант, ровно 1 событие, heartbeat, reclaim,
  consumer-group), `test_dequeue_acquire.py` (атомарный отказ без снятия из очереди,
  parity с `Queue.dequeue`, lease читается `Slots.heartbeat`).

## Requeue + preempt + свипер (Ф3.4: `queue.lua:REQUEUE` + `queue.py` + `slots.py:sweep_expired`)

- **Per-call HASH** `ws:call:{shelf}:{call}` = `{prio, class, job, epoch,
  attempt, vft, starve_deadline}` — пишется `enqueue` (ключ в ARGV — паттерн
  `lease_prefix`), источник метаданных для preempt-кредита и epoch-fencing;
  читает `Queue.call_record` (наблюдение/тесты).
- **requeue сохраняет vft и starve-дедлайн** (I2, anti-livelock): `ZADD q` с
  исходным/override-vft, `ZADD starve` со СТАРЫМ дедлайном — aging-пол НЕ
  сбрасывается вытеснением (иначе preempt-loop ⇒ livelock/голодание batch);
  `attempt++` (retry++ спека §3). Ключи по известным именам, без `SCAN` (I12).
- **epoch-fencing (I4):** `requeue(epoch=…)` при `stored.epoch > epoch` →
  `False` БЕЗ записей — поздний redelivery (рестарт Redis) со старым epoch
  не «воскресает». `preempt` передаёт текущий stored-epoch.
- **preempt-кредит (спека §4):** `vft' = vft − cost_done/w(p,c)` — за сделанное;
  слот освобождает сам воркер (`Slots.release`) ДО requeue.
- **`sweep_expired()`** (`slots.py`): `SMEMBERS holders` → вызов без живого
  lease (`EXISTS == 0`) → `RECLAIM_EXPIRED` (слот возвращён + событие
  `lease_expired`); гонку «lease ожил» закрывает сам Lua (повторный `EXISTS`).
  Requeue вызова — шаг caller'а (scheduler tick), свипер только возвращает слот.
- **Дальше:** Ф3.5 — Mode engine (потребитель `ws:events`); Ф3.8 — полка `gpu`.
