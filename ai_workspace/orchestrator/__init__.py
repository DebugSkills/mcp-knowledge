"""orchestrator — Mode engine и job-store AI-верстака (Ф3.1+).

Ф3.1: ``job`` — durable job-store (``ws:job:{id}``): статус-машина job'а,
CAS по version (одна Lua-операция), epoch-fencing, effect_id-идемпотентность.
Queue.lua/семафоры/engine-loop — Ф3.2+.
"""
