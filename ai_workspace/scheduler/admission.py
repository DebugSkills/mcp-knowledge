"""Admission pre-check ДО постановки + списание токенов по факту (Ф4.2).

trace_id: arch-2026-10-05-ai-workspace (план REV.13 + ревизия критика Ф4:
P1-2/P1-3/P1-4/P2-3/P2-4), решения оператора D3-D7. Паттерн Lua-обвязки —
slots.py/queue.py (Ф3.2/Ф3.3).

Квоты (D3/D6): ``tokens_per_day``/``conc`` — личные лимиты участника
(``registry/quotas.py``, Ф4.1); ``None`` = личного лимита нет, действует
только общий K полки (slots.lua, Ф3.3). Ключи:
- ``ws:quota:tok:{user}:{day}`` — расход токенов дня (day = локальная дата
  ``%Y-%m-%d``, D7); списание ``charge_tokens`` ПОСЛЕ завершения по факту
  usage, TTL — до следующей локальной полуночи;
- ``ws:quota:conc:{user}`` — резерв личного параллелизма (D6), БЕЗ TTL;
- ``ws:quota:conchold:{user}`` — SET job-маркеров ВЛАДЕНИЯ резервом (P1-2):
  списание по факту владения — ``conc_release(user, job)`` атомарно решает
  SREM-маркером, чужой/повторный вызов резерв не трогает (обход D6 закрыт);
- ``ws:quota:conclease:{user}:{job}`` — TTL-lease резерва (P1-3, паттерн
  ``ws:lease``): воркер продлевает ``conc_heartbeat``; мёртвый воркер →
  ``conc_reclaim_expired``/``sweep_expired_conc`` снимает резерв (вечного
  deny после SIGKILL нет);
- ``ws:quota:events`` — Stream событий квот-контура (reclaim/degraded),
  ``MAXLEN ~`` (I12);
- ``ws:budget:global:{month}`` — расход ext-контура за ЛОКАЛЬНЫЙ месяц
  (D4, period: month; микро-₽ int) — admission только ЧИТАЕТ; пишет
  ``budget.charge_budget`` (P0-1, вместе со списанием токенов на терминале
  job), сверяет ``budget.reconcile_budget`` (nightly, журнал списаний).

РЕЖИМ CONC-СЧЁТЧИКА — РЕЗЕРВ (admit = резервация на время job):
проверка и INCR живут в ОДНОЙ Lua (иначе две параллельные гонки обе
пройдут; «наблюдательный» режим без инкремента дал бы N allow при нуле).
Следствия:
- ``allow`` инкрементирует ``ws:quota:conc:{user}`` (+SADD/lease при
  ``job=``); воркер ОБЯЗАН освободить резерв в finally — новый контракт:
  ``conc_release(user, job_id)`` (по владению; повторный вызов no-op);
  легаси-агрегат ``conc_exit(user)`` — только для admit без ``job=``;
- ``deny`` и ``park`` НЕ резервируют ничего (мутация только при финальном
  allow); resume из парка (Ф4.3) повторяет admit с тем же ``job=``;
- TTL на самом счётчике НЕТ намеренно (резерв живёт как job); от утечек
  при крахе воркера защищает lease + свипер (P1-3), а не TTL счётчика.

Бюджет (D4/D5): при ``shelf='ext'`` (heavy-класс, conformance: local|ext)
``ws:budget:global:{month} >= budgets.ext.limit_micro`` → ``park`` (НЕ deny:
парк/resume реализует Ф4.3; парк — сигнал вызывающему). ЕДИНИЦА — целочисленный
микро-₽: ``Budget.limit_micro`` конвертирует ₽-лимит при загрузке квот
(единственная точка согласования единиц, P0-1); списание — только INCRBY int.
Enforcement только глобальный: per-user зеркало ``ws:budget:user:{u}:{month}``
ведёт ``charge_budget``/reconcile (D4, per_user_mirror); второго hard-limit
на пользователя в схеме Q11 нет. Списание денег и сверка — ``scheduler/budget.py``.

Деградация ws-redis (P1-4) — FAIL-CLOSED + ALARM: короткие socket-таймауты
(``redis_client.make_ws_redis``), Connection/Timeout → ``QuotaRedisUnavailable``
(понятный отказ, не трейс/зависание) + ALARM-запись (logging + best-effort
событие ``quota_degraded``). Обоснование полярности: в отличие от
GPU-admission (I7: fail-open + ALARM — там локальный GPU не должен
останавливать KB-поиск), квота — гейт постановки в ОБЩИЙ ws-контур: при
недоступном ws-redis очередь/постановка всё равно не работают, fail-open
открывал бы обход D3/D6 при каждом сетевом чихе. Отказ = fail-closed.

Семантика границ (R3/P2-3, осознанные трейд-оффы):
- hot-reload квот НЕ пересчитывает живое: счётчики/резервы — факт расхода,
  enforcement только в момент admit; уже допущенные job'ы живут по старым
  правилам до своего освобождения (свипер/выход);
- окно admit→charge: enforcement на постановке, списание по факту usage —
  границы перерасхода фиксирует wiring-контракт (per-call re-admit на
  re-enqueue tool-loop либо документированный bound; Ф3 §7.4).

Атомарность: все проверки + инкремент — ОДИН round-trip (Lua ADMIT).
Время Lua сам не читает: ``now`` инъектируется (паттерн queue.py).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Literal, TypeVar

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler.lua_scripts import extract_sections, section_of

__all__ = [
    "BUDGET_GLOBAL_PREFIX",
    "DEFAULT_CONC_LEASE_TTL_MS",
    "EXT_SHELF",
    "QUOTA_EVENTS_KEY",
    "AdmissionDenied",
    "Decision",
    "QuotaRedisUnavailable",
    "admit",
    "budget_global_key",
    "budget_user_key",
    "charge_tokens",
    "conc_enter",
    "conc_exit",
    "conc_heartbeat",
    "conc_key",
    "conc_reclaim_expired",
    "conc_release",
    "conchold_key",
    "conclease_key",
    "quota_fail_closed",
    "seconds_to_local_midnight",
    "sweep_expired_conc",
    "tok_key",
]

EXT_SHELF = "ext"
"""Полка внешнего провайдера (heavy-класс; conformance: полки local|ext)."""

BUDGET_GLOBAL_PREFIX = "ws:budget:global"
"""Префикс глобального расхода ext-контура (D4); месяц — в суффиксе ключа."""

QUOTA_EVENTS_KEY = "ws:quota:events"
"""Stream событий квот-контура (reclaim/degraded; MAXLEN ~, I12, P1-3/P1-4)."""

DEFAULT_QUOTA_STREAM_MAXLEN = 10_000

DEFAULT_CONC_LEASE_TTL_MS = 90_000
"""TTL lease conc-резерва (P1-3; паттерн ``slots.DEFAULT_LEASE_TTL_MS``):
воркер ОБЯЗАН продлевать ``conc_heartbeat``; мёртвый воркер распознаётся
свипером после истечения lease."""

_LUA_DIR = Path(__file__).with_name("lua")
_LUA_SOURCE = (_LUA_DIR / "admission.lua").read_text(encoding="utf-8")
"""SSOT Lua-кода — читается один раз при импорте (паттерн slots.py)."""

_SECTIONS = extract_sections(_LUA_SOURCE, source_name="admission.lua")
"""Секции ``-- @script`` (общий загрузчик lua_scripts, P2-5)."""


@lru_cache(maxsize=128)
def _cached_script(client: Any, name: str) -> Any:
    """Script-объект секции, КЭШ на клиента (P2-4: раньше register_script
    на каждый вызов; паттерн ``Queue.__init__`` — регистрация один раз)."""
    return client.register_script(section_of(_SECTIONS, name, source_name="admission.lua"))


_T = TypeVar("_T")

_DEGRADED_LOG = logging.getLogger("ai_workspace.scheduler.admission")


class QuotaRedisUnavailable(RuntimeError):
    """ws-redis недоступен в контуре квот — операция ОТКАЗАНА (P1-4).

    Политика: fail-closed + ALARM (см. докстроку модуля). «Чёрная дыра»
    (порт открыт, ответа нет) закрыта socket-таймаутами ``make_ws_redis``.
    Прочие ошибки Redis (например, ``ResponseError`` от битого значения
    ключа) проходят НАРУЖУ как есть — это не деградация, а дефект данных.
    """


def _quota_event(type_: str, **fields: Any) -> str:
    """JSON события квот-контура для ``ws:quota:events`` (наблюдение)."""
    payload: dict[str, Any] = {"type": type_, "ts": round(time.time(), 6)}
    payload.update(fields)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _emit_degraded(op: str, exc: Exception, redis: Any) -> None:
    """ALARM-запись деградации (P1-4): logging — всегда (redis внизу не
    помеха); XADD ``quota_degraded`` в ``ws:quota:events`` — best-effort
    (одноразовый таймаут мог пройти к моменту записи)."""
    _DEGRADED_LOG.error(
        "quota_degraded: op=%s err=%s: %s — квотирование отказало (fail-closed)",
        op,
        type(exc).__name__,
        exc,
    )
    if redis is None:
        return
    try:
        redis.xadd(
            QUOTA_EVENTS_KEY,
            {"event": _quota_event("quota_degraded", op=op, err=type(exc).__name__)},
            maxlen=DEFAULT_QUOTA_STREAM_MAXLEN,
            approximate=True,
        )
    except (RedisConnectionError, RedisTimeoutError):
        pass  # redis всё ещё недоступен — ALARM уже зафиксирован в logging


def quota_fail_closed(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Декоратор политики деградации (P1-4): Connection/Timeout →
    ``QuotaRedisUnavailable`` + ALARM; прочие исключения — наружу как есть.

    Публичный: общая политика КВОТ-контура (admission + budget, P0-1)."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return fn(*args, **kwargs)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            _emit_degraded(fn.__name__, exc, kwargs.get("redis"))
            raise QuotaRedisUnavailable(
                f"quota op {fn.__name__!r}: ws-redis недоступен "
                f"({type(exc).__name__}) — отказ fail-closed (P1-4); "
                "подробности — logging ALARM quota_degraded"
            ) from exc

    return wrapper


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


def conchold_key(user: str) -> str:
    """SET job-маркеров владения conc-резервом (P1-2): списание по факту
    владения — ``SREM`` решает, был ли резерв ЭТОГО job'а."""
    return f"ws:quota:conchold:{user}"


def conclease_key(user: str, job: str) -> str:
    """TTL-lease резерва конкретного job'а (P1-3, паттерн ``ws:lease``):
    живёт пока воркер продлевает ``conc_heartbeat``; истёк → свипер."""
    return f"ws:quota:conclease:{user}:{job}"


def budget_global_key(month: str | None = None) -> str:
    """Глобальный расход ext-контура за месяц: ``ws:budget:global:{YYYY-MM}``.

    Месяц — ЛОКАЛЬНЫЙ (``%Y-%m``, границы календарные — паттерн дневного
    ключа D7); ``None`` -> текущий. Единица значения — микро-₽ (int, P0-1).
    """
    return f"{BUDGET_GLOBAL_PREFIX}:{month or _local_month_key()}"


def budget_user_key(user: str, month: str | None = None) -> str:
    """Per-user зеркало ext-бюджета месяца (``per_user_mirror``, D4):
    ``ws:budget:user:{user}:{YYYY-MM}`` (``None`` -> текущий месяц)."""
    return f"ws:budget:user:{user}:{month or _local_month_key()}"


def _local_month_key() -> str:
    """Текущий локальный месяц (для ключей-дефолтов budget_*_key)."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m")


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


@quota_fail_closed
def admit(
    user: str,
    role: str,
    *,
    registry: QuotaRegistry,
    redis: Any,
    shelf: str | None = None,
    now: datetime | None = None,
    job: str | None = None,
    lease_ttl_ms: int = DEFAULT_CONC_LEASE_TTL_MS,
) -> Decision:
    """Pre-check ДО постановки — атомарно, один round-trip (Lua ADMIT).

    Порядок: токены (deny ``quota_tokens_exhausted``) → ext-бюджет (park
    ``budget_ext_exhausted``) → conc+резерв (deny ``quota_conc_exceeded`` /
    allow+INCR). Мутация (INCR conc) — только при финальном allow.

    - ``role`` неизвестна → квота ``defaults.role`` (фолбэк Ф4.1, не ошибка;
      валидатор Q14 гарантирует фолбэку личные лимиты — least-privilege);
    - ``job`` — id job'а для PER-JOB владения резервом (P1-2): allow →
      INCR + SADD-маркер + lease (если ``lease_ttl_ms > 0``); освобождение —
      ``conc_release(user, job)`` ровно один раз. ``None`` — легаси-агрегат
      без маркера (освобождение только агрегатным ``conc_exit``);
    - ``shelf='ext'`` подключает бюджетную проверку (D4/D5), иначе — нет;
    - ``now`` инъектируется (детерминированные тесты; Lua время не читает);
    - ``redis`` — клиент ws-контура (``redis_client.make_ws_redis``);
      недоступен → ``QuotaRedisUnavailable`` (fail-closed + ALARM, P1-4).
    """
    quota = registry.quota_for(role)
    now = _now_local(now)
    budget = registry.budgets[EXT_SHELF] if shelf == EXT_SHELF else None
    action, code, detail = _cached_script(redis, "ADMIT")(
        keys=[
            tok_key(user, _local_day(now)),
            conc_key(user),
            # бюджетный ключ — месяц-скоуп, единица микро-₽ (P0-1)
            budget_global_key(now.strftime("%Y-%m")),
            conchold_key(user),
            conclease_key(user, job) if job else "",
        ],
        args=[
            "" if quota.tokens_per_day is None else str(quota.tokens_per_day),
            "" if quota.conc is None else str(quota.conc),
            # порог — limit_micro (₽ × 10^6, конверсия в QuotaRegistry.Budget)
            "" if budget is None else str(budget.limit_micro),
            job or "",
            lease_ttl_ms if job else 0,
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
        rub = int(detail) / 1_000_000 if detail.isdigit() else detail
        reason = (
            f"ext-бюджет {rub if isinstance(rub, str) else round(rub, 2)}/"
            f"{budget.limit if budget else '?'} "
            f"{budget.currency if budget else ''} (микро-₽: {detail}/"
            f"{budget.limit_micro if budget else '?'}) — парк до reconcile (D5)"
        )
    return Decision(action=action, code=code or None, reason=reason)


@quota_fail_closed
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

    Границы окна admit→charge (P2-3): enforcement на постановке, списание
    пост-фактум; bound параллельного перерасхода = conc-резервы, добор —
    wiring-контракт (per-call re-admit на re-enqueue, Ф3 §7.4).
    """
    if tokens < 0:
        raise ValueError(f"tokens должен быть >= 0, получено {tokens}")
    now = _now_local(now)
    raw = _cached_script(redis, "CHARGE")(
        keys=[tok_key(user, _local_day(now))],
        args=[tokens, int(_next_midnight(now).timestamp())],
    )
    return int(raw)


@quota_fail_closed
def conc_enter(user: str, *, redis: Any) -> int:
    """Взять conc-резерв БЕЗ проверок; возвращает новый in-flight.

    ЛЕГАСИ-агрегат (без job-маркера). Новые вызовы должны идти
    ``admit(..., job=...)`` — per-job владение (P1-2) + lease (P1-3);
    освобождение агрегата — только ``conc_exit``.
    """
    return int(_cached_script(redis, "CONC_ENTER")(keys=[conc_key(user)], args=[]))


@quota_fail_closed
def conc_exit(user: str, *, redis: Any) -> int:
    """Освободить conc-резерв АГРЕГАТОМ (легаси-путь; остаток).

    Идемпотентен с полом на 0: лишний exit — no-op, в минус счётчик не
    уходит (иначе утечка «в минус» открывала бы чужие слоты). Для admit
    с ``job=`` используй ``conc_release`` — списание по владению (P1-2):
    агрегатный exit не снимает маркер и может задеть чужой резерв.
    """
    return int(_cached_script(redis, "CONC_EXIT")(keys=[conc_key(user)], args=[]))


@quota_fail_closed
def conc_release(user: str, job: str, *, redis: Any) -> bool:
    """Освободить резерв КОНКРЕТНОГО job'а — по факту владения (P1-2).

    Атомарная Lua CONC_RELEASE: SREM job-маркера из ``conchold`` решает;
    снял → DEL lease + DECR (ровно один раз). ``False`` — job НЕ владел
    (повторный вызов no-op, чужой резерв не тронут — двойной park/resume
    не декрементит дважды, обход D6 закрыт). Контракт воркера: finally
    после ``admit(..., job=...)``.
    """
    raw = _cached_script(redis, "CONC_RELEASE")(
        keys=[conc_key(user), conchold_key(user), conclease_key(user, job)],
        args=[job],
    )
    return bool(int(raw))


@quota_fail_closed
def conc_heartbeat(
    user: str,
    job: str,
    *,
    redis: Any,
    lease_ttl_ms: int = DEFAULT_CONC_LEASE_TTL_MS,
) -> bool:
    """Продлить lease резерва (воркер жив; P1-3, паттерн Slots.heartbeat).

    ``False`` — резерва нет (истёк и снят свипером): воркер обязан
    остановить работу — его место уже отдано, списание будет чужим.
    """
    raw = _cached_script(redis, "CONC_HEARTBEAT")(
        keys=[conclease_key(user, job)],
        args=[lease_ttl_ms],
    )
    return bool(int(raw))


@quota_fail_closed
def conc_reclaim_expired(
    user: str,
    job: str,
    *,
    redis: Any,
    stream_maxlen: int = DEFAULT_QUOTA_STREAM_MAXLEN,
) -> bool:
    """Снять МЁРТВЫЙ резерв job'а: маркер есть + lease истёк (P1-3).

    Атомарная Lua CONC_RECLAIM (паттерн ``Slots.reclaim_expired``): SREM+DECR
    одной Lua, гонка «lease ожил между проверкой и снятием» закрыта
    перепроверкой EXISTS внутри скрипта. Пишет событие
    ``conc_reservation_reclaimed`` в ``ws:quota:events`` (I12: MAXLEN ~).
    ``False`` — job не владеет резервом или lease жив (воркер дышит).
    """
    raw = _cached_script(redis, "CONC_RECLAIM")(
        keys=[
            conc_key(user),
            conchold_key(user),
            conclease_key(user, job),
            QUOTA_EVENTS_KEY,
        ],
        args=[
            job,
            _quota_event("conc_reservation_reclaimed", user=user, job=job),
            stream_maxlen,
        ],
    )
    return bool(int(raw))


@quota_fail_closed
def sweep_expired_conc(user: str, *, redis: Any) -> list[str]:
    """Снять ВСЕ истёкшие резервы пользователя (свипер P1-3).

    ``SMEMBERS ws:quota:conchold:{user}`` → ``conc_reclaim_expired`` по
    каждому (без SCAN/KEYS — I12: ключ пользователя известен). Возвращает
    sorted-список снятых job'ов (детерминированно для наблюдения/тестов).
    Глобальный обход всех пользователей — wiring-контур (P1-5: перечисление
    живых пользователей по job-store, не по ключам redis).
    """
    reclaimed: list[str] = []
    for job in redis.smembers(conchold_key(user)):
        if conc_reclaim_expired(user, job, redis=redis):
            reclaimed.append(job)
    return sorted(reclaimed)
