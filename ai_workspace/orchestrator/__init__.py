"""orchestrator — Mode engine, граф режима, ledger и job-store AI-верстака (Ф3.1+).

Ф3.1: ``job`` — durable job-store (``ws:job:{id}``): статус-машина job'а, CAS по
version (одна Lua-операция), epoch-fencing, effect_id-идемпотентность.
Ф3.5a: ``mode_schema``/``mode_lint`` — схемная и рантайм-валидация режимов.
Ф3.5b-1: ``board`` — версионируемая доска job'а (CAS v→v+1, single-writer секций).
Ф3.5b-2: ``graph`` — граф режима; ``ledger`` — эффекты (I4) и resume-токены;
``engine`` — исполнение графа (llm/tool/critic/human-gate, курсор, pause/resume).
"""
