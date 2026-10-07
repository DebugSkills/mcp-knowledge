"""Per-job priority override (Ф4.5a, решение D8 «разово»): ``ws:prio:{job}``.

trace_id: arch-2026-10-05-ai-workspace. Контракт оператора: override живёт в
Redis и читается при admission/enqueue **последующих** вызовов job'а;
ОЧЕРЕДЬ НЕ ТРОГАЕТСЯ — уже стоящие в ``ws:q:{shelf}`` вызовы не реордерятся,
``vft``/``vftlast``/``ws:pos`` не пересчитываются (никакого
``requeue(vft_override)``). Этот модуль очередных структур не касается
вообще: только STRING-ключ + события + чистая резолюция приоритета.

- Ключ ``ws:prio:{job}``: значение ``high|med|low``; запись ``SET EX`` —
  значение и TTL атомарны, повторная установка ПРОДЛЕВАЕТ TTL (каждый SET
  перезаписывает EXPIRE). ``DEFAULT_TTL_S`` = 24 ч: D8 «разово» → забытый
  буст не перекашивает WFQ-матрицу вечно и не требует ручной чистки; 24 ч
  покрывает рабочий день + ночные повторные запуски/resume и превышает
  максимальный aging-горизонт (T_STARVE background = 2 ч).
- Валидация fail-closed, SSOT — ``policy.MULT`` (ключи weight-матрицы WFQ):
  значение override протекает в ``Queue.enqueue → policy.weight →
  MULT[prio]`` (KeyError при невалиде) — множество допустимых значений =
  ровно ключи MULT, валидируем у потребителя; ``registry.quotas.PRIORITIES``
  — DOWNSTREAM от MULT (D2) и требует Registry-инстанции + I/O, ``policy``
  — чистый модуль.
- ``effective_priority``: мусор в ключе (возможен только ручной правкой
  мимо API) → fallback на приоритет аккаунта + ``logging.warning`` + событие
  ``job_priority_invalid``. Availability > strictness: override —
  операционная ручка, а не гейт; покоцанный ключ не должен останавливать
  admission пользователя при полностью рабочем дефолте (приоритет аккаунта);
  аномалия фиксируется логом + событием оператору.
- События — в ``ws:quota:events`` (MAXLEN ~, I12), JSON c ``ts`` — формат
  SSOT ``admission._quota_event`` (переиспользован: единый вид стрима).
  Читать хвостом ``XREVRANGE`` — стрим общий, накапливается.
- Деградация ws-redis — политика admission (P1-4): Connection/Timeout →
  ``QuotaRedisUnavailable`` (fail-closed) + ALARM. ``wiring.submit`` читает
  override ДО admit → отказ происходит ДО взятия conc-резерва, модель
  отказа постановки не меняется (admit следом так же fail-closed).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ai_workspace.scheduler.admission import (
    DEFAULT_QUOTA_STREAM_MAXLEN,
    QUOTA_EVENTS_KEY,
    QuotaRedisUnavailable,
    _emit_degraded,
    _quota_event,
)
from ai_workspace.scheduler.policy import MULT

__all__ = [
    "DEFAULT_TTL_S",
    "VALID_PRIORITIES",
    "clear_job_priority",
    "effective_priority",
    "emit_event",
    "get_job_priority",
    "prio_key",
    "set_job_priority",
]

DEFAULT_TTL_S = 86_400
"""TTL override по умолчанию — 24 ч (см. докстроку модуля)."""

VALID_PRIORITIES = frozenset(MULT)
"""Допустимые значения = ключи ``policy.MULT`` (SSOT, §отчёта Ф4.5a)."""

logger = logging.getLogger("ai_workspace.scheduler.prio")

_T = TypeVar("_T")


def prio_key(job: str) -> str:
    """Ключ override приоритета job'а: ``ws:prio:{job}``."""
    return f"ws:prio:{job}"


def emit_event(client: Any, type_: str, **fields: Any) -> None:
    """Событие приоритет-контура в ``ws:quota:events`` — best-effort.

    Наблюдение не имеет права валить операцию (паттерн best-effort XADD из
    ``_emit_degraded``); потребитель — оператор (хвост стрима). Ловится весь
    ``RedisError``, не только Connection/Timeout: при ``--maxmemory`` +
    ``noeviction`` (Ф6 TODO 8, I12/D2) исчерпание ws-redis отказывает на
    записывающих командах OOM'ом (``OutOfMemoryError`` — наследник
    ``RedisError``); событие наблюдения глотается с логом, постановка job'а
    (``wiring.submit`` → ``_resolve_priority``) не валится.
    """
    try:
        client.xadd(
            QUOTA_EVENTS_KEY,
            {"event": _quota_event(type_, **fields)},
            maxlen=DEFAULT_QUOTA_STREAM_MAXLEN,
            approximate=True,
        )
    except RedisError as exc:
        # Ф6 TODO 8 (I12/D2): ws-redis — --maxmemory 200mb + noeviction
        # (compose.workspace.yml); при исчерпании памяти XADD отказывает
        # OOM'ом (redis-py: OutOfMemoryError/ResponseError — наследники
        # RedisError), а не Connection/Timeout. Событие — наблюдение:
        # глотаем ЛЮБОЙ RedisError (лог), submit/узел не валятся.
        logger.warning(
            "prio: событие %s не записано (ws-redis: %s)", type_, exc
        )


def _prio_fail_closed(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Деградация ws-redis → ``QuotaRedisUnavailable`` + ALARM (P1-4).

    Та же политика, что ``admission.quota_fail_closed``, но клиент здесь —
    позиционный ``client`` (контракт Ф4.5a), а не kwarg ``redis``: ALARM
    передаётся явно в ``_emit_degraded`` (одно место формата деградации).
    """

    @wraps(fn)
    def wrapper(client: Any, *args: Any, **kwargs: Any) -> _T:
        try:
            return fn(client, *args, **kwargs)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            _emit_degraded(fn.__name__, exc, client)
            raise QuotaRedisUnavailable(
                f"prio op {fn.__name__!r}: ws-redis недоступен "
                f"({type(exc).__name__}) — отказ fail-closed (Ф4.5a); "
                "подробности — logging ALARM quota_degraded"
            ) from exc

    return wrapper


def _validate_prio(prio: str) -> None:
    """Fail-closed валидация значения: SSOT — ``policy.MULT``."""
    if prio not in MULT:
        raise ValueError(
            f"prio должен быть одним из {sorted(MULT)} "
            f"(SSOT — policy.MULT), получено: {prio!r}"
        )


@_prio_fail_closed
def set_job_priority(
    client: Any,
    job: str,
    prio: str,
    *,
    ttl_s: int = DEFAULT_TTL_S,
    actor: str = "",
    reason: str = "",
) -> None:
    """Установить/продлить override приоритета job'а (``SET EX``).

    Валидация — ДО любого обращения к redis (fail-closed: мусор через API
    попасть в ключ не может). Повторный вызов перезаписывает значение и
    ПРОДЛЕВАЕТ TTL. Событие ``job_priority_set`` (job/prio/ttl_s/actor/
    reason) — в ``ws:quota:events``, best-effort. Очередь НЕ трогается:
    override подхватят только ПОСЛЕДУЮЩИЕ admit/enqueue вызовов job'а.
    """
    if not isinstance(job, str) or not job.strip():
        raise ValueError(f"job должен быть непустой строкой, получено: {job!r}")
    _validate_prio(prio)
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or ttl_s <= 0:
        raise ValueError(f"ttl_s должен быть целым > 0, получено: {ttl_s!r}")
    client.set(prio_key(job), prio, ex=ttl_s)
    emit_event(
        client, "job_priority_set",
        job=job, prio=prio, ttl_s=ttl_s, actor=actor, reason=reason,
    )


@_prio_fail_closed
def clear_job_priority(client: Any, job: str, *, actor: str = "") -> bool:
    """Снять override (``DEL``); ``True`` — ключ был, ``False`` — нет.

    Идемпотентен; событие ``job_priority_cleared`` (с ``removed``) пишется
    всегда — фиксируется сама команда оператора.
    """
    removed = bool(client.delete(prio_key(job)))
    emit_event(client, "job_priority_cleared", job=job, actor=actor, removed=removed)
    return removed


@_prio_fail_closed
def get_job_priority(client: Any, job: str) -> str | None:
    """Текущий override job'а; ``None`` — ключа нет (работает аккаунт)."""
    return client.get(prio_key(job))


def effective_priority(
    account_prio: str,
    job_prio: str | None,
    *,
    redis: Any = None,
    job: str = "",
) -> tuple[str, str]:
    """Резолюция: ``(prio, source)``, ``source ∈ {"job", "account"}``.

    - ``job_prio is None`` → ``(account_prio, "account")``;
    - валидный → ``(job_prio, "job")``;
    - мусор → fallback на аккаунт + ``logging.warning`` + событие
      ``job_priority_invalid`` (best-effort, при ``redis``): availability >
      strictness — см. докстроку модуля.

    Чистая по сути: redis нужен только событию идентичности (``job``);
    ``account_prio`` приходит из реестра (валидирован при загрузке, Ф4.1).
    """
    if job_prio is None:
        return account_prio, "account"
    if job_prio in MULT:
        return job_prio, "job"
    logger.warning(
        "job_priority_invalid: ws:prio:%s=%r не входит в %s — fallback на "
        "приоритет аккаунта %r (availability > strictness, Ф4.5a)",
        job, job_prio, sorted(MULT), account_prio,
    )
    if redis is not None:
        emit_event(
            redis, "job_priority_invalid",
            job=job, value=job_prio, fallback=account_prio,
        )
    return account_prio, "account"
