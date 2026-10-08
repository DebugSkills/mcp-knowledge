"""Структурное правило достаточности дельта-контекста (Ф6-a 6a.4 Ф1, вариант В3/в).

Стоп-сигнал Ф1.0 (``plans/_provenance/arch-2026-10-05-ai-workspace/
Ф6a-4-Ф1.0-hardcases.md``): слабая модель НЕ признаёт недостаточность дельты
(``escalation_rate = 0.00`` — вместо эскалации уверенный ложный PASS) ⇒
достаточность дельты решает **движок детерминированно**, без опроса модели.

Правило (чистая функция, ядро фазы):

    sufficient = referenced ⊆ changed

- ``changed`` — множество секций, изменённых между версией доски, которую узел
  видел в прошлый раз (``board.diff``), и текущей; ``None`` — предыдущего
  состояния нет (первый прогон);
- ``referenced`` — секции, к которым отсылает предыдущая критика; извлечение
  СТРУКТУРНОЕ (inputs узла-критика), консервативное; ``None`` — охват из
  вывода критики надёжно не извлекается.

Любая неопределённость — нет предыдущего состояния, неопределимый/пустой
охват критики — даёт ``full`` (fail-safe: полный контекст никогда не хуже).
Пустой ``referenced`` НЕ даёт «вакуумно истинного» delta: без критики сузить
контекст нельзя (проверка — тест «empty reference is full»).
"""

from __future__ import annotations

from typing import AbstractSet

__all__ = [
    "CONTEXT_DELTA",
    "CONTEXT_FULL",
    "CONTEXT_MODES",
    "DELTA_CONSUMER_KINDS",
    "decide_context_mode",
]

CONTEXT_FULL = "full"
"""Каноническое значение режима контекста «полный» (поведение по умолчанию)."""

CONTEXT_DELTA = "delta"
"""Опт-ин «дельта»: изменённые секции + предыдущая критика, если достаточна."""

CONTEXT_MODES: frozenset[str] = frozenset({CONTEXT_FULL, CONTEXT_DELTA})
"""Допустимые значения поля ``nodes[].context`` (схемная проверка S9)."""

DELTA_CONSUMER_KINDS: frozenset[str] = frozenset({"llm-step", "tool-step"})
"""Kind-ы, которым разрешён ``context: delta`` (линт L15 + движок).

``critic-gate`` защищён protected-принципом: вердикт качества выносится на
ПОЛНОМ контексте решения (стоп-сигнал Ф1.0 — дельта на гейте даёт ложный
PASS). ``human-gate`` промпт из секций не собирает.
"""


def decide_context_mode(
    changed: AbstractSet[str] | None,
    referenced: AbstractSet[str] | None,
) -> str:
    """Достаточна ли дельта: ``referenced ⊆ changed`` → ``delta``, иначе ``full``.

    Fail-safe по каждой неопределённости:

    - ``changed is None`` — предыдущего состояния доски нет (первый прогон
      узла) → ``full`` (0 изменений — сужать не от чего);
    - ``referenced is None`` — охват критики структурно не извлекается
      (правка человека, критик без объявленных inputs, неоднозначный писатель)
      → ``full``; догадок нет;
    - ``referenced`` пуст — критики нет → ``full`` (вакуумная истинность
      исключена: «referenced ⊆ changed» при ∅ всегда истинно, но дельта без
      критики не обоснована);
    - непустое пересечение вне ``changed`` → ``full``: критика касается
      секций, которых нет в дельта-виде — HD1-паттерн (дефект в неизменённой
      секции) ловится структурно, до модели.
    """
    if changed is None or not referenced:
        return CONTEXT_FULL
    return CONTEXT_DELTA if referenced <= changed else CONTEXT_FULL
