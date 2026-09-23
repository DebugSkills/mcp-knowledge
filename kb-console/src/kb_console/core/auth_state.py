"""AuthState + типизированные исключения (code-2026-09-22-007, §7.2).

Источник 401-шторма: поллеры с «except Exception: pass» молотят API
каждую секунду при мёртвом ключе. Решение — key-scoped блок + бэкофф.

Иерархия (N-1, канон имён — единообразно):
    AuthError(RuntimeError)           — базовый класс 401/403 (back-compat,
                                        OQ-2: чужие «except RuntimeError»
                                        продолжают работать)
    ├── AuthenticationError           — HTTP 401 (неверный API-ключ)
    └── ForbiddenError                — HTTP 403 (недостаточно прав)
    TransportError(RuntimeError)      — 429/5xx/сеть/таймаут/decode/JSON-RPC

AuthState — per-process singleton, key-scoped блок (OQ-1/OQ-8):
блок ключа A не трогает поллеры ключа B (готовность к per-role ключам).
Реальный ключ и его хэш НЕ хранятся — только метка key_ref (литерал
"global"; в будущем "role:editor").
"""

from __future__ import annotations

import time


class AuthError(RuntimeError):
    """Базовый класс ошибок аутентификации/авторизации (HTTP 401/403).

    Наследует RuntimeError для back-compat: страницы со старыми
    «except RuntimeError» продолжают ловить (R4 — перехват строго выше).
    """

    def __init__(self, message: str, *, key_ref: str | None = None) -> None:
        super().__init__(message)
        self.key_ref = key_ref


class AuthenticationError(AuthError):
    """HTTP 401 — неверный API-ключ. Поллеры останавливаются."""


class ForbiddenError(AuthError):
    """HTTP 403 — недостаточно прав (текст БЕЗ «введите ключ»)."""


class TransportError(RuntimeError):
    """Транспортный сбой: 429/5xx/ConnectError/Timeout/decode/JSON-RPC.

    Поллеры НЕ останавливаются — бэкофф (см. auth_polling.Backoff).
    """


class AuthState:
    """Key-scoped состояние блокировки поллинга (per-process, OQ-1).

    block(kind, ts, key_ref) / is_blocked(key_ref). Реальный ключ
    и его хэш НЕ хранятся — только метка key_ref (OQ-8).
    """

    def __init__(self) -> None:
        self._blocked: dict[str, tuple[str, float]] = {}

    def block(self, kind: str, ts: float, *, key_ref: str) -> None:
        """Заблокировать поллинг для ключа key_ref.

        Args:
            kind: вид блокировки — "AuthenticationError" / "ForbiddenError".
            ts: time.time() момента отказа (для диагностики).
            key_ref: метка ключа (обязательный kwarg, без дефолта — P2-D).
        """
        if not key_ref:
            raise ValueError("key_ref обязателен (code-2026-09-22-007, OQ-8)")
        self._blocked[key_ref] = (kind, ts)

    def is_blocked(self, *, key_ref: str) -> bool:
        """Заблокирован ли поллинг для этого ключа."""
        return key_ref in self._blocked

    def blocked_kind(self, *, key_ref: str) -> str | None:
        """Вид блокировки ключа (для баннера) или None."""
        entry = self._blocked.get(key_ref)
        return entry[0] if entry else None

    def blocked_since(self, *, key_ref: str) -> float | None:
        """ts блокировки ключа или None."""
        entry = self._blocked.get(key_ref)
        return entry[1] if entry else None

    def unblock(self, *, key_ref: str) -> None:
        """Снять блок (ручное возобновление: «Проверить и продолжить» / F5)."""
        self._blocked.pop(key_ref, None)

    def clear(self) -> None:
        """Полная очистка (тесты)."""
        self._blocked.clear()


# Per-process singleton (OQ-1: per-page отклонён — вкладки одного
# процесса гаснут вместе при мёртвом ключе; key-scope даёт изоляцию ключей).
_auth_state = AuthState()


def get_auth_state() -> AuthState:
    """Единственный AuthState процесса."""
    return _auth_state


def record_auth_error(exc: AuthError, *, state: AuthState | None = None) -> None:
    """Записать отказ ключа в AuthState (kind из класса исключения)."""
    state = state if state is not None else get_auth_state()
    key_ref = exc.key_ref or "global"
    state.block(type(exc).__name__, time.time(), key_ref=key_ref)
