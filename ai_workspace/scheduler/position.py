"""Позиции вызовов в очереди полки (Ф4.4a): материализация рангов для UI.

UI читает ГОТОВЫЕ скаляры — ``ws:pos:{call}`` (ранг) и ``ws:posq:{shelf}``
(глубина): правило порядка НЕ дублируется на клиенте (единственный источник
истины порядка — ``policy.pick_best`` / ``queue.lua``; здесь — его проекция
в ранги для отображения).

Источники — те же индексы, что у планировщика (структура как в queue.lua
@enqueue): ``ws:q:{shelf}`` (ZSET, score = raw VFT) и ``ws:starve:{shelf}``
(ZSET, score = starve_deadline). Per-call HASH ``ws:call:{shelf}:{call}``
читателю ранга НЕ нужен — оба поля ключа сортировки уже в ZSET (HASH —
источник REQUEUE, ``update_pos`` его не трогает). Член ``ws:q`` без члена
``ws:starve`` — «под-снятый» вызов: aging-права НЕТ (fail-safe к чистому
WFQ — то же правило, что в queue.lua/dequeue).

Порядок рангов БИТ-В-БИТ = ``policy.pick_best`` — тот же двухуровневый
ключ (просроченные ``dl <= now`` первыми, FIFO по дедлайну; иначе argmin
vft; тай-брейк — лекс. имя). ``rank_positions`` — ОДНА сортировка этим
ключом, НЕ pick_best в цикле (O(n log n) вместо O(n^2)).

Best-effort / display-only: ``update_pos`` в проде дергается хуком
``Queue.on_queue_change`` (Ф4.4a); сбой панели не ломает планирование —
исключения хука глотаются в queue.py, здесь ошибки честные наружу.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_MAX_DETAIL",
    "PositionStore",
    "pos_key",
    "posidx_key",
    "posq_key",
    "rank_positions",
]

logger = logging.getLogger(__name__)

DEFAULT_MAX_DETAIL = 2000
"""Порог детализации (защита от O(depth) записи на вызов хука): глубина
свыше него — пишется только ``ws:posq``, пер-вызовные ранги не
материализуются (большая очередь — общая глубина без N ключей)."""


def pos_key(call: str) -> str:
    """``ws:pos:{call}`` — ранг вызова, 1-based (меньше = раньше)."""
    return f"ws:pos:{call}"


def posq_key(shelf: str) -> str:
    """``ws:posq:{shelf}`` — глубина очереди полки (== ZCARD ws:q)."""
    return f"ws:posq:{shelf}"


def posidx_key(shelf: str) -> str:
    """``ws:posidx:{shelf}`` — SET call-ов с написанным рангом (diff-DEL)."""
    return f"ws:posidx:{shelf}"


def _order_key(now: float) -> Callable[[Mapping[str, Any]], tuple[int, float, str]]:
    """Двухуровневый ключ ТОТ ЖЕ, что ``policy.pick_best`` (parity-тесты
    test_position.py). ``starve_deadline is None`` (член q без starve) —
    просрочка НЕ признаётся: чистый WFQ (fail-safe queue.lua/dequeue)."""

    def key(c: Mapping[str, Any]) -> tuple[int, float, str]:
        dl = c.get("starve_deadline")
        if dl is not None and dl <= now:
            return (0, float(dl), c["call"])
        return (1, float(c["vft"]), c["call"])

    return key


def rank_positions(
    candidates: list[Mapping[str, Any]], now: float
) -> dict[str, int]:
    """Ранги кандидатов, 1-based (меньше = раньше).

    ``candidates``: ``[{call, vft, starve_deadline}]`` (тот же контракт,
    что у ``policy.pick_best``; ``starve_deadline`` может быть ``None``).
    Возвращает ``{call: ранг}``; при дубле call в списке побеждает
    последний (dict-семантика; в ZSET полки дублей не бывает).
    """
    ordered = sorted(candidates, key=_order_key(now))
    return {str(c["call"]): i for i, c in enumerate(ordered, start=1)}


class PositionStore:
    """Пишатель/читатель рангов панели очереди на ws-redis (Ф4.4a).

    ``update_pos`` — проекция порядка планировщика в скаляры: читает
    q+starve одним pipeline, считает ранги ``rank_positions``, пишет
    ``ws:pos:{call}`` + ``ws:posq:{shelf}``, гасит устаревшие ранги через
    diff по ``ws:posidx`` (вызов ушёл из очереди -> DEL его ранга: панель
    не показывает снятого как ждущего). Идемпотентен.
    """

    def __init__(self, client: Any, *, clock: Callable[[], float] = time.time) -> None:
        """``client`` — ws-redis (decode_responses=True); ``clock`` — «сейчас»
        (инъекция для детерминированных тестов, паттерн Queue)."""
        self.client = client
        self.clock = clock

    def update_pos(
        self,
        shelf: str,
        *,
        now: float | None = None,
        max_detail: int = DEFAULT_MAX_DETAIL,
    ) -> int:
        """Пересчитать ранги полки; возвращает глубину очереди.

        Стоимость: O(depth) чтение (2×ZRANGE + SMEMBERS одним pipeline) +
        O(depth) запись на вызов — хук срабатывает на КАЖДОЕ изменение
        очереди; ``max_detail`` ограничивает запись детальных рангов
        (глубже порога — только ``ws:posq`` + warning; прежде написанные
        детальные ключи гасятся, чтобы панель не показывала устаревшие
        ранги). Идемпотентен: повторный вызов без изменений очереди
        переписывает те же значения.
        """
        now = self.clock() if now is None else now
        pipe = self.client.pipeline()
        pipe.zrange(f"ws:q:{shelf}", 0, -1, withscores=True)
        pipe.zrange(f"ws:starve:{shelf}", 0, -1, withscores=True)
        pipe.smembers(posidx_key(shelf))
        q_rows, starve_rows, prev_members = pipe.execute()

        deadlines = {call: float(dl) for call, dl in starve_rows}
        candidates = [
            {
                "call": call,
                "vft": float(vft),
                "starve_deadline": deadlines.get(call),  # None -> без aging
            }
            for call, vft in q_rows
        ]
        ranks = rank_positions(candidates, now)
        depth = len(candidates)

        detailed = depth <= max_detail
        if not detailed:
            logger.warning(
                "update_pos(%s): depth=%d > max_detail=%d — детальные ws:pos "
                "не пишутся, только ws:posq=%d",
                shelf, depth, max_detail, depth,
            )
        prev = set(prev_members)
        new_calls = set(ranks) if detailed else set()
        stale = prev - new_calls  # ушли из очереди ИЛИ детализация выключена

        pipe = self.client.pipeline()
        pipe.set(posq_key(shelf), depth)
        for call in sorted(stale):
            pipe.delete(pos_key(call))
        pipe.delete(posidx_key(shelf))
        if new_calls:
            pipe.sadd(posidx_key(shelf), *sorted(new_calls))
            for call in sorted(ranks):
                pipe.set(pos_key(call), ranks[call])
        pipe.execute()
        return depth

    def position_of(self, call: str) -> int | None:
        """Ранг вызова из кэша панели; ``None`` — ключа нет (вне очереди
        или детализация выключена). Свежесть определяется частотой
        ``update_pos`` (хук изменений очереди)."""
        raw = self.client.get(pos_key(call))
        return None if raw is None else int(raw)
