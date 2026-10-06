"""Тесты unified GPU-контура (Ф3.8): K-слоты, отказ-не-очередь, reconcile.

Offline — FakeSlots (семантика семафора воспроизведена); integration — живой
ws-redis: K=1 держит один вид работ, второй получает ``GpuBusy``; ``reconcile``
снимает слот с мёртвым lease.
"""

from __future__ import annotations

import pytest

from ai_workspace.gpu import (
    DEFAULT_GPU_K,
    ENV_GPU_K,
    GPU_KINDS,
    GPU_SHELF,
    GpuBusy,
    GpuContour,
    GpuError,
    env_gpu_k,
)
from ai_workspace.tests.conftest import requires_redis


class FakeSlots:
    """Семафор K в памяти: holders SET + lease с TTL по часам."""

    def __init__(self, k: int = 1, clock=None) -> None:
        self.k = k
        self.lease_ttl_ms = 90_000
        self.holders_key = f"ws:slots:{GPU_SHELF}"
        self.clock = clock or (lambda: 1_700_000_000.0)
        self.holders: set[str] = set()
        self.leases: dict[str, float] = {}
        self.client = self  # для _holders: smembers

    # API, который использует GpuContour
    def smembers(self, key: str) -> set[str]:
        return set(self.holders)

    def acquire(self, call: str, *, job="", epoch=0, state="") -> bool:
        if len(self.holders) >= self.k:
            return False
        self.holders.add(call)
        self.leases[call] = self.clock() + self.lease_ttl_ms / 1000
        return True

    def release(self, call: str) -> bool:
        if call not in self.holders:
            return False
        self.holders.discard(call)
        self.leases.pop(call, None)
        return True

    def heartbeat(self, call: str) -> bool:
        if call not in self.leases:
            return False
        self.leases[call] = self.clock() + self.lease_ttl_ms / 1000
        return True

    def sweep_expired(self) -> list[str]:
        dead = [c for c in sorted(self.holders) if self.leases.get(c, 0) <= self.clock()]
        for call in dead:
            self.holders.discard(call)
            self.leases.pop(call, None)
        return dead

    def used(self) -> int:
        return len(self.holders)

    def free(self) -> int:
        return max(0, self.k - len(self.holders))


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _contour(k: int = 1) -> tuple[GpuContour, FakeSlots]:
    clock = Clock()
    slots = FakeSlots(k=k, clock=clock)
    return GpuContour(slots=slots, k=k, clock=clock), slots


# ── offline ──────────────────────────────────────────────────────────────


def test_env_gpu_k_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_GPU_K, raising=False)
    assert env_gpu_k() == DEFAULT_GPU_K
    monkeypatch.setenv(ENV_GPU_K, "3")
    assert env_gpu_k() == 3
    monkeypatch.setenv(ENV_GPU_K, "мусор")
    assert env_gpu_k() == DEFAULT_GPU_K
    monkeypatch.setenv(ENV_GPU_K, "0")
    assert env_gpu_k() == 1


def test_unknown_kind_is_rejected() -> None:
    contour, _ = _contour()
    with pytest.raises(GpuError):
        contour.acquire("training", "job-1")
    with pytest.raises(GpuError):
        contour.call_id("ffmpeg", "x")


def test_capacity_is_shared_across_kinds() -> None:
    """K общий для embed/vision/reindex: контур один, а не три независимых."""
    contour, _ = _contour(k=1)
    assert contour.acquire("embed", "a") == "embed:a"

    with pytest.raises(GpuBusy):
        contour.acquire("vision", "b")  # другой вид — тот же контур, слотов нет

    contour.release("embed", "a")
    assert contour.acquire("vision", "b") == "vision:b"


def test_try_acquire_returns_none_instead_of_raising() -> None:
    contour, _ = _contour(k=1)
    assert contour.try_acquire("reindex", "r1") == "reindex:r1"
    assert contour.try_acquire("embed", "e1") is None  # отказ-не-очередь
    contour.release("reindex", "r1")
    assert contour.try_acquire("embed", "e1") == "embed:e1"


def test_lease_context_manager_releases_on_error() -> None:
    contour, _ = _contour(k=1)
    with pytest.raises(RuntimeError), contour.lease("embed", "a"):
        raise RuntimeError("GPU-работа упала")
    assert contour.status().used == 0  # finally освободил слот


def test_status_reports_holdings_by_kind() -> None:
    contour, _ = _contour(k=2)
    contour.acquire("embed", "a")
    contour.acquire("vision", "b")
    status = contour.status()

    assert status.capacity == 2 and status.used == 2 and status.free == 0
    assert status.holdings["embed"] == ["a"] and status.holdings["vision"] == ["b"]
    assert status.holdings["reindex"] == []
    assert status.lease_ms == 90_000


def test_reconcile_reclaims_dead_lease_and_reports_by_kind() -> None:
    clock = Clock()
    slots = FakeSlots(k=2, clock=clock)
    contour = GpuContour(slots=slots, k=2, clock=clock)
    contour.acquire("embed", "alive")
    contour.acquire("vision", "dead")

    clock.now += 91  # оба lease истекли; «alive» продлим heartbeat'ом
    contour.heartbeat("embed", "alive")
    report = contour.reconcile()

    assert report["reclaimed"] == {"embed": 0, "vision": 1, "reindex": 0}
    assert "vision:dead" not in slots.holders and "embed:alive" in slots.holders
    assert report["status"].used == 1


def test_heartbeat_and_release_of_foreign_call() -> None:
    contour, slots = _contour(k=1)
    assert contour.heartbeat("embed", "нет-такого") is False
    assert contour.release("embed", "нет-такого") is False
    assert slots.used() == 0


def test_kinds_and_call_id_roundtrip() -> None:
    contour, _ = _contour()
    for kind in GPU_KINDS:
        call = contour.call_id(kind, "ref-1")
        assert call == f"{kind}:ref-1" and contour.kind_of(call) == kind


# ── integration: живой ws-redis ──────────────────────────────────────────


@pytest.mark.integration
@requires_redis
def test_integration_gpu_contour_on_redis() -> None:
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    client.delete(f"ws:slots:{GPU_SHELF}", f"ws:events:{GPU_SHELF}")
    for key in client.scan_iter(match=f"ws:lease:{GPU_SHELF}:*"):
        client.delete(key)

    contour = GpuContour(client=client, k=1, lease_ttl_ms=60_000)
    assert contour.status().capacity == 1

    assert contour.acquire("embed", "it-1") == "embed:it-1"
    with pytest.raises(GpuBusy):
        contour.acquire("vision", "it-2")
    assert contour.heartbeat("embed", "it-1") is True
    assert contour.release("embed", "it-1") is True
    assert contour.acquire("vision", "it-2") == "vision:it-2"

    # reconcile: слот с удалённым lease (имитация упавшего воркера) снимается
    client.delete(f"ws:lease:{GPU_SHELF}:vision:it-2")
    report = contour.reconcile()
    assert report["reclaimed"]["vision"] == 1
    assert contour.status().used == 0

    entries = client.xrange(f"ws:events:{GPU_SHELF}")
    payload = " ".join(str(value) for _, fields in entries for value in fields.values())
    assert entries and "acquired" in payload and "released" in payload and "lease_expired" in payload

    client.delete(f"ws:slots:{GPU_SHELF}", f"ws:events:{GPU_SHELF}")
    for key in client.scan_iter(match=f"ws:lease:{GPU_SHELF}:*"):
        client.delete(key)
