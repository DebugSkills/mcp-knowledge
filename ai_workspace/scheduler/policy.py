"""Чистая политика планировщика WFQ 3×3 + aging (Ф3.2) — БЕЗ Redis.

Спека Scheduler §3 (plans/_provenance/arch-2026-10-05-ai-workspace/
…-scheduler-spec.md, строки 37-60): ``w(p,c) = base[c]*mult[p]``; T_starve —
aging-ПОЛ: вызов с истёкшим ``starve_deadline`` получает АБСОЛЮТНОЕ право
(инвариант I2 плана: просроченный из ws:starve побеждает любого). Функции
детерминированы и тестируются offline; ``queue.lua`` реализует то же правило
атомарно (parity — tests/test_queue_lua.py).

Уточнение спеки (§6, риски 8/10): линейный ``aged = vft - alpha*wait`` не
даёт порядка, который нельзя выразить полом, но усложняет parity Lua↔Python;
пол применяем как «сначала все просроченные (FIFO по дедлайну), затем
argmin(vft)» — эквивалент ``aged -> -inf`` при ``wait > T_starve``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BASE: dict[str, float] = {"interactive": 100.0, "batch": 10.0, "background": 1.0}
"""Базовые веса 3 классов вызова (спека §3)."""

MULT: dict[str, float] = {"high": 4.0, "med": 2.0, "low": 1.0}
"""Множители 3 приоритетов аккаунта (спека §3)."""

T_STARVE: dict[str, float] = {
    "interactive": 60.0,
    "batch": 30 * 60.0,
    "background": 2 * 3600.0,
}
"""Aging-пол по классу вызова, секунды: 60s / 30m / 2h (спека §3)."""

STARVE_FLOOR_OFFSET = 1e18
"""Сдвиг в минус для aged-score просроченных: результат строго меньше любого
реального vft (vft << 1e18), порядок внутри просроченных — по дедлайну."""


def weight(prio: str, call_class: str) -> float:
    """w(p,c) = base[c] * mult[p]. Неизвестный класс/приоритет → KeyError
    (fail-closed: молчаливый дефолт исказил бы матрицу 3×3)."""
    return BASE[call_class] * MULT[prio]


def select_order(call: str, now: float, vft: float, starve_deadline: float) -> float:
    """ИНДИКАТОРНЫЙ aged-score (метрики/визуализация): меньше — раньше.

    - ``starve_deadline <= now`` (просрочен, пол I2):
      ``starve_deadline - STARVE_FLOOR_OFFSET`` — ниже любого реального
      vft (пока vft < 1e18);
    - иначе: ``vft`` (классический WFQ).

    ДЛЯ ТОЧНОГО СРАВНЕНИЯ НЕ ИСПОЛЬЗОВАТЬ: один double не может одновременно
    нести «пол ниже любого vft» и точный порядок дедлайнов — при смещении
    1e18 разрешение сглаживается (ulp ≈ 128 c) и FIFO по дедлайну ломается
    (найдено parity-тестом Ф3.2). Авторитетный порядок дают ``pick_best``
    (двухуровневый ключ) и queue.lua (сырые dl). ``call`` — в подписи
    контракта (идентификация), на значение не влияет.
    """
    if starve_deadline <= now:
        return starve_deadline - STARVE_FLOOR_OFFSET
    return vft


def pick_best(candidates: list[Mapping[str, Any]], now: float) -> str:
    """Лучший кандидат — ТОЧНОЕ правило (parity с queue.lua/dequeue):

    - просроченные (``starve_deadline <= now``) раньше остальных, среди них
      FIFO по дедлайну (пол I2 — абсолютное право);
    - иначе argmin vft;
    - тай-брейк — лекс. имя (как ``table.sort`` в queue.lua).

    Реализован двухуровневым ключом, а НЕ argmin select_order: один float
    сглаживает близкие дедлайны (ulp(1e18) ≈ 128 c) и ломает FIFO.
    ``candidates``: ``[{call, vft, class, prio, starve_deadline}]``;
    ``class``/``prio`` — контекст вызывающего, правило их не использует.
    """

    def key(c: Mapping[str, Any]) -> tuple[int, float, str]:
        if c["starve_deadline"] <= now:
            return (0, c["starve_deadline"], c["call"])
        return (1, c["vft"], c["call"])

    return min(candidates, key=key)["call"]


def virtual_finish(now_vt: float, last_vft: float, w: float, cost_est: float) -> float:
    """vft при enqueue (спека §3): ``max(V, last) + cost_est/w`` — вставка без
    гонки: если виртуальные часы убежали вперёд, вызов очереди не прыгает
    перед своими же предыдущими вызовами."""
    return max(now_vt, last_vft) + cost_est / w
