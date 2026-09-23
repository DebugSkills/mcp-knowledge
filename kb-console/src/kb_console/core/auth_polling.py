"""auth_polling — auth-aware примитив поллинга + бэкофф (code-2026-09-22-007, §7.5).

Контракт одного шага поллинга (все 9 поллеров — P1..P9):

- AuthError (401/403): ключ блокируется в AuthState (key-scoped), fetch
  перестаёт вызываться — 0 АВТОМАТИЧЕСКИХ запросов (AC#1). Возобновление
  только ручное: кнопка «Проверить и продолжить» (auth_banner) или F5.
- TransportError (429/5xx/сеть/таймаут/decode): бэкофф ×2 до 60 с
  (AC#3: после 5-го сбоя factor=16 ⇒ 3.75 req/мин ≤ 4).
- Успех: reset бэкоффа (при живом ключе примитив прозрачен — R3).

Арбитраж интервалов §7.5 (P7): пока backoff.factor > 1, адаптивная ветка
progress_panel (:104-106, :187-190) НЕ пишет интервал — бэкофф сильнее.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .auth_state import (
    AuthError,
    AuthState,
    TransportError,
    get_auth_state,
    record_auth_error,
)

T = TypeVar("T")


@dataclass
class Backoff:
    """Экспоненциальный бэкофф: интервал ×2 после повторных сбоев, cap 60 с.

    factor(n) = 2^(n-1) для n сбоев (n≥1), 1 при n=0 — ПЕРВЫЙ сбой не
    растягивает интервал (R3: живой ключ + единичный флап → поведение
    бит-в-бит прежнее). Последовательность interval после сбоев
    1→2→4→8→16…cap 60 (§7.6; после 5-го сбоя factor=16 ⇒ 3.75 req/мин).

    base — штатный интервал поллера (POLL_FAST/EXPANDED_POLL/REFRESH…),
    интервалы НЕ меняются (scope 007): бэкофф только умножает.
    """

    base: float = 1.0
    cap: float = 60.0

    def __post_init__(self) -> None:
        if self.base <= 0:
            raise ValueError("base должен быть > 0")
        if self.cap < self.base:
            raise ValueError("cap не может быть меньше base")
        self._failures = 0

    @property
    def failures(self) -> int:
        """Число последовательных транспорт-сбоев."""
        return self._failures

    @property
    def factor(self) -> float:
        """Текущий множитель: 1 (нет сбоев / один сбой), далее ×2."""
        if self._failures <= 1:
            return 1.0
        return min(2.0 ** (self._failures - 1), self.cap / self.base)

    @property
    def interval(self) -> float:
        """Интервал таймера с учётом бэкоффа (min(base×factor, cap))."""
        return min(self.base * self.factor, self.cap)

    def bump(self) -> float:
        """Зафиксировать транспорт-сбой; вернуть новый интервал."""
        self._failures += 1
        return self.interval

    def reset(self) -> None:
        """Успех — сброс бэкоффа к штатному интервалу."""
        self._failures = 0


@dataclass
class PollOutcome(Generic[T]):
    """Результат одного шага поллинга.

    skipped=True — шаг не дал данных (блок ключа или транспорт-сбой);
    value=None; interval — новый интервал таймера (backoff.interval).
    """

    skipped: bool
    value: T | None = None
    interval: float = 1.0
    auth_blocked: bool = False
    transport_error: TransportError | None = None


async def poll_step(
    fetch: Callable[[], Awaitable[T]],
    *,
    key_ref: str,
    backoff: Backoff,
    state: AuthState | None = None,
    on_auth_blocked: Callable[[AuthError], None] | None = None,
) -> PollOutcome[T]:
    """Один шаг поллинга: стоп на AuthError, бэкофф на TransportError.

    Args:
        fetch: async-вызов (хелпер MCPClient). Ключ заблокирован —
            fetch НЕ вызывается (AC#1: 0 автоматических запросов).
        key_ref: метка ключа ("global"; OQ-8 — обязательный kwarg).
        backoff: бэкофф поллера (reset при успехе, bump при транспорте).
        state: AuthState (по умолчанию per-process singleton).
        on_auth_blocked: колбэк для показа баннера (получает AuthError).

    Returns:
        PollOutcome: skipped/value/interval/auth_blocked/transport_error.
    """
    state = state if state is not None else get_auth_state()
    if state.is_blocked(key_ref=key_ref):
        # Блок уже стоит: 0 запросов, интервал не трогаем (AC#1).
        return PollOutcome(skipped=True, interval=backoff.interval, auth_blocked=True)

    try:
        value = await fetch()
    except AuthError as exc:
        if exc.key_ref is None:
            exc.key_ref = key_ref
        record_auth_error(exc, state=state)
        backoff.reset()
        if on_auth_blocked is not None:
            on_auth_blocked(exc)
        return PollOutcome(
            skipped=True, interval=backoff.interval, auth_blocked=True
        )
    except TransportError as exc:
        backoff.bump()
        return PollOutcome(
            skipped=True,
            interval=backoff.interval,
            transport_error=exc,
        )

    backoff.reset()
    return PollOutcome(skipped=False, value=value, interval=backoff.interval)


__all__ = [
    "AuthError",
    "AuthState",
    "Backoff",
    "PollOutcome",
    "TransportError",
    "get_auth_state",
    "poll_step",
    "record_auth_error",
]
