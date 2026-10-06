#!/usr/bin/env python3
"""Per-job приоритет AI-верстака (Ф4.5a, решение D8 «разово»): ws:prio:{job}.

trace_id: arch-2026-10-05-ai-workspace. Override читается admission'ом
(``QuotaWiring.submit``) ПОСЛЕДУЮЩИХ вызовов job'а; очередь не реордерится.
Валидация fail-closed (SSOT — ``policy.MULT``: high|med|low), TTL по
умолчанию 24 ч (DEFAULT_TTL_S), повторный set продлевает окно.

Запуск (WS_REDIS_URL ОБЯЗАН указывать на ПРОД ws-redis; дефолта НЕТ —
fail-closed, тестовый 6390 подставлять осознанно):
    WS_REDIS_URL=redis://<прод-ws-redis>:6379/0 python scripts/ws_prio.py \\
        set --job <id> --prio high --actor op --reason "горящий дедлайн"
    WS_REDIS_URL=... python scripts/ws_prio.py show --job <id>
    WS_REDIS_URL=... python scripts/ws_prio.py clear --job <id> --actor op

Выход: JSON. Exit-коды: 0 — ок; 2 — ws-redis недоступен/не сконфигурирован
(fail-closed); 3 — ошибка валидации (prio/ttl). Прод-ws-redis internal-only
(I6): запуск с хоста — через docker-сеть (паттерн в ai_workspace/README.md,
см. ws-budget-reconcile). События job_priority_set/cleared — в ws:quota:events
(хвост XREVRANGE). Канон: plans/_provenance/.../Ф4.5a-job-priority-report.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # ai_workspace импортируется из репо

from ai_workspace.redis_client import make_ws_redis
from ai_workspace.scheduler.admission import QuotaRedisUnavailable
from ai_workspace.scheduler.prio import (
    DEFAULT_TTL_S,
    clear_job_priority,
    get_job_priority,
    set_job_priority,
)


def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ws_prio", description="Per-job приоритет ws:prio:{job} (Ф4.5a)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="установить/продлить override")
    p_set.add_argument("--job", required=True, help="id job'а")
    p_set.add_argument("--prio", required=True, help="high | med | low (SSOT policy.MULT)")
    p_set.add_argument("--ttl", type=int, default=DEFAULT_TTL_S,
                       help=f"TTL, сек (по умолчанию {DEFAULT_TTL_S} = 24 ч)")
    p_set.add_argument("--actor", default="", help="кто установил (аудит)")
    p_set.add_argument("--reason", default="", help="причина (аудит)")

    p_clear = sub.add_parser("clear", help="снять override")
    p_clear.add_argument("--job", required=True)
    p_clear.add_argument("--actor", default="")

    p_show = sub.add_parser("show", help="показать текущий override")
    p_show.add_argument("--job", required=True)

    args = parser.parse_args(argv)
    try:
        client = make_ws_redis()
    except RuntimeError as exc:  # WS_REDIS_URL не задан — конфиг fail-closed
        _out({"error": "ws-redis не сконфигурирован", "detail": str(exc)})
        return 2
    try:
        if args.cmd == "set":
            set_job_priority(
                client, args.job, args.prio,
                ttl_s=args.ttl, actor=args.actor, reason=args.reason,
            )
            _out({"ok": True, "job": args.job, "prio": args.prio, "ttl_s": args.ttl})
        elif args.cmd == "clear":
            removed = clear_job_priority(client, args.job, actor=args.actor)
            _out({"ok": True, "job": args.job, "removed": removed})
        else:  # show
            _out({"job": args.job, "prio": get_job_priority(client, args.job)})
        return 0
    except ValueError as exc:  # fail-closed валидация (prio/ttl/job)
        _out({"error": "валидация fail-closed", "detail": str(exc)})
        return 3
    except QuotaRedisUnavailable as exc:  # ws-redis недоступен (P1-4)
        _out({"error": "ws-redis недоступен (fail-closed)", "detail": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
