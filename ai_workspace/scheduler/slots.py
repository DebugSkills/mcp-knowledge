"""Python-обёртка семафора K полки и событий Scheduler'а (Ф3.3).

Инварианты (план, I1/I3/I12):
- ``concurrency_into_litellm(shelf) <= k`` — ожидание живёт ТОЛЬКО в
  ``ws:q:{shelf}``; попытка сверх K = ``acquire() -> False`` (отказ, не очередь).
- Все мутации слота и ``XADD`` события — В ОДНОЙ Lua (``slots.lua``), чтобы
  не было «слот взят, событие потеряно» и наоборот.
- ``XADD MAXLEN ~ stream_maxlen`` (I12).

Ключи: ``ws:slots:{shelf}`` (SET holders), ``ws:lease:{shelf}:{call}`` (TTL),
``ws:events:{shelf}`` (Stream). Потребитель событий — Mode engine (Ф3.5):
``XGROUP CREATE``/``XREADGROUP``/``XACK``.

Тот же класс используется и для unified-полки ``gpu`` (Ф3.8) — ``shelf``
параметр, код общий.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LUA_DIR = Path(__file__).with_name("lua")
_COMMON = (_LUA_DIR / "_common.lua").read_text(encoding="utf-8")
_SLOTS_SOURCE = (_LUA_DIR / "slots.lua").read_text(encoding="utf-8")

DEFAULT_LEASE_TTL_MS = 90_000
"""TTL lease воркера (спека §2: `ws:lease:{call}` TTL 90 c)."""
DEFAULT_STREAM_MAXLEN = 10_000


def _section(name: str) -> str:
    """Вырезать секцию ``-- @script {name}`` из slots.lua."""
    marker = f"-- @script {name}"
    start = _SLOTS_SOURCE.find(marker)
    if start < 0:
        raise RuntimeError(f"slots.lua: секция {marker!r} не найдена")
    start += len(marker)
    end = _SLOTS_SOURCE.find("\n-- @script ", start)
    body = _SLOTS_SOURCE[start : end if end > 0 else len(_SLOTS_SOURCE)]
    return _COMMON + body.strip() + "\n"


class Slots:
    """Семафор K полки ``shelf`` + producer событий в ``ws:events:{shelf}``."""

    def __init__(
        self,
        client: Any,
        shelf: str = "local",
        k: int = 1,
        *,
        lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``k`` — число слотов полки (для local = K семафора; сверяется с
        ``max_parallel_requests`` шлюза, I1). ``clock`` — источник ``ts``."""
        if k < 1:
            raise ValueError("k должен быть >= 1")
        self.client = client
        self.shelf = shelf
        self.k = k
        self.lease_ttl_ms = lease_ttl_ms
        self.stream_maxlen = stream_maxlen
        self.clock = clock
        self.holders_key = f"ws:slots:{shelf}"
        self.stream_key = f"ws:events:{shelf}"
        self._acquire = client.register_script(_section("ACQUIRE"))
        self._release = client.register_script(_section("RELEASE"))
        self._heartbeat = client.register_script(_section("HEARTBEAT"))
        self._reclaim = client.register_script(_section("RECLAIM_EXPIRED"))

    # ── API ──────────────────────────────────────────────────────────────

    def lease_key(self, call: str) -> str:
        """Ключ lease конкретного вызова."""
        return f"ws:lease:{self.shelf}:{call}"

    def event(
        self,
        type_: str,
        call: str,
        *,
        job: str = "",
        epoch: int = 0,
        state: str = "",
        ts: float | None = None,
    ) -> str:
        """JSON события (поле ``event`` в Stream)."""
        ts = self.clock() if ts is None else ts
        return json.dumps(
            {
                "type": type_,
                "shelf": self.shelf,
                "call": call,
                "job": job,
                "epoch": epoch,
                "ts": round(ts, 6),
                "state": state,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def acquire(
        self,
        call: str,
        *,
        job: str = "",
        epoch: int = 0,
        state: str = "running",
    ) -> bool:
        """Взять слот. ``False`` = сверх K (отказ без побочных эффектов)."""
        raw = self._acquire(
            keys=[self.holders_key, self.lease_key(call), self.stream_key],
            args=[
                call,
                self.k,
                self.lease_ttl_ms,
                self.event("acquired", call, job=job, epoch=epoch, state=state),
                self.stream_maxlen,
            ],
        )
        return bool(raw)

    def release(self, call: str) -> bool:
        """Освободить слот (идемпотентно: не держали → ``False``, без события)."""
        raw = self._release(
            keys=[self.holders_key, self.lease_key(call), self.stream_key],
            args=[
                call,
                self.event("released", call, state="released"),
                self.stream_maxlen,
            ],
        )
        return bool(raw)

    def heartbeat(self, call: str) -> bool:
        """Продлить lease; ``False`` — lease не наш/истёк."""
        raw = self._heartbeat(
            keys=[self.lease_key(call)],
            args=[call, self.lease_ttl_ms],
        )
        return bool(raw)

    def reclaim_expired(self, call: str) -> bool:
        """Вернуть слот с мёртвым lease (``True`` + событие ``lease_expired``)."""
        raw = self._reclaim(
            keys=[self.holders_key, self.lease_key(call), self.stream_key],
            args=[
                call,
                self.event("lease_expired", call, state="reclaimed"),
                self.stream_maxlen,
            ],
        )
        return bool(raw)

    def sweep_expired(self) -> list[str]:
        """Свипер мёртвых слотов (Ф3.4): ``SMEMBERS holders`` → вызов без
        живого lease (``EXISTS lease == 0``) → ``RECLAIM_EXPIRED``.

        I12: без ``SCAN``/``KEYS`` — ключи по известным именам (lease-ключ
        выводится из имени члена holders). Возвращает список возвращённых
        вызовов (sorted — детерминированно для наблюдения/тестов). Requeue
        вызова — ответственность caller'а (scheduler tick): свипер только
        возвращает слот + пишет событие ``lease_expired``. Гонка
        «lease ожил между EXISTS и RECLAIM» закрыта в Lua: ``RECLAIM_EXPIRED``
        сам перепроверяет ``EXISTS`` и отказывает без записей.
        """
        reclaimed: list[str] = []
        for call in self.client.smembers(self.holders_key):
            if self.client.exists(self.lease_key(call)):
                continue  # lease жив — воркер дышит
            if self.reclaim_expired(call):
                reclaimed.append(call)
        return sorted(reclaimed)

    # ── наблюдение ───────────────────────────────────────────────────────

    def used(self) -> int:
        """Занятые слоты (SCARD holders)."""
        return int(self.client.scard(self.holders_key))

    def free(self) -> int:
        """Свободные слоты полки (``k - used``, не ниже 0)."""
        return max(0, self.k - self.used())
