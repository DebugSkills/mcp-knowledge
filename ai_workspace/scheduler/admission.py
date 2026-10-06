"""Admission pre-check ДО постановки + списание токенов по факту (Ф4.2).

trace_id: arch-2026-10-05-ai-workspace (план REV.13), решения оператора
D3-D7. Паттерн Lua-обвязки — slots.py/queue.py (Ф3.2/Ф3.3).

Квоты (D3/D6): ``tokens_per_day``/``conc`` — личные лимиты участника
(``registry/quotas.py``, Ф4.1); ``None`` = личного лимита нет, действует
только общий K полки (slots.lua, Ф3.3). Ключи:
- ``ws:quota:tok:{user}:{day}`` — расход токенов дня (day = локальная дата
  ``%Y-%m-%d``, D7); списание ``charge_tokens`` ПОСЛЕ завершения по факту
  usage, TTL — до следующей локальной полуночи;
- ``ws:quota:conc:{user}`` — резерв личного параллелизма (D6), БЕЗ TTL;
- ``ws:budget:global`` — глобальный расход ext-контура (D4, ₽); пишет
  reconcile-контур (Ф4.3+), admission только читает.

РЕЖИМ CONC-СЧЁТЧИКА — РЕЗЕРВ (admit = резервация на время job):
проверка и INCR живут в ОДНОЙ Lua (иначе две параллельные гонки обе
пройдут; «наблюдательный» режим без инкремента дал бы N allow при нуле).
Следствия:
- ``allow`` инкрементирует ``ws:quota:conc:{user}``; воркер ОБЯЗАН вызвать
  ``conc_exit(user)`` в finally (паттерн ``Slots.release``, Ф3.3);
  ``conc_exit`` идемпотентен с полом на 0 — в минус счётчик не уходит;
- ``deny`` и ``park`` НЕ резервируют ничего (мутация только при финальном
  allow); resume из парка (Ф4.3) повторяет admit либо берёт ``conc_enter``;
- TTL на conc-ключе НЕТ намеренно: резерв живёт столько, сколько job;
  защиту от утечек (мёртвый воркер не вышел) даст свипер Ф4.3+ — аналог
  ``reclaim_expired`` в slots.lua; в Ф4.2 выход — контракт вызывающего.

Бюджет (D4/D5): при ``shelf='ext'`` (heavy-класс, conformance: local|ext)
``ws:budget:global >= budgets.ext.limit`` → ``park`` (НЕ deny: парк/resume
реализует Ф4.3; парк — сигнал вызывающему). Enforcement только глобальный:
per-user зеркало ``ws:budget:{user}`` ведёт reconcile-контур (сверка с
LiteLLM, ``budgets.ext.reconcile``); второго hard-limit на пользователя
в схеме Q11 нет — ключ здесь только назван (``budget_user_key``).

Атомарность: все проверки + инкремент — ОДИН round-trip (Lua ADMIT).
Время Lua сам не читает: ``now`` инъектируется (паттерн queue.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from ai_workspace.registry.quotas import QuotaRegistry

__all__ = [
    "BUDGET_GLOBAL_KEY",
    "EXT_SHELF",
    "AdmissionDenied",
    "Decision",
    "admit",
    "budget_user_key",
    "charge_tokens",
    "conc_enter",
    "conc_exit",
    "conc_key",
    "seconds_to_local_midnight",
    "tok_key",
]

EXT_SHELF = "ext"
"""Полка внешнего провайдера (heavy-класс; conformance: полки local|ext)."""

BUDGET_GLOBAL_KEY = "ws:budget:global"
"""Глобальный расход ext-контура (D4); пишет reconcile-контур (Ф4.3+)."""

_LUA_DIR = Path(__file__).with_name("lua")
_LUA_SOURCE = (_LUA_DIR / "admission.lua").read_text(encoding="utf-8")
"""SSOT Lua-кода — читается один раз при импорте (паттерн slots.py)."""


def _script(name: str) -> str:
    """Вырезать секцию ``-- @script {name}`` из admission.lua."""
    marker = f"-- @script {name}"
    start = _LUA_SOURCE.find(marker)
    if start < 0:
        raise RuntimeError(f"admission.lua: секция {marker!r} не найдена")
    start += len(marker)
    end = _LUA_SOURCE.find("\n-- @script ", start)
    return _LUA_SOURCE[start : end if end > 0 else len(_LUA_SOURCE)].strip() + "\n"


class AdmissionDenied(RuntimeError):
    """Личный лимит исчерпан — постановка ОТКАЗАНА (не парк, не очередь).

    Паттерн ``GpuError``/``GpuBusy``: базовый класс свой у каждого контура
    ai_workspace, общий предок проект не вводит. Коды:
    ``quota_tokens_exhausted`` (D3 — до полуночи), ``quota_conc_exceeded``
    (D6 — до освобождения резерва). Бюджет — НЕ отказ:
    ``Decision(action='park')`` (D5, resume Ф4.3).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Decision:
    """Результат admit: ``allow`` | ``deny`` (личный лимит) | ``park`` (D5)."""

    action: Literal["allow", "deny", "park"]
    code: str | None = None
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        """``True`` только для allow (park/deny требуют реакции вызывающего)."""
        return self.action == "allow"

    def raise_if_denied(self) -> None:
        """Fail-fast хелпер: ``deny`` → ``AdmissionDenied``; allow/park — no-op
        (парк — штатный сигнал Ф4.3, не исключение)."""
        if self.action == "deny":
            raise AdmissionDenied(
                self.code or "denied", self.reason or "admission denied"
            )


def tok_key(user: str, day: str) -> str:
    """Дневной ключ расхода токенов: ``ws:quota:tok:{user}:{day}`` (D3/D7)."""
    return f"ws:quota:tok:{user}:{day}"


def conc_key(user: str) -> str:
    """Ключ резерва личного параллелизма: ``ws:quota:conc:{user}`` (D6)."""
    return f"ws:quota:conc:{user}"


def budget_user_key(user: str) -> str:
    """Per-user зеркало ext-бюджета (``per_user_mirror``, D4) для reconcile."""
    return f"ws:budget:{user}"


def _local_day(now: datetime) -> str:
    """Локальная дата дневного ключа (D7: сброс в 00:00 локального дня)."""
    return now.strftime("%Y-%m-%d")


def _next_midnight(now: datetime) -> datetime:
    """Следующая локальная полночь (naive wall-clock арифметика — граница
    дня календарная)."""
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

def _now_local(now: datetime | None = None) -> datetime:
    """Нормализованное локальное «сейчас» (DTZ-чисто, проект tz-aware).

    ``None`` → aware-локальное (``now(utc).astimezone`` — канонический
    идиом без DTZ005); naive-вход → aware-локальное (``astimezone``
    считает naive локальным — контракт D7 именно wall-clock); aware —
    как есть. Дальше везде только wall-clock арифметика (timedelta/replace).
    """
    if now is None:
        return datetime.now(timezone.utc).astimezone()
    return now.astimezone() if now.tzinfo is None else now


def seconds_to_local_midnight(now: datetime | None = None) -> int:
    """Секунд до следующей локальной полуночи (D7); ``None`` → сейчас.

    ``now`` — naive-локальный (считается локальным) либо aware. Ровно в
    полночь → 86400 (следующая полночь, не 0): дневной ключ рождается и
    умирает на границе дня.
    """
    now = _now_local(now)
    return int((_next_midnight(now) - now).total_seconds())


def admit(
    user: str,
    role: str,
    *,
    registry: QuotaRegistry,
    redis: Any,
    shelf: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Pre-check ДО постановки — атомарно, один round-trip (Lua ADMIT).

    Порядок: токены (deny ``quota_tokens_exhausted``) → ext-бюджет (park
    ``budget_ext_exhausted``) → conc+резерв (deny ``quota_conc_exceeded`` /
    allow+INCR). Мутация (INCR conc) — только при финальном allow.

    - ``role`` неизвестна → квота ``defaults.role`` (фолбэк Ф4.1, не ошибка);
    - ``shelf='ext'`` подключает бюджетную проверку (D4/D5), иначе — нет;
    - ``now`` инъектируется (детерминированные тесты; Lua время не читает);
    - ``redis`` — клиент ws-контура (``redis_client.make_ws_redis``).
    """
    quota = registry.quota_for(role)
    now = _now_local(now)
    budget = registry.budgets[EXT_SHELF] if shelf == EXT_SHELF else None
    action, code, detail = redis.register_script(_script("ADMIT"))(
        keys=[
            tok_key(user, _local_day(now)),
            conc_key(user),
            BUDGET_GLOBAL_KEY,
        ],
        args=[
            "" if quota.tokens_per_day is None else str(quota.tokens_per_day),
            "" if quota.conc is None else str(quota.conc),
            "" if budget is None else f"{budget.limit:.17g}",
        ],
    )
    if action == "allow":
        return Decision(action="allow")
    if code == "quota_tokens_exhausted":
        reason = (
            f"дневной лимит токенов {detail}/{quota.tokens_per_day} "
            f"({user}, день {_local_day(now)}, сброс в полночь D7)"
        )
    elif code == "quota_conc_exceeded":
        reason = f"личный параллелизм {detail}/{quota.conc} ({user})"
    else:  # budget_ext_exhausted
        reason = (
            f"ext-бюджет {detail}/{budget.limit if budget else '?'} "
            f"{budget.currency if budget else ''} — парк до reconcile (D5)"
        )
    return Decision(action=action, code=code or None, reason=reason)


def charge_tokens(
    user: str,
    tokens: int,
    *,
    redis: Any,
    now: datetime | None = None,
) -> int:
    """Списать ``tokens`` ПО ФАКТУ usage (после завершения вызова/job).

    INCRBY + EXPIREAT(полночь) одной Lua (CHARGE) — пара атомарна; возвращает
    новый дневной расход. EXPIREAT — АБСОЛЮТНАЯ метка локальной полуночи от
    Python: каждый charge ставит одну и ту же метку (идемпотентно — «TTL не
    растёт» выполняется по построению, GT-семантика не нужна), а относительный
    TTL при charge в 23:59 мог бы продлить ключ ЗА полночь и сломать сброс D7.
    """
    if tokens < 0:
        raise ValueError(f"tokens должен быть >= 0, получено {tokens}")
    now = _now_local(now)
    raw = redis.register_script(_script("CHARGE"))(
        keys=[tok_key(user, _local_day(now))],
        args=[tokens, int(_next_midnight(now).timestamp())],
    )
    return int(raw)


def conc_enter(user: str, *, redis: Any) -> int:
    """Взять conc-резерв БЕЗ проверок; возвращает новый in-flight.

    Путь resume из парка (Ф4.3): лимит уже был подтверждён admit'ом до
    парка, повторная проверка не нужна (и могла бы ложно отказать —
    резерв на время парка не держится).
    """
    return int(
        redis.register_script(_script("CONC_ENTER"))(keys=[conc_key(user)], args=[])
    )


def conc_exit(user: str, *, redis: Any) -> int:
    """Освободить conc-резерв (ОБЯЗАТЕЛЕН в finally воркера); остаток.

    Идемпотентен с полом на 0: лишний exit — no-op, в минус счётчик не
    уходит (иначе утечка «в минус» открывала бы чужие слоты).
    """
    return int(
        redis.register_script(_script("CONC_EXIT"))(keys=[conc_key(user)], args=[])
    )
