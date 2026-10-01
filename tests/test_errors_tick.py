"""T1-3 (Ф-A2): errors_tick.py — единый оповещатель (new-P0 мгновенно, burst нет,
дайджест 1×/сутки, weekly MSK-гейт, канонический лок).

AC (мастер §T1 T1-3 дословно + REV.2/REV.3):
  new-P0 мгновенно · burst НЕ мгновенно (kinds=("new_p0",)) · дайджест ровно
  1×/сутки (P0-1: attempt-маркер ДО send, С0–С5) · догон пропущенного 10:00 ·
  T-D1 «прокси лёг в 10:00 → ровно один дайджест» · T-D2 «сбой state → ≤1/час» ·
  T-D3 v2 overflow (re-send запрещён) · weekly MSK-гейт границы · плановые вне
  бюджета 3/час · канонический лок (гонка с prune/конкурентом) · TZ-независимость.

Герметичность: send_telegram/flush/cmd_weekly мокаются, now инъецируется, 0 сети.
"""

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # регистрируем, чтобы from-import внутри других скриптов переиспользовал
    spec.loader.exec_module(mod)
    return mod


# Канонические имена + регистрация в sys.modules: errors_tick импортирует
# errors_alert/errors_report/errors_notify, поэтому они обязаны быть ОДНИМИ и
# теми же объектами — иначе monkeypatch ea.send_telegram не затронет run_alerts.
ea = _load("errors_alert", "scripts/errors_alert.py")   # первым — его импортирует tick
er = _load("errors_report", "scripts/errors_report.py")
et = _load("errors_tick", "scripts/errors_tick.py")

MSK = ZoneInfo("Europe/Moscow")


def mk_sink(tmp_path, aggs=None, alert_state=None, notify=True, config=None):
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "aggregates" / "signatures.json").write_text(
        json.dumps(aggs or {}), encoding="utf-8")
    if alert_state is not None:
        (tmp_path / "alert_state.json").write_text(json.dumps(alert_state), encoding="utf-8")
    if notify:
        (tmp_path / "notify.json").write_text(
            json.dumps({"bot_token": "T", "chat_id": "C", "host": "lup"}), encoding="utf-8")
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def _agg(priority="P1", first_seen="2026-09-24T19:00:00Z", last_seen="2026-09-30T10:00:00Z",
         burst_ts=None, count=5, status="active", sources=None, **extra):
    a = {
        "priority": priority, "class": "T", "status": status,
        "first_seen": first_seen, "last_seen": last_seen,
        "count_total": count, "count_7d": count, "count_prev_7d": 0,
        "daily": {}, "actors": [], "sources": sources or ["docker_logs"],
        "last_example": {"message": "example"},
    }
    if burst_ts is not None:
        a["burst_ts"] = burst_ts
        a["burst_count_5m"] = 54
    a.update(extra)
    return a


# ── new-P0 мгновенно / burst НЕ мгновенно ──

def test_new_p0_instant(tmp_path, monkeypatch):
    """new-P0 уходит мгновенно (kinds=("new_p0",)); до 10:00 MSK дайджест молчит."""
    now = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)  # 08:00 MSK Чт
    first = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sink = mk_sink(tmp_path, {"x|p0": _agg("P0", first_seen=first, last_seen=first)})
    ea_sent, et_sent = [], []
    monkeypatch.setattr(ea, "send_telegram", lambda s, t, **kw: ea_sent.append(t) or 1)
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: et_sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert any("NEW P0" in t for t in ea_sent)
    assert et_sent == []  # дайджест до 10:00 не уходит


def test_burst_not_instant(tmp_path, monkeypatch):
    """burst-кандидат НЕ уходит мгновенно — kinds=("new_p0",) фильтрует burst."""
    now = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)
    bts = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sink = mk_sink(tmp_path, {"x|bursty": _agg("P1", burst_ts=bts, last_seen=bts)})
    ea_sent, et_sent = [], []
    monkeypatch.setattr(ea, "send_telegram", lambda s, t, **kw: ea_sent.append(t) or 1)
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: et_sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert ea_sent == []  # burst не отправлен
    assert et_sent == []  # дайджест до 10:00 не ушёл


# ── дайджест ровно 1×/сутки (AC3 идемпотентность) + догон ──

def test_digest_once_per_day(tmp_path, monkeypatch):
    """Дайджест ровно 1×/сутки: два прогона → 1 build+send (AC3)."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 MSK Чт
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    sent = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert len(sent) == 1
    assert sent[0].startswith(er.DIGEST_MARKER + " ·")
    # второй прогон в тот же день (10:05) → 0 новых отправок
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=5)) == 0
    assert len(sent) == 1


def test_digest_catchup(tmp_path, monkeypatch):
    """Догон пропущенного 10:00: хост лежал → первый тик после 10:00 шлёт."""
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # 15:00 MSK Чт
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    sent = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert len(sent) == 1


# ── P0-1: идемпотентность против durable-спула (T-D1/T-D2/T-D3) ──

def test_digest_spool_no_duplicate(tmp_path, monkeypatch):
    """T-D1: «прокси лёг в 10:00» → ровно ОДИН дайджест доставлен (ни второго build,
    ни flush-then-send)."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 MSK Чт
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    delivered = []
    transport = {"up": False}

    def fake_send(s, text, **kw):
        if transport["up"]:
            delivered.append(text)
            return 1
        # транспорт лежит → спулим копию (как errors_notify._spool)
        d = Path(s) / "reports" / "tg-pending"
        d.mkdir(parents=True, exist_ok=True)
        payload = {"created_at": now.isoformat(), "host": "lup", "chat_id": "C",
                   "text": f"🤖[mcp-errors@lup]\n{text}", "attempts": 1,
                   "last_error": "connect failed"}
        (d / f"{now.strftime('%Y%m%dT%H%M%S.000000Z')}-1-1.json").write_text(
            json.dumps(payload), encoding="utf-8")
        return 0

    def fake_flush(s):
        d = Path(s) / "reports" / "tg-pending"
        n = 0
        for f in sorted(d.glob("*.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            delivered.append(p.get("text", ""))
            f.unlink()
            n += 1
        return {"sent": 0, "spooled": 0, "pending": 0, "flushed": n,
                "failed": 0, "skipped": False}

    monkeypatch.setattr(et, "send_telegram", fake_send)
    monkeypatch.setattr(et, "flush", fake_flush)

    # тик 1 (10:00): транспорт лежит → спул, spooled_date=today, retry=+60м
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert delivered == []
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["digest"]["spooled_date"] == "2026-10-01"
    assert st["digest"]["retry_not_before"]

    # тик 2 (10:05): backoff → ничего
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=5)) == 0
    assert delivered == []

    # тик 3 (11:05, транспорт восстановлен): flush-путь → дренаж спула, last_date
    transport["up"] = True
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=65)) == 0
    assert len(delivered) == 1  # ровно одна копия (flush спула), дубля нет
    assert any("🛰 Ошибки" in t for t in delivered)
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["digest"]["last_date"] == "2026-10-01"

    # тик 4 (11:10): last_date=today → 0
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=70)) == 0
    assert len(delivered) == 1


def test_digest_state_write_fail_no_storm(tmp_path, monkeypatch):
    """T-D2: post-attempt запись state кидает OSError → ≤1 send/час, НЕ 12."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 MSK Чт
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    sends = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sends.append(t) or 1)
    real_write = et._write_digest_state
    calls = {"n": 0}

    def flaky(s, d):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        real_write(s, d)

    monkeypatch.setattr(et, "_write_digest_state", flaky)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert len(sends) == 1  # первый тик: send ушёл (attempt-маркер записан)
    # 60 мин тиков (12 × 5 мин) → C4 assume-delivered, 0 отправок
    for i in range(1, 13):
        assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=5 * i)) == 0
    assert len(sends) == 1  # ≤1/час: никакого шторма


def test_t_d3_overflow_no_resend(tmp_path, monkeypatch, capsys):
    """T-D3 v2: overflow-потеря копии = честная потеря (at-most-once), re-send запрещён."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 MSK Чт
    today = "2026-10-01"
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")},
                   alert_state={"digest": {"attempt_date": today, "attempt_ts": now.isoformat()}})
    (sink / "reports").mkdir(parents=True, exist_ok=True)
    (sink / "reports" / "tg-errors.log").write_text(
        f"{now.isoformat()} tg-pending overflow: pruned 5 oldest (limit 200)\n",
        encoding="utf-8")
    sends = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sends.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert sends == []  # 0 отправок
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["digest"]["last_date"] == today
    assert "digest lost to overflow" in capsys.readouterr().out
    # после retry_not_before — снова 0 (last_date блокирует C0, re-send запрещён)
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=120)) == 0
    assert sends == []


def test_assume_delivered_logs_line(tmp_path, monkeypatch, capsys):
    """C4: незакрытая попытка без спула → assume-delivered лог-строка, 0 отправок."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")},
                   alert_state={"digest": {"attempt_date": "2026-10-01",
                                           "attempt_ts": now.isoformat()}})
    sends = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sends.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert sends == []
    assert "assume-delivered (attempt open, no spool)" in capsys.readouterr().out
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["digest"]["last_date"] == "2026-10-01"


def test_digest_spool_detection_tz_safe(tmp_path):
    """P3-2: сравнение attempt_ts/created_at — оба aware ISO UTC (fromisoformat)."""
    d = tmp_path / "reports" / "tg-pending"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "a.json"
    attempt_ts = "2026-10-01T07:00:00+00:00"
    p.write_text(json.dumps({"created_at": attempt_ts,
                             "text": f"🤖\n{er.DIGEST_MARKER} x"}))
    assert et._digest_chunk_spooled(tmp_path, attempt_ts) is True
    p.write_text(json.dumps({"created_at": "2026-10-01T06:59:59+00:00",
                             "text": f"🤖\n{er.DIGEST_MARKER} x"}))
    assert et._digest_chunk_spooled(tmp_path, attempt_ts) is False
    p.write_text(json.dumps({"created_at": attempt_ts, "text": "другое"}))
    assert et._digest_chunk_spooled(tmp_path, attempt_ts) is False


def test_multi_chunk_tail_spool_no_duplicate(tmp_path, monkeypatch):
    """R3.1.3: свежая попытка — чанк-1 (маркер) доставлен, хвост спулен БЕЗ маркера
    → снапшот не находит маркерный чанк → last_date=today (не spooled); 0 повторных send."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})

    def fake_send(s, text, **kw):
        # чанк-1 (маркер) «доставлен», хвост — спулим БЕЗ маркера (симуляция мультичанка)
        d = Path(s) / "reports" / "tg-pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / "tail.json").write_text(json.dumps(
            {"created_at": now.isoformat(), "text": "🤖[mcp-errors@lup]\n(хвост без маркера)"}),
            encoding="utf-8")
        return 1

    monkeypatch.setattr(et, "send_telegram", fake_send)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["digest"]["last_date"] == "2026-10-01"  # снапшот не нашёл маркер
    assert "spooled_date" not in st["digest"]
    # следующий тик → C0 (last_date) → 0 send, хвост не ре-билдится
    calls = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: calls.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=5)) == 0
    assert calls == []


# ── weekly: MSK-гейт + догон ──

def test_weekly_msk_gate():
    """P1-3: weekly MSK-гейт. Границы: Вс 23:59 UTC=Пн 02:59 MSK → нет;
    Пн 09:59 → нет; Пн 10:00 → да; Вт — догон; Ср 23:59 → да; Чт → нет."""
    # Вс 2026-09-27 23:59 UTC = Пн 02:59 MSK → нет
    assert not et._weekly_due(datetime(2026, 9, 27, 23, 59, tzinfo=timezone.utc), MSK)[0]
    # Пн 2026-09-28 09:59 MSK = 06:59 UTC → нет
    assert not et._weekly_due(datetime(2026, 9, 28, 6, 59, tzinfo=timezone.utc), MSK)[0]
    # Пн 10:00 MSK = 07:00 UTC → да
    due, week = et._weekly_due(datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc), MSK)
    assert due
    lm = date(2026, 9, 28)
    assert week == f"{lm.isocalendar().year}-W{lm.isocalendar().week:02d}"
    # Вт 2026-09-29 08:00 MSK = 05:00 UTC → да (догон)
    assert et._weekly_due(datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc), MSK)[0]
    # Ср 2026-09-30 23:59 MSK = 20:59 UTC → да (последняя секунда окна)
    assert et._weekly_due(datetime(2026, 9, 30, 20, 59, tzinfo=timezone.utc), MSK)[0]
    # Чт 2026-10-01 00:00 MSK = 21:00 UTC → нет
    assert not et._weekly_due(datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc), MSK)[0]


def test_weekly_fires_cmd_weekly(tmp_path, monkeypatch):
    """Weekly due → cmd_weekly вызван + last_week записан; повтор → 0 (догон Вт)."""
    now = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)  # Вт 08:00 MSK (догон)
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    called = []
    monkeypatch.setattr(et, "cmd_weekly", lambda s, send: called.append(send))
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert called == [True]
    st = json.loads((sink / "alert_state.json").read_text())
    lm = date(2026, 9, 28)
    week = f"{lm.isocalendar().year}-W{lm.isocalendar().week:02d}"
    assert st["weekly"]["last_week"] == week
    # повторный тик — last_week==week → 0 повторных вызовов
    assert et.run_tick(sink, send_tg=True, now=now + timedelta(minutes=5)) == 0
    assert called == [True]


# ── бюджет + канонический лок ──

def test_planned_outside_budget(tmp_path, monkeypatch):
    """Плановые отправки (digest) вне бюджета max_per_hour=3 (считается только new-P0)."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 MSK Чт
    first = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sink = mk_sink(tmp_path, {
        "x|p0": _agg("P0", first_seen=first, last_seen=first),
        "docker_logs|T|e": _agg("P1"),
    })
    ea_sent, et_sent = [], []
    monkeypatch.setattr(ea, "send_telegram", lambda s, t, **kw: ea_sent.append(t) or 1)
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: et_sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True, now=now) == 0
    assert len(ea_sent) == 1  # new-P0 (в бюджете)
    assert len(et_sent) == 1  # дайджест (вне бюджета)
    st = json.loads((sink / "alert_state.json").read_text())
    assert st["_alerts_meta"]["sent_this_hour"] == 1  # только new-P0, дайджест не считан


def test_state_lock_excludes_concurrent_writer(tmp_path, monkeypatch, capsys):
    """Канонический лок: конкурентный писатель под локом → skip + rc 0, стейт не порчен."""
    now = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    sends = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sends.append(t) or 1)
    with et.alerts_lock(sink) as held:
        assert held is True
        rc = et.run_tick(sink, send_tg=True, now=now)
        assert rc == 0
        assert sends == []
        assert "busy (lock)" in capsys.readouterr().out
        assert not (sink / "alert_state.json").exists()  # стейт не записан


# ── TZ-независимость (фикс. now в UTC и MSK) ──

def test_tz_independence_msk_now(tmp_path, monkeypatch):
    """now инъецируется в MSK — тот же гейт 10:00 (не зависит от часового пояса now)."""
    sink = mk_sink(tmp_path, {"docker_logs|T|e": _agg("P1")})
    sent = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sent.append(t) or 1)
    assert et.run_tick(sink, send_tg=True,
                       now=datetime(2026, 10, 1, 10, 0, tzinfo=MSK)) == 0
    assert len(sent) == 1  # 10:00 MSK → дайджест ушёл
    # 09:59 MSK → нет (другой sink, тот же день)
    sink2 = mk_sink(tmp_path / "b", {"docker_logs|T|e": _agg("P1")})
    sent2 = []
    monkeypatch.setattr(et, "send_telegram", lambda s, t, **kw: sent2.append(t) or 1)
    assert et.run_tick(sink2, send_tg=True,
                       now=datetime(2026, 10, 1, 9, 59, tzinfo=MSK)) == 0
    assert sent2 == []
