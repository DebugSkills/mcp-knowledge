"""Scheduler AI-верстака (Ф3.2): WFQ 3×3 (приоритет × класс) + aging-пол.

- ``policy`` — чистые функции выбора (offline-тестируемы);
- ``queue.Queue`` — обёртки атомарных Lua-скриптов ``queue.lua``
  (enqueue/dequeue/complete); dequeue снимает вызов из ДВУХ индексов
  (``ws:q`` + ``ws:starve``) одной Lua-операцией.
"""

from ai_workspace.scheduler.policy import (
    BASE,
    MULT,
    T_STARVE,
    pick_best,
    select_order,
    virtual_finish,
    weight,
)
from ai_workspace.scheduler.queue import Queue

__all__ = [
    "BASE",
    "MULT",
    "T_STARVE",
    "Queue",
    "pick_best",
    "select_order",
    "virtual_finish",
    "weight",
]
