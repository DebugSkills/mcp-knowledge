# ai_workspace — control-plane AI-верстака (Ф3.1)

Пакет серверной логики AI-верстака сообщества (plan arch-2026-10-05-ai-workspace):
будущее — Scheduler (Redis+Lua, WFQ+aging) + Mode engine. **Ф3.1** — каркас +
durable job-store.

## Что здесь (Ф3.1)
- `orchestrator/job.py` — job-store `ws:job:{id}` (HASH): `JobState` (8 статусов),
  `ALLOWED_TRANSITIONS` + `validate_transition` (чистая функция), `JobStore`
  (`create`/`get`/`transition`), CAS по `version` (hget+hset в ОДНОМ EVALSHA),
  epoch-fencing (I4: `epoch < current` → `StaleEpoch`, без записи),
  `compute_effect_id = H(job, node, effect)` — без attempt (идемпотентность I4).
- `redis_client.py` — тонкая фабрика ws-redis-клиента (`WS_REDIS_URL`).

## Ключи `ws:*` (спека Scheduler §2)
| Ключ | Тип | Владелец |
|---|---|---|
| `ws:job:{id}` | HASH | job-store (этот модуль, Ф3.1) |
| `ws:q:{shelf}`, `ws:slots:*`, `ws:lease:*`, `ws:gate:*`, `ws:pos:*` | — | scheduler/engine — Ф3.2+ |

## Тесты
- unit (без Redis): `make ws-test` (все зелёные; integration авто-skip).
- integration (живой Redis): `make ws-up-test` → `make ws-test-integration`
  (Redis `127.0.0.1:6390` — test-only overlay `compose.workspace.test.yml`,
  loopback; авто-skip, если `WS_REDIS_URL` не задан или PING не прошёл).

## DBD-Scan-решение: redis_client.py
Паттерн клиента **ПОВТОРЕН** из `kb-console/src/kb_console/core/redis_client.py`
(Ф2 #5b: ленивый import, fail-closed env, `decode_responses=True`), НЕ импортирован:
направление зависимости — kb-console (клиентское приложение) зависит от
ai_workspace (control-plane), не наоборот. Осознанный ограниченный дубль (~50 строк);
консолидация — kb-console переключить на `ai_workspace.redis_client` в Ф3.x.

## НЕ входит (Ф3.2+)
queue.lua, семафоры слотов `ws:slots:*`, lease/sweeper, Mode engine, admission.
