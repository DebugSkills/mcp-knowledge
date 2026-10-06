#!/usr/bin/env python3
"""Свип истёкших conc-резервов AI-верстака (P1-3 iter2: реальный вызов sweep_all).

trace_id: arch-2026-10-05-ai-workspace (Ф4.2e). Снимает мёртвые резервы
``ws:quota:conchold:{user}`` (lease истёк, воркер не продлевает) — до этого
``QuotaWiring.sweep_all`` не звался никем (grep: только тесты), и любой
мертвый резерв требовал ручной правки ключей. Механика: пользователи
перечисляются по job-store (SSOT «кто живёт в контуре»), по каждому
``sweep_expired_conc`` → ``CONC_RECLAIM`` (события ``conc_reservation_reclaimed``
в ``ws:quota:events``). Идемпотентен, безопасен при параллельном запуске
(перепроверка lease внутри Lua).

Запуск (WS_REDIS_URL ОБЯЗАН указывать на ПРОД ws-redis; дефолта НЕТ —
fail-closed, тестовый 6390 подставлять осознанно):
    WS_REDIS_URL=redis://<прод-ws-redis>:6379/0 python scripts/ws_quota_sweep.py

ВЛАДЕЛЕЦ/КАДЕНС: оператор — периодически, пока conc-lease TTL = 90 c
(рекомендация ≤ 60 c; cron НЕ подключён — остаток с владельцем-оператором).
Штатное место вызова — reconcile-tick wiring-воркера (Ф4.7): когда воркер
появится, вызов переезжает туда, этот скрипт останется ручным fallback.
Прод-ws-redis internal-only (I6): запуск с хоста — через docker-сеть (см.
ai_workspace/README.md, паттерн ws-budget-reconcile).

Выход: JSON-отчёт {reclaimed: {user: [job_id]}, users_scanned} — только
снятое (пустой reclaimed = чисто).
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
from ai_workspace.registry.quotas import QuotaRegistry
from ai_workspace.scheduler.admission import QuotaRedisUnavailable
from ai_workspace.scheduler.wiring import QuotaWiring


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--registry-dir",
        default=str(REPO_ROOT / "ai_workspace" / "registry"),
        help="каталог YAML-реестров (по умолчанию — ai_workspace/registry репо)",
    )
    args = parser.parse_args()

    wiring = QuotaWiring(
        make_ws_redis(), registry=QuotaRegistry(Registry(Path(args.registry_dir)))
    )
    users_scanned = len(
        {
            u
            for u in (
                wiring.client.hget(k, "user")
                for k in wiring.client.scan_iter(match="ws:job:*")
            )
            if u
        }
    )
    try:
        reclaimed = wiring.sweep_all()
    except QuotaRedisUnavailable as exc:
        print(json.dumps({"error": "ws-redis недоступен (fail-closed)", "detail": str(exc)},
                         ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {"reclaimed": reclaimed, "users_scanned": users_scanned},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
