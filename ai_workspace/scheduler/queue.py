"""Python-обёртки атомарных Lua-скриптов планировщика (Ф3.2).

Единица планирования — один LLM-вызов (``call`` = ``make_call(...)``);
9 логических очередей (3 приоритета × 3 класса) живут в ДВУХ ZSET полки:
``ws:q:{shelf}`` (score = vft) и ``ws:starve:{shelf}`` (score = дедлайн
aging-пола); снятие из обоих индексов — одной Lua (queue.lua/dequeue).

Ключи (спека §2): ``ws:q:{shelf}``, ``ws:starve:{shelf}``, ``ws:vt:{shelf}``,
``ws:vftlast:{shelf}:{p}:{c}``; per-call запись ``ws:call:{shelf}:{call}``
(HASH: prio/class/job/epoch/attempt/vft/starve_deadline) — пишется
``enqueue`` (Ф3.4), читается ``requeue``/``preempt``/``call_record``.
``now`` инъектируется (clock callable) — детерминированные тесты; Lua время
сам не читает.

Панель очереди (Ф4.4a): опциональный хук ``on_queue_change(shelf)`` стреляет
после изменения состава очереди (enqueue/requeue/preempt-через-requeue/park);
снятие (dequeue/dequeue_and_acquire/complete(call=...)/park_call) ДОПОЛНИТЕЛЬНО
гасит ``ws:pos:{call}`` изъятого вызова. Хук best-effort: панель — display-only,
любой её сбой глотается с warning и не пробрасывается (позиция не входит в
критический путь планирования). Прод-проводка — ``wiring.make_on_queue_change``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ai_workspace.scheduler import policy
from ai_workspace.scheduler.lua_scripts import extract_sections, section_of

_LUA_SOURCE = Path(__file__).with_name("queue.lua").read_text(encoding="utf-8")
"""SSOT Lua-кода — читается один раз при импорте (файл рядом с модулем)."""

_SECTIONS = extract_sections(_LUA_SOURCE, source_name="queue.lua")
"""Секции ``-- @script`` (общий загрузчик lua_scripts, P2-5 критики Ф4)."""

logger = logging.getLogger(__name__)
"""Лог обёрток очереди: предупреждения best-effort хука панели (Ф4.4a)."""


def _script(name: str) -> str:
    """Секция ``-- @script {name}`` из queue.lua (загружено при импорте)."""
    return section_of(_SECTIONS, name, source_name="queue.lua")


def f2s(x: float) -> str:
    """Round-trip строка float для ARGV/score (%.17g — без усечения Redis)."""
    return f"{x:.17g}"


class Queue:
    """Очередь полки ``shelf``: enqueue / dequeue / requeue / preempt / complete."""

    def __init__(
        self,
        client: Any,
        shelf: str = "local",
        clock: Callable[[], float] = time.time,
        on_queue_change: Callable[[str], None] | None = None,
    ) -> None:
        """``client`` — redis-клиент с decode_responses=True (redis_client);
        ``clock`` — источник ``now`` (по умолчанию time.time; монотонность
        обеспечивает caller — wall-clock нужен для starve-дедлайнов);
        ``on_queue_change`` — best-effort хук панели очереди (Ф4.4a):
        ``None`` -> панель не подключена (юнит-тесты без Redis-зависимостей)."""
        self.client = client
        self.shelf = shelf
        self.clock = clock
        self.on_queue_change = on_queue_change
        self.q_key = f"ws:q:{shelf}"
        self.starve_key = f"ws:starve:{shelf}"
        self.vt_key = f"ws:vt:{shelf}"
        self._enqueue = client.register_script(_script("enqueue"))
        self._dequeue = client.register_script(_script("dequeue"))
        self._complete = client.register_script(_script("complete"))
        self._dequeue_acquire = client.register_script(
            _script("DEQUEUE_ACQUIRE")
        )
        self._requeue = client.register_script(_script("REQUEUE"))
        self._park = client.register_script(_script("PARK"))

    def _notify_queue_change(self, removed: Sequence[str] = ()) -> None:
        """Панель очереди (Ф4.4a): позвать ``on_queue_change(shelf)`` + ПОСЛЕ
        него снять ``ws:pos:{call}`` изъятых вызовов. Best-effort — ЛЮБОЙ
        сбой глотается с warning и НЕ пробрасывается: панель display-only,
        её отказ не имеет права ломать постановку/снятие вызовов (тест
        «сломанный хук не валит enqueue»). ``removed`` — вызовы, покинувшие
        очередь: ранг гасится сразу, не дожидаясь пересчёта хуком.

        ПОРЯДОК «хук → DEL» осознан (контракт complete: «ранг не должен
        пережить завершение вызова»): хук пересчитывает ранги по ws:q и
        может МАТЕРИАЛИЗОВАТЬ ранг вызову с устаревшим членством в очереди
        (снятие мимо хука, half-removed); DEL ПОСЛЕ хука гасит ранг в любом
        случае — снятие побеждает пересчёт. Обратный порядок (DEL → хук)
        «воскрешал» бы ранг завершённого вызова (дефект Ф4.4a-добивка).
        """
        if self.on_queue_change is not None:
            try:
                self.on_queue_change(self.shelf)
            except Exception:  # display-only: сбой хука не пробрасывается
                logger.warning(
                    "queue[%s]: on_queue_change упал (best-effort, игнор)",
                    self.shelf, exc_info=True,
                )
        if not removed:
            return
        try:
            pipe = self.client.pipeline()
            for call in removed:
                pipe.delete(f"ws:pos:{call}")
                pipe.srem(f"ws:posidx:{self.shelf}", call)
            pipe.execute()
        except Exception:  # display-only: панель не валит очередь
            logger.warning(
                "queue[%s]: гашение ws:pos упало (best-effort, игнор)",
                self.shelf, exc_info=True,
            )

    # ── API ──────────────────────────────────────────────────────────────

    def vftlast_key(self, prio: str, call_class: str) -> str:
        """Ключ last-VFT конкретной логической очереди (p, c) полки."""
        return f"ws:vftlast:{self.shelf}:{prio}:{call_class}"

    def call_key(self, call: str) -> str:
        """Ключ per-call записи ``ws:call:{shelf}:{call}`` (Ф3.4): исходное
        состояние (vft/starve/epoch) для requeue/preempt."""
        return f"ws:call:{self.shelf}:{call}"

    @staticmethod
    def make_call(job_id: str, step: int, attempt: int = 0) -> str:
        """Уникальный id вызова (член ZSET): ``job:step:attempt``."""
        return f"{job_id}:{step}:{attempt}"

    def enqueue(
        self,
        call: str,
        *,
        prio: str,
        call_class: str,
        cost_est: float,
        now: float | None = None,
        starve_deadline: float | None = None,
        weight: float | None = None,
        job: str = "",
        epoch: int = 0,
        attempt: int = 0,
    ) -> float:
        """Поставить вызов в очередь; возвращает vft.

        ``starve_deadline=None`` → ``now + T_starve[call_class]`` (aging-пол);
        ``weight=None`` → ``policy.weight(prio, call_class)``.
        ``job``/``epoch``/``attempt`` (Ф3.4) — в per-call HASH
        ``ws:call:{shelf}:{call}``: источник метаданных для preempt-кредита
        и epoch-fencing при requeue.
        """
        now = self.clock() if now is None else now
        w = policy.weight(prio, call_class) if weight is None else weight
        if starve_deadline is None:
            starve_deadline = now + policy.T_STARVE[call_class]
        raw = self._enqueue(
            keys=[
                self.q_key,
                self.vt_key,
                self.vftlast_key(prio, call_class),
                self.starve_key,
            ],
            args=[
                call,
                prio,
                call_class,
                f2s(w),
                f2s(cost_est),
                f2s(now),
                f2s(starve_deadline),
                job,
                epoch,
                attempt,
                self.call_key(call),
            ],
        )
        self._notify_queue_change()  # Ф4.4a: панель (сбой — best-effort)
        return float(raw)

    def dequeue(self, *, now: float | None = None, limit: int = 1) -> list[str]:
        """Снять до ``limit`` вызовов (правило ``policy.pick_best``;
        двух-индексное снятие q+starve). Порядок списка = порядок
        обслуживания. Слоты — НЕ здесь (Ф3.3)."""
        now = self.clock() if now is None else now
        out: Any = self._dequeue(
            # KEYS[3]=vt, KEYS[4]=vftlast — контракт dequeue-группы, скриптом
            # не читаются (""-заглушка: per-queue vftlast знает только caller).
            keys=[self.q_key, self.starve_key, self.vt_key, ""],
            args=[f2s(now), limit],
        )
        taken = list(out)
        self._notify_queue_change(removed=taken)  # Ф4.4a: снял — погаси ранги
        return taken

    def requeue(
        self,
        call: str,
        *,
        now: float | None = None,
        epoch: int = 0,
        vft_override: float | None = None,
    ) -> bool:
        """Вернуть снятый вызов в очередь (Ф3.4: preempt / свипер /
        human-gate resume). ``True`` — вызов снова в ОБЕИХ индексах.

        Инварианты (queue.lua/REQUEUE):
        - **I2 (anti-livelock):** starve-дедлайн СОХРАНЯЕТСЯ — исходный score
          ``ws:starve`` кладётся как есть, aging-пол не сбрасывается
          вытеснением; ``attempt`` инкрементируется (``retry++``);
        - **I4 (epoch-fencing):** ``stored.epoch > epoch`` → ``False`` БЕЗ
          записей — поздний redelivery со старым epoch не «воскресает».

        ``vft_override`` — новый vft (preempt-кредит ``vft − cost_done/w``);
        ``None`` → исходный stored vft. ``False`` также при отсутствии
        per-call записи (вызов enqueue-ился без HASH — легаси-путь).
        """
        now = self.clock() if now is None else now
        raw = self._requeue(
            keys=[self.q_key, self.starve_key, self.call_key(call)],
            args=[
                call,
                f2s(now),
                epoch,
                "" if vft_override is None else f2s(vft_override),
            ],
        )
        ok = bool(int(raw))
        if ok:
            self._notify_queue_change()  # Ф4.4a (preempt стреляет здесь же)
        return ok

    def preempt(self, call: str, *, cost_done: float, cost_est: float) -> bool:
        """Вытеснение на границе вызова (спека §4): re-enqueue с vft-кредитом
        за сделанное ``vft − cost_done/w(p,c)``; prio/class — из per-call
        записи; epoch — ТЕКУЩИЙ stored (актуальный, не stale).

        Слот освобождает сам воркер ДО requeue (``Slots.release``) —
        протокол воркера, очередь здесь ни при чём. ``cost_est`` принят для
        интерфейсной симметрии с ``enqueue`` (кредит по спеке §4 — только
        ``cost_done``; зарезервирован для будущей EMA-валидации).
        ``False`` — per-call записи нет (нечего вытеснять). Хук панели
        (Ф4.4a) стреляет внутри ``requeue`` — одна операция, один выстрел.
        """
        rec = self.call_record(call)
        if not rec:
            return False
        w = policy.weight(rec["prio"], rec["class"])
        return self.requeue(
            call, epoch=rec["epoch"], vft_override=rec["vft"] - cost_done / w
        )

    def park_call(
        self,
        call: str,
        *,
        job: str = "",
        event_json: str,
        stream_maxlen: int = 10_000,
    ) -> int:
        """Снять вызов из ВСЕХ индексов полки + освободить слот + событие
        ``parked`` — ОДНОЙ Lua (Ф4.3, I10/D5; бюджетный hard-stop).

        Изымает из ``ws:q`` + ``ws:starve`` (двух-индексность — инвариант
        dequeue-группы), ``SREM`` holders + ``DEL`` lease (слот возвращён),
        ``DEL ws:pos:{job}`` (панель очереди не показывает parked как ждущего;
        ключ Ф3.5+ — DEL отсутствующего = no-op), ``XADD`` события (I3/I12).
        Per-call HASH не трогается: vft/starve-кредит — источник REQUEUE при
        resume (позиция восстанавливается). Возвращает число реально
        изъятых индексов (0..3). XADD УСЛОВЕН (P1-2): событие пишется только
        если что-то изъято и ``event_json != ''`` — повторный вызов (индексы
        уже пусты) не дублирует событие; ``event_json=''`` — тихое изъятие
        (компенсация провала CAS в resume, P1-1).
        """
        raw = self._park(
            keys=[
                self.q_key,
                self.starve_key,
                f"ws:slots:{self.shelf}",
                f"ws:lease:{self.shelf}:{call}",
                f"ws:pos:{job}",
                f"ws:events:{self.shelf}",
            ],
            args=[call, event_json, stream_maxlen],
        )
        # Ф4.4a: погасить пер-вызовный ранг (Lua выше гасит только
        # legacy ws:pos:{job}) + хук пересчёта панели.
        self._notify_queue_change(removed=[call])
        return int(raw)

    def call_record(self, call: str) -> dict[str, Any]:
        """Per-call запись ``ws:call:{shelf}:{call}`` (наблюдение/тесты).
        Пустой dict — записи нет; числовые поля приведены к float/int,
        отсутствующие vft/starve_deadline → KeyError (fail-closed: запись
        без них непригодна для requeue)."""
        raw: Any = self.client.hgetall(self.call_key(call))
        if not raw:
            return {}
        return {
            "prio": raw["prio"],
            "class": raw["class"],
            "job": raw.get("job", ""),
            "epoch": int(raw.get("epoch", 0)),
            "attempt": int(raw.get("attempt", 0)),
            "vft": float(raw["vft"]),
            "starve_deadline": float(raw["starve_deadline"]),
        }

    def complete(
        self,
        *,
        prio: str,
        call_class: str,
        cost_actual: float,
        weight: float | None = None,
        call: str | None = None,
    ) -> float:
        """on_complete: ``ws:vt += cost_actual/w`` (атомарно); возвращает
        новый vt. ``call`` (Ф4.4a, опционально) — какой вызов завершён:
        его ``ws:pos:{call}`` гасится + хук панели (снятие мог случиться
        мимо хука — ранг не должен пережить завершение вызова)."""
        w = policy.weight(prio, call_class) if weight is None else weight
        vftlast = self.vftlast_key(prio, call_class)
        raw = self._complete(
            keys=[self.vt_key, vftlast],
            args=[vftlast, f2s(w), f2s(cost_actual)],
        )
        if call is not None:
            self._notify_queue_change(removed=[call])
        return float(raw)

    def dequeue_and_acquire(
        self,
        *,
        k_max: int,
        now: float | None = None,
        limit: int = 1,
        lease_ttl_ms: int = 90_000,
        stream_maxlen: int = 10_000,
    ) -> list[str]:
        """АТОМАРНО: снять до ``limit`` вызовов + взять слот + ``XADD`` событие
        одной Lua (I2/I3). Слотов нет → ``[]`` и очередь НЕ изменена.

        ``k_max`` — число слотов полки (== ``max_parallel_requests``, I1).
        Возвращает список взятых вызовов в порядке обслуживания.
        """
        now = self.clock() if now is None else now
        out: Any = self._dequeue_acquire(
            keys=[
                self.q_key,
                self.starve_key,
                f"ws:slots:{self.shelf}",
                f"ws:events:{self.shelf}",
            ],
            args=[
                f2s(now),
                limit,
                k_max,
                lease_ttl_ms,
                stream_maxlen,
                f"ws:lease:{self.shelf}:",
                self.shelf,
                f2s(now),
            ],
        )
        taken = list(out)
        self._notify_queue_change(removed=taken)  # Ф4.4a: панель
        return taken

    def size(self) -> int:
        """Число ожидающих вызовов полки (ZCARD ws:q:{shelf})."""
        return int(self.client.zcard(self.q_key))
