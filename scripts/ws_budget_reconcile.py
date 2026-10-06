#!/usr/bin/env python3
"""Ночная сверка бюджет-счётчиков AI-верстака с журналом списаний (P0-1, Ф4-rev).

trace_id: arch-2026-10-05-ai-workspace. Приводит ``ws:budget:global:{month}``
и per-user зеркала к суммам журнала ``ws:budget:journal`` (SSOT факта
расхода) — чинит дрейф в обе стороны. Подробности и GAP (LiteLLM spend
недоступен без DATABASE_URL — сверка по нашему журналу, best-effort):
``ai_workspace/scheduler/budget.py`` (докстрока модуля).

Запуск (WS_REDIS_URL ОБЯЗАН указывать на ПРОД ws-redis; дефолта НЕТ —
fail-closed, тестовый 6390 подставлять осознанно):
    WS_REDIS_URL=redis://<прод-ws-redis>:6379/0 python scripts/ws_budget_reconcile.py

ВЛАДЕЛЕЦ/КАДЕНС: оператор, nightly (``quotas.yaml: budgets.ext.reconcile:
nightly``). В cron/ansible НЕ подключено — остаток с владельцем-оператором
(трасса ai-workspace Ф4.7: reconcile-tick в wiring-воркере). Прод-ws-redis
internal-only (I6): запуск с хоста — через docker-сеть (см. README).

Выход: JSON-отчёт сверки (JSON-first CLI — паттерн scripts/).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # ai_workspace импортируется из репо

from ai_workspace.redis_client import make_ws_redis
from ai_workspace.registry import Registry
from ai_workspace.registry.pricing import PricingRegistry
from ai_workspace.scheduler.admission import QuotaRedisUnavailable
from ai_workspace.scheduler.budget import reconcile_budget


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry-dir",
        default=str(REPO_ROOT / "ai_workspace" / "registry"),
        help="каталог YAML-реестров (по умолчанию — ai_workspace/registry репо)",
    )
    parser.add_argument(
        "--max-downward-micro",
        type=int,
        default=None,
        help=(
            "гейт корректировки ВНИЗ глобального счётчика, микро-₽ (P2-3): "
            "большая корректировка = подозрение на потерю журнала — отказ "
            "вместо молчаливого «возврата» бюджета; None = без гейта"
        ),
    )
    args = parser.parse_args()

    registry = Registry(Path(args.registry_dir))
    PricingRegistry(registry).price_for("ext")  # fail-closed: прайс обязан быть валиден
    redis = make_ws_redis()

    try:
        report = reconcile_budget(
            redis=redis, max_downward_micro=args.max_downward_micro
        )
    except QuotaRedisUnavailable as exc:
        print(json.dumps({"error": "ws-redis недоступен (fail-closed)", "detail": str(exc)},
                         ensure_ascii=False))
        return 2
    except ValueError as exc:
        # гейт корректировки ВНИЗ (P2-3): отказ осознанный, счётчики не тронуты
        print(json.dumps({"error": "reconcile отказан (гейт вниз)", "detail": str(exc)},
                         ensure_ascii=False))
        return 3
    print(
        json.dumps(
            {
                "month": report.month,
                "journal_entries": report.journal_entries,
                "journal_total_micro": report.journal_total_micro,
                "global_before_micro": report.global_before_micro,
                "global_after_micro": report.global_after_micro,
                "global_delta_micro": report.global_delta_micro,
                "per_user_micro": report.per_user,
                "unit": "micro-RUB (1 RUB = 10^6)",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
