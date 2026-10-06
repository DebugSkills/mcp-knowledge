-- queue.lua — атомарные Lua-скрипты планировщика WFQ 3×3 + aging (Ф3.2).
-- Спека: plans/_provenance/arch-2026-10-05-ai-workspace/
--         arch-2026-10-05-ai-workspace-scheduler-spec.md §2-3.
-- Файл — SSOT Lua-кода: scheduler/queue.py нарезает секции по маркерам
-- «-- @script <name>» и регистрирует через client.register_script
-- (EVALSHA с fallback на EVAL — паттерн Ф3.1 orchestrator/job.py).
--
-- Общий контракт: KEYS/ARGV-стиль; только redis.call; ветвлений по времени
-- НЕТ — «now» приходит из Python (инъекция clock → детерминированные тесты);
-- float передаётся строкой %.17g (round-trip без усечения: конверсия
-- Lua-number в аргументе redis.call отбрасывает дробную часть);
-- возвраты — простые типы (string / array-of-string).

-- @script enqueue
-- Постановка вызова: vft = max(V, last) + cost_est/w (спека §3, on_enqueue).
-- KEYS[1] q       = ws:q:{shelf}               ZSET, score = raw VFT
-- KEYS[2] vt      = ws:vt:{shelf}              string-float, виртуальные часы
-- KEYS[3] vftlast = ws:vftlast:{shelf}:{p}:{c} string-float, last-VFT очереди
-- KEYS[4] starve  = ws:starve:{shelf}          ZSET, score = starve_deadline
-- ARGV[1] call — id вызова (член ZSET)
-- ARGV[2] prio, ARGV[3] class — метаданные 3×3 (ключ vftlast приходит в KEYS;
--        Ф3.4: пишутся в per-call HASH — источник prio/class для preempt)
-- ARGV[4] weight = w(p,c) (посчитан в Python: policy.weight)
-- ARGV[5] cost_est, ARGV[6] now, ARGV[7] starve_deadline = now + T_starve[class]
-- ARGV[8] job, ARGV[9] epoch, ARGV[10] attempt (Ф3.4: per-call HASH);
-- ARGV[11] callkey = ws:call:{shelf}:{call} ("" → HASH не пишется; ключ в
--        ARGV, не в KEYS — паттерн lease_prefix DEQUEUE_ACQUIRE).
-- Возвращает: vft (строка %.17g).
-- PUBLISH ws:kick / XADD event-commit — НЕ здесь (Ф3.3).
local function f2s(x) return string.format('%.17g', x) end
local w = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])
if not w or w <= 0 or not cost or cost < 0 then
  return redis.error_reply('enqueue: bad ARGV weight/cost_est')
end
local V = tonumber(redis.call('GET', KEYS[2])) or 0.0
local last = tonumber(redis.call('GET', KEYS[3])) or 0.0
local vft = math.max(V, last) + cost / w
redis.call('SET', KEYS[3], f2s(vft))
redis.call('ZADD', KEYS[1], f2s(vft), ARGV[1])
redis.call('ZADD', KEYS[4], ARGV[7], ARGV[1])  -- дедлайн уже строкой из Python
-- per-call HASH (Ф3.4): исходное состояние для requeue/preempt — vft и
-- starve_deadline хранятся строками %.17g (round-trip без усечения).
if ARGV[11] and ARGV[11] ~= '' then
  redis.call('HSET', ARGV[11],
    'prio', ARGV[2], 'class', ARGV[3], 'job', ARGV[8],
    'epoch', ARGV[9], 'attempt', ARGV[10],
    'vft', f2s(vft), 'starve_deadline', ARGV[7])
end
return f2s(vft)

-- @script dequeue
-- Снятие до limit вызовов по правилу policy.pick_best + ДВУХ-ИНДЕКСНОЕ
-- удаление ОДНИМ скриптом (иначе гонка «half-removed» между q и starve).
-- KEYS[1] q      = ws:q:{shelf}      — индекс 1/2 (WFQ, score = vft)
-- KEYS[2] starve = ws:starve:{shelf} — индекс 2/2 (aging, score = дедлайн)
-- KEYS[3] vt, KEYS[4] vftlast — скриптом НЕ читаются (vt двигает complete;
--        в подписи для единого dequeue-контракта ключей полки, Ф3.3+).
-- ARGV[1] now, ARGV[2] limit.
-- Правило (инвариант I2 — aging-пол = АБСОЛЮТНОЕ право):
--   1) просроченные (starve_deadline <= now) — прямо из starve-индекса,
--      FIFO по дедлайну, БЕЗ окна 32: окно WFQ (32 младших vft) может не
--      вместить background с большим vft, и «побеждает любого» нарушалось бы;
--   2) добор до limit — из окна ZRANGE q 0 31 (32 младших VFT, спека §3):
--      argmin vft; тай-брейк — лекс. имя (как в policy.pick_best).
-- Возвращает: array имён снятых вызовов в порядке обслуживания.
local function f2s(x) return string.format('%.17g', x) end
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if not now or not limit or limit < 1 then
  return redis.error_reply('dequeue: bad ARGV now/limit')
end
local taken = {}
local taken_set = {}
local function take(call)
  -- ДВУХ-ИНДЕКСНОЕ снятие: q (WFQ) + starve (aging) — атомарно одной Lua.
  redis.call('ZREM', KEYS[1], call)
  redis.call('ZREM', KEYS[2], call)
  taken_set[call] = true
  taken[#taken + 1] = call
end
-- 1) просроченные: FIFO по дедлайну (ZRANGEBYSCORE уже ascending;
-- тай-брейк ZSET — лекс. член, тот же, что в policy.pick_best).
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', f2s(now),
                           'LIMIT', 0, limit)
for i = 1, #expired do
  if #taken >= limit then break end
  take(expired[i])
end
-- 2) добор до limit из WFQ-окна 32 младших VFT.
if #taken < limit then
  local cands = redis.call('ZRANGE', KEYS[1], 0, 31, 'WITHSCORES')
  local entries = {}
  for i = 1, #cands, 2 do
    local call = cands[i]
    if not taken_set[call] then
      local dl_raw = redis.call('ZSCORE', KEYS[2], call)
      -- dl == nil: член есть в q, но нет в starve — вызов «под-снят»
      -- инкогнито; aging-права НЕ даём (fail-safe к чистому WFQ; requeue
      -- Ф3.4 обязан класть вызов в ОБА индекса — это его контракт).
      entries[#entries + 1] = { call, tonumber(cands[i + 1]),
                                dl_raw and tonumber(dl_raw) or nil }
    end
  end
  table.sort(entries, function(a, b)
    local ae = a[3] ~= nil and a[3] <= now  -- просроченный внутри окна
    local be = b[3] ~= nil and b[3] <= now
    if ae ~= be then return ae end          -- просроченный — раньше
    if ae then
      if a[3] ~= b[3] then return a[3] < b[3] end  -- FIFO по дедлайну
    else
      if a[2] ~= b[2] then return a[2] < b[2] end  -- argmin vft
    end
    return a[1] < b[1]                      -- тай-брейк: лекс. имя
  end)
  for i = 1, #entries do
    if #taken >= limit then break end
    take(entries[i][1])
  end
end
return taken

-- @script complete
-- on_complete (спека §3): ws:vt:{shelf} += cost_actual/w — атомарный
-- read-modify-write одной Lua. Отдельно от dequeue: снятие из очереди и ход
-- виртуальных часов — разные транзакции (между ними живёт воркер).
-- KEYS[1] vt = ws:vt:{shelf}
-- KEYS[2] vftlast = ws:vftlast:{shelf}:{p}:{c} — контракт Ф3.2: принимается,
--        НЕ пишется: enqueue сам берёт max(V, last) — подтягивать vftlast
--        до vt здесь значило бы double-count; читать его будет requeue Ф3.4
--        (сохранить vft/starve-дедлайн, иначе preempt-livelock).
-- ARGV[1] shelf_pc_key — тот же vftlast-ключ (инфо/журнал);
-- ARGV[2] weight, ARGV[3] cost_actual.
-- Возвращает: новый vt (строка %.17g).
local function f2s(x) return string.format('%.17g', x) end
local w = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
if not w or w <= 0 or not cost or cost < 0 then
  return redis.error_reply('complete: bad ARGV weight/cost_actual')
end
local vt = (tonumber(redis.call('GET', KEYS[1])) or 0.0) + cost / w
redis.call('SET', KEYS[1], f2s(vt))
return f2s(vt)

-- @script DEQUEUE_ACQUIRE
-- АТОМАРНО: выбрать вызовы (правило dequeue) + ВЗЯТЬ слот + XADD событие —
-- одной Lua (инварианты I2 «снятие из двух индексов + выдача слота — одной
-- Lua» и I3 «XADD в той же Lua, что слот-операция»).
-- Логика выбора ДУБЛИРУЕТ dequeue осознанно: `take` здесь имеет побочные
-- эффекты (holders/lease/event), а секции регистрируются отдельными чанками.
-- Синхронность правил обязательна → parity-тест (test_dequeue_acquire.py).
-- KEYS[1] q, KEYS[2] starve, KEYS[3] holders = ws:slots:{shelf},
-- KEYS[4] stream = ws:events:{shelf}.
-- ARGV[1] now, ARGV[2] limit, ARGV[3] k_max, ARGV[4] lease_ttl_ms,
-- ARGV[5] stream_maxlen, ARGV[6] lease_prefix = "ws:lease:{shelf}:",
-- ARGV[7] shelf (для event-json), ARGV[8] ts.
-- Возврат: array имён взятых вызовов (в порядке обслуживания).
--   ПУСТОЙ массив, если слотов нет — при этом очередь НЕ изменяется
--   (отказ ДО любого снятия — не вакуумный инвариант).
local function f2s(x) return string.format('%.17g', x) end
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local k_max = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
if not now or not limit or limit < 1 then
  return redis.error_reply('DEQUEUE_ACQUIRE: bad ARGV now/limit')
end
if not k_max or k_max < 1 or not ttl or ttl < 1 then
  return redis.error_reply('DEQUEUE_ACQUIRE: bad ARGV k_max/lease_ttl_ms')
end
local used = redis.call('SCARD', KEYS[3])
local take_limit = math.min(limit, k_max - used)
if take_limit < 1 then return {} end  -- слотов нет: НИЧЕГО не снимаем

local taken = {}
local taken_set = {}
local function take(call)
  -- ДВУХ-ИНДЕКСНОЕ снятие + слот + событие — одной Lua.
  redis.call('ZREM', KEYS[1], call)
  redis.call('ZREM', KEYS[2], call)
  redis.call('SADD', KEYS[3], call)
  redis.call('SET', ARGV[6] .. call, call, 'PX', ttl)
  local ev = string.format(
    '{"type":"acquired","shelf":"%s","call":"%s","ts":%s,"state":"running"}',
    ARGV[7], call, ARGV[8])
  redis.call('XADD', KEYS[4], 'MAXLEN', '~', ARGV[5], '*', 'event', ev)
  taken_set[call] = true
  taken[#taken + 1] = call
end
-- 1) просроченные (aging-пол = абсолютное право) — прямо из starve.
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', f2s(now),
                           'LIMIT', 0, take_limit)
for i = 1, #expired do
  if #taken >= take_limit then break end
  take(expired[i])
end
-- 2) добор из WFQ-окна 32 младших VFT.
if #taken < take_limit then
  local cands = redis.call('ZRANGE', KEYS[1], 0, 31, 'WITHSCORES')
  local entries = {}
  for i = 1, #cands, 2 do
    local call = cands[i]
    if not taken_set[call] then
      local dl_raw = redis.call('ZSCORE', KEYS[2], call)
      entries[#entries + 1] = { call, tonumber(cands[i + 1]),
                                dl_raw and tonumber(dl_raw) or nil }
    end
  end
  table.sort(entries, function(a, b)
    local ae = a[3] ~= nil and a[3] <= now
    local be = b[3] ~= nil and b[3] <= now
    if ae ~= be then return ae end
    if ae then
      if a[3] ~= b[3] then return a[3] < b[3] end
    else
      if a[2] ~= b[2] then return a[2] < b[2] end
    end
    return a[1] < b[1]
  end)
  for i = 1, #entries do
    if #taken >= take_limit then break end
    take(entries[i][1])
  end
end
return taken

-- @script REQUEUE
-- Возврат снятого вызова в очередь (Ф3.4; preempt / свипер / human-gate
-- resume — спека §3 «sweeper: re-enqueue(vft сохранить, retry++)» и §4
-- «re-enqueue с vft-кредитом за сделанное»). Пишет в ОБА индекса — контракт,
-- на который ориентируется fail-safe-ветка dequeue (член q без starve).
-- ИНВАРИАНТЫ:
--   I2 — starve-дедлайн СОХРАНЯЕТСЯ (исходный score ws:starve): aging-пол
--        не сбрасывается вытеснением, иначе preempt-loop → livelock;
--   I4 — epoch-fencing: stored.epoch > ARGV[3] → отказ БЕЗ записей
--        (поздний redelivery со старым epoch не «воскресает»).
-- KEYS[1] q, KEYS[2] starve, KEYS[3] callkey = ws:call:{shelf}:{call}.
-- ARGV[1] call, ARGV[2] now (контракт группы; скриптом не читается — время
--        инъектируется из Python), ARGV[3] epoch, ARGV[4] vft_override|""
--        (preempt-кредит; "" → исходный stored vft).
-- Возврат: 1 — requeued (ZADD q + ZADD starve СТАРЫЙ дедлайн + HSET vft +
--          HINCRBY attempt); 0 — per-call записи нет ИЛИ stale epoch
--          (в обоих случаях НИКАКИХ записей).
local epoch = tonumber(ARGV[3])
if not epoch then
  return redis.error_reply('REQUEUE: bad ARGV epoch')
end
local rec = redis.call('HGETALL', KEYS[3])
if #rec == 0 then return 0 end
local stored = {}
for i = 1, #rec, 2 do stored[rec[i]] = rec[i + 1] end
if (tonumber(stored['epoch']) or 0) > epoch then
  return 0  -- I4: stale epoch — отказ без записи
end
local vft_str
if ARGV[4] ~= '' then
  if not tonumber(ARGV[4]) then
    return redis.error_reply('REQUEUE: bad ARGV vft_override')
  end
  vft_str = ARGV[4]
else
  vft_str = stored['vft']
end
local dl_str = stored['starve_deadline']
if not vft_str or not dl_str then return 0 end  -- повреждённая запись
-- score — СТРОКАМИ %.17g: конверсия Lua-number в redis.call усекает дробь.
redis.call('ZADD', KEYS[1], vft_str, ARGV[1])
redis.call('ZADD', KEYS[2], dl_str, ARGV[1])  -- I2: дедлайн КАК ЕСТЬ
redis.call('HSET', KEYS[3], 'vft', vft_str)
redis.call('HINCRBY', KEYS[3], 'attempt', 1)  -- retry++
return 1
