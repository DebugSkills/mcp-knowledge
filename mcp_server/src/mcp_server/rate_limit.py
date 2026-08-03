"""E2: Token Bucket rate limiter — per-key, batch-aware (P1-5).

Фаза 3 E2 (v1.1): In-memory Token Bucket с batch-aware check().
Безопасен при WORKERS=1 инварианте (один процесс → один bucket map).
Batch-aware: JSON-RPC batch [A,B,C] → count=3 → тратит 3 токена, не 1.

Usage:
    limiter = TokenBucketLimiter(refill_rate=100.0/60.0, burst_size=10)
    if not await limiter.check(key_hash, count=len(batch_methods)):
        raise RateLimitedError  # → MCP_RATE_LIMITED (-32003)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("mcp_knowledge.rate_limit")

# LRU eviction: удаляем ключи при превышении
MAX_BUCKET_KEYS = 5000


class RateLimitedError(Exception):
    """Rate limit exceeded — должен вернуть MCP_RATE_LIMITED (-32003)."""
    pass


class TokenBucketLimiter:
    """In-memory Token Bucket rate limiter (per-key).

    Каждый key_hash получает свой bucket с burst_size начальных токенов.
    Токены пополняются с refill_rate токенов/сек, ограничены burst_size.

    Attributes:
        refill_rate: токенов в секунду
        burst_size: максимальное число токенов в bucket
        stats: {"allowed": int, "denied": int} — счётчики
    """

    def __init__(self, refill_rate: float, burst_size: int):
        if burst_size < 0:
            raise ValueError(f"burst_size must be >= 0, got {burst_size}")

        self.refill_rate = refill_rate
        self.burst_size = burst_size

        # bucket: key_hash → (tokens: float, last_refill_ts: float)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

        # Stats
        self.stats: dict[str, int] = {"allowed": 0, "denied": 0}

    async def check(self, key_hash: str, count: int = 1) -> bool:
        """Проверить, может ли key_hash потребить `count` токенов.

        Batch-aware: для JSON-RPC batch из 5 методов → count=5 → тратит 5 токенов.

        Args:
            key_hash: идентификатор ключа (sha256 хеш)
            count: число токенов для потребления (≥1)

        Returns:
            True если запрос разрешён, False если лимит превышен.
        """
        if self.burst_size == 0:
            self.stats["denied"] += 1
            return False

        async with self._lock:
            now = time.monotonic()

            # Получить или инициализировать bucket
            tokens, last_ts = self._buckets.get(key_hash, (float(self.burst_size), now))

            # Refill: вычислить токены за прошедшее время
            elapsed = now - last_ts
            if elapsed > 0 and self.refill_rate > 0:
                tokens = min(float(self.burst_size), tokens + elapsed * self.refill_rate)

            # Проверка
            if tokens >= count:
                tokens -= count
                self._buckets[key_hash] = (tokens, now)
                self._maybe_evict()
                self.stats["allowed"] += 1
                return True

            # Недостаточно токенов
            self._buckets[key_hash] = (tokens, now)
            self._stats_denied_for_log(key_hash, count, tokens)
            self.stats["denied"] += 1
            return False

    def _stats_denied_for_log(self, key_hash: str, requested: int, available: float) -> None:
        """Log denied request (rate-limited)."""
        logger.warning(
            "Rate limit exceeded: key=%s requested=%d tokens, available=%.1f tokens "
            "(stats: allowed=%d denied=%d)",
            key_hash[:12], requested, available,
            self.stats["allowed"], self.stats["denied"],
        )

    def _maybe_evict(self) -> None:
        """LRU eviction: если bucket'ов > MAX_BUCKET_KEYS, удалить самые старые.

        WORKERS=1 + ~10 ключей на практике → eviction почти никогда не срабатывает.
        Но защита от unbounded growth (P2-2).
        """
        if len(self._buckets) > MAX_BUCKET_KEYS:
            # Удаляем 10% самых старых ключей (по порядку вставки)
            excess = len(self._buckets) - MAX_BUCKET_KEYS
            to_remove = max(excess, int(MAX_BUCKET_KEYS * 0.1))
            keys_to_remove = list(self._buckets.keys())[:to_remove]
            for key in keys_to_remove:
                del self._buckets[key]
            logger.warning(
                "Rate limiter LRU eviction: removed %d keys (total was %d, now %d)",
                to_remove, len(self._buckets) + to_remove, len(self._buckets),
            )
