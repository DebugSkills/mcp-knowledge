#!/usr/bin/env python3
"""errors_alert.py — немедленные TG-алерты new-P0/burst цикла Error→Rule (016, В2-блок а).

Детект (§7.2-а, читает ТОЛЬКО агрегат — гвард 008 не дублируется):
  • new_p0   — priority==P0 И first_seen в окне alerts.new_p0_window_min=10 мин
               И нет p0_alerted_at в alert_state;
  • burst    — burst_ts в окне И priority in (P0,P1) (sticky-эскалация 008)
               И (burst_alerted_at отсутствует ИЛИ старше burst_ts — новый
               инцидент). Ленивые поля (P3): burst_ts/burst_count_5m/p0_alerted_at
               могут ОТСУСТВОВАТЬ — все предикаты через .get() + робастный
               _safe_parse (битый/пустой ts → epoch-0 → вне окна), без KeyError.

Фильтр кандидатов ДО сортировки/лимитов (дельта 2.2, USER GATE): НЕ алертить
suppressed (активная запись suppression.json; истёкшая и битый-until → НЕ
фильтрует — parse-guard P3-a, семантика guard._suppression_active не меняется)
и investigating=true (видимость остаётся в weekly-подсекции).

Анти-шторм (дельта 2.1, оператор — ЖЁСТЧЕ): ≤2 основных + 1 хвост-сводка за
прогон · потолок ≤3 send-вызовов/час (хвост включён; 2+1=3 закрывает час при
такте */5) · cooldown 120 мин/сигнатуру. Отброшенные часовым потолком теряются
by design (§7.2-а-loss): backstop — weekly P0/burst-секции + storm-limit-строка
в tg-errors.log. Лимиты — config.json "alerts" (kill-switch по прецеденту guard).

Идемпотентность: per-sig p0_alerted_at/burst_alerted_at/alert_kind/
alert_cooldown_until (поле-аддитивно; cooldown_until weekly-семантики НЕ
трогаем) + служебный _alerts_meta {hour_bucket, sent_this_hour, last_run}
(consumers: prune/classify_weekly/metrics берут alert.get(sig) — мету терпят).
Стейт мутируется ТОЛЬКО при попытке отправки (нет notify.json → skip+лог,
алерты «дозреют» — AC-deg-1).

Каждое сообщение — через errors_notify.send_telegram: тег 🤖[mcp-errors@<host>]
первой строкой (--host / MCP_ERRORS_HOST / notify.host / gethostname), прокси
ProxyHandler, маскировка. Python ≥3.9, stdlib-only. Выход 0 всегда (best-effort:
исключения ловятся на верхнем уровне — cron не роняется).

Запуск: make errors-alert [TG=1]; cron: errors_cron.sh (*/5, cron_wrap).
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (  # sibling-импорт по прецеденту errors_report.py:31
    DATA_ROOT,
    atomic_write_json,
    load_config,
    load_json,
    parse_ts,
)
from errors_guard import audit_event, load_suppression
from errors_notify import log_tg_error, notify_ready, send_telegram

EPOCH0 = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_ALERTS = {"new_p0_window_min": 10, "cooldown_min": 120,
                  "max_per_run": 2, "max_per_hour": 3}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    """Формат now_iso() для инъецируемого dt (фикс T, 016): идентично
    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") в проде, но
    детерминистично в тестах — штампы стейта берутся из now_dt, не из
    реальных часов (time-bomb: тест зелёный, пока UTC < NOW+121мин).
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_parse(raw):
    """parse_ts без исключений: None/битый/отсутствующий ts → epoch-0 (P3)."""
    try:
        return parse_ts(raw)
    except (ValueError, TypeError, AttributeError):
        return EPOCH0


def suppression_filters(entry, today) -> bool:
    """Активна ли suppression-запись ДЛЯ АЛЕРТ-ФИЛЬТРА (не гварда).

    Отличие от guard._suppression_active (P3-a parse-guard): битый/непарсируемый
    until → НЕ фильтрует («подавление кончилось») — лексикографическое сравнение
    guard'а оставляем нетронутым (границы 008).
    """
    until = entry.get("until")
    if not until:
        return True  # until=null — бессрочно
    try:
        datetime.strptime(str(until), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False  # битый ts → подавление не активно
    return str(until) >= today


def detect_candidates(aggs, alert_state, suppression, acfg, now_dt):
    """→ [{sig, kind(new_p0|burst), ts, agg}] — отфильтровано и отсортировано.

    Сортировка: new_p0 → burst, внутри — свежее first_seen/burst_ts первым.
    """
    window = timedelta(minutes=int(acfg["new_p0_window_min"]))
    today = now_dt.strftime("%Y-%m-%d")
    cands = []
    for sig, a in aggs.items():
        st = alert_state.get(sig) or {}
        if not isinstance(st, dict):
            st = {}
        if st.get("investigating"):
            continue  # дельта 2.2: разбираемую не будим (видно в weekly)
        entry = suppression.get(sig)
        if entry and suppression_filters(entry, today):
            continue  # дельта 2.2: suppressed не будим
        cd = st.get("alert_cooldown_until")
        if cd and _safe_parse(cd) > now_dt:
            continue  # анти-шторм (iii): 120 мин/сигнатуру
        kind = ts = None
        if a.get("priority") == "P0" and not st.get("p0_alerted_at"):
            fs = _safe_parse(a.get("first_seen"))
            if fs >= now_dt - window:
                kind, ts = "new_p0", fs
        if kind is None and a.get("burst_ts"):
            bts = _safe_parse(a.get("burst_ts"))
            in_window = bts >= now_dt - window and a.get("priority") in ("P0", "P1")
            if in_window and bts > _safe_parse(st.get("burst_alerted_at")):  # новый инцидент
                kind, ts = "burst", bts
        if kind:
            cands.append({"sig": sig, "kind": kind, "ts": ts, "agg": a})
    cands.sort(key=lambda c: (0 if c["kind"] == "new_p0" else 1, -c["ts"].timestamp()))
    return cands


def alert_body(cand):
    """Текст одного алерта (тег добавит send_telegram первой строкой)."""
    a, sig = cand["agg"], cand["sig"]
    head = "🟠 NEW P0" if cand["kind"] == "new_p0" else "🔴 BURST"
    ex = (a.get("last_example") or {}).get("message", "")[:90].replace("\n", " ")
    parts = f"{head} · {sig[:120]} · 7d={a.get('count_7d', 0)} total={a.get('count_total', 0)}"
    if cand["kind"] == "burst":
        parts += f" count_5m={a.get('burst_count_5m')}"
    return f"{parts} — {ex} — детали: make errors-view / errors_query"


def tail_body(n):
    """Хвост-сводка одного прогона (один send-вызов, входит в max_per_hour)."""
    return f"⚠ …и ещё {n} инцидентов за прогон (лимит основных на прогон) — детали: make errors-view"


def _mark_alerted(alert, cand, now_dt, cooldown_min):
    st = alert.setdefault(cand["sig"], {})
    if not isinstance(st, dict):
        st = alert[cand["sig"]] = {}
    stamp = _iso(now_dt)  # фикс T: из инъецируемых часов, не now_iso()
    if cand["kind"] == "new_p0":
        st["p0_alerted_at"] = stamp
    else:
        st["burst_alerted_at"] = stamp
    st["alert_kind"] = cand["kind"]
    st["alert_cooldown_until"] = (now_dt + timedelta(minutes=int(cooldown_min))).isoformat()


def run_alerts(sink, send_tg=False, chat=None, host=None, dry_run=False, now=None):
    now_dt = now or _now()
    aggs = load_json(sink / "aggregates" / "signatures.json", {})
    alert_path = sink / "alert_state.json"
    alert = load_json(alert_path, {})
    acfg = dict(DEFAULT_ALERTS)
    acfg.update(load_config(sink).get("alerts") or {})
    suppression = load_suppression(sink)

    if send_tg and not dry_run and not notify_ready(sink):
        # AC-deg-1: skip + лог, стейт НЕ мутируется — алерты «дозреют»
        print("TG: skip (notify.json отсутствует/пуст)")
        log_tg_error(sink, "TG: skip (notify.json отсутствует/пуст — алерты дозреют)")
        return 0

    n_supp = sum(1 for s in aggs
                 if s in suppression and isinstance(suppression[s], dict)
                 and suppression_filters(suppression[s], now_dt.strftime("%Y-%m-%d")))
    n_inv = sum(1 for st in alert.values()
                if isinstance(st, dict) and st.get("investigating"))
    cands = detect_candidates(aggs, alert, suppression, acfg, now_dt)
    print(f"кандидаты: {len(cands)} (filtered: suppressed={n_supp} investigating={n_inv})")
    if not cands:
        return 0
    if dry_run or not send_tg:
        for c in cands:
            print(f"  [{c['kind'].replace('_', ' ').upper()}] {c['sig'][:120]}")
        why = "dry-run" if dry_run else "--send-tg не задан"
        print(f"(план алертов: {len(cands)}; отправка: {why})")
        return 0

    # анти-шторм (дельта 2.1): 2 основных + 1 хвост / ≤3 send-час / cooldown
    meta = alert.get("_alerts_meta") if isinstance(alert.get("_alerts_meta"), dict) else {}
    bucket = now_dt.strftime("%Y-%m-%dT%H")
    sent_hour = int(meta.get("sent_this_hour") or 0) if meta.get("hour_bucket") == bucket else 0
    budget = int(acfg["max_per_hour"]) - sent_hour
    mains = cands[:int(acfg["max_per_run"])]

    sends, sent_mains = 0, []
    for cand in mains:
        if sends >= budget:
            break
        send_telegram(sink, alert_body(cand), chat_override=chat, host_override=host)
        sends += 1
        sent_mains.append(cand)
        _mark_alerted(alert, cand, now_dt, acfg["cooldown_min"])
    not_sent = len(cands) - len(sent_mains)
    if not_sent > 0:
        if sends < budget:
            send_telegram(sink, tail_body(not_sent), chat_override=chat, host_override=host)
            sends += 1
        else:
            # часовой потолок исчерпан: потери by design (§7.2-а-loss),
            # backstop — weekly + storm-limit-строка (наблюдаемость)
            log_tg_error(sink, f"storm-limit: suppressed {not_sent} alerts "
                               f"(hour cap {acfg['max_per_hour']})")
            print(f"storm-limit: suppressed {not_sent} alerts")
    if sends:
        alert["_alerts_meta"] = {"hour_bucket": bucket,
                                 "sent_this_hour": sent_hour + sends,
                                 "last_run": _iso(now_dt)}  # фикс T: детерминизм
        atomic_write_json(alert_path, alert)  # запись ТОЛЬКО при попытке отправки
    print(f"TG-алерты: отправлено {sends} (прогон), час: {sent_hour + sends}/{acfg['max_per_hour']}")
    return 0


# 028-B: канонический стаб aggregates (errors_collect.py:792-799) — для создания
# отсутствующей половины при resolve (N-1c: скелет обязан проходить реальные
# update_aggregates и classify_weekly без KeyError).
AGG_STUB = {"priority": "P2", "class": "T", "count_total": 0, "daily": {},
            "actors": [], "sources": [], "last_example": None,
            "status": "active", "fixed_at": None}


def resolve_sig(sink, sig, reason=None, actor=None, dry_run=False) -> int:
    """028-B: пометить сигнатуру «исправлено» НЕМЕДЛЕННО (не ждать E4-тишину).

    Пишет ОБЕ половины (N-1: иначе classify_weekly вернёт regressed и обнулит
    fixed_at). Цель ищется в любом файле; отсутствующая половина создаётся по
    канонам. exit: 0 — применено/no-op, 1 — сигнатуры нет ни в одном файле.
    """
    agg_path = sink / "aggregates" / "signatures.json"
    alert_path = sink / "alert_state.json"
    aggs = load_json(agg_path, {})
    alert = load_json(alert_path, {})
    in_agg, in_alert = sig in aggs, sig in alert
    if not in_agg and not in_alert:
        print(f"resolve: сигнатура не найдена ни в aggregates, ни в alert_state: {sig[:110]}")
        near = [k for k in list(aggs) + list(alert) if sig[:8] in k][:5]
        if near:
            print("  похожие:")
            for k in near:
                print(f"    {k[:110]}")
        return 1
    now = _iso(_now())
    a = aggs.get(sig) or dict(AGG_STUB, first_seen=now, last_seen=now)
    st = alert.get(sig) or {
        "first_seen": a.get("first_seen") or now, "last_seen": a.get("last_seen") or now,
        "last_reported_week": None, "status": "new", "reported_by_user": False,
        "fixed_at": None, "investigating": False, "cooldown_until": None,
    }
    was = (a.get("status"), st.get("status"))
    if was == ("resolved", "resolved") and a.get("fixed_at") and st.get("fixed_at"):
        print(f"resolve: no-op — уже resolved (fixed_at={st.get('fixed_at')}): {sig[:110]}")
        return 0
    if dry_run:
        print(f"resolve (DRY-RUN): {sig[:110]} — было {was} → станет resolved")
        return 0
    a["status"], a["fixed_at"] = "resolved", now
    st["status"], st["fixed_at"] = "resolved", now
    st["resolve_reason"], st["resolved_by"] = reason, actor or "operator"
    aggs[sig], alert[sig] = a, st
    atomic_write_json(agg_path, aggs)    # B-7: сначала aggregates, затем alert
    atomic_write_json(alert_path, alert)
    audit_event(sink, "resolve", sig, reason, None, actor or "operator")
    print(f"resolved: {sig[:110]} (было {was} → resolved, fixed_at={now})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Error→Rule: немедленные алерты new-P0/burst (+TG)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    ap.add_argument("--resolve", default=None, metavar="SIG",
                    help="028-B: пометить сигнатуру исправленной (resolved + fixed_at, обе половины)")
    ap.add_argument("--reason", default=None, help="причина resolve (пишется в alert_state/audit)")
    ap.add_argument("--actor", default=None, help="кто зафиксировал (по умолчанию operator)")
    ap.add_argument("--send-tg", action="store_true", help="отправить алерты в TG (best-effort)")
    ap.add_argument("--chat", default=None, help="override chat_id (ручной запас)")
    ap.add_argument("--host", default=None, help="override host-тега источника (дельта 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="план алертов без отправки и без записи стейта (HITL)")
    args = ap.parse_args(argv)
    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    if args.resolve:  # B-5: ветка resolve — ВЫХОД до run_alerts (алерты не отправляем)
        return resolve_sig(sink, args.resolve, reason=args.reason, actor=args.actor,
                           dry_run=args.dry_run)
    try:
        return run_alerts(sink, send_tg=args.send_tg, chat=args.chat,
                          host=args.host, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — best-effort: cron не роняем (§7.8)
        print(f"[errors_alert] FAIL: {exc}")
        try:
            log_tg_error(sink, f"errors_alert fail: {exc}")
        except Exception as log_exc:  # noqa: BLE001 — лог-путь сам не должен ронять exit-0
            print(f"[errors_alert] лог-путь упал: {log_exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
