# ai_workspace — control-plane AI-верстака (Ф3.1–Ф3.4)

Пакет серверной логики AI-верстака сообщества (plan arch-2026-10-05-ai-workspace):
Scheduler (Redis+Lua, WFQ+aging) + Mode engine. **Ф3.1** — каркас + durable
job-store; **Ф3.2** — планировщик очередей вызовов (`scheduler/`); **Ф3.3** —
слот-семафор + события; **Ф3.4** — requeue/preempt/свипер.

## Что здесь
- `orchestrator/job.py` — job-store `ws:job:{id}` (HASH): `JobState` (9 статусов),
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
- `scheduler/park.py` — **Ф4.3 budget-hard-stop (I10/D5)**: `ParkControl.park/
  resume` — см. раздел «Park/resume (Ф4.3)» ниже.

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

## Park/resume (Ф4.3, `scheduler/park.py` + `queue.lua:PARK` + `job.py:parked`)
- **park** (D5: `admit() -> Decision(park)` из Ф4.2, или команда): состояние →
  `parked` (НЕ failure — `ws:fx:*` не трогаются); одна Lua `PARK` изымает вызов
  из `ws:q`+`ws:starve`, возвращает слот (`SREM holders`+`DEL lease`), снимает
  `ws:pos:{job}` и пишет событие `parked`; `conc_exit(user)` — резерв не течёт;
  vft/starve-кредит зеркалится в хеш job (`vft`/`starve_deadline`); epoch+1 —
  допарковые эффекты fenced (I4). Идемпотентен (повтор → `False`).
- **resume** (nightly reconcile D4 / админ): перепроверка `admit()` — бюджет
  ещё исчерпан → `False`, job остаётся `parked` без записей; иначе `requeue`
  с исходным vft/starve из per-call HASH (приоритет НЕ теряется, I2) и CAS
  `queued`; conc берётся ровно один раз — резервом `admit(allow)` (путь
  «admit ИЛИ conc_enter», без двойного учёта). Не-parked → `JobNotParked`
  (fail-loud). Mode engine на parked-job отказывается исполнять (paused).
- Отказы в сторону hard-stop: индексы/слот/conc освобождаются ДО CAS —
  проигранный CAS не оставляет вызов обслуживаемым.

## Реестры режимов (Ф3.5a-1, `registry/`)
- `registry/` — data-only YAML-реестры `roles`/`tools`/`gates`/`model_classes`/
  `shapes` + загрузчик `Registry` (`load`/`get`/`reload_if_changed`).
- Hot-reload по mtime каждого файла; битый/отсутствующий YAML → `RegistryError`
  (fail-closed: путь + причина, без молчаливых дефолтов).
- Валидатор реестров — Ф3.5a-2; движок режимов — Ф3.5b.

## Режимы: линт L1–L11 (Ф3.5a-3, L11 — Ф3.9)

`make modes-validate` гоняет ДВА контура: схему (S1–S8, `orchestrator/mode_schema.py`) и
рантайм-линт (L1–L11, `orchestrator/mode_lint.py`): DAG/циклы критика, роли+seed-скиллы,
инструменты, покрытие вход/выход (транзитивно), human-gate для strategic/brainstorm,
citation-политика, резолв `model_class` (+private→local-only), терминируемость, fork/join,
single-writer секций доски. На каждое правило — фикстур-нарушитель в `tests/fixtures/modes/`.

## Board-store (Ф3.5b-1, `orchestrator/board.py`)
- `BoardStore` (`ws:board:{job}` = `version`+`sec:*`, `:owner` = секция→узел,
  `:v:{n}` = immutable-снапшоты): CAS одной inline-Lua — stale → `StaleBoard`
  (без записи), duplicate (==current) → идемпотентный no-op, `current+1` →
  запись + снапшот полного состояния.
- Single-writer секций: чужой `writer_node` → `SectionConflict` (или перехват
  при `single_writer=False`); `diff(v1,v2)` по снапшотам; тесты — integration
  (`test_board_store.py`, авто-skip).

## Board-store job'а (Ф3.5b-1, `orchestrator/board.py`)

Ключи: `ws:board:{job}` (HASH: `version` + `sec:<name>`), `ws:board:{job}:owner` (секция→узел),
`ws:board:{job}:v:{n}` (immutable снапшот). CAS одной Lua: `expect<current` → `StaleBoard`
(без записи), duplicate (`expect==current-1` и значения уже записаны) → идемпотентный no-op,
`expect==current` → версия+1 + снапшот. `single_writer=True` — секцию пишет один узел
(`SectionConflict`); `read_version(n)`/`diff(a,b)` — replay/аудит.

## Mode engine (Ф3.5b-2, `orchestrator/engine.py` + `graph.py` + `ledger.py`)
Исполняет декларативный граф режима: `llm-step`/`tool-step`/`critic-gate`/`human-gate`,
курсор в job, pause/resume на human-gate (single-use `resume_token`), эффекты
идемпотентны (`compute_effect_id` → ledger), секции пишутся через board-CAS.
Зависимости инъектируются (`jobs`/`boards`/`llm`/`mcp`/`ledger`/`artifacts`).
`fork`/`join` — Ф3.8+ (`UnsupportedNode`, fail-loud).

## Режим «статья» (Ф3.6, `modes/statya.yaml`)
Первый drop-in-режим: `analyst → structure(gate) → critic(on_revise) → editor →
citer(strict) → publish(gate)`. Гейт, отвеченный `approve`, дальше pass-through
(REVISE-петля не спрашивает человека повторно); `edit` показывает гейт снова после
переработки, а текст правки попадает во вход адресата (`on_edit`/`on_approve`).

## Artifact-store (Ф3.7, `ai_workspace/artifacts.py`)
Готовая работа ≠ знание (I13): артефакт живёт в `ws:artifact:{user}:{id}` (retention
30 дней, TTL), индексируется в `ws:artifacts:{user}` (ZSET по `expires_at`), дедуп по
`sha256[:16]`. API: `save/get/list/export/delete/prune/promote`. В KB ведёт ТОЛЬКО
явный `promote` (через `mcp.write_knowledge`, с обязательными `domain`/`subject`/`zone`).
Движок по завершении job сохраняет композицию `output.sections` режима.

## Unified GPU-контур (Ф3.8, `ai_workspace/gpu.py`)
Один контур на все GPU-работы: верстак (эмбеддинги артефактов, vision) и mcp-knowledge
(reindex/embed) ходят через общую полку `gpu` — иначе процессы переподпишут VRAM.
Реализация — переиспользование семафора полок (Ф3.3): `Slots(client, shelf="gpu", k=K)`.
Виды работ: `embed` / `vision` / `reindex`; **K общий** → сверх K отказ `GpuBusy`
(не очередь, I1). API: `acquire/release/heartbeat/try_acquire/status/reconcile` и
контекст `with gpu.lease("embed", ref):`. Ключи `ws:slots:gpu`, `ws:lease:gpu:{kind}:{ref}`,
`ws:events:gpu`; K — env `WS_GPU_K` (dev 8 ГБ → 1). `reconcile()` снимает слоты с мёртвыми
lease и отчитывается по видам — упавший воркер не держит GPU вечно.

## Conformance-гейт (Ф3.9, `ai_workspace/conformance.py` + `tests/golden/`)
Паттерн «Local-First Conformance Gate»: три раздельных вердикта.
- **T** (transport) — зонный маршрут; unit `zone→egress` **red-first**: двойной ассерт
  «отказ при `private` вне local» **и** «счётчик ext-egress == 0»; снятие зонного предиката
  ловится тестом (`assert_no_ext_egress`).
- **I** (interface) — **parity обязательна**: `decoding-pin` (temp=0/seed=42/thinking=off,
  parity только при равных параметрах) и `prompt-hash parity` обеих полок (hash
  РЕЗОЛВНУТОГО промпта); model-specific ветка → parity падает. Линт L11 в
  `make modes-validate` запрещает упоминания моделей в режимах.
- **Q** (quality, non-parity) — `tests/golden/golden-set.yaml` (5 заданий, `version: 1`),
  `QReport` = **отчёт-таблица** (не ассерт): порог Q-floor по зоне (`public 0.80`,
  `private 0.85`; владелец — оператор, `decide_by: Ф3.9-старт`), ниже порога → маркер
  `local-draft`, N≥2 прогонов + флаг вариативности.
