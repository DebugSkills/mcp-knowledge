-- budget.lua — списание денег ext-контура + сверка счётчиков (P0-1 ревизии Ф4).
-- Спека: plans/_provenance/arch-2026-10-05-ai-workspace/critique-F4-quota-machine.md
--         (P0-1: до этого ws:budget:* никто не писал — park по бюджету был
--         недостижим). Паттерн секций -- slots.lua/admission.lua (Ф3.3/Ф4.2).
--
-- Ключи (все суммы — ЦЕЛОЧИСЛЕННЫЕ микро-₽, 1 ₽ = 10^6; никаких float):
--   ws:budget:global:{YYYY-MM}  — расход месяца ext-контура (D4; месяц —
--                                 локальный, инъектируется Python);
--   ws:budget:user:{u}:{YYYY-MM} — per-user зеркало (D4, per_user_mirror);
--   ws:budget:journal            — Stream-журнал списаний (SSOT факта для
--                                 reconcile; MAXLEN ~ большой — см. budget.py);
--   ws:quota:events              — события квот-контура (budget_reconciled).
-- Пишет charge_budget (вместе с charge_tokens на терминале job, wiring);
-- сверяет reconcile_budget (scripts/ws_budget_reconcile.py, nightly).

-- @script BUDGET_CHARGE
-- Атомарное списание: INCRBY global + INCRBY user-зеркало + XADD журнала —
-- ОДНИМ вызовом (иначе крах между ними расщепляет факт и счётчик).
-- KEYS[1]=global:{month}, KEYS[2]=user:{month}, KEYS[3]=journal.
-- ARGV[1]=rub_micro (int >= 0), ARGV[2]=journal-json, ARGV[3]=stream_maxlen.
-- Возврат: новый глобальный расход месяца (int).
local g = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('INCRBY', KEYS[2], ARGV[1])
redis.call('XADD', KEYS[3], 'MAXLEN', '~', tonumber(ARGV[3]), '*', 'event', ARGV[2])
return g

-- @script BUDGET_RECONCILE
-- Сверка счётчика с журналом: каждый счётчик := переданная сумма (INCRBY
-- дельты — чинит дрейф в ОБЕ стороны: и завышенный, и заниженный счётчик).
-- KEYS[1]=global:{month}, KEYS[2..N]=user-зеркала; ARGV[i] — ожидаемая сумма
-- ключа KEYS[i] (выровнены попарно). Возврат: список дельт (tostring).
local deltas = {}
for i = 1, #KEYS do
  local total = tonumber(ARGV[i])
  local cur = tonumber(redis.call('GET', KEYS[i]) or '0')
  local d = total - cur
  if d ~= 0 then
    redis.call('INCRBY', KEYS[i], d)
  end
  table.insert(deltas, tostring(d))
end
return deltas
