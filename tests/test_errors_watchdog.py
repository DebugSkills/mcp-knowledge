"""T1-3b (Ф-A2b): errors_watchdog.py — независимый watchdog молчания tick.

AC (мастер §T1 T1-3b + §R2.5/P2-B/P3-4):
  • tick молчит >30 мин → синтетическое P1-событие source=watchdog (сигнатура
    watchdog|tick_silent) И короткое TG (cooldown 120 мин, анти-шторм);
  • живой tick → 0 ложных;
  • cooldown → reports/.watchdog-cooldown (НЕ alert_state.json — P2-B);
  • синтетика → incoming/watchdog.jsonl (контракт pulled_error_log, P3-4);
  • источник дозаписывается в config.pulled_error_log идемпотентно.

Герметичность: send_telegram мокается, now инъецируется, 0 сети; sink/tick.log —
только tmp_path. Реальный crontab/прод-данные не трогаются.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # from-import внутри других скриптов переиспользует
    spec.loader.exec_module(mod)
    return mod


# Канонические имена: errors_watchdog импортирует errors_collect/errors_notify,
# поэтому они обязаны быть ОДНИМИ и теми же объектами (monkeypatch ew.send_telegram
# бьёт по send_telegram, которую зовёт check_tick).
ec = _load("errors_collect", "scripts/errors_collect.py")
_ = _load("errors_notify", "scripts/errors_notify.py")
ew = _load("errors_watchdog", "scripts/errors_watchdog.py")

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _write_tick_log(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


def _tick_log_line(ts):
    return f"[CRON] job=tick exit=0 dur=1s ts={ts}"


def _synthetic_lines(sink):
    sp = sink / "incoming" / "watchdog.jsonl"
    if not sp.exists():
        return []
    return sp.read_text(encoding="utf-8").strip().splitlines()


# ── AC T1-3b: живой tick → 0 ложных ──

def test_alive_tick_no_false_positive(tmp_path, monkeypatch):
    """Свежий маркер [CRON] job=tick → ни synthetic, ни TG, ни записи конфига."""
    tick_log = tmp_path / "tick.log"
    fresh = (NOW - timedelta(minutes=3)).isoformat()
    _write_tick_log(tick_log, [_tick_log_line(fresh)])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW) == 0
    assert sent == []
    assert _synthetic_lines(sink) == []
    assert not (sink / "config.json").exists()  # живой tick → конфиг не трогаем


def test_alive_tick_no_false_positive_ignore_other_jobs(tmp_path, monkeypatch):
    """Маркеры job=tick_silent / job=collector не считаются тиком (exact match)."""
    tick_log = tmp_path / "tick.log"
    fresh = (NOW - timedelta(minutes=3)).isoformat()
    _write_tick_log(tick_log, [
        f"[CRON] job=tick_silent exit=0 dur=1s ts={(NOW - timedelta(minutes=50)).isoformat()}",
        f"[CRON] job=collector exit=0 dur=1s ts={(NOW - timedelta(minutes=50)).isoformat()}",
        _tick_log_line(fresh),
    ])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW) == 0
    assert sent == [] and _synthetic_lines(sink) == []


# ── AC T1-3b: tick молчит → сигнал в sink + TG ──

def test_silent_tick_signals(tmp_path, monkeypatch):
    """Маркер старше 30 мин → synthetic P1 (watchdog.jsonl) + TG + конфиг + cooldown."""
    tick_log = tmp_path / "tick.log"
    _write_tick_log(tick_log, [_tick_log_line((NOW - timedelta(minutes=40)).isoformat())])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW) == 0
    # TG: один вызов, короткое сообщение
    assert len(sent) == 1
    assert "watchdog" in sent[0] and "tick молчит" in sent[0]
    # synthetic: append-документ kind=signature с нужными полями
    docs = [json.loads(l) for l in _synthetic_lines(sink)]
    assert len(docs) == 1 and docs[0]["kind"] == "signature"
    row = docs[0]["rows"][0]
    assert row["signature"] == "tick_silent"
    assert row["marker"] == "tick_silent"
    assert row["priority_hint"] == "watchdog_silent"
    # конфиг: pulled_error_log дозаписан источником watchdog (идемпотентно)
    cfg = json.loads((sink / "config.json").read_text())
    assert any(i.get("source") == "watchdog" for i in cfg.get("pulled_error_log", []))
    # cooldown — в reports/.watchdog-cooldown (НЕ alert_state.json, P2-B)
    assert (sink / "reports" / ".watchdog-cooldown").exists()
    assert not (sink / "alert_state.json").exists()


def test_no_marker_is_silent(tmp_path, monkeypatch):
    """tick ни разу не отчитался (нет [CRON] job=tick) = молчит → сигнал."""
    tick_log = tmp_path / "tick.log"
    _write_tick_log(tick_log, ["# пустой лог — тик ни разу не отчитался"])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW) == 0
    assert len(sent) == 1
    assert len(_synthetic_lines(sink)) == 1


# ── AC T1-3b: cooldown 120 мин (анти-шторм) ──

def test_cooldown_storm_guard(tmp_path, monkeypatch):
    """Молчание: TG 1 раз в окне cooldown; synthetic копится каждый прогон;
    после cooldown TG снова разрешён."""
    tick_log = tmp_path / "tick.log"
    _write_tick_log(tick_log, [_tick_log_line((NOW - timedelta(minutes=40)).isoformat())])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW) == 0
    assert len(sent) == 1
    # в пределах cooldown (ещё молчит): TG не повторяется, synthetic пишется
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW + timedelta(minutes=15)) == 0
    assert len(sent) == 1
    assert len(_synthetic_lines(sink)) == 2
    # после cooldown 120 мин: TG снова уходит
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW + timedelta(minutes=121)) == 0
    assert len(sent) == 2
    assert len(_synthetic_lines(sink)) == 3


# ── P3-4: синтетика входит в агрегаты через контракт pulled_error_log ──

def test_synthetic_enters_aggregates(tmp_path):
    """Синтетическое событие ингестируется коллектором → агрегат P1, source=watchdog."""
    tick_log = tmp_path / "tick.log"
    _write_tick_log(tick_log, [_tick_log_line((NOW - timedelta(minutes=40)).isoformat())])
    sink = tmp_path / "sink"
    ew.check_tick(sink, tick_log=tick_log, now=NOW)

    cfg = ec.load_config(sink)
    events = ec.collect_pulled_error_log(sink, {}, cfg)
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "watchdog"
    assert ev["signature"].startswith("watchdog|tick_silent|")
    assert ev["priority_hint"] == "watchdog_silent"

    ec.update_aggregates(sink, events, cfg)
    aggs = ec.load_json(sink / "aggregates" / "signatures.json", {})
    assert len(aggs) == 1
    (sig, a), = aggs.items()
    assert a["priority"] == "P1"          # ladder-rung watchdog_silent → P1
    assert a["sources"] == ["watchdog"]
    assert sig.startswith("watchdog|tick_silent")


# ── dry-run: без побочных эффектов (HITL-превью) ──

def test_dry_run_no_side_effects(tmp_path, monkeypatch):
    tick_log = tmp_path / "tick.log"
    _write_tick_log(tick_log, [_tick_log_line((NOW - timedelta(minutes=40)).isoformat())])
    sent = []
    monkeypatch.setattr(ew, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    sink = tmp_path / "sink"
    assert ew.check_tick(sink, tick_log=tick_log, now=NOW, dry_run=True) == 0
    assert sent == []
    assert _synthetic_lines(sink) == []
    assert not (sink / "config.json").exists()
    assert not (sink / "reports" / ".watchdog-cooldown").exists()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
