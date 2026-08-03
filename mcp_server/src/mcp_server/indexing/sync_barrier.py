"""C2: Sync-флаг барьер (CPU-aware) — выделен из pipeline.py.

Задача 2.10 плана Фазы 2.

CPU-aware: при backend=cpu и >20 чанков → сразу pending:true + приоритет.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import settings
from ..models import WriteResult

logger = logging.getLogger("mcp_knowledge.sync_barrier")

# Порог для CPU-aware: больше этого количества чанков → pending сразу
CPU_CHUNK_THRESHOLD = 20
SYNC_TIMEOUT = 30.0  # секунд


class SyncBarrier:
    """Барьер синхронизации для wait_for_index.

    Позволяет дождаться завершения индексации конкретного knowledge_id.
    CPU-aware: при EMBEDDING_BACKEND=cpu и >CPU_CHUNK_THRESHOLD чанков →
    сразу возвращает pending:true.
    """

    def __init__(self):
        self._events: dict[str, asyncio.Event] = {}

    def register(self, knowledge_id: str) -> asyncio.Event:
        """Зарегистрировать knowledge_id для ожидания.

        Returns:
            asyncio.Event, который будет установлен при завершении индексации.
        """
        event = asyncio.Event()
        self._events[knowledge_id] = event
        logger.debug("SyncBarrier: registered %s", knowledge_id)
        return event

    def signal(self, knowledge_id: str) -> None:
        """Сигнализировать о завершении индексации knowledge_id."""
        event = self._events.pop(knowledge_id, None)
        if event:
            event.set()
            logger.debug("SyncBarrier: signalled %s", knowledge_id)

    def signal_batch(self, knowledge_ids: list[str]) -> None:
        """Сигнализировать о завершении батча."""
        for kid in knowledge_ids:
            self.signal(kid)

    async def wait(self, knowledge_id: str, timeout: float = SYNC_TIMEOUT) -> WriteResult:
        """Ждать завершения индексации knowledge_id.

        Args:
            knowledge_id: ID записи
            timeout: таймаут ожидания (default 30s)

        Returns:
            WriteResult с indexed/pending статусом
        """
        event = self._events.get(knowledge_id)
        result = WriteResult(
            knowledge_id=knowledge_id,
            indexed=False,
            pending=True,
        )

        if event is None:
            logger.warning("SyncBarrier: no event for %s — returning pending", knowledge_id)
            return result

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            result.indexed = True
            result.pending = False
            logger.debug("SyncBarrier: %s indexed (waited)", knowledge_id)
        except asyncio.TimeoutError:
            logger.warning(
                "SyncBarrier: timeout for %s after %.1fs (CPU backend?)",
                knowledge_id, timeout,
            )
            result.pending = True
        finally:
            self._events.pop(knowledge_id, None)

        return result

    def is_cpu_bound(self, chunk_count: int) -> bool:
        """CPU-aware: определить, стоит ли ждать или сразу вернуть pending.

        Returns:
            True если индексация займёт значительное время (CPU backend + много чанков).
        """
        is_cpu = settings.EMBEDDING_BACKEND == "cpu"
        return is_cpu and chunk_count > CPU_CHUNK_THRESHOLD

    @property
    def pending_count(self) -> int:
        """Количество ожидающих knowledge_id."""
        return len(self._events)
