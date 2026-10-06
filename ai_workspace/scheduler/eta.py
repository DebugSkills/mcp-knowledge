"""ETA обслуживания очереди полки (Ф4.4a) — по наблюдённым wall-длительностям job.

UI читает готовый JSON ``ws:eta:{shelf}`` и формирует диапазон
``eta_range`` (нижняя EMA / верхняя p95) — правило не дублируется на
клиенте. Наблюдение пишет воркер: ``ModeEngine.on_job_terminal`` (движок)
-> wiring-фабрика -> ``ETAStore.observe`` (прод-проводка Ф4.4a).

Формулировка «оценка, >=/~» (риск R5 — ETA-оптимизм при волнах preempt):
ожидающему вызову ранга ``pos`` отвечаем ДИАПАЗОНОМ, а не точкой:

- нижняя граница ``(pos-1) * ema_s``: каждый впереди стоящий в среднем не
  быстрее EMA -> ожидание >= lower (~ при спокойной очереди; pos=1 -> 0);
- верхняя граница ``pos * p95_s``: каждый (включая себя) может стоить до
  p95 — медленный хвост, retry, вытеснения -> «не хуже ~ upper».

Продвижение очереди НЕ линейно при волнах preempt (REQUEUE возвращает
вызовы назад, ранг растёт) — точечная оценка систематически оптимистична
(R5); диапазон честен в обоих режимах.

Ключи:
- ``ws:eta:obs:{shelf}`` — ZSET наблюдений: score = ts, member =
  ``f"{ts}:{seconds}"`` (уникальность по паре; одинаковые ts+seconds
  схлопываются — для дискретных тест-часов это дедупликация);
- ``ws:eta:{shelf}`` — string(JSON) ``{"ema_s","p95_s","n","updated_at"}``.

Методы:
- EMA — по хронологии (порядок возрастания score, детерминированно):
  ``ema_0 = x_0;  ema_i = a*x_i + (1-a)*ema_{i-1}``, ``a = EMA_ALPHA``;
- p95 — квантиль БЛИЖАЙШЕГО РАНГА (nearest-rank): sort asc, индекс
  ``ceil(0.95*n) - 1``, БЕЗ интерполяции (не «размазывает» хвост между
  наблюдениями; n=1 -> само значение).

Best-effort / display-only: ``observe`` в проде дергается хуком терминала
job — сбой панели не ломает терминал (глотается в engine.py).
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_MAX_N",
    "DEFAULT_WINDOW_S",
    "EMA_ALPHA",
    "ETAStore",
    "ema",
    "eta_key",
    "eta_obs_key",
    "eta_range",
    "p95",
]

logger = logging.getLogger(__name__)

EMA_ALPHA = 0.3
DEFAULT_WINDOW_S = 86400.0
DEFAULT_MAX_N = 200


def eta_obs_key(shelf: str) -> str:
    """``ws:eta:obs:{shelf}`` — ZSET наблюдений (score=ts, member="ts:sec")."""
    return f"ws:eta:obs:{shelf}"


def eta_key(shelf: str) -> str:
    """``ws:eta:{shelf}`` — JSON-агрегат панели (ema/p95/n/updated_at)."""
    return f"ws:eta:{shelf}"


def ema(values: list[float]) -> float | None:
    """EMA по хронологии (alpha=EMA_ALPHA=0.3); пусто -> ``None``."""
    if not values:
        return None
    acc = float(values[0])
    for x in values[1:]:
        acc = EMA_ALPHA * float(x) + (1.0 - EMA_ALPHA) * acc
    return acc


def p95(values: list[float]) -> float | None:
    """p95 ближайшего ранга (nearest-rank, без интерполяции); пусто -> ``None``."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return float(ordered[rank - 1])


def eta_range(position: int, snap: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """Диапазон ожидания ``(lower, upper)`` для ранга ``position`` (R5, см.
    модуль): ``(max(0, pos-1)*ema_s, max(1, pos)*p95_s)``.

    ``None`` — снимка нет (наблюдений ещё не было) либо ``position < 1``
    (вызов не в очереди). Битый снимок (нет/нечисловые поля) — тоже
    ``None``: read-хелпер UI не падает на чужих данных.
    """
    if snap is None or position < 1:
        return None
    try:
        ema_s = float(snap["ema_s"])
        p95_s = float(snap["p95_s"])
    except (KeyError, TypeError, ValueError):
        return None
    return (max(0, position - 1) * ema_s, max(1, position) * p95_s)


class ETAStore:
    """Наблюдатель wall-длительностей job + агрегат ETA на ws-redis (Ф4.4a)."""

    def __init__(self, client: Any, *, clock: Callable[[], float] = time.time) -> None:
        """``client`` — ws-redis (decode_responses=True); ``clock`` — «сейчас»."""
        self.client = client
        self.clock = clock

    def observe(
        self,
        shelf: str,
        seconds: float,
        *,
        now: float | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        max_n: int = DEFAULT_MAX_N,
    ) -> dict[str, float | int]:
        """Записать наблюдение длительности (ZADD score=now, member=
        "now:seconds"), обрезать окно ``]now-window_s, now]``
        (ZREMRANGEBYSCORE) и хвост сверх ``max_n`` старейших
        (ZREMRANGEBYRANK), пересчитать и записать агрегат
        ``ws:eta:{shelf}``. Возвращает записанный снимок (наблюдение/тесты).
        """
        now = self.clock() if now is None else now
        seconds = float(seconds)
        obs = eta_obs_key(shelf)
        pipe = self.client.pipeline()
        pipe.zadd(obs, {f"{now}:{seconds}": now})
        pipe.zremrangebyscore(obs, "-inf", f"({now - window_s}")
        pipe.zremrangebyrank(obs, 0, -(max_n + 1))
        pipe.execute()
        return self._recompute(shelf, now=now)

    def _recompute(self, shelf: str, *, now: float) -> dict[str, float | int]:
        """Пересчитать агрегат по логу наблюдений (хронология = порядок
        score). Вызывается из ``observe`` (лог непуст)."""
        rows = self.client.zrange(eta_obs_key(shelf), 0, -1, withscores=True)
        seq = [float(member.split(":", 1)[1]) for member, _ in rows]
        snap: dict[str, float | int] = {
            "ema_s": float(ema(seq) or 0.0),
            "p95_s": float(p95(seq) or 0.0),
            "n": len(seq),
            "updated_at": float(now),
        }
        self.client.set(eta_key(shelf), json.dumps(snap, separators=(",", ":")))
        return snap

    def snapshot(self, shelf: str) -> dict[str, float | int] | None:
        """Текущий агрегат панели; ``None`` — наблюдений не было или битый
        JSON (warning, read-хелпер UI не падает)."""
        raw = self.client.get(eta_key(shelf))
        if raw is None:
            return None
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except ValueError:
            logger.warning(
                "eta.snapshot(%s): битый JSON в %s — игнор", shelf, eta_key(shelf)
            )
            return None
