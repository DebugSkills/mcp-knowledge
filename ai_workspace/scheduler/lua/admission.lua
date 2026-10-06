-- admission.lua — admission pre-check + списание квот (Ф4.2).
-- Спека: plans/_provenance/arch-2026-10-05-ai-workspace/ (план REV.13, Ф4.2),
--         решения оператора D3-D7. Паттерн секций/ключей — slots.lua (Ф3.3).
--
-- Ключи: ws:quota:tok:{user}:{day} (расход токенов дня, TTL до полуночи),
--        ws:quota:conc:{user} (резерв параллелизма, БЕЗ TTL — живёт как job),
--        ws:quota:conchold:{user} (SET job-маркеров владения резервом, P1-2),
--        ws:quota:conclease:{user}:{job} (TTL-lease резерва, P1-3),
--        ws:quota:events (Stream событий квот-контура: reclaim/degraded),
--        ws:budget:global:{month} (расход ext-контура за месяц, микро-₽ int;
--        пишет budget.charge_budget / сверяет reconcile_budget, P0-1).
-- Режим conc — РЕЗЕРВ: проверка и INCR в ОДНОЙ Lua (иначе две гонки обе
-- пройдут); мутация ТОЛЬКО при финальном allow (deny/park ничего не пишут).
-- Времени Lua сам не читает — границы дня/полночь инъектирует Python.

-- @script ADMIT
-- Атомарный pre-check постановки. KEYS[1]=tok, KEYS[2]=conc,
-- KEYS[3]=ws:budget:global, KEYS[4]=conchold (SET job-маркеров, P1-2),
-- KEYS[5]=conclease ('' → без per-job учёта).
-- ARGV[1]=tokens_limit ('' = без личного лимита), ARGV[2]=conc_limit
-- ('' = без личного лимита), ARGV[3]=budget_limit ('' = полка не ext →
-- бюджетная проверка выключена), ARGV[4]=job ('' → агрегатный INCR без
-- маркера — легаси-путь; освобождение ТОЛЬКО агрегатным CONC_EXIT),
-- ARGV[5]=lease_ttl_ms (0 → lease не ставить: маркер без auto-reclaim).
-- Возврат: {action, code, detail}; action=allow|deny|park; detail — текущее
-- значение проверяемой метрики (для reason вызывающего).
-- Идемпотентность по (user, job) — P1-B iter2: маркер владения ЭТОГО job
-- уже стоит → allow + refresh lease БЕЗ повторного INCR (двойное взятие
-- резерва = перманентная утечка без маркера; закрывает окно «lease истёк,
-- свип ещё не прошёл» и submit-ретрай между admit и create).
-- Владение+lease при job≠'' ставятся БЕЗУСЛОВНО — P1-A iter2: conc=null
-- (admin) не должен отключать lease, иначе heartbeat воркера немедленно
-- валит живой job; счётчик INCR — только при личном лимите (симметрия с
-- CONC_RELEASE: DECR под гвардом inflight > 0).
local function numkey(key)
  -- GET с валидацией (P2-9): отсутствует → 0; НЕчисловое значение →
  -- понятный отказ (error), а не runtime-error на сравнении nil.
  local raw = redis.call('GET', key)
  if not raw then return 0 end
  local v = tonumber(raw)
  if v == nil then
    error('ADMIT: нечисловое значение ' .. key .. ': ' .. tostring(raw))
  end
  return v
end
local tok_limit, conc_limit, budget_limit = ARGV[1], ARGV[2], ARGV[3]
if (tok_limit ~= '' and not tonumber(tok_limit))
  or (conc_limit ~= '' and not tonumber(conc_limit))
  or (budget_limit ~= '' and not tonumber(budget_limit)) then
  return redis.error_reply('ADMIT: bad ARGV limits')
end
-- P1-B iter2: идемпотентность по (user, job) — ГВАРД ДО всех проверок:
-- свой протухший (несвипнутый) резерв не должен self-deny гостя, а уже
-- взятый резерв — дублироваться. Refresh lease = «воркер ожил».
if ARGV[4] ~= '' and redis.call('SISMEMBER', KEYS[4], ARGV[4]) == 1 then
  if tonumber(ARGV[5]) > 0 then
    redis.call('SET', KEYS[5], ARGV[4], 'PX', tonumber(ARGV[5]))
  end
  return {'allow', '', ''}
end
if tok_limit ~= '' then
  local spent = numkey(KEYS[1])
  if spent >= tonumber(tok_limit) then
    return {'deny', 'quota_tokens_exhausted', tostring(spent)}
  end
end
if budget_limit ~= '' then
  local spent = numkey(KEYS[3])
  if spent >= tonumber(budget_limit) then
    return {'park', 'budget_ext_exhausted', tostring(spent)}
  end
end
local after = ''
if conc_limit ~= '' then
  local inflight = numkey(KEYS[2])
  if inflight >= tonumber(conc_limit) then
    return {'deny', 'quota_conc_exceeded', tostring(inflight)}
  end
  after = redis.call('INCR', KEYS[2])
end
-- per-job владение (P1-2 + P1-A iter2): маркер+lease — БЕЗУСЛОВНО при
-- job≠'' (роль без личного лимита тоже владеет резервом и живёт под
-- heartbeat); резерв снимается conc_release(user, job) ровно один раз —
-- по маркеру, а не «ещё одним DECR» агрегата.
if ARGV[4] ~= '' then
  redis.call('SADD', KEYS[4], ARGV[4])
  if tonumber(ARGV[5]) > 0 then
    redis.call('SET', KEYS[5], ARGV[4], 'PX', tonumber(ARGV[5]))
  end
end
return {'allow', '', tostring(after)}

-- @script CHARGE
-- Списание токенов по факту usage. KEYS[1]=tok. ARGV[1]=tokens (int >= 0),
-- ARGV[2]=expireat (unix sec — АБСОЛЮТНАЯ метка локальной полуночи от Python).
-- EXPIREAT абсолютен: повторные charge ставят ту же метку (идемпотентно,
-- TTL не растёт — GT-семантика не нужна), ключ не может пережить полночь
-- (сброс D7); относительный TTL при charge в 23:59 продлил бы ключ за день.
-- Возврат: новый расход дня.
local spent = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIREAT', KEYS[1], tonumber(ARGV[2]))
return spent

-- @script CONC_ENTER
-- Взять резерв БЕЗ проверок (легаси-агрегат, без job-маркера; новые вызовы
-- должны идти admit(job=...) — см. admission.py). KEYS[1]=conc.
-- Возврат: новый in-flight.
return redis.call('INCR', KEYS[1])

-- @script CONC_EXIT
-- Освободить резерв АГРЕГАТОМ (легаси-путь; пол 0). KEYS[1]=conc.
-- Возврат: остаток; пол на 0 — лишний exit это no-op, в минус счётчик
-- не уходит.
local inflight = tonumber(redis.call('GET', KEYS[1]) or '0')
if inflight <= 0 then return 0 end
return redis.call('DECR', KEYS[1])

-- @script CONC_RELEASE
-- Освобождение резерва ПО ФАКТУ ВЛАДЕНИЯ (P1-2): SREM job-маркера решает
-- атомарно — чужой резерв не декрементируется, повторный вызов no-op
-- (двойной park/resume не может списать дважды — обход D6 закрыт).
-- KEYS[1]=conc, KEYS[2]=conchold, KEYS[3]=conclease. ARGV[1]=job.
-- Возврат: 1 — резерв этого job'а снят (SREM+DEL lease+DECR); 0 — job
-- не владел резервом (no-op без записей).
if redis.call('SREM', KEYS[2], ARGV[1]) == 0 then
  return 0
end
redis.call('DEL', KEYS[3])
local raw = redis.call('GET', KEYS[1])
local inflight = tonumber(raw)
if inflight and inflight > 0 then
  redis.call('DECR', KEYS[1])
end
return 1

-- @script CONC_HEARTBEAT
-- Продлить lease резерва job'а (воркер жив; паттерн slots.HEARTBEAT, P1-3).
-- KEYS[1]=conclease. ARGV[1]=ttl_ms. Возврат: 1 — продлён; 0 — резерва нет
-- (истёк и снят свипером) — воркер обязан остановить работу.
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[1]))
return 1

-- @script CONC_RECLAIM
-- Свипер мёртвых резервов (P1-3): маркер есть + lease истёк → снять резерв
-- (SREM+DECR одной Lua — паттерн slots.RECLAIM_EXPIRED; гонка «lease ожил»
-- закрыта перепроверкой EXISTS внутри скрипта). KEYS[1]=conc,
-- KEYS[2]=conchold, KEYS[3]=conclease, KEYS[4]=stream = ws:quota:events.
-- ARGV[1]=job, ARGV[2]=event-json ('' → без события), ARGV[3]=stream_maxlen.
-- Возврат: 1 — снят (+событие conc_reservation_reclaimed); 0 — не владел
-- или lease жив (воркер дышит).
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 0 then return 0 end
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
redis.call('SREM', KEYS[2], ARGV[1])
local raw = redis.call('GET', KEYS[1])
local inflight = tonumber(raw)
if inflight and inflight > 0 then
  redis.call('DECR', KEYS[1])
end
if ARGV[2] ~= '' then
  redis.call('XADD', KEYS[4], 'MAXLEN', '~', tonumber(ARGV[3]), '*', 'event', ARGV[2])
end
return 1
