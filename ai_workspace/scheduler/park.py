"""Budget-hard-stop: park/resume job'а (Ф4.3, инвариант I10 / решение D5).

trace_id: arch-2026-10-05-ai-workspace (план REV.13), Ф4.3. Паттерн обвязки —
queue.py/slots.py (Ф3.2/Ф3.3), паттерн ошибок — orchestrator/job.py.

park (D5: ext-бюджет исчерпан, сигнал ``admit() -> Decision(park)`` из Ф4.2,
или команда оператора):
- состояние job → ``parked`` (БЕЗ failed-эффектов: ``ws:fx:*`` не трогается,
  это НЕ failure — бюджетный pause);
- вызов изымается из ВСЕХ индексов полки одной Lua (``queue.lua:PARK`` через
  ``Queue.park_call``): ``ZREM ws:q`` + ``ZREM ws:starve`` + ``SREM holders``
  + ``DEL lease`` + ``DEL ws:pos:{job}`` + ``XADD`` события ``parked``;
- conc-резерв пользователя освобождается (``conc_exit``, Ф4.2) — parked не
  занимает личный параллелизм;
- vft- и starve-кредит зеркалится в хеш job (``vft``/``starve_deadline``,
  durable) — SSOT для восстановления остаётся per-call HASH
  ``ws:call:{shelf}:{call}`` (его читает REQUEUE), хеш job — наблюдение и
  резерв;
- epoch инкрементируется: park забирает владение, поздние коммиты
  допаркового владельца отвергаются (I4, ``StaleEpoch``);
- идемпотентен: park уже-parked → ``False`` без записей и событий.

resume (nightly reconcile D4 «бюджет вернулся» или команда админа):
- перепроверяет admission (``admit()`` Ф4.2): ``park``/``deny`` → ``False``,
  job остаётся ``parked`` БЕЗ записей (не падать);
- ``allow`` уже берёт conc-резерв (режим РЕЗЕРВ) — ``conc_enter`` НЕ
  вызывается: путь «admit ИЛИ conc_enter» (admission.py), двойного учёта нет;
- вызов возвращается ``Queue.requeue`` — исходные vft и starve-дедлайн из
  per-call HASH (I2): resume НЕ теряет приоритет (встаёт впереди
  одноуровневых, вставших за время парковки);
- epoch-FENCING сохраняется: requeue с текущим epoch job (после park-бампа),
  допарковые эффекты с меньшим epoch не проходят и после resume.

Отказы устойчивы в сторону hard-stop: индексы/слот/conc освобождаются ДО
CAS-перехода — проигранный CAS оставляет вызов вне очереди (dequeue его не
выдаст, бюджет не тратится), caller перечитывает и повторяет park.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ai_workspace.orchestrator.job import (
    JobState,
    JobStore,
    validate_transition,
)
from ai_workspace.scheduler.admission import admit, conc_exit
from ai_workspace.scheduler.queue import Queue
from ai_workspace.scheduler.slots import DEFAULT_STREAM_MAXLEN, Slots

__all__ = ["JobNotParked", "ParkControl", "ParkError", "pos_key"]


class ParkError(RuntimeError):
    """Базовая ошибка контура park/resume (паттерн JobStoreError)."""


class JobNotParked(ParkError):
    """``resume`` применён к job вне ``parked``.

    Fail-loud, а не молчаливый no-op: тихий «успех» маскировал бы дрейф
    статус-машины (двойной resume, resume чужого состояния). Повторный
    resume после успешного тоже попадает сюда — состояние уже ``queued``.
    """


def pos_key(job_id: str) -> str:
    """Ключ панели очереди (спека §2): ``ws:pos:{job}`` (потребитель Ф3.5+;
    park его снимает — панель не показывает parked как ждущего)."""
    return f"ws:pos:{job_id}"


class ParkControl:
    """Оркестратор park/resume над job-store + очередями + admission полки.

    Отдельный модуль (НЕ методы Queue/JobStore): операция сквозная — статус
    (JobStore), индексы полки (Queue), слот (Slots), квоты (admission);
    замыкание её в один из классов потянуло бы импорты остальных (нарушение
    разделения Ф3.1/Ф3.2). Каждая часть переиспользуется как есть.
    """

    def __init__(
        self,
        client: Any,
        *,
        shelf: str = "local",
        store: JobStore | None = None,
        stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``client`` — ws-redis (decode_responses=True); ``store`` — job-store
        (по умолчанию свой ``JobStore(client)``); ``clock`` — источник ``now``
        (инъекция для детерминированных тестов, паттерн queue.py)."""
        self.client = client
        self.shelf = shelf
        self.store = store if store is not None else JobStore(client)
        self.queue = Queue(client, shelf=shelf, clock=clock)
        self.slots = Slots(client, shelf=shelf, clock=clock)
        self.stream_maxlen = stream_maxlen
        self.clock = clock

    def _event(
        self,
        type_: str,
        call: str,
        job_id: str,
        epoch: int,
        state: str,
        reason: str,
    ) -> str:
        """JSON события в ``ws:events:{shelf}`` (формат Slots.event + reason;
        отдельный строитель — reason нужен только этому контуру, один потребитель)."""
        payload: dict[str, Any] = {
            "type": type_,
            "shelf": self.shelf,
            "call": call,
            "job": job_id,
            "epoch": epoch,
            "ts": round(self.clock(), 6),
            "state": state,
        }
        if reason:
            payload["reason"] = reason
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    # ── API ──────────────────────────────────────────────────────────────

    def park(
        self,
        job_id: str,
        *,
        call: str | None = None,
        reason: str = "command",
    ) -> bool:
        """Увести job в парк (D5/команда). ``True`` — уведён, ``False`` — уже
        ``parked`` (идемпотентный no-op без записей).

        ``call`` — текущий вызов job'а (``job:step:attempt``): если job стартовал
        — слот и lease освобождаются; если стоит в очереди — изымается из обоих
        индексов; ``None`` — парк до постановки (admission-park: только статус,
        conc и событие). ``reason`` — в событие ``parked`` (наблюдение).
        Нелегальный переход (терминал/waiting_human) — ``IllegalTransition``
        ДО любых записей Redis.
        """
        rec = self.store.get(job_id)  # JobNotFound наружу
        if rec.state is JobState.PARKED:
            return False
        validate_transition(rec.state, JobState.PARKED)
        patch: dict[str, float] | None = None
        if call is not None:
            self.queue.park_call(
                call,
                job=job_id,
                event_json=self._event(
                    "parked", call, job_id, rec.epoch, "parked", reason
                ),
                stream_maxlen=self.stream_maxlen,
            )
            crec = self.queue.call_record(call)
            if crec:  # durable-зеркало кредитов в хеше job (0-кредит — паркуй как есть)
                patch = {"vft": crec["vft"], "starve_deadline": crec["starve_deadline"]}
        # conc-резерв не течёт (Ф4.2): exit идемпотентен (пол 0), берётся всегда —
        # путь park мог пройти и без admit-резерва (команда оператора).
        conc_exit(rec.user, redis=self.client)
        # CAS-переход: state→parked + epoch+1 (владение у park: допарковые
        # коммиты с меньшим epoch → StaleEpoch, I4) + кредиты в хеш job.
        self.store.transition(
            job_id,
            JobState.PARKED,
            expect_version=rec.version,
            epoch=rec.epoch + 1,
            patch=patch,
        )
        return True

    def resume(self, job_id: str, *, registry: Any, call: str | None = None) -> bool:
        """Вернуть job из парка. ``True`` — вернул (state → ``queued``, вызов в
        очереди с исходным vft/starve), ``False`` — admission всё ещё блокирует
        (``park``: бюджет D5; ``deny``: личная квота), job остаётся ``parked``
        без записей. Не-parked → ``JobNotParked`` (fail-loud).

        ``registry`` — ``QuotaRegistry`` (Ф4.1); ``call`` — вызов для
        восстановления (``None`` — парк был до постановки: только статус).
        conc учитывается ровно один раз — резервом из ``admit(allow)``.
        """
        rec = self.store.get(job_id)
        if rec.state is not JobState.PARKED:
            raise JobNotParked(
                f"resume: job {job_id!r} не в parked (state={rec.state.value}); "
                "resume применяется только к parked"
            )
        decision = admit(
            rec.user,
            rec.account_level,
            registry=registry,
            redis=self.client,
            shelf=self.shelf,
        )
        if not decision.allowed:
            return False
        # REQUEUE: vft/starve из per-call HASH (I2 — приоритет сохранён),
        # fence по текущему epoch job (после park-бампа — I4 держит и
        # после resume). Отказ = записи нет = кредит утерян: fail-closed —
        # вернуть взятый admit-ом conc-резерв, job остаётся в парке.
        if call is not None and not self.queue.requeue(
            call, now=self.clock(), epoch=rec.epoch
        ):
            conc_exit(rec.user, redis=self.client)
            raise ParkError(
                f"resume: per-call запись {call!r} недоступна — vft/starve-"
                f"кредит утерян; job {job_id!r} оставлен в парке"
            )
        # Порядок requeue → CAS: между ними вызов уже в очереди при state=
        # parked — dequeue может выдать его «рано», но это и есть цель resume
        # (бюджет уже подтверждён admit-ом выше). Обратный порядок оставлял бы
        # state=queued без вызова в очереди — невидимый ствол до след. tick.
        self.store.transition(
            job_id,
            JobState.QUEUED,
            expect_version=rec.version,
            epoch=rec.epoch,
        )
        self.client.xadd(
            f"ws:events:{self.shelf}",
            {"event": self._event("resumed", call or "", job_id, rec.epoch, "queued", "")},
            maxlen=self.stream_maxlen,
            approximate=True,
        )
        return True
