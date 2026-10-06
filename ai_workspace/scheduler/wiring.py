"""Wiring-контур квот (P1-5 ревизии Ф4): постановка / порт движка / свип.

trace_id: arch-2026-10-05-ai-workspace (план REV.13 + ревизия критика Ф4,
P1-5). Назначенный владелец вызовов машины квот — ДО этого модуля ни один
прод-код не звал admit/charge/conc_release (E10 критики). Паттерн —
scheduler/park.py (сквозной модуль над JobStore/Queue/admission, не метод
одного из них).

- ``QuotaWiring.submit`` — admission ДО создания job (D5): ``deny`` → job
  НЕ создаётся, вызывающему — ``AdmissionDenied`` со структурным кодом
  (``quota_tokens_exhausted`` / ``quota_conc_exceeded``); ``park`` (бюджет
  ext-полки) → job создаётся и СРАЗУ паркуется (``ParkControl.park``,
  Ф4.3 — budget-hard-stop держится до reconcile); ``allow`` → обычный
  путь, conc-резерв уже взят admit'ом (``job=`` per-job владение, P1-2) —
  второй раз НЕ берётся (движок только heartbeat'ит);
- ``RedisQuotaPort`` — реализация ``engine.QuotaPort`` над живым ws-redis:
  readmit/heartbeat/charge/release → admission (Ф4.2); на полке ``ext``
  ``charge`` ДОПОЛНИТЕЛЬНО списывает деньги (``budget.charge_budget``:
  микро-₽, журнал; P0-1) — та же точка, что и ``charge_tokens`` (терминал
  job через ``engine._quota_finalize``), дубля списания нет;
- ``QuotaWiring.sweep_all`` — глобальный свип истёкших conc-резервов
  (P1-3): пользователи перечисляются по job-store (SCAN ``ws:job:*`` →
  HGET user; SSOT пользователей — job-store, НЕ ключи квот), по каждому
  ``sweep_expired_conc``. Точка подключения reconcile-tick'а контура
  (cron/worker-loop — Ф4.7; паттерн ``gpu.GpuSlots.reconcile``).

Порядок deny-безопасности на постановке: admit → create. Deny ничего не
мутирует (Lua ADMIT пишет только при финальном allow) → после отказа в
сторе нет job, счётчики не тронуты. Бюджетный park тоже не резервирует —
зарезервированный парковать незачем.

Деградация ws-redis (P1-4): admit/charge/conc обёрнуты fail-closed —
``QuotaRedisUnavailable`` наружу: на постановке это ОТКАЗ с понятным
кодом (job не создаётся), на терминале — исключение в лог воркера; ALARM
``quota_degraded`` пишет admission (logging + best-effort XADD).

Роль для квоты — ``account_level`` job'а (participant-роль АККАУНТА:
admin/member/guest, quotas.yaml Ф4.1; node-роли режимов — другое
пространство имён, roles.yaml). ``shelf`` — полка КОНТУРА: ``ext``
включает бюджетную проверку D4/D5 (heavy-класс), ``local`` — только
личные лимиты D3/D6.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ai_workspace.orchestrator.job import JobAlreadyExists, JobStore
from ai_workspace.scheduler.admission import (
    DEFAULT_CONC_LEASE_TTL_MS,
    EXT_SHELF,
    admit,
    charge_tokens,
    conc_heartbeat,
    conc_release,
    sweep_expired_conc,
)
from ai_workspace.scheduler.budget import charge_budget
from ai_workspace.scheduler.park import ParkControl

__all__ = ["QuotaWiring", "RedisQuotaPort"]


def _require_pricing(shelf: str, pricing: Any) -> None:
    """ext-полка без прайса — конструкция ЗАПРЕЩЕНА (P0-1, fail-fast).

    Без прайса списание денег невозможно: молчаливый skip = fail-open по
    бюджету (дефект, закрытый этой ревизией); честный отказ на конструкции —
    раньше любого рантайм-списания.
    """
    if shelf == EXT_SHELF and pricing is None:
        raise ValueError(
            "ext-полка требует pricing (PricingRegistry): без прайса "
            "бюджет-списание невозможно — fail-fast (P0-1)"
        )


class RedisQuotaPort:
    """``engine.QuotaPort`` над живым ws-redis (обёртки admission, Ф4.2).

    Stateless (клиент/реестр/полка в конструкторе) — движок передаёт
    user/role/job_id на каждый вызов; в offline-тестах заменяется фейком
    (контракт — протокол в engine.py, не isinstance).
    """

    def __init__(
        self,
        *,
        registry: Any,
        redis: Any,
        shelf: str = "local",
        lease_ttl_ms: int = DEFAULT_CONC_LEASE_TTL_MS,
        pricing: Any = None,
    ) -> None:
        """``pricing`` — ``PricingRegistry`` (Ф4-rev P0-1): обязателен для
        полки ``ext`` (без прайса денежное списание невозможно — fail-fast на
        конструкции, а не первый отказ в рантайме); полке ``local`` не нужен
        (ollama — ₽0, бюджет не списывается)."""
        _require_pricing(shelf, pricing)
        self.registry = registry
        self.redis = redis
        self.shelf = shelf
        self.lease_ttl_ms = lease_ttl_ms
        self.pricing = pricing

    def readmit(self, user: str, role: str, job_id: str) -> str:
        """Резерв заново: admit(job=...) → 'allow' | 'deny' | 'park'."""
        decision = admit(
            user,
            role,
            registry=self.registry,
            redis=self.redis,
            shelf=self.shelf,
            job=job_id,
            lease_ttl_ms=self.lease_ttl_ms,
        )
        return decision.action

    def heartbeat(self, user: str, job_id: str) -> bool:
        """Продлить conc-lease (воркер жив; P1-3)."""
        return conc_heartbeat(user, job_id, redis=self.redis, lease_ttl_ms=self.lease_ttl_ms)

    def charge(self, user: str, tokens: int) -> None:
        """Списать фактический расход: токены дня (D3/D7) + деньги ext (P0-1).

        ext-полка: ``charge_budget`` по прайсу (микро-₽, атомарно: счётчики
        месяца + журнал). Fallback разбивки in/out ДОКУМЕНТИРОВАН: usage
        движка — суммарная оценка ((prompt+output)//4, ``engine.usage_of``)
        без разбивки, поэтому ВСЁ списывается по ВЫХОДНОЙ цене (дороже
        входной): перерасход не занижается, hard-stop срабатывает раньше,
        а не позже. Точная разбивка (prompt_tokens/completion_tokens) —
        остаток на расширение LLMClient-протокола (Ф4.7).
        """
        charge_tokens(user, tokens, redis=self.redis)
        if self.shelf == EXT_SHELF:
            charge_budget(
                user,
                tokens_in=0,
                tokens_out=tokens,
                redis=self.redis,
                pricing=self.pricing,
            )

    def release(self, user: str, job_id: str) -> None:
        """Освободить резерв job'а по владению (идемпотентно, P1-2)."""
        conc_release(user, job_id, redis=self.redis)


class QuotaWiring:
    """Постановка/свип квот-контура: admit ДО создания job + reconcile-свип.

    Сквозной модуль (паттерн ParkControl): JobStore (создание job) +
    admission (Ф4.2) + ParkControl (бюджетный парк Ф4.3). НЕ метод
    JobStore: постановка — решение контура квот, а не хранилища.
    """

    def __init__(
        self,
        client: Any,
        *,
        registry: Any,
        store: JobStore | None = None,
        park: ParkControl | None = None,
        shelf: str = "local",
        lease_ttl_ms: int = DEFAULT_CONC_LEASE_TTL_MS,
        clock: Callable[[], float] = time.time,
        pricing: Any = None,
    ) -> None:
        """``client`` — ws-redis (decode_responses=True); ``registry`` —
        ``QuotaRegistry`` (Ф4.1); ``shelf`` — полка контура (``ext`` включает
        бюджет D4/D5 + денежное списание P0-1); ``lease_ttl_ms`` — TTL
        conc-lease резерва (P1-3); ``pricing`` — ``PricingRegistry`` (обязателен
        для ext, см. ``RedisQuotaPort``)."""
        _require_pricing(shelf, pricing)
        self.client = client
        self.registry = registry
        self.store = store if store is not None else JobStore(client)
        self.park = park if park is not None else ParkControl(
            client, shelf=shelf, store=self.store, clock=clock
        )
        self.shelf = shelf
        self.lease_ttl_ms = lease_ttl_ms
        self.clock = clock
        self.pricing = pricing

    def make_port(self) -> RedisQuotaPort:
        """Порт движка на том же клиенте/реестре/полке (ModeEngine(quota=...))."""
        return RedisQuotaPort(
            registry=self.registry, redis=self.client,
            shelf=self.shelf, lease_ttl_ms=self.lease_ttl_ms,
            pricing=self.pricing,
        )

    def submit(
        self,
        *,
        user: str,
        account_level: str,
        job_class: str,
        mode: str,
        zone: str,
        vft: float = 0.0,
        job_id: str | None = None,
    ):
        """Поставить job с admission-гейтом ДО создания (D5/P1-5).

        - ``deny`` (личный лимит D3/D6) → ``AdmissionDenied`` (структурный
          код + message): job НЕ создан, счётчики не тронуты (deny не
          мутирует), вызывающий показывает код пользователю;
        - ``park`` (ext-бюджет D4) → job создаётся (queued) и СРАЗУ
          паркуется (``ParkControl.park(call=None)``: статус parked, резерв
          не берётся — park-решение ничего не резервирует; epoch+1);
          возвращает запись уже в ``parked``;
        - ``allow`` → job в ``queued``; conc-резерв взят admit'ом (per-job
          маркер + lease, P1-2/P1-3) — движок продлевает heartbeat'ом,
          терминал возвращает (``engine.ModeEngine(quota=port)``).

        ``account_level`` — participant-роль (quotas.yaml); ``job_id``
          можно передать для идемпотентного ретрая постановки: повторный
          admit того же job НЕ дублирует резерв (SISMEMBER-гвард, P1-B),
          а ``JobAlreadyExists`` компенсируется release'ом здесь же
          (P2-1) — вызывающему ничего освобождать не нужно.
        """
        job_id = job_id or uuid4().hex
        decision = admit(
            user,
            account_level,
            registry=self.registry,
            redis=self.client,
            shelf=self.shelf,
            job=job_id,
            lease_ttl_ms=self.lease_ttl_ms,
        )
        decision.raise_if_denied()  # deny → AdmissionDenied ДО создания job
        try:
            self.store.create(
                user=user,
                account_level=account_level,
                job_class=job_class,
                mode=mode,
                zone=zone,
                vft=vft,
                job_id=job_id,
            )
        except JobAlreadyExists:
            # P2-1 iter2: идемпотентный ретрай постановки наткнулся на живой
            # job — резерв компенсируем release'ом (обычно no-op: admit был
            # идемпотентен по (user, job), P1-B; но если job на паузе БЕЗ
            # маркера — этот вызов мог взять резерв заново, его никто не
            # вернёт). Живой job переберёт резерв readmit'ом на старте
            # движка; исключение — наружу (семантика create не менялась).
            conc_release(user, job_id, redis=self.client)
            raise
        if decision.action == "park":
            # Бюджетный hard-stop (D5): job жив, но не исполняется до
            # reconcile/resume (Ф4.3). call=None — очереди ещё не касались.
            self.park.park(job_id, call=None, reason="budget")
        return self.store.get(job_id)

    def sweep_all(self) -> dict[str, list[str]]:
        """Свип истёкших conc-резервов всех пользователей (P1-3, reconcile).

        Перечисление пользователей — по job-store (SCAN ``ws:job:*`` →
        user), не по ключам квот: SSOT «кто живёт в контуре» — job-store;
        ключи квот могут пережить job (lease-хвосты) и наоборот. По каждому
        пользователю ``sweep_expired_conc`` (SMEMBERS conchold → reclaim
        истёкших; события ``conc_reservation_reclaimed`` в ws:quota:events).
        Возврат: {user: [job_id]} — только у кого что-то снялось
        (наблюдение reconcile-tick'а; пустой словарь = чисто).

        РЕАЛЬНЫЙ ВЫЗОВ (P1-3 iter2): ``scripts/ws_quota_sweep.py`` /
        ``make ws-quota-sweep`` (JSON-отчёт, ``WS_REDIS_URL`` — ПРОД
        ws-redis, дефолта НЕТ — fail-closed). ВЛАДЕЛЕЦ/КАДЕНС: оператор —
        периодически, пока lease TTL = 90 c (рекомендация ≤ 60 c; cron НЕ
        подключён — остаток с владельцем-оператором); штатное место —
        reconcile-tick wiring-воркера (Ф4.7), куда этот вызов переедет.
        """
        users: set[str] = set()
        for key in self.client.scan_iter(match="ws:job:*"):
            user = self.client.hget(key, "user")
            if user:
                users.add(user)
        reclaimed: dict[str, list[str]] = {}
        for user in sorted(users):
            jobs = sweep_expired_conc(user, redis=self.client)
            if jobs:
                reclaimed[user] = jobs
        return reclaimed
