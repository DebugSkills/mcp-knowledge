#!/usr/bin/env python3
"""errors_report.py — weekly-отчёт + read-only просмотр цикла Error→Rule (Ф3, code-2026-09-22-003).

Режимы:
  --view [--top N]   read-only: сводка по источникам + топ-N сигнатур (дефолт 50),
                     P0-блок первым (E7: ничего не мутирует).
  --weekly [--send-tg]
                     6 секций (M6): сводка / P0 / топ-P1P2 / P3+noise_ratio /
                     метрики §8 канона / кандидаты-на-правило (M5).
                     Пишет reports/report-<ISO-week>.md; обновляет alert_state.json
                     (known-list: new/known/regressed/resolved; кулдаун-поля —
                     задел под будущий P0-алерт, при weekly НЕ активны — M7).
  --send-tg          best-effort Telegram: notify.json (0600, из vault) →
                     api.telegram.org sendMessage, чанки ≤4096; фейл →
                     reports/tg-errors.log, exit 0; токен НИКОГДА не печатается.

Запуск: make prod-errors / prod-errors-report [TG=1] (errors.yml теги view|report).
Python ≥3.9, stdlib-only. Выход 0 всегда (best-effort, M7).
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (
    DATA_ROOT,
    atomic_write_json,
    load_json,
    parse_ts,
)

TG_CHUNK = 4096
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def iso_week(now=None):
    d = now or datetime.now(timezone.utc)
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def load_aggregates(sink):
    return load_json(sink / "aggregates" / "signatures.json", {})


# ── --view: read-only просмотр (E7) ──

def cmd_view(sink, top):
    aggs = load_aggregates(sink)
    if not aggs:
        print(f"(sink пуст: {sink}/aggregates/signatures.json — нет данных или коллектор не запускался)")
        return 0
    items = sorted(aggs.items(),
                   key=lambda kv: (PRIORITY_ORDER.get(kv[1].get("priority"), 9),
                                   -kv[1].get("count_7d", 0)))
    by_source = {}
    for a in aggs.values():
        for s in a.get("sources", []):
            by_source[s] = by_source.get(s, 0) + a.get("count_total", 0)
    print(f"=== Error→Rule sink: {sink} · сигнатур: {len(aggs)} ===")
    print("Сводка по источникам (событий по сигнатурам источника):")
    for s, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {s:<14} {n}")
    p0 = [(sig, a) for sig, a in items if a.get("priority") == "P0"]
    rest = [(sig, a) for sig, a in items if a.get("priority") != "P0"][:max(0, top - len(p0))]
    print(f"\n--- P0 ({len(p0)}) — первыми ---")
    for sig, a in p0 + rest:
        actors = ",".join(a.get("actors", [])[:3]) or "-"
        ex = (a.get("last_example") or {}).get("message", "")[:100].replace("\n", " ")
        print(f"[{a.get('priority')}] {a.get('class')} 7d={a.get('count_7d', 0):<5} "
              f"total={a.get('count_total', 0):<6} sup={a.get('suppressed_total', 0):<5} "
              f"actors={actors:<20} "
              f"last={a.get('last_seen', '?')[:16]}  {ex}")
        print(f"        sig: {sig[:150]}")
    return 0


# ── known-list (M7): new / known / regressed / resolved ──

def classify_weekly(aggs, alert):
    """→ {status: [(sig, agg)]}; мутирует alert (first/last_seen, переходы, week)."""
    now = datetime.now(timezone.utc)
    week = iso_week()
    result = {"new": [], "known": [], "regressed": [], "resolved": []}
    for sig, a in aggs.items():
        st = alert.get(sig) or {
            "first_seen": a["first_seen"], "last_seen": a["last_seen"],
            "last_reported_week": None, "status": "new", "reported_by_user": False,
            "fixed_at": None, "investigating": False, "cooldown_until": None,
        }
        st["first_seen"], st["last_seen"] = a["first_seen"], a["last_seen"]
        try:
            age7 = (now - parse_ts(a["last_seen"])).days < 7
            fresh = (now - parse_ts(a["first_seen"])).days < 7
        except ValueError:
            age7, fresh = True, True
        fixed_at = a.get("fixed_at")
        regressed_now = bool(fixed_at and a["last_seen"] > fixed_at and age7)
        if a.get("status") == "resolved" and not age7 and not regressed_now:
            status, st["fixed_at"] = "resolved", fixed_at or a["last_seen"]
        elif regressed_now or (st.get("fixed_at") and a.get("status") == "active"
                               and st.get("status") == "resolved"):
            status = "regressed"
            st["fixed_at"] = None
        elif fresh and st.get("last_reported_week") is None:
            status = "new"
        else:
            status = "known"
        st["status"] = status
        st["last_reported_week"] = week
        alert[sig] = st
        result[status].append((sig, a))
    return result


# ── raw-статистика 7d: noise_ratio по строкам + объём sink (метрика МБ/мес) ──

def raw_stats_7d(sink, aggs):
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    total = p3 = 0
    sig_priority = {sig: a.get("priority") for sig, a in aggs.items()}
    for day in days:
        path = sink / "events" / "raw" / f"{day}.jsonl"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    sig = json.loads(line).get("signature")
                    if sig_priority.get(sig) == "P3":
                        p3 += 1
                except json.JSONDecodeError:
                    continue
    raw_dir = sink / "events" / "raw"
    size_bytes = sum(p.stat().st_size for p in raw_dir.glob("*.jsonl")) if raw_dir.exists() else 0
    files = sorted(raw_dir.glob("*.jsonl")) if raw_dir.exists() else []
    span_days = 1
    if files:
        try:
            first = parse_ts(files[0].stem)
            span_days = max(1, (now - first).days)
        except ValueError:
            pass
    return {
        "lines_7d": total, "p3_lines_7d": p3,
        "noise_ratio": round(p3 / total, 3) if total else 0.0,
        "raw_mb": round(size_bytes / 1e6, 2),
        "mb_per_month": round(size_bytes / 1e6 / span_days * 30, 2),
    }


# ── метрики §8 канона ──

def metrics(aggs, alert, raw):
    now = datetime.now(timezone.utc)
    p01_active, p01_30d, found_before_user = 0, 0, 0
    ttf, rules_candidates, recidives = [], 0, []
    # D3 (Ф5): reported_by_user никем не выставляется → «н/д», а не псевдо-1.0;
    # формула остаётся для случая, когда источник появится (жалобы оператора)
    has_user_reports = any(st.get("reported_by_user") for st in alert.values())
    for sig, a in aggs.items():
        pri = a.get("priority")
        try:
            seen_30d = (now - parse_ts(a["last_seen"])).days <= 30
        except ValueError:
            seen_30d = True
        if pri in ("P0", "P1"):
            p01_30d += 1 if seen_30d else 0
            if a.get("status") != "resolved":
                p01_active += 1
                if not alert.get(sig, {}).get("reported_by_user", False):
                    found_before_user += 1
        if a.get("status") == "resolved" and a.get("fixed_at"):
            if pri in ("P0", "P1"):
                rules_candidates += 1
            try:
                ttf.append((parse_ts(a["fixed_at"]) - parse_ts(a["first_seen"])).total_seconds() / 3600)
            except ValueError:
                pass
            if a["last_seen"] > a["fixed_at"]:  # события ПОСЛЕ фикса = рецидив
                recidives.append(sig)
    ttf.sort()
    med_ttf = round(ttf[len(ttf) // 2], 1) if ttf else None
    if not has_user_reports:
        found_ratio, found_str = None, "н/д (источник reported_by_user не подключён)"
    elif p01_30d:
        found_ratio = round(found_before_user / p01_30d, 2)
        found_str = f"{found_before_user}/{p01_30d} = {found_ratio}"
    else:
        found_ratio, found_str = None, "0/0"
    return {
        "found_before_user_ratio": found_ratio,
        "found_before_user": found_str,
        "rules_candidates": rules_candidates,
        "median_ttf_h": med_ttf,
        "recidives_7d": recidives,
        "p0p1_active": p01_active,
    }


RULE_BY_HINT = {  # M5: тип правила по природе причины
    "traceback": "pytest-регресс (воспроизвести traceback тестом)",
    "critical": "pytest-регресс / postmortem incidents.md",
    "5xx": "pytest-регресс (HTTP-контракт)",
    "oom": "ansible-гард (mem_limit) / postmortem",
    "restart": "ansible-preflight-гард (health-гейт)",
    "cron_nonzero": "ansible-гард (cron dry-run) / backup --verify",
    "health_degraded": "ansible-preflight-гард (health-гейт)",
    "hang": "pytest-регресс (timeout-контракт tool-вызова)",
    "disk_critical": "ansible-гард (host-пороги df)",
    # 008 storm-guard (§7.5): burst-маркер [GUARD]
    "burst": "разбор источника шторма (§11.2-чеклист) + проверка стоп-условий",
}


def rule_hint(sig, a):
    ex = (a.get("last_example") or {}).get("message", "")
    for hint, rule in RULE_BY_HINT.items():
        if hint in sig or hint.replace("_", " ") in ex.lower():
            return rule
    return "L2-инсайт (причина вне словаря M5) — разбор вручную"


def suppressed_7d_map(aggs):
    """sig → suppressed за 7d — вычисляемое из suppressed_daily (008 P2-7,
    единообразно с count_7d; не хранится — одна правда при обрезке 21d)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    return {sig: sum(n for d, n in (a.get("suppressed_daily") or {}).items()
                     if str(d) >= cutoff)
            for sig, a in aggs.items()}


# ── --weekly: 6 секций ──

def cmd_weekly(sink, send_tg):
    aggs = load_aggregates(sink)
    alert_path = sink / "alert_state.json"
    alert = load_json(alert_path, {})
    week = iso_week()
    classes = classify_weekly(aggs, alert)
    raw = raw_stats_7d(sink, aggs)
    met = metrics(aggs, alert, raw)

    p0 = sorted([(s, a) for s, a in aggs.items() if a.get("priority") == "P0"],
                key=lambda kv: -kv[1].get("count_7d", 0))
    p12 = sorted([(s, a) for s, a in aggs.items() if a.get("priority") in ("P1", "P2")
                  and a.get("status") != "resolved"],
                 key=lambda kv: (-len(kv[1].get("actors", [])), -kv[1].get("count_7d", 0)))[:10]
    p3 = sorted([(s, a) for s, a in aggs.items() if a.get("priority") == "P3"],
                key=lambda kv: -kv[1].get("count_7d", 0))[:10]
    candidates = sorted([(s, a) for s, a in aggs.items()
                         if a.get("status") == "resolved" and a.get("fixed_at")
                         and a.get("priority") in ("P0", "P1")],
                        key=lambda kv: kv[1]["fixed_at"], reverse=True)

    def fmt_list(items, limit=10, sup=False):
        lines = []
        for sig, a in items[:limit]:
            ex = (a.get("last_example") or {}).get("message", "")[:90].replace("\n", " ")
            sup_part = f" sup={a.get('suppressed_total', 0)}" if sup else ""
            lines.append(f"- [{a.get('priority')}] 7d={a.get('count_7d', 0)}{sup_part} "
                         f"actors={','.join(a.get('actors', [])[:2]) or '-'} — {ex}")
        return lines or ["- (пусто)"]

    L = []
    L.append(f"# Error→Rule weekly-отчёт {week} · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    L.append("")
    L.append("## 1. Сводка")
    L.append(f"- сигнатур всего: {len(aggs)} · new: {len(classes['new'])} · "
             f"regressed: {len(classes['regressed'])} · known: {len(classes['known'])} · "
             f"resolved: {len(classes['resolved'])}")
    L.append(f"- raw-строк за 7d: {raw['lines_7d']} · объём raw: {raw['raw_mb']} МБ "
             f"(~{raw['mb_per_month']} МБ/мес)")
    # 008 (§7.5): suppressed-строка — честность «мы скрыли N» + контракт
    # верификации E4-при-гварде: глушилась (suppressed_total>0) и замолчала
    # (count_7d==0 и suppressed_7d==0) = источник устранён при активном гварде.
    sup7 = suppressed_7d_map(aggs)
    sup_total_7d = sum(sup7.values())
    sup_top = sorted(((s, n) for s, n in sup7.items() if n > 0),
                     key=lambda kv: -kv[1])[:3]
    guard_fixed = sum(1 for s, a in aggs.items()
                      if a.get("suppressed_total", 0) > 0 and a.get("count_7d", 0) == 0
                      and sup7.get(s, 0) == 0)
    L.append(f"- suppressed за 7d (гвард): {sup_total_7d}"
             + (f" по {len(sup_top)} сигнатурам; топ: "
                + "; ".join(f"{s[:60]}={n}" for s, n in sup_top) if sup_top else "")
             + (f" · источник устранён при гварде: {guard_fixed}" if guard_fixed else ""))
    L.append("")
    L.append("## 2. P0 (поломка — независимо от числа)")
    L.extend(fmt_list(p0))
    L.append("")
    L.append("## 3. Топ-10 P1/P2 (акторы/рост)")
    L.extend(fmt_list(p12))
    L.append("")
    L.append("## 4. P3-baseline (шум)")
    L.extend(fmt_list(p3, 5, sup=True))
    L.append(f"- noise_ratio (P3-строк/всех за 7d): {raw['noise_ratio']}")
    # 008 (§7.5/P1-1): burst-подсекция — фильтр по burst_ts в окне 7d (не по
    # sticky-флагу — декей 7d исключает неограниченный рост), свежие первыми
    burst_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bursts = sorted(((s, a) for s, a in aggs.items()
                     if a.get("burst_ts") and a["burst_ts"] >= burst_cutoff),
                    key=lambda kv: kv[1]["burst_ts"], reverse=True)
    L.append("")
    L.append("### Burst-инциденты за 7d ([GUARD]-гвард)")
    if bursts:
        for s, a in bursts[:10]:
            L.append(f"- burst_ts={str(a['burst_ts'])[:16]} count_5m={a.get('burst_count_5m')} "
                     f"7d={a.get('count_7d', 0)} [{a.get('priority')}] — {s[:120]}")
    else:
        L.append("- (пусто)")
    L.append("")
    L.append("## 5. Метрики цикла (канон §8)")
    ratio_part = f" = {met['found_before_user_ratio']}" if met["found_before_user_ratio"] is not None else ""
    L.append(f"- доля ошибок, найденных ДО жалобы (P0/P1 30d, "
             f"reported_by_user=false): {met['found_before_user']}{ratio_part}")
    L.append(f"- кандидатов на правило (resolved P0/P1): {met['rules_candidates']}")
    L.append(f"- median time-to-fix P0/P1: {met['median_ttf_h']} ч")
    L.append(f"- рецидивы 7d (события после fixed_at): {len(met['recidives_7d'])} "
             + ("· " + "; ".join(met['recidives_7d'][:3]) if met['recidives_7d'] else ""))
    L.append(f"- объём sink: {raw['mb_per_month']} МБ/мес")
    L.append("")
    L.append("## 6. Кандидаты на правило (M5: pytest / ansible-гард / lint + L2-инсайт)")
    for sig, a in candidates[:10]:
        L.append(f"- [resolved {a['fixed_at'][:10]}] {sig[:120]}")
        L.append(f"    → {rule_hint(sig, a)}; P0 → postmortem в incidents.md")
    if not candidates:
        L.append("- (пусто)")
    report = "\n".join(L)

    reports_dir = sink / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"report-{week}.md"
    tmp = report_path.with_name(report_path.name + ".tmp")
    tmp.write_text(report + "\n", encoding="utf-8")
    os.replace(tmp, report_path)
    atomic_write_json(alert_path, alert)  # known-list обновлён (P2-3 атомарно)

    print(report)
    print(f"\n(report → {report_path}; alert_state: new={len(classes['new'])} "
          f"regressed={len(classes['regressed'])} known={len(classes['known'])} "
          f"resolved={len(classes['resolved'])})")

    if send_tg:
        send_telegram(sink, report)
    return 0


# ── Telegram (M7: best-effort, weekly-only; токен не печатается) ──

def send_telegram(sink, text):
    notify = load_json(sink / "notify.json", None)
    log_path = sink / "reports" / "tg-errors.log"
    if not isinstance(notify, dict) or not notify.get("bot_token") or not notify.get("chat_id"):
        msg = "TG: skip (notify.json отсутствует/пуст — рендер ansible errors.yml setup)"
        print(msg)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
        return
    token, chat_id = notify["bot_token"], notify["chat_id"]
    lines, chunks, cur = text.splitlines(), [], ""
    for line in lines:  # чанки ≤4096, не рвём строки
        if len(cur) + len(line) + 1 > TG_CHUNK - 20:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for i, chunk in enumerate(chunks, 1):
        try:
            data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    sent += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = re.sub(token, "<token>", str(exc)) if token else str(exc)
            print(f"TG: ошибка доставки чанка {i}: {reason}")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} chunk {i}/{len(chunks)}: {reason}\n")
    print(f"TG: отправлено ({sent}/{len(chunks)} чанков)" if sent else "TG: не отправлено (см. tg-errors.log)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Error→Rule: view / weekly-отчёт (+TG)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    ap.add_argument("--view", action="store_true", help="read-only просмотр топ-сигнатур")
    ap.add_argument("--top", type=int, default=50, help="сколько сигнатур в --view (дефолт 50)")
    ap.add_argument("--weekly", action="store_true", help="weekly-отчёт (6 секций) + alert_state")
    ap.add_argument("--send-tg", action="store_true", help="отправить отчёт в TG (best-effort)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    if args.weekly:
        return cmd_weekly(sink, args.send_tg)
    return cmd_view(sink, args.top)  # дефолт = read-only view (E7)


if __name__ == "__main__":
    sys.exit(main())
