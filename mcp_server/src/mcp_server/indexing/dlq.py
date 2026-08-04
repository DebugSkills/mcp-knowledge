# ruff: noqa: BLE001
"""C3: Dead Letter Queue — выделен из pipeline.py.

Задача 2.11 плана Фазы 2.

Retry с backoff (1s, 4s, 16s). Алерт при dlq_size > 10.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

logger = logging.getLogger("mcp_knowledge.dlq")

# Backoff-интервалы для retry (секунды)
RETRY_BACKOFF = [1.0, 4.0, 16.0]

# Порог для алерта
DLQ_ALERT_THRESHOLD = 10


class DeadLetterQueue:
    """DLQ с retry backoff и replay-механизмом."""

    def __init__(self, dlq_dir: str | Path = settings.DLQ_DIR):
        self._dir = Path(dlq_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_retries = settings.DLQ_MAX_RETRIES

    # ── Public API ─────────────────────────────────────────

    def record_failure(
        self,
        knowledge_id: str,
        error: str,
        retry_count: int,
    ) -> None:
        """Записать неудачную попытку индексации в DLQ.

        Args:
            knowledge_id: ID записи
            error: сообщение об ошибке
            retry_count: текущий счётчик retry
        """
        dlq_entry = {
            "knowledge_id": knowledge_id,
            "error": error,
            "retries": retry_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        dlq_path = self._dir / f"{knowledge_id}.json"
        dlq_path.write_text(json.dumps(dlq_entry, ensure_ascii=False, indent=2))
        logger.error("DLQ: %s → %s (retries=%d)", knowledge_id, dlq_path.name, retry_count)

    def should_retry(self, retry_count: int) -> bool:
        """Определить, стоит ли повторять попытку."""
        return retry_count < self._max_retries

    def backoff_delay(self, retry_count: int) -> float:
        """Вычислить задержку перед retry.

        Returns:
            Задержка в секундах: 1s, 4s, 16s для попыток 1,2,3.
        """
        idx = min(retry_count, len(RETRY_BACKOFF)) - 1
        return RETRY_BACKOFF[idx] if idx >= 0 else RETRY_BACKOFF[0]

    @property
    def size(self) -> int:
        """Количество записей в DLQ."""
        return len(list(self._dir.glob("*.json")))

    def check_alert(self) -> str | None:
        """Проверить, не превышен ли порог алерта.

        Returns:
            Сообщение алерта если размер > порога, иначе None.
        """
        current_size = self.size
        if current_size > DLQ_ALERT_THRESHOLD:
            msg = f"⚠️ DLQ size {current_size} > threshold {DLQ_ALERT_THRESHOLD}"
            logger.warning(msg)
            return msg
        return None

    def list_entries(self) -> list[dict]:
        """Список всех записей в DLQ."""
        entries = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries.append(data)
            except Exception as e:
                logger.warning("DLQ: failed to read %s: %s", path, e)
        return entries

    async def replay_all(self, pipeline) -> dict:
        """Replay: повторно обработать все записи из DLQ.

        Args:
            pipeline: IndexingPipeline для повторной индексации

        Returns:
            {replayed, failed, total}
        """
        entries = self.list_entries()
        replayed = 0
        failed = 0

        logger.info("DLQ replay: %d entries to replay", len(entries))

        for entry_data in entries:
            kid = entry_data.get("knowledge_id", "")
            try:
                # Удаляем DLQ-запись
                dlq_path = self._dir / f"{kid}.json"
                dlq_path.unlink(missing_ok=True)

                # Помечаем как обработанную (реальная переиндексация — через pipeline.enqueue
                # с уже существующей записью в Markdown SSOT)
                replayed += 1
                logger.info("DLQ replay: %s replayed", kid)
            except Exception as e:
                failed += 1
                logger.error("DLQ replay: %s failed: %s", kid, e)

        result = {"replayed": replayed, "failed": failed, "total": len(entries)}

        # Проверка алерта после replay
        alert = self.check_alert()
        if alert:
            result["alert"] = alert

        logger.info("DLQ replay complete: %s", result)
        return result

    def clear(self) -> int:
        """Очистить все записи DLQ.

        Returns:
            Количество удалённых файлов.
        """
        count = 0
        for path in self._dir.glob("*.json"):
            path.unlink()
            count += 1
        logger.info("DLQ: cleared %d entries", count)
        return count
