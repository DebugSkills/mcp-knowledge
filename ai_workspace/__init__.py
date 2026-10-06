"""ai_workspace — control-plane AI-верстака сообщества (arch-2026-10-05-ai-workspace).

Будущее содержимое: Scheduler (Redis+Lua, WFQ+aging) и Mode engine.
Ф3.1 (эта фаза): каркас пакета + durable job-store — статус-машина job'а
(``ws:job:{id}``), CAS по version, epoch-fencing. См. ``orchestrator/job.py``
и ``README.md``.
"""

__version__ = "0.1.0"
