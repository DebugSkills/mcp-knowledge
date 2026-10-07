# ws-redis: лимиты памяти и алерт при исчерпании (Ф6 TODO 8)

**Трасса:** `arch-2026-10-05-ai-workspace` (Ф6, TODO 8) | **Область:** ws-redis, Makefile, errors-alert
**Назначение:** контролируемое поведение session-store верстака (ws-redis) при исчерпании
памяти: отказ записи вместо тихой потери сессий + алерт на пороге (инвариант I12,
решение оператора №4). Runbook — **единственный носитель чисел** (maxmemory/AOF/порог).

## 1. Числа (применены в `compose.workspace.yml`)

| Параметр | Значение | Носитель |
|---|---|---|
| `--maxmemory` | **200mb** (измерено: `CONFIG GET maxmemory` → `209715200`) | `compose.workspace.yml:28` |
| `mem_limit` | **256m** → headroom **56 МБ** (запас на AOF fork/COW) | `compose.workspace.yml:39` |
| AOF | `--appendonly yes` — персистентность сессий `ws:*` | `compose.workspace.yml:28` |
| Политика | **`noeviction`** — при исчерпании Redis отвечает ошибкой записи (OOM), НЕ вытесняет ключи | `compose.workspace.yml:28` |
| Порог алерта | **80%** от maxmemory = **160 МБ** | make-цель `ws-redis-check` |

## 2. Проверка (read-only)

```bash
make ws-redis-check
# ws-redis memory: used=1.5MB / max=200.0MB (1%)   ← измерено 2026-10-07, exit 0
```

При `used >= 80%` печатает в stderr `ALERT: ws-redis used>=80% maxmemory (…)` и
вызывает `make errors-alert` (сообщение генерится ошибками sink; **dry-run по
умолчанию**, `TG=1` — реальная отправка: `make ws-redis-check TG=1`).

Живой маршрут на пороге (проверено 2026-10-07):

```bash
docker exec mcp-knowledge-ws-redis redis-cli CONFIG SET maxmemory 1mb    # принудительный порог
make ws-redis-check    # → ALERT … 151% → вызван scripts/errors_alert.py
docker exec mcp-knowledge-ws-redis redis-cli CONFIG SET maxmemory 209715200   # возврат 200mb
```

## 3. Доставка алерта (non-dry-run; запуск — за оператором)

Штатная цель `errors-alert` по умолчанию dry-run. Реальная отправка — прямой
вызов скрипта с тем же sink:

```bash
.venv/bin/python scripts/errors_alert.py --sink <каталог-sink|фикстура> --send-tg [--host <TAG>]
# либо через make:  make errors-alert TG=1
```

Сообщение в Telegram уходит **владельцу канала** — запуск `--send-tg` за
оператором, из runbook не выполняется.

**Каденс проверки (предложение):** `*/15 * * * *` (при шумности — ежечасно).

```cron
*/15 * * * *  make -C /kvm/mcp-knowledge/mcp-knowledge ws-redis-check
```

Установка cron — за оператором: существующий контур `make errors-cron-install`
ставит 4 джобы errors-контура и этой проверки НЕ содержит — добавить строку
в crontab руками (модель `FILE=…` у `errors-cron-*`).

## 4. Границы (осознанные)

- **Мониторинга диска volume ws-redis нет** (F9) — известная граница базовой фазы:
  AOF растёт на диске без алерта; заведение мониторинга — P2-хвост.
- **OOM-путь не ломает постановку job'ов:** `emit_event` глотает любой
  `RedisError` (best-effort, с логом) — при OOM событие наблюдения теряется,
  но `submit`/узел не падают (`ai_workspace/scheduler/prio.py`, расширение
  catch до `RedisError` — коммит TODO 8).

---
**v1.0** | 2026-10-07 | Создано в трассе `arch-2026-10-05-ai-workspace` (Ф6 TODO 8, I12/D2).
