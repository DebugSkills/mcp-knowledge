#!/usr/bin/env python3
"""errors_watchdog.py — независимый watchdog молчания tick (Ф-T1-3b, code-2026-09-30-errors-digest).

Наблюдает за heartbeat tick'а ВНЕ tick (P1-4/§R2.5): единый оповещатель
`errors_tick.py` после миграции несёт new-P0 + дайджест + weekly — его тихая
смерть теряет всё сразу. Heartbeat 018 фиксирует только успешные запуски
(`errors_collect.py:500-566` — [CRON] exit=0 → P3), детектора «джоба молчит» нет.

Логика:
  1. читает последний маркер `[CRON] job=tick … ts=<iso>` из
     `$DATA_ROOT/logs/cron/tick.log` (пишет cron_wrap.sh);
  2. staleness > 30 мин (= 6 пропущенных тиков */5; худший тик ≈9 мин — ложных
     нет) → (а) синтетическое P1-событие `source=watchdog` (стабильная сигнатура
     `watchdog|tick_silent`) через контракт pulled_error_log —
     append-документ `{"kind":"signature","rows":[…]}` в `incoming/watchdog.jsonl`
     (читается коллектором в его */5-цикле — единый писатель raw/aggregates
     сохранён; конфиг `pulled_error_log` дозаписывается идемпотентно) и
     (б) короткое TG через errors_notify (cooldown 120 мин против шторма;
     cooldown — в `reports/.watchdog-cooldown`, НЕ в alert_state.json — P2-B,
     watchdog исключён из писателей alert_state и не гейтится .alerts.lock);
  3. живой tick (маркер свежий) → 0 ложных.

Honest boundary (§R2.5/R2.14-4): собственное тихое зависание watchdog'а
наблюдается только через exit≠0 → cron_nonzero (cron_wrap). Поэтому main() НЕ
глотает исключения — неожиданная ошибка → exit≠0 → P0 cron_nonzero.

Своя cron-строка `*/15` через cron_wrap.sh (job=watchdog, watchdog.log) — свой
[CRON]-маркер → собственный heartbeat виден 018.

Python ≥3.9, stdlib-only. Отправка ТОЛЬКО через errors_notify.send_telegram.
Выход 0 при штатной работе (в т.ч. при срабатывании сигнала).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors_collect import (  # sibling-импорт по прецеденту errors_alert.py:47
    DATA_ROOT,
    atomic_write_json,
    load_json,
    parse_ts,
)
from errors_notify import send_telegram

STALENESS_MIN = 30
COOLDOWN_MIN = 120
# stable-сигнатура watchdog (R2.5): источник задаёт конфиг pulled_error_log
# (source="watchdog"), marker/key = "tick_silent" → сигнатура watchdog|tick_silent|…
SIG_TICK_SILENT = "tick_silent"
HINT_WATCHDOG_SILENT = "watchdog_silent"
TICK_LOG_NAME = "tick.log"
SYNTHETIC_NAME = "watchdog.jsonl"
COOLDOWN_NAME = ".watchdog-cooldown"

# маркер cron_wrap: "[CRON] job=tick exit=0 dur=5s ts=<iso>"
CRON_JOB_RE = re.compile(r"\bjob=tick\b[^\n]*\bts=(\S+)")


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_tick_log(sink):
    """Путь tick.log по умолчанию: $DATA_ROOT/logs/cron/tick.log."""
    return Path(DATA_ROOT) / "logs" / "cron" / TICK_LOG_NAME


def _last_tick_ts(tick_log):
    """→ datetime | None: ts последнего [CRON] job=tick из tick.log.

    Отсутствующий файл / нет маркера → None (тик ни разу не отчитался = молчит).
    Битый ts → пропуск строки (не роняет watchdog).
    """
    try:
        lines = tick_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    last = None
    for line in lines:
        if "[CRON]" not in line:
            continue
        m = CRON_JOB_RE.search(line)
        if not m:
            continue
        try:
            last = parse_ts(m.group(1))
        except ValueError:
            continue
    return last


def _synthetic_path(sink):
    return Path(sink) / "incoming" / SYNTHETIC_NAME


def _cooldown_path(sink):
    return Path(sink) / "reports" / COOLDOWN_NAME


def ensure_pulled_entry(sink):
    """Идемпотентно добавить источник watchdog в config.pulled_error_log.

    Без этого коллектор не прочитает incoming/watchdog.jsonl (P3-4). Возвращает
    True, если запись добавлена (конфиг переписан), иначе False. Атомарная
    запись (tmp+os.replace) — гонки с чтением конфига коллектором нет.
    """
    cfgp = Path(sink) / "config.json"
    cfg = load_json(cfgp, {})
    if not isinstance(cfg, dict):
        cfg = {}
    path_s = str(_synthetic_path(sink))
    pl = cfg.get("pulled_error_log")
    if not isinstance(pl, list):
        pl = []
    for item in pl:
        if isinstance(item, dict) and item.get("path") == path_s:
            return False
    pl.append({"path": path_s, "source": "watchdog", "origin": "watchdog"})
    cfg["pulled_error_log"] = pl
    atomic_write_json(cfgp, cfg)
    return True


def append_synthetic(sink, now_dt):
    """Append-документ kind=signature в incoming/watchdog.jsonl (P3-4).

    Контракт collect_pulled_error_log (byte-offset дедуп): append-only, НЕ
    truncate (иначе «offset > size → с нуля» = дубли при повторном ингесте).
    """
    d = Path(sink) / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "kind": "signature",
        "rows": [{
            "signature": SIG_TICK_SILENT,     # error_code / key сигнатуры
            "marker": SIG_TICK_SILENT,        # key в make_signature (вместо error_log)
            "priority_hint": HINT_WATCHDOG_SILENT,  # ladder → P1/T
            "ts": _iso(now_dt),
        }],
    }
    with open(_synthetic_path(sink), "a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def _read_cooldown(sink):
    p = _cooldown_path(sink)
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return parse_ts(raw)
    except ValueError:
        return None


def _write_cooldown(sink, now_dt):
    p = _cooldown_path(sink)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(now_dt.isoformat() + "\n", encoding="utf-8")
    tmp.replace(p)


def _tg_message(last_ts, now_dt):
    if last_ts is None:
        seen = "нет маркера [CRON] job=tick"
    else:
        age_min = max(0, int((now_dt - last_ts).total_seconds() // 60))
        seen = f"last={last_ts.isoformat()} ({age_min} мин назад)"
    return (f"⚠ watchdog: tick молчит >{STALENESS_MIN} мин ({seen}) — "
            f"проверьте cron/errors_tick; детали: make errors-view")


def check_tick(sink, tick_log=None, now=None, staleness_min=STALENESS_MIN,
               cooldown_min=COOLDOWN_MIN, dry_run=False):
    """Одна итерация watchdog. → int (0 всегда; best-effort, но НЕ глотает
    неожиданные исключения — они уходят в exit≠0 → cron_nonzero)."""
    now_dt = now or _now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    if tick_log is None:
        tick_log = default_tick_log(sink)
    tick_log = Path(tick_log)

    last_ts = _last_tick_ts(tick_log)
    stale = last_ts is None or (now_dt - last_ts) > timedelta(minutes=staleness_min)

    if not stale:
        if dry_run:
            print(f"watchdog: tick жив (last={last_ts.isoformat()}) — 0 ложных")
        return 0

    if dry_run:
        print(f"watchdog: tick молчит >{staleness_min} мин "
              f"(last={last_ts.isoformat() if last_ts else 'нет маркера'}) → "
              f"ПЛАН: synthetic P1 + TG (cooldown {cooldown_min} мин)")
        return 0

    ensure_pulled_entry(sink)
    append_synthetic(sink, now_dt)

    alerted_at = _read_cooldown(sink)
    if alerted_at is None or (now_dt - alerted_at) >= timedelta(minutes=cooldown_min):
        try:
            send_telegram(sink, _tg_message(last_ts, now_dt))
        except Exception as exc:  # noqa: BLE001 — best-effort: TG не роняет watchdog
            print(f"watchdog: TG failed: {exc}")
        _write_cooldown(sink, now_dt)
        print("watchdog: tick молчит — synthetic P1 записан + TG отправлен")
    else:
        print("watchdog: tick молчит — synthetic P1 записан (TG: cooldown)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Error→Rule: watchdog молчания tick (staleness [CRON] job=tick > 30 мин → P1 + TG)")
    ap.add_argument("--sink", default=None, help="override каталога sink (dev/фикстуры)")
    ap.add_argument("--tick-log", default=None,
                    help="override пути tick.log (по умолчанию $DATA_ROOT/logs/cron/tick.log)")
    ap.add_argument("--staleness-min", type=int, default=STALENESS_MIN,
                    help="порог молчания в минутах (дефолт 30)")
    ap.add_argument("--cooldown-min", type=int, default=COOLDOWN_MIN,
                    help="cooldown TG в минутах (дефолт 120)")
    ap.add_argument("--dry-run", action="store_true",
                    help="план без записи synthetic и без отправки TG (HITL)")
    args = ap.parse_args(argv)

    sink = Path(args.sink) if args.sink else DATA_ROOT / "logs" / "errors"
    tick_log = Path(args.tick_log) if args.tick_log else None
    # НЕ глотаем исключения: неожиданная ошибка → exit≠0 → cron_wrap → cron_nonzero P0
    return check_tick(sink, tick_log=tick_log, staleness_min=args.staleness_min,
                      cooldown_min=args.cooldown_min, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
