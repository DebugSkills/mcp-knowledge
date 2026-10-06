-- _common.lua — общие хелперы Lua-скриптов планировщика (Ф3.3).
-- Предваряется (string-concat) к телу секций slots.lua при register_script —
-- один источник для всех future-скриптов; queue.lua остаётся
-- самодостаточным (секционные local-хелперы, Ф3.2 — не трогаем).
-- `local` (не глобальные!): Redis 7 защищает глобальную таблицу
-- скрипта («Attempt to modify a readonly table»); видимость обеспечивает
-- string-concat в один чанк до вызова register_script.
local function ws_f2s(x) return string.format('%.17g', x) end

local function ws_err(msg) return redis.error_reply(msg) end
