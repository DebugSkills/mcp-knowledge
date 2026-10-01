#!/usr/bin/env python3
"""errors_tick.py — единый оповещатель цикла Error→Rule (Ф-A2, T1-3, code-2026-09-30-errors-digest).

Один cron-тик (*/5 --send-tg), заменяющий per-сигнатурные burst-алерты и
отдельную weekly-джобу. Сам решает, что делать, по общему alert_state.json:

  1. new-P0 — мгновенно, через готовый errors_alert.run_alerts с фильтром
     kinds=("new_p0",) (P1-1: burst НЕ мгновенно — решение оператора №2,
     burst виден только в сводке/weekly).
  2. Дайджест по хостам — ровно 1×/сутки (P0-1: attempt-маркер ДО send;
     повторная доставка = flush уже спуленной копии, НЕ новый build; критерий
     «отправлено» = отсутствие дайджест-чанка в tg-pending/, не возврат
     send_telegram). Таблица состояний С0–С5 (REV.3 R3.2).
  3. Weekly — через cmd_weekly с MSK-гейтом и догоном (P1-3: Пн 10:00 ≤
     now_msk ≤ Ср 23:59 MSK ∧ weekly.last_week ≠ iso_week(last_monday); неделя
     считается от MSK-даты, не от UTC-now). Пропущенный Пн догоняется Вт-Ср.

Канонический лок (P1-5/R2.6): вся итерация тика держит errors_alert.alerts_lock
(sink/.alerts.lock, flock LOCK_EX|LOCK_NB); busy → skip + видимая строка + rc 0.

Плановые отправки (digest/weekly/flush-retry) идут вне бюджета max_per_hour=3
(он остаётся для инцидентных new-P0) — только под attempt-дисциплиной.

TZ-независимость: Europe/Moscow (config digest.tz), now инъецируется (тесты с
фиксированным now в UTC и MSK). Python ≥3.9, stdlib-only. Выход 0 всегда
(best-effort: cron не роняется). Отправка ТОЛЬКО через errors_notify.send_telegram.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_alert import alerts_lock, run_alerts
from errors_collect import (
    DATA_ROOT,
    atomic_write_json,
    load_config,
    load_json,
    parse_ts,
)
from errors_notify import flush, send_telegram
from errors_report import DIGEST_MARKER, build_digest, cmd_weekly

RETRY_MIN = 60
DEFAULT_TZ = "Europe/Moscow"
DIGEST_AT = "digest"
WEEKLY_AT = "weekly"


def _now():
    return datetime.now(timezone.utc)


def _tzinfo(tz):
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except Exception:  # noqa: BLE001 — падение TZ не должно ронять тик
            return timezone(timedelta(hours=3))
    return tz


def _weekly_due(now_dt, tzinfo):
    """→ (due, iso_week) — MSK-гейт weekly (P1-3/R2.4).

    Неделя считается от MSK-даты (last_monday), не от UTC-now. Окно догона:
    Пн 10:00 → Ср 23:59:59 MSK; Чт-Вс неделя пропускается (last_week не пишется).
    """
    now_msk = now_dt.astimezone(tzinfo)
    last_monday = now_msk.date() - timedelta(days=now_msk.weekday())
    monday_10 = datetime(last_monday.year, last_monday.month, last_monday.day,
                         10, 0, 0, tzinfo=tzinfo)
    wed_2359 = monday_10 + timedelta(days=2, hours=13, minutes=59, seconds=59)
    week = f"{last_monday.isocalendar().year}-W{last_monday.isocalendar().week:02d}"
    return (monday_10 <= now_msk <= wed_2359), week


def _write_digest_state(sink, d):
    alert_path = Path(sink) / "alert_state.json"
    alert = load_json(alert_path, {})
    if not isinstance(alert, dict):
        alert = {}
    alert[DIGEST_AT] = d
    atomic_write_json(alert_path, alert)


def _write_weekly_state(sink, w):
    alert_path = Path(sink) / "alert_state.json"
    alert = load_json(alert_path, {})
    if not isinstance(alert, dict):
        alert = {}
    alert[WEEKLY_AT] = w
    atomic_write_json(alert_path, alert)


def _digest_chunk_spooled(sink, attempt_ts):
    """Найден ли дайджест-чанк в tg-pending/ с created_at ≥ attempt_ts (P0-1).

    Детектор копии: DIGEST_MARKER in text (единый источник титула, P3-5).
    attempt_ts/created_at — оба aware ISO UTC (формат бит-в-бит, P3-2),
    сравнение через fromisoformat-парсер parse_ts.
    """
    d = Path(sink) / "reports" / "tg-pending"
    if not d.is_dir():
        return False
    try:
        at = parse_ts(attempt_ts)
    except (ValueError, TypeError):
        return False
    for f in sorted(d.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        text = payload.get("text") or ""
        ca = payload.get("created_at")
        if not ca or DIGEST_MARKER not in text:
            continue
        try:
            if parse_ts(ca) >= at:
                return True
        except (ValueError, TypeError):
            continue
    return False


def _overflow_after(sink, attempt_ts):
    """Есть ли overflow-строка в tg-errors.log с ts ≥ attempt_ts (C5/R3.1.2)."""
    if not attempt_ts:
        return False
    path = Path(sink) / "reports" / "tg-errors.log"
    if not path.is_file():
        return False
    try:
        at = parse_ts(attempt_ts)
    except (ValueError, TypeError):
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if "overflow: pruned" not in line:
            continue
        iso = line.split(" ", 1)[0]
        try:
            if parse_ts(iso) >= at:
                return True
        except (ValueError, TypeError):
            continue
    return False


def _digest_fresh(sink, d, today, now_dt, tzinfo):
    """C2: свежая попытка — attempt-маркер ДО send (fail-safe), build+send."""
    attempt_ts = now_dt.isoformat()
    d["attempt_date"] = today
    d["attempt_ts"] = attempt_ts
    d.pop("spooled_date", None)
    try:
        _write_digest_state(sink, d)  # attempt-маркер ДО send
    except OSError:
        print("digest: state write failed before send — skip (fail-safe, 0 отправок)")
        return
    text = build_digest(sink, now=now_dt, tz=tzinfo)
    send_telegram(sink, text)
    if _digest_chunk_spooled(sink, attempt_ts):
        d["spooled_date"] = today
        d["retry_not_before"] = (now_dt + timedelta(minutes=RETRY_MIN)).isoformat()
    else:
        d["last_date"] = today
        d.pop("retry_not_before", None)
    try:
        _write_digest_state(sink, d)
    except OSError:
        print("digest: state write failed after send (attempt-маркер держит — next tick assume-delivered)")


def _digest_flush(sink, d, today, now_dt):
    """C3: спуленная копия стоит — flush без нового build/send."""
    stats = flush(sink)
    if stats.get("pending", 0) == 0:
        # R3.1.2: prune после снапшота — наблюдаемость потери, не re-send.
        if _overflow_after(sink, d.get("attempt_ts")):
            print("digest lost to overflow (post-snapshot prune)")
        d["last_date"] = today
        d.pop("retry_not_before", None)
    else:
        d["retry_not_before"] = (now_dt + timedelta(minutes=RETRY_MIN)).isoformat()
    try:
        _write_digest_state(sink, d)
    except OSError:
        print("digest: state write failed after flush")


def _digest_assume(sink, d, today, now_dt):
    """C4/C5: незакрытая попытка без спула — assume-delivered (0 отправок)."""
    if _overflow_after(sink, d.get("attempt_ts")):
        print("digest lost to overflow (see tg-errors.log)")  # C5: re-send запрещён
    print("digest: assume-delivered (attempt open, no spool)")
    d["last_date"] = today
    d.pop("retry_not_before", None)
    try:
        _write_digest_state(sink, d)
    except OSError:
        print("digest: state write failed (assume-delivered)")


def _tick_digest(sink, now_dt, tzinfo, send):
    """Ветка дайджеста: ровно 1 build+send/сутки (С0–С5)."""
    now_msk = now_dt.astimezone(tzinfo)
    today = now_msk.strftime("%Y-%m-%d")
    if now_msk.hour < 10:  # С0: до 10:00 MSK
        return
    alert = load_json(Path(sink) / "alert_state.json", {})
    d = alert.get(DIGEST_AT) if isinstance(alert.get(DIGEST_AT), dict) else {}
    if d.get("last_date") == today:  # С0: уже доставлено сегодня
        return
    rnb = d.get("retry_not_before")
    if rnb:
        try:
            if parse_ts(rnb) > now_dt:  # С1: анти-шторм backoff
                return
        except (ValueError, TypeError):
            pass
    if d.get("attempt_date") != today:
        if send:
            _digest_fresh(sink, d, today, now_dt, tzinfo)
        else:
            print(f"(digest: свежая попытка {today} — build+send сводки)")
    elif d.get("spooled_date") == today:
        if send:
            _digest_flush(sink, d, today, now_dt)
        else:
            print(f"(digest: спуленная копия {today} — flush без нового build)")
    else:
        if send:
            _digest_assume(sink, d, today, now_dt)
        else:
            print(f"(digest: assume-delivered {today} — 0 отправок)")


def _tick_weekly(sink, now_dt, tzinfo, send):
    """Ветка weekly: MSK-гейт + догон, симметричная attempt-дисциплина."""
    due, week = _weekly_due(now_dt, tzinfo)
    if not due:
        return
    alert = load_json(Path(sink) / "alert_state.json", {})
    w = alert.get(WEEKLY_AT) if isinstance(alert.get(WEEKLY_AT), dict) else {}
    if w.get("last_week") == week:
        return
    rnb = w.get("retry_not_before")
    if rnb:
        try:
            if parse_ts(rnb) > now_dt:
                return
        except (ValueError, TypeError):
            pass
    if not send:
        print(f"(weekly: план — отправить отчёт {week})")
        return
    w["attempt_week"] = week
    w["attempt_ts"] = now_dt.isoformat()
    try:
        _write_weekly_state(sink, w)  # attempt-маркер ДО send
    except OSError:
        print("weekly: state write failed before send — skip")
        return
    cmd_weekly(sink, send)
    w["last_week"] = week
    w.pop("retry_not_before", None)
    try:
        _write_weekly_state(sink, w)
    except OSError:
        print("weekly: state write failed after send")


def run_tick(sink, send_tg=False, chat=None, host=None, dry_run=False, now=None):
    """Единая итерация оповещателя. Возвращает 0 всегда (best-effort)."""
    now_dt = now or _now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cfg = load_config(sink)
    tz = (cfg.get("digest") or {}).get("tz", DEFAULT_TZ)
    tzinfo = _tzinfo(tz)
    send = send_tg and not dry_run

    with alerts_lock(sink) as held:
        if not held:
            print("alert_state: busy (lock) — skip")
            return 0
        # 1) new-P0 мгновенно (burst НЕ мгновенно — kinds=("new_p0",))
        run_alerts(sink, send_tg=send_tg, chat=chat, host=host,
                   dry_run=dry_run, now=now_dt, kinds=("new_p0",))
        # 2) дайджест ровно 1×/сутки (P0-1)
        _tick_digest(sink, now_dt, tzinfo, send)
        # 3) weekly с MSK-гейтом и догоном (P1-3)
        _tick_weekly(sink, now_dt, tzinfo, send)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Error→Rule: единый оповещатель (new-P0 мгновенно + сводка 10:00 MSK + weekly Пн)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    ap.add_argument("--send-tg", action="store_true", help="фактически отправить в TG (иначе dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="план ветвления (какой P0 ушёл бы / дайджест / weekly) без отправки и записи стейта")
    ap.add_argument("--chat", default=None, help="override chat_id (ручной запас)")
    ap.add_argument("--host", default=None, help="override host-тега источника")
    args = ap.parse_args(argv)
    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    try:
        return run_tick(sink, send_tg=args.send_tg, chat=args.chat,
                        host=args.host, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — best-effort: cron не роняем
        print(f"[errors_tick] FAIL: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
