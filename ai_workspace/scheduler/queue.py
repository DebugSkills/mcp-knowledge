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
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_workspace.scheduler import policy

_LUA_SOURCE = Path(__file__).with_name("queue.lua").read_text(encoding="utf-8")
"""SSOT Lua-кода — читается один раз при импорте (файл рядом с модулем)."""


def _script(name: str) -> str:
    """Вырезать секцию ``-- @script {name}`` из queue.lua."""
    marker = f"-- @script {name}"
    start = _LUA_SOURCE.find(marker)
    if start < 0:
        raise RuntimeError(f"queue.lua: секция {marker!r} не найдена")
    start += len(marker)
    end = _LUA_SOURCE.find("\n-- @script ", start)
    return _LUA_SOURCE[start : end if end > 0 else len(_LUA_SOURCE)].strip() + "\n"


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
    ) -> None:
        """``client`` — redis-клиент с decode_responses=True (redis_client);
        ``clock`` — источник ``now`` (по умолчанию time.time; монотонность
        обеспечивает caller — wall-clock нужен для starve-дедлайнов)."""
        self.client = client
        self.shelf = shelf
        self.clock = clock
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
        return list(out)

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
        return bool(int(raw))

    def preempt(self, call: str, *, cost_done: float, cost_est: float) -> bool:
        """Вытеснение на границе вызова (спека §4): re-enqueue с vft-кредитом
        за сделанное ``vft − cost_done/w(p,c)``; prio/class — из per-call
        записи; epoch — ТЕКУЩИЙ stored (актуальный, не stale).

        Слот освобождает сам воркер ДО requeue (``Slots.release``) —
        протокол воркера, очередь здесь ни при чём. ``cost_est`` принят для
        интерфейсной симметрии с ``enqueue`` (кредит по спеке §4 — только
        ``cost_done``; зарезервирован для будущей EMA-валидации).
        ``False`` — per-call записи нет (нечего вытеснять).
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
        изъятых индексов (0..3). Повторный вызов безопасен на уровне
        индексов, но событие пишет — дедупликацию парковки делает ParkControl.
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
    ) -> float:
        """on_complete: ``ws:vt += cost_actual/w`` (атомарно); возвращает
        новый vt."""
        w = policy.weight(prio, call_class) if weight is None else weight
        vftlast = self.vftlast_key(prio, call_class)
        raw = self._complete(
            keys=[self.vt_key, vftlast],
            args=[vftlast, f2s(w), f2s(cost_actual)],
        )
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
        return list(out)

    def size(self) -> int:
        """Число ожидающих вызовов полки (ZCARD ws:q:{shelf})."""
        return int(self.client.zcard(self.q_key))
