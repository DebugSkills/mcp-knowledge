"""Бюджет-контур ext-полки: списание денег (микро-₽, int) + ночная сверка (P0-1).

trace_id: arch-2026-10-05-ai-workspace, ревизия критика Ф4 (P0-1: «₽-hard-stop
не существует end-to-end» — писателей ``ws:budget:*`` не было, E1-E4). Этот
модуль — недостающий ПРОИЗВОДИТЕЛЬ: ``charge_budget`` пишет счётчики по
факту usage, ``reconcile_budget`` ночью приводит их к журналу.

ЕДИНИЦА ДЕНЕГ — целочисленный микро-₽ (1 ₽ = 10^6 микро-₽; ``Budget.limit_micro``
конвертирует ``budgets.ext.limit`` при загрузке — единственная точка
согласования единиц; admission сравнивает счётчик с ``limit_micro``).
Никакого float в пути денег: цена (USD/1M токенов x курс) округляется до
int микро-₽/1M при загрузке реестра, списание — ``cost_micro`` (чистая
целочисленная арифметика) + Redis INCRBY (int-only).

Ключи (месяц — ЛОКАЛЬНЫЙ ``%Y-%m``, границы календарные — как дневной ключ
токенов D7; смена месяца = новый счётчик с нуля, period: month):
- ``ws:budget:global:{month}`` — расход месяца ext-контура (гейтит admit);
- ``ws:budget:user:{user}:{month}`` — per-user зеркало (D4, per_user_mirror;
  гейт НЕ читает — наблюдение/аудит, сверяется reconcile);
- ``ws:budget:journal`` — Stream-журнал списаний (SSOT факта для reconcile).

GAP — ЧЕСТНО (LiteLLM spend НЕдоступен, доказано статически): в
``litellm.config.yaml``/``litellm.local_only.config.yaml`` нет ``database_url``
(grep — 0 совпадений), а /spend-эндпоинты и штатный ``max_budget`` LiteLLM
требуют proxy-БД (Prisma/Postgres) для spend-tracking. Поэтому:
- наш enforcement (admit-порог + списание) — ЕДИНСТВЕННЫЙ (решение R1);
- SSOT факта расхода — журнал ``ws:budget:journal`` (наши списания), reconcile
  сводит счётчики к журналу (best-effort);
- сверка с LiteLLM / включение max_budget как defense-in-depth — остаток
  (владелец: оператор; предпосылка — DATABASE_URL в gateway-контуре, Ф6+).

Fallback usage (документирован, НЕ молча): движок знает суммарную оценку
токенов (``engine.usage_of``: (len(prompt)+len(output)+3)//4), БЕЗ разбивки
вход/выход; ``charge_budget`` из wiring списывает ВСЁ по ВЫХОДНОЙ цене
(дороже входной — перерасход не занижается, hard-stop срабатывает раньше,
не позже). Точная разбивка — после расширения LLMClient протокола
(prompt_tokens/completion_tokens) — остаток (Ф4.7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from ai_workspace.scheduler.admission import (
    EXT_SHELF,
    QUOTA_EVENTS_KEY,
    budget_global_key,
    budget_user_key,
    quota_fail_closed,
)
from ai_workspace.scheduler.lua_scripts import extract_sections, section_of

__all__ = [
    "BUDGET_JOURNAL_KEY",
    "BUDGET_JOURNAL_MAXLEN",
    "BudgetReconcileReport",
    "charge_budget",
    "local_month",
    "reconcile_budget",
]

BUDGET_JOURNAL_KEY = "ws:budget:journal"
"""Stream-журнал списаний (SSOT факта расхода для reconcile)."""

BUDGET_JOURNAL_MAXLEN = 1_000_000
"""MAXLEN ~ журнала. Осознанное отступление от событийных стримов (I12 = 10^4):
журнал — ДЕНЕЖНЫЙ факт, а не наблюдение; 1M записей ≫ месячный объём
пилота. Reconcile читает ТОЛЬКО текущий месяц, поэтому усечение записей
прошлых месяцев не влияет на сверку; усечение текущего месяца (недостижимо
в пилоте) занизило бы сумму — наблюдение через событие budget_reconciled."""

_LUA_DIR = Path(__file__).with_name("lua")
_LUA_SOURCE = (_LUA_DIR / "budget.lua").read_text(encoding="utf-8")

_SECTIONS = extract_sections(_LUA_SOURCE, source_name="budget.lua")


@lru_cache(maxsize=128)
def _cached_script(client: Any, name: str) -> Any:
    """Script-объект секции budget.lua, КЭШ на клиента (паттерн admission)."""
    return client.register_script(section_of(_SECTIONS, name, source_name="budget.lua"))


def local_month(now: datetime | None = None) -> str:
    """Локальный месяц ``%Y-%m`` бюджет-ключа (``None`` -> сейчас; naive
    считается локальным — тот же контракт, что ``admission._now_local``)."""
    if now is None:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    return (now.astimezone() if now.tzinfo is None else now).strftime("%Y-%m")


@dataclass(frozen=True)
class BudgetReconcileReport:
    """Итог сверки: месяц, суммы журнала, дельты применённых исправлений."""

    month: str
    journal_entries: int
    journal_total_micro: int
    global_before_micro: int
    global_after_micro: int
    per_user: dict[str, int] = field(default_factory=dict)

    @property
    def global_delta_micro(self) -> int:
        """Исправление глобального счётчика (может быть отрицательным —
        завышенный счётчик приведён вниз к журналу)."""
        return self.global_after_micro - self.global_before_micro


def _journal_event(
    user: str, month: str, rub_micro: int, tokens_in: int, tokens_out: int, job: str
) -> str:
    return json.dumps(
        {
            "user": user,
            "month": month,
            "rub_micro": rub_micro,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "job": job,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


@quota_fail_closed
def charge_budget(
    user: str,
    *,
    tokens_in: int,
    tokens_out: int,
    redis: Any,
    pricing: Any,
    now: datetime | None = None,
    job: str | None = None,
) -> int:
    """Списать деньги за вызов ext-модели: микро-₽ (int) в global + зеркало.

    Считает по прайсу полки ``ext`` (``PricingRegistry.price_for`` — hot-reload
    aware: смена цены применяется к последующим списаниям), ``cost_micro`` —
    чистая целочисленная арифметика. Атомарно одной Lua: INCRBY global +
    INCRBY user-зеркало + XADD журнала (расщепление = расхождение счётчика
    и факта). Возвращает новый глобальный расход месяца (микро-₽).

    Вызовная точка — ``wiring.RedisQuotaPort.charge`` (рядом с
    ``charge_tokens``, на терминале job через ``engine._quota_finalize``):
    дублирования списания нет — одна точка на терминал.
    """
    price = pricing.price_for(EXT_SHELF)
    micro = price.cost_micro(tokens_in, tokens_out)
    month = local_month(now)
    raw = _cached_script(redis, "BUDGET_CHARGE")(
        keys=[
            budget_global_key(month),
            budget_user_key(user, month),
            BUDGET_JOURNAL_KEY,
        ],
        args=[
            micro,
            _journal_event(user, month, micro, tokens_in, tokens_out, job or ""),
            BUDGET_JOURNAL_MAXLEN,
        ],
    )
    return int(raw)


@quota_fail_closed
def reconcile_budget(
    *,
    redis: Any,
    now: datetime | None = None,
    stream_maxlen: int = 10_000,
) -> BudgetReconcileReport:
    """Ночная сверка: счётчики месяца := суммы журнала (best-effort, P0-1).

    Читает ``ws:budget:journal`` батчами (XREAD, без полного XRANGE),
    суммирует rub_micro записей ТЕКУЩЕГО месяца (глобально + per-user) и
    одной Lua (BUDGET_RECONCILE) приводит ``ws:budget:global:{month}`` и
    user-зеркала к journal-суммам — чинит дрейф в обе стороны (завышенный
    счётчик «вернёт» бюджет: parked job'ы смогут resume). Пользователи БЕЗ
    журнальных записей не трогаются (их дельта неизвестна; направление
    безопасное — зеркало гейт не читает).

    Семантика доверия: журнал — SSOT факта. Потеря журнала (усечение/
    FLUSHDB) привела бы к занижению — событие ``budget_reconciled`` с
    дельтой публикуется в ``ws:quota:events`` для наблюдения (отрицательная
    дельта = сигнал разбора).

    ВЛАДЕЛЕЦ/КАДЕНС: оператор — cron (пока НЕ подключен: строка запуска —
    ``make ws-budget-reconcile``, см. ai_workspace/README.md; подключение
    cron/ansible — остаток с владельцем-оператором, трасса Ф4.7).
    """
    month = local_month(now)
    per_user: dict[str, int] = {}
    total = 0
    entries = 0
    last_id = "0-0"
    while True:
        batch = redis.xread({BUDGET_JOURNAL_KEY: last_id}, count=1000, block=None)
        if not batch:
            break
        for _stream, items in batch:
            for entry_id, fields in items:
                last_id = entry_id
                try:
                    data = json.loads(fields.get("event", "{}"))
                except ValueError:
                    continue  # битая запись журнала — не рушим сверку
                if data.get("month") != month:
                    continue  # прошлые месяцы: их счётчики больше не сверяются
                rub = int(data.get("rub_micro", 0))
                total += rub
                entries += 1
                user = str(data.get("user", ""))
                per_user[user] = per_user.get(user, 0) + rub
        if len(batch[0][1]) < 1000:
            break  # журнал дочитан (последний батч неполный)

    keys = [budget_global_key(month)] + [
        budget_user_key(u, month) for u in sorted(per_user)
    ]
    args = [total] + [per_user[u] for u in sorted(per_user)]
    before_raw = redis.mget(keys)
    deltas = _cached_script(redis, "BUDGET_RECONCILE")(keys=keys, args=args)

    report = BudgetReconcileReport(
        month=month,
        journal_entries=entries,
        journal_total_micro=total,
        global_before_micro=int(before_raw[0] or 0),
        global_after_micro=int(before_raw[0] or 0) + int(deltas[0]),
        per_user=per_user,
    )
    redis.xadd(
        QUOTA_EVENTS_KEY,
        {
            "event": json.dumps(
                {
                    "type": "budget_reconciled",
                    "month": month,
                    "entries": entries,
                    "journal_total_micro": total,
                    "global_delta_micro": report.global_delta_micro,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
        },
        maxlen=stream_maxlen,
        approximate=True,
    )
    return report
