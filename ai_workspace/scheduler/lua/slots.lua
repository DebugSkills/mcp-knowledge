-- slots.lua — семафор K полки и события Scheduler'а (Ф3.3, producer-сторона).
-- Спека: plans/_provenance/arch-2026-10-05-ai-workspace/
--         arch-2026-10-05-ai-workspace-scheduler-spec.md §2-5.
-- Инварианты: I1 «сверх K — ОТКАЗ, не очередь»; I3 «XADD в ТОЙ ЖЕ Lua, что
-- слот-операция»; I12 «XADD MAXLEN ~ N». Ветвлений по времени в Lua НЕТ.
--
-- Ключи: ws:slots:{shelf} (SET holders), ws:lease:{shelf}:{call} (TTL),
--        ws:events:{shelf} (Stream). Формат события (поле `event`, JSON):
--        {"type":acquired|released|lease_expired,"shelf":..,"call":..,
--         "job":..,"epoch":..,"ts":..,"state":..}
--        (job/epoch передаёт вызывающий; в dequeue-and-acquire-событии
--         отдельной Lua job НЕ дублируется — call уже `job:step:attempt`).
-- Совместимость с потребителем: XGROUP CREATE/XREADGROUP/XACK (тест).

-- @script ACQUIRE
-- Взять слот. KEYS[1]=holders, KEYS[2]=lease, KEYS[3]=stream.
-- ARGV[1]=call, ARGV[2]=k_max, ARGV[3]=lease_ttl_ms, ARGV[4]=event_json,
-- ARGV[5]=stream_maxlen.
-- Возврат: 1 — слот взят (SADD+SET PX+XADD); 0 — нет свободных слотов
--          (НИКАКИХ записей: ни holders, ни lease, ни события).
local k_max = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if not k_max or k_max < 1 then return ws_err('ACQUIRE: bad k_max') end
if not ttl or ttl < 1 then return ws_err('ACQUIRE: bad lease_ttl_ms') end
if redis.call('SCARD', KEYS[1]) >= k_max then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], ARGV[1], 'PX', ttl)
redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[5], '*', 'event', ARGV[4])
return 1

-- @script RELEASE
-- Освободить слот (воркер завершил/вытеснен). KEYS[1]=holders, KEYS[2]=lease,
-- KEYS[3]=stream. ARGV[1]=call, ARGV[2]=event_json, ARGV[3]=stream_maxlen.
-- Возврат: 1 — слот был у нас и освобождён (+событие); 0 — идемпотентный no-op
--          (не держали слот → событие НЕ пишем, снятия чужих lease нет).
local removed = redis.call('SREM', KEYS[1], ARGV[1])
if removed == 0 then return 0 end
redis.call('DEL', KEYS[2])
redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[3], '*', 'event', ARGV[2])
return 1

-- @script HEARTBEAT
-- Продлить lease воркера. KEYS[1]=lease. ARGV[1]=call, ARGV[2]=lease_ttl_ms.
-- Возврат: 1 — продлён; 0 — lease не наш/истёк (слот будет возвращён свипером).
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1

-- @script RECLAIM_EXPIRED
-- Вернуть слот, чей воркер умер (lease-ключа нет). Свипер/ступор-детектор.
-- KEYS[1]=holders, KEYS[2]=lease (конкретного call), KEYS[3]=stream.
-- ARGV[1]=call, ARGV[2]=event_json, ARGV[3]=stream_maxlen.
-- Возврат: 1 — слот возвращён (+событие lease_expired); 0 — занят живым или
--          уже не в holders.
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 0 then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SREM', KEYS[1], ARGV[1])
redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[3], '*', 'event', ARGV[2])
return 1
