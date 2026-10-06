"""Unified GPU-контур AI-верстака (Ф3.8): K слотов на embed/vision/reindex.

Один общий контур: и верстак (эмбеддинги артефактов, vision), и mcp-knowledge
(reindex/embed) ходят через ОДНУ полку ``gpu`` — иначе два процесса переподпишут
VRAM (dev 8 ГБ → K=1; целевой прод 32 ГБ → K≥2).

Реализация — **переиспользование** семафора полок (Ф3.3, ``Scheduler/slots.lua``):
``Slots(client, shelf="gpu", k=K)`` даёт атомарные acquire/release/heartbeat/reclaim
+ события ``ws:events:gpu``. Здесь — только доменные имена видов работ, отказ-не-очередь
(I1: сверх K → ``GpuBusy``, ожидание жило бы в очереди, а GPU-работа ждать не умеет),
контекст-менеджер и ``reconcile`` (снятие мёртвых слотов + отчёт по видам).

Ключи: ``ws:slots:gpu`` (SET holders), ``ws:lease:gpu:{kind}:{ref}`` (TTL),
``ws:events:gpu`` (Stream). ``K`` — env ``WS_GPU_K`` (дефолт 1).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ai_workspace.redis_client import make_ws_redis
from ai_workspace.scheduler.slots import DEFAULT_LEASE_TTL_MS, Slots

__all__ = [
    "DEFAULT_GPU_K",
    "GPU_KINDS",
    "GPU_SHELF",
    "GpuBusy",
    "GpuContour",
    "GpuError",
    "env_gpu_k",
]

GPU_SHELF = "gpu"
"""Служебная полка GPU-контура (общая для верстака и mcp-knowledge)."""

GPU_KINDS: tuple[str, ...] = ("embed", "vision", "reindex")
"""Виды GPU-работ: эмбеддинги, vision-модель, переиндексация."""

DEFAULT_GPU_K = 1
"""Слотов по умолчанию (dev 8 ГБ: один GPU-резидент)."""

ENV_GPU_K = "WS_GPU_K"


class GpuError(RuntimeError):
    """Базовая ошибка GPU-контура."""


class GpuBusy(GpuError):
    """Свободных GPU-слотов нет (отказ-не-очередь, I1): повторить позже."""


def env_gpu_k() -> int:
    """K из env ``WS_GPU_K`` (некорректное/не задано → ``DEFAULT_GPU_K``)."""
    raw = os.environ.get(ENV_GPU_K, "").strip()
    if not raw:
        return DEFAULT_GPU_K
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_GPU_K


@dataclass(frozen=True)
class GpuStatus:
    """Снимок состояния контура (наблюдение/оператор)."""

    capacity: int
    used: int
    free: int
    lease_ms: int
    holdings: dict[str, list[str]] = field(default_factory=dict)


class GpuContour:
    """Единый GPU-контур: ``k`` слотов на виды ``embed``/``vision``/``reindex``."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        slots: Any | None = None,
        k: int | None = None,
        kinds: tuple[str, ...] = GPU_KINDS,
        lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.kinds = tuple(kinds)
        self.clock = clock
        resolved_k = env_gpu_k() if k is None else max(1, int(k))
        self.slots = slots or Slots(
            client or make_ws_redis(), shelf=GPU_SHELF, k=resolved_k,
            lease_ttl_ms=lease_ttl_ms, clock=clock,
        )
        self.k = int(getattr(self.slots, "k", resolved_k))
        self.lease_ttl_ms = int(getattr(self.slots, "lease_ttl_ms", lease_ttl_ms))

    # ── идентификаторы ───────────────────────────────────────────────────

    def call_id(self, kind: str, ref: str) -> str:
        """ID вызова в контуре: ``<kind>:<ref>`` (валидирует вид)."""
        self._check_kind(kind)
        if not ref:
            raise GpuError("ref (идентификатор работы) обязателен")
        return f"{kind}:{ref}"

    def kind_of(self, call: str) -> str:
        """Вид работы по ID вызова (``<kind>:<ref>`` → ``<kind>``)."""
        return str(call).split(":", 1)[0]

    def _check_kind(self, kind: str) -> None:
        if kind not in self.kinds:
            raise GpuError(f"неизвестный вид GPU-работы: {kind!r}; ожидается один из {list(self.kinds)}")

    # ── захват/освобождение ──────────────────────────────────────────────

    def try_acquire(self, kind: str, ref: str, *, job: str = "", epoch: int = 0) -> str | None:
        """Взять слот; ``None`` — контур занят (отказ-не-очередь, I1)."""
        call = self.call_id(kind, ref)
        return call if self.slots.acquire(call, job=job, epoch=epoch, state=kind) else None

    def acquire(self, kind: str, ref: str, *, job: str = "", epoch: int = 0) -> str:
        """Взять слот или бросить ``GpuBusy`` (никогда не ждём внутри GPU-работы)."""
        call = self.try_acquire(kind, ref, job=job, epoch=epoch)
        if call is None:
            raise GpuBusy(
                f"GPU-контур занят: {kind}:{ref} не получил слот "
                f"(K={self.k}, занято {self.slots.used()})"
            )
        return call

    def release(self, kind: str, ref: str) -> bool:
        """Освободить слот (идемпотентно: не держали → ``False``)."""
        return bool(self.slots.release(self.call_id(kind, ref)))

    def heartbeat(self, kind: str, ref: str) -> bool:
        """Продлить lease (воркер жив); ``False`` — lease потерян."""
        return bool(self.slots.heartbeat(self.call_id(kind, ref)))

    @contextmanager
    def lease(self, kind: str, ref: str, *, job: str = "", epoch: int = 0) -> Iterator[str]:
        """Контекст GPU-работы: захват → тело → гарантированное освобождение.

        ``GpuBusy`` поднимается ДО тела (никакой работы без слота). Освобождение —
        в ``finally``: исключение в теле не оставляет висящий слот.
        """
        call = self.acquire(kind, ref, job=job, epoch=epoch)
        try:
            yield call
        finally:
            self.release(kind, ref)

    # ── наблюдение и reconcile ───────────────────────────────────────────

    def status(self) -> GpuStatus:
        """Занято/свободно + раскладка держателей по видам работ."""
        holdings: dict[str, list[str]] = {kind: [] for kind in self.kinds}
        for call in sorted(self._holders()):
            kind = self.kind_of(call)
            holdings.setdefault(kind, []).append(call.split(":", 1)[1])
        return GpuStatus(
            capacity=self.k,
            used=self.slots.used(),
            free=self.slots.free(),
            lease_ms=self.lease_ttl_ms,
            holdings=holdings,
        )

    def reconcile(self) -> dict[str, Any]:
        """Снять слоты с мёртвыми lease и отчитаться по видам работ.

        Вызывается по таймеру (cron/tick): упавший воркер не должен навсегда
        держать GPU. Возвращает ``{reclaimed: {kind: n}, status: GpuStatus}``.
        """
        reclaimed = {kind: 0 for kind in self.kinds}
        for call in self.slots.sweep_expired():
            kind = self.kind_of(call)
            reclaimed[kind] = reclaimed.get(kind, 0) + 1
        return {"reclaimed": reclaimed, "status": self.status()}

    def _holders(self) -> list[str]:
        holders_key = getattr(self.slots, "holders_key", f"ws:slots:{GPU_SHELF}")
        return [str(m) for m in self.slots.client.smembers(holders_key)]


def gpu_contour(*, k: int | None = None) -> GpuContour:
    """Контур на ws-redis (env ``WS_REDIS_URL``; K — ``WS_GPU_K``)."""
    return GpuContour(k=k)
