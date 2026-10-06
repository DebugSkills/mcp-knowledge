"""Durable job-store AI-верстака: ``ws:job:{id}`` + статус-машина (Ф3.1).

Спека Scheduler §2 (plans/_provenance/arch-2026-10-05-ai-workspace/
…-scheduler-spec.md, строки 23-36): ``ws:job:{id}`` — HASH с полями user,
account_level, class, mode, step, state, vft, retry, epoch; Ф3.1 добавляет
id, attempt, zone, cursor, board_versions, created, updated, version.

Инварианты плана (REV.12):
- I3: владелец статуса job — Mode engine; union-статус один и живёт здесь.
  Scheduler-очереди (``ws:q:*``) — отдельные ключи (Ф3.2+): переход в
  running/preempted меняет ТОЛЬКО job-статус, постановка в ZSET — не здесь.
- I4: эффекты идемпотентны через ``effect_id = H(job, node, effect)`` БЕЗ
  attempt (``compute_effect_id``); коммиты fenced по epoch: запись с
  ``epoch < current.epoch`` отвергается (``StaleEpoch``), поздний redelivery
  старого владельца не проходит. Новый владелец забирает job переходом с
  бОльшим epoch — записанный epoch становится его.

Конкурентность (CAS):
- ``transition`` сверяет и инкрементит ``version`` В ОДНОЙ Lua-операции
  (EVALSHA: hget + hset атомарно) — read-then-write гонки нет. Легальность
  перехода проверяется на клиенте по только что прочитанной записи: любой
  конкурентный переход инкрементит version → CAS второго писателя падает
  (``VersionConflict``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

KEY_PREFIX = "ws:job:"
"""Префикс ws-контура (спека §2); job-store владеет только ws:job:{id}."""


class JobStoreError(RuntimeError):
    """Базовая ошибка job-store."""


class IllegalTransition(JobStoreError):
    """Переход не разрешён таблицей ALLOWED_TRANSITIONS."""


class JobNotFound(JobStoreError):
    """Ключ ws:job:{id} отсутствует."""


class JobAlreadyExists(JobStoreError):
    """create: job с таким id уже есть (hsetnx id проигран)."""


class VersionConflict(JobStoreError):
    """CAS: version в хранилище != expect_version (пишал конкурентная сторона)."""


class StaleEpoch(JobStoreError):
    """Epoch-fencing: epoch коммита < текущего epoch job (поздний redelivery)."""


class JobState(StrEnum):
    """Union-статус job (I3); waiting_human = human-gate, sleeping = сон/backoff."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SLEEPING = "sleeping"
    PREEMPTED = "preempted"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    # admission создал job; воркер взял → running; отмена/исчерпание retry до старта
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    # спека §3 (sweeper: lease истёк → re-enqueue, retry++) даёт running→queued;
    # §4: human-gate → sleeping/waiting; preempt на границе вызова; финалы
    JobState.RUNNING: frozenset(
        {
            JobState.QUEUED,
            JobState.WAITING_HUMAN,
            JobState.SLEEPING,
            JobState.PREEMPTED,
            JobState.DONE,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    # оператор ответил (resume) / отмена / таймаут-политика gate
    JobState.WAITING_HUMAN: frozenset(
        {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}
    ),
    # проснулся → running; снятие с ожидания
    JobState.SLEEPING: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    # слот возвращён; re-acquire → running; переклассификация → queued
    JobState.PREEMPTED: frozenset(
        {JobState.RUNNING, JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.DONE: frozenset(),
    # retry по явному решению engine (off-cycle)
    JobState.FAILED: frozenset({JobState.QUEUED}),
    JobState.CANCELLED: frozenset(),
}


def validate_transition(old: JobState | str, new: JobState | str) -> None:
    """Чистая функция: легален ли переход old→new. Нелегален → IllegalTransition.

    Строки коэрсятся в JobState; неизвестное значение → ValueError.
    """
    old_s = JobState(old)
    new_s = JobState(new)
    if new_s not in ALLOWED_TRANSITIONS[old_s]:
        raise IllegalTransition(
            f"нелегальный переход статуса job: {old_s.value} -> {new_s.value} "
            f"(разрешены: {sorted(s.value for s in ALLOWED_TRANSITIONS[old_s]) or 'нет — терминальный'})"
        )


def compute_effect_id(job_id: str, node: str, effect: str) -> str:
    """Детерминированный effect-id (I4): H(job, node, effect), БЕЗ attempt.

    Повторная доставка того же эффекта с того же узла даёт тот же id —
    дедупликация на стороне исполнения; attempt в id НЕ входит.
    """
    return hashlib.sha256(f"{job_id}\x1f{node}\x1f{effect}".encode()).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class JobRecord:
    """Материализованная запись ws:job:{id} (все поля HASH — см. job_to_hash)."""

    id: str
    user: str
    account_level: str
    job_class: str          # HASH-поле "class" (python-ключевое слово)
    mode: str
    zone: str
    state: JobState = JobState.QUEUED
    step: int = 0
    vft: float = 0.0
    retry: int = 0
    epoch: int = 0
    attempt: int = 0
    cursor: str = ""
    board_versions: dict[str, int] = field(default_factory=dict)
    created: str = ""
    updated: str = ""
    version: int = 1


_INT_FIELDS = frozenset({"step", "retry", "epoch", "attempt", "version"})
_HASH_TO_REC = {"class": "job_class"}  # python-имя отличается от HASH-поля
_REC_TO_HASH = {"job_class": "class"}

_PATCHABLE_FIELDS = frozenset(
    {"mode", "step", "vft", "retry", "attempt", "zone", "cursor", "board_versions"}
)
"""Поля, разрешённые в transition(patch=...); id/version/created/state/updated/
epoch — под управлением хранилища (epoch ставит Lua-fencing, state — new_state)."""


def job_to_hash(rec: JobRecord) -> dict[str, str]:
    """JobRecord → плоский mapping для HSET (board_versions — JSON, отсортирован)."""
    out: dict[str, str] = {}
    for f in fields(rec):
        name = _REC_TO_HASH.get(f.name, f.name)
        val = getattr(rec, f.name)
        if name == "board_versions":
            out[name] = json.dumps(val, sort_keys=True, ensure_ascii=False)
        elif isinstance(val, StrEnum):
            out[name] = str(val)
        else:
            out[name] = str(val)
    return out


def job_from_hash(data: Mapping[str, str]) -> JobRecord:
    """HGETALL (все значения str) → JobRecord; отсутствующие поля → дефолты."""
    kwargs: dict[str, Any] = {}
    for f in fields(JobRecord):
        hash_name = _REC_TO_HASH.get(f.name, f.name)
        raw = data.get(hash_name)
        if raw is None:
            continue
        if f.name == "board_versions":
            kwargs[f.name] = json.loads(raw) if raw else {}
        elif f.name == "state":
            kwargs[f.name] = JobState(raw)
        elif f.name in ("step", "retry", "epoch", "attempt", "version"):
            kwargs[f.name] = int(raw)
        elif f.name == "vft":
            kwargs[f.name] = float(raw)
        else:
            kwargs[f.name] = raw
    kwargs.setdefault("state", JobState.QUEUED)
    kwargs.setdefault("version", 1)
    return JobRecord(**kwargs)


# CAS-переход: проверка expect_version + fencing по epoch + запись — одна
# операция EVAL (redis-py register_script даёт EVALSHA с fallback на EVAL).
# Возвращает 'NOT_FOUND' | 'VERSION_CONFLICT' | 'STALE_EPOCH' | новую version.
# ARGV: [1]=expect_version [2]=epoch [3]=new_state [4]=updated, далее k/v патча.
_TRANSITION_LUA = r"""
local ver = redis.call('HGET', KEYS[1], 'version')
if not ver then return 'NOT_FOUND' end
if tonumber(ver) ~= tonumber(ARGV[1]) then return 'VERSION_CONFLICT' end
local new_epoch = tonumber(ARGV[2])
local cur_epoch = tonumber(redis.call('HGET', KEYS[1], 'epoch')) or 0
if new_epoch < cur_epoch then return 'STALE_EPOCH' end
local nv = tonumber(ver) + 1
redis.call('HSET', KEYS[1], 'epoch', new_epoch, 'version', nv, 'updated', ARGV[4])
if ARGV[3] ~= '' then
  redis.call('HSET', KEYS[1], 'state', ARGV[3])
end
local i = 5
while i < #ARGV do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
  i = i + 2
end
return tostring(nv)
"""


class JobStore:
    """Durable job-store поверх HASH ``ws:job:{id}`` (владелец статуса — I3)."""

    def __init__(self, client: Any) -> None:
        """``client`` — redis-клиент с decode_responses=True (см. redis_client)."""
        self.client = client
        self._transition = client.register_script(_TRANSITION_LUA)

    @staticmethod
    def _key(job_id: str) -> str:
        return f"{KEY_PREFIX}{job_id}"

    # ── API ──────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        user: str,
        account_level: str,
        job_class: str,
        mode: str,
        zone: str,
        job_id: str | None = None,
        step: int = 0,
        vft: float = 0.0,
        retry: int = 0,
        epoch: int = 0,
        attempt: int = 0,
        cursor: str = "",
        board_versions: dict[str, int] | None = None,
    ) -> JobRecord:
        """Создать job в state=queued, version=1 (атомарный захват id: HSETNX)."""
        job_id = job_id or uuid4().hex
        now = _utcnow_iso()
        rec = JobRecord(
            id=job_id,
            user=user,
            account_level=account_level,
            job_class=job_class,
            mode=mode,
            zone=zone,
            state=JobState.QUEUED,
            step=step,
            vft=vft,
            retry=retry,
            epoch=epoch,
            attempt=attempt,
            cursor=cursor,
            board_versions=board_versions or {},
            created=now,
            updated=now,
            version=1,
        )
        key = self._key(job_id)
        if not self.client.hsetnx(key, "id", job_id):
            raise JobAlreadyExists(f"job {job_id!r} уже существует")
        self.client.hset(key, mapping=job_to_hash(rec))
        return rec

    def get(self, job_id: str) -> JobRecord:
        """Прочитать запись целиком; отсутствует → JobNotFound."""
        data = self.client.hgetall(self._key(job_id))
        if not data:
            raise JobNotFound(f"job {job_id!r} не найден ({KEY_PREFIX}*)")
        return job_from_hash(data)

    def transition(
        self,
        job_id: str,
        new_state: JobState | str,
        *,
        expect_version: int,
        epoch: int,
        patch: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """CAS-переход статуса (+патч полей) с epoch-fencing. Возвращает запись.

        Гонки: конкурентная запись между get() и EVAL инкрементит version →
        VERSION_CONFLICT у проигравшего; state проверен по прочитанной версии,
        поэтому CAS-победитель валиден (validate_transition до Lua).
        """
        new_state_s = JobState(new_state)
        current = self.get(job_id)  # JobNotFound наружу
        validate_transition(current.state, new_state_s)
        encoded_patch = self._encode_patch(patch)
        args: list[Any] = [expect_version, epoch, new_state_s.value, _utcnow_iso()]
        for k, v in encoded_patch.items():
            args.extend((k, v))
        result = self._transition(keys=[self._key(job_id)], args=args)
        if result == "NOT_FOUND":  # pragma: no cover — удалён между get и EVAL
            raise JobNotFound(f"job {job_id!r} исчез между get и CAS")
        if result == "VERSION_CONFLICT":
            raise VersionConflict(
                f"CAS: job {job_id!r} ожидалась version={expect_version}, "
                f"фактически {current.version + 1}? перечитайте и повторите"
            )
        if result == "STALE_EPOCH":
            raise StaleEpoch(
                f"epoch-fencing: job {job_id!r} epoch={epoch} < текущего; "
                "redelivery старого владельца отвергнут"
            )
        return self.get(job_id)

    def patch(
        self,
        job_id: str,
        *,
        expect_version: int,
        epoch: int,
        patch: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """CAS-патч полей **без смены статуса** (курсор/версии доски; Ф3.5b-2).

        Тот же Lua, что ``transition``, но ``state`` не пишется (ARGV[3]=''):
        легальность перехода не проверяется — статус остаётся прежним.
        """
        encoded_patch = self._encode_patch(patch)
        args: list[Any] = [expect_version, epoch, "", _utcnow_iso()]
        for k, v in encoded_patch.items():
            args.extend((k, v))
        result = self._transition(keys=[self._key(job_id)], args=args)
        if result == "NOT_FOUND":
            raise JobNotFound(f"job {job_id!r} не найден при patch")
        if result == "VERSION_CONFLICT":
            raise VersionConflict(
                f"CAS(patch): job {job_id!r} ожидалась version={expect_version}; перечитайте"
            )
        if result == "STALE_EPOCH":
            raise StaleEpoch(f"epoch-fencing (patch): job {job_id!r} epoch={epoch} устарел")
        return self.get(job_id)

    # ── внутреннее ───────────────────────────────────────────────────────

    @staticmethod
    def _encode_patch(patch: Mapping[str, Any] | None) -> dict[str, str]:
        if not patch:
            return {}
        out: dict[str, str] = {}
        for k, v in patch.items():
            if k not in _PATCHABLE_FIELDS:
                raise ValueError(
                    f"поле {k!r} не патчится через transition "
                    f"(разрешены: {sorted(_PATCHABLE_FIELDS)})"
                )
            if k == "board_versions":
                out[k] = json.dumps(v, sort_keys=True, ensure_ascii=False)
            else:
                out[k] = str(v)
        return out
