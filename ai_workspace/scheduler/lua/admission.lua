-- admission.lua — admission pre-check + списание квот (Ф4.2).
-- Спека: plans/_provenance/arch-2026-10-05-ai-workspace/ (план REV.13, Ф4.2),
--         решения оператора D3-D7. Паттерн секций/ключей — slots.lua (Ф3.3).
--
-- Ключи: ws:quota:tok:{user}:{day} (расход токенов дня, TTL до полуночи),
--        ws:quota:conc:{user} (резерв параллелизма, БЕЗ TTL — живёт как job),
--        ws:budget:global (расход ext-контура; пишет reconcile, Ф4.3+).
-- Режим conc — РЕЗЕРВ: проверка и INCR в ОДНОЙ Lua (иначе две гонки обе
-- пройдут); мутация ТОЛЬКО при финальном allow (deny/park ничего не пишут).
-- Времени Lua сам не читает — границы дня/полночь инъектирует Python.

-- @script ADMIT
-- Атомарный pre-check постановки. KEYS[1]=tok, KEYS[2]=conc,
-- KEYS[3]=ws:budget:global. ARGV[1]=tokens_limit ('' = без личного лимита),
-- ARGV[2]=conc_limit ('' = без личного лимита), ARGV[3]=budget_limit
-- ('' = полка не ext → бюджетная проверка выключена).
-- Возврат: {action, code, detail}; action=allow|deny|park; detail — текущее
-- значение проверяемой метрики (для reason вызывающего).
local tok_limit = ARGV[1]
if tok_limit ~= '' then
  local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
  if spent >= tonumber(tok_limit) then
    return {'deny', 'quota_tokens_exhausted', tostring(spent)}
  end
end
local budget_limit = ARGV[3]
if budget_limit ~= '' then
  local spent = tonumber(redis.call('GET', KEYS[3]) or '0')
  if spent >= tonumber(budget_limit) then
    return {'park', 'budget_ext_exhausted', tostring(spent)}
  end
end
local conc_limit = ARGV[2]
if conc_limit ~= '' then
  local inflight = tonumber(redis.call('GET', KEYS[2]) or '0')
  if inflight >= tonumber(conc_limit) then
    return {'deny', 'quota_conc_exceeded', tostring(inflight)}
  end
  return {'allow', '', tostring(redis.call('INCR', KEYS[2]))}
end
return {'allow', '', ''}

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
-- Взять резерв БЕЗ проверок (resume из парка, Ф4.3 — лимит подтверждён
-- admit'ом до парка). KEYS[1]=conc. Возврат: новый in-flight.
return redis.call('INCR', KEYS[1])

-- @script CONC_EXIT
-- Освободить резерв (finally воркера). KEYS[1]=conc. Возврат: остаток;
-- пол на 0 — лишний exit это no-op, в минус счётчик не уходит.
local inflight = tonumber(redis.call('GET', KEYS[1]) or '0')
if inflight <= 0 then return 0 end
return redis.call('DECR', KEYS[1])
