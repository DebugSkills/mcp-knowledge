"""E2: TokenBucketLimiter tests — rate limiting with batch awareness.

Tests:
- Initial tokens: can consume up to burst in first check
- Refill: tokens regenerate over time
- Exceed limit: returns False when bucket empty
- Batch count: consume N tokens for N JSON-RPC methods
- Multiple keys: independent buckets
- Zero burst: all checks fail (rate limiting disabled)
"""
from __future__ import annotations

import asyncio

import pytest
from mcp_server.rate_limit import TokenBucketLimiter

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def limiter_read() -> TokenBucketLimiter:
    """Read key rate limiter: 100/min ≈ 1.67 tokens/sec, burst 10."""
    return TokenBucketLimiter(refill_rate=100.0 / 60.0, burst_size=10)


@pytest.fixture
def limiter_write() -> TokenBucketLimiter:
    """Write key rate limiter: 20/min ≈ 0.33 tokens/sec, burst 5."""
    return TokenBucketLimiter(refill_rate=20.0 / 60.0, burst_size=5)


# ── Basic token consumption ───────────────────────────────────


class TestTokenBucketBasic:
    """Basic token bucket operations."""

    async def test_initial_burst_can_consume_full_burst(self, limiter_read):
        """First N checks (up to burst) should all pass."""
        for i in range(10):
            assert await limiter_read.check("key-a") is True, f"Burst token {i} failed"

    async def test_exceed_burst_fails(self, limiter_read):
        """After consuming all burst tokens, next check fails."""
        for _ in range(10):
            await limiter_read.check("key-a")
        assert await limiter_read.check("key-a") is False

    async def test_single_token_after_burst_fails(self, limiter_read):
        """Burst exhausted → single token fails."""
        for _ in range(10):
            assert await limiter_read.check("key-a")
        assert await limiter_read.check("key-a") is False

    async def test_zero_burst_always_fails(self):
        """burst_size=0 → no tokens ever available."""
        limiter = TokenBucketLimiter(refill_rate=1.0, burst_size=0)
        assert await limiter.check("key") is False

    async def test_negative_refill_rate_is_valid(self):
        """Negative refill rate is fine — token math may underflow, but shouldn't crash."""
        limiter = TokenBucketLimiter(refill_rate=-1.0, burst_size=10)
        result = await limiter.check("key")
        assert isinstance(result, bool)


# ── Refill behavior ────────────────────────────────────────────


class TestTokenBucketRefill:
    """Token refill over time."""

    async def test_refill_after_wait(self, limiter_read):
        """After exhausting burst, waiting allows new tokens."""
        # Exhaust burst
        for _ in range(10):
            await limiter_read.check("key-a")
        assert await limiter_read.check("key-a") is False

        # Wait for ~1 token (refill_rate ≈ 1.67 tok/s → 1 token ≈ 0.6s, use 0.7 for safety)
        await asyncio.sleep(0.7)
        assert await limiter_read.check("key-a") is True

    async def test_partial_token_not_enough_for_check(self, limiter_read):
        """If < 1 token refilled, check still fails."""
        for _ in range(10):
            await limiter_read.check("key-a")

        # Wait too short — < 1 full token refilled
        await asyncio.sleep(0.05)
        assert await limiter_read.check("key-a") is False

    async def test_refill_capped_at_burst(self, limiter_read):
        """Refilling beyond burst_size is capped."""
        for _ in range(10):
            await limiter_read.check("key-a")

        # Wait long enough that without cap, we'd have > burst tokens
        await asyncio.sleep(20.0)  # 20s × 1.67 tok/s = 33 tokens

        # Can only consume up to burst (10) in a burst, then no more
        for i in range(10):
            assert await limiter_read.check("key-a") is True, f"Refilled token {i} failed"
        assert await limiter_read.check("key-a") is False


# ── Batch-aware: count parameter ───────────────────────────────


class TestTokenBucketBatch:
    """Batch-aware: consume N tokens at once."""

    async def test_consume_multiple_tokens(self, limiter_read):
        """count=5 consumes 5 tokens from burst."""
        assert await limiter_read.check("key-a", count=5) is True
        assert await limiter_read.check("key-a", count=5) is True
        assert await limiter_read.check("key-a", count=1) is False

    async def test_count_exceeds_burst_fails(self, limiter_read):
        """count > burst_size fails immediately."""
        assert await limiter_read.check("key-a", count=11) is False

    async def test_batch_count_refill(self, limiter_read):
        """After partial batch, refill allows remaining."""
        # Consume all 10
        assert await limiter_read.check("key-a", count=10) is True
        assert await limiter_read.check("key-a", count=1) is False

        # Wait for ~2 tokens
        await asyncio.sleep(1.3)  # 1.3s × 1.67 ≈ 2.2 tokens

        # Can consume 2
        assert await limiter_read.check("key-a", count=2) is True
        assert await limiter_read.check("key-a", count=1) is False


# ── Multiple independent keys ──────────────────────────────────


class TestTokenBucketMultiKey:
    """Separate buckets per key_hash."""

    async def test_keys_have_independent_buckets(self, limiter_read):
        """Exhausting key-a does not affect key-b."""
        for _ in range(10):
            await limiter_read.check("key-a")
        assert await limiter_read.check("key-a") is False

        # key-b has its own full burst
        assert await limiter_read.check("key-b") is True

    async def test_multiple_keys_refill_independently(self, limiter_read):
        """Each key refills independently."""
        # Exhaust both
        for _ in range(10):
            await limiter_read.check("key-a")
            await limiter_read.check("key-b")

        assert await limiter_read.check("key-a") is False
        assert await limiter_read.check("key-b") is False

        # Wait — both refill
        await asyncio.sleep(0.7)
        assert await limiter_read.check("key-a") is True
        assert await limiter_read.check("key-b") is True


# ── Thread safety (asyncio.Lock) ──────────────────────────────


class TestTokenBucketConcurrency:
    """Async concurrent access safety."""

    async def test_concurrent_checks_dont_corrupt_state(self, limiter_read):
        """Multiple concurrent checks — each gets a consistent view."""
        # 5 concurrent coroutines, each trying 3 checks → 15 total tries
        # With burst 10, some should fail (last 5)
        async def worker(key: str) -> list[bool]:
            results = []
            for _ in range(3):
                results.append(await limiter_read.check(key))
                await asyncio.sleep(0.01)
            return results

        tasks = [worker("shared") for _ in range(5)]
        all_results_list = await asyncio.gather(*tasks)
        all_results = [r for sublist in all_results_list for r in sublist]

        # Out of 15 checks, exactly 10 should pass (burst 10) and 5 should fail
        passed = sum(all_results)
        assert passed == 10, f"Expected 10 passes, got {passed} (burst=10, 15 checks)"
        assert len(all_results) - passed == 5


# ── Stats ───────────────────────────────────────────────────────


class TestTokenBucketStats:
    """Stats tracking for monitoring."""

    async def test_stats_initial_zero(self):
        """Stats start at 0."""
        limiter = TokenBucketLimiter(refill_rate=1.0, burst_size=5)
        assert limiter.stats == {"allowed": 0, "denied": 0}

    async def test_stats_count_allowed(self, limiter_read):
        """Each successful check increments allowed."""
        await limiter_read.check("key-a")
        assert limiter_read.stats["allowed"] == 1

    async def test_stats_count_denied(self, limiter_read):
        """Each denied check increments denied."""
        for _ in range(11):  # burst 10, last one denied
            await limiter_read.check("key-a")
        assert limiter_read.stats["allowed"] == 10
        assert limiter_read.stats["denied"] == 1

    async def test_stats_batch_count(self, limiter_read):
        """Batch count maps to stats correctly."""
        await limiter_read.check("key-a", count=3)
        assert limiter_read.stats["allowed"] == 1  # 1 check, not 3 tokens
        assert limiter_read.stats["denied"] == 0
