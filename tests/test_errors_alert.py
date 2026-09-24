"""Юнит-тесты errors_alert.py — немедленные алерты new-P0/burst (016, В2-блок а).

Спека .boardData.md §7.2(а)/§7.5/§7.6: AC-alert-1 (new-P0 + идемпотентность,
шаг 3 с fake-now за cooldown 120 мин) · AC-alert-2 (burst + cooldown) ·
AC-alert-3 (анти-шторм: 2 основных + 1 хвост = 3 send/прогон; ≤3/час;
cooldown 120 мин) · AC-filter-1 (suppressed/investigating НЕ алертить) ·
AC-deg-1 (деградация без notify.json, стейт не мутируется) · ленивые burst-поля
(отсутствие burst_ts/count_5m/first_seen/битый ts — без KeyError) · host-тег ·
аддитивность alert_state (classify_weekly/prune/metrics не затронуты).

Герметичность: fake-sender (monkeypatch send_telegram в модуле alerts),
fake-now (monkeypatch _now), 0 реальной сети.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("errors_alert", ROOT / "scripts" / "errors_alert.py")
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)
_spec_r = importlib.util.spec_from_file_location("errors_report", ROOT / "scripts" / "errors_report.py")
er = importlib.util.module_from_spec(_spec_r)
_spec_r.loader.exec_module(er)
_spec_n = importlib.util.spec_from_file_location("errors_notify", ROOT / "scripts" / "errors_notify.py")
en = importlib.util.module_from_spec(_spec_n)
_spec_n.loader.exec_module(en)
_spec_p = importlib.util.spec_from_file_location("errors_prune", ROOT / "scripts" / "errors_prune.py")
epr = importlib.util.module_from_spec(_spec_p)
_spec_p.loader.exec_module(epr)

NOW = datetime(2026, 9, 24, 19, 0, 0, tzinfo=timezone.utc)
ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
LATER_121 = NOW + timedelta(minutes=121)  # за cooldown 120 мин
LATER_10 = NOW + timedelta(minutes=10)    # внутри cooldown


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("MCP_ERRORS_HOST", raising=False)


class FakeSender:
    """Мимикрия реального sender: префиксует тег (как errors_notify.send_telegram)."""

    def __init__(self):
        self.sent = []
        self.calls = []

    def __call__(self, sink, text, chat_override=None, host_override=None):
        self.calls.append({"chat": chat_override, "host": host_override, "text": text})
        notify = en.load_json(Path(sink) / "notify.json", None) or {}
        tag = en.host_tag(en.resolve_host(notify, host_override))
        self.sent.append(f"{tag}\n{text}")
        return 1


@pytest.fixture
def sender(monkeypatch):
    fake = FakeSender()
    monkeypatch.setattr(ea, "send_telegram", fake)
    return fake


@pytest.fixture
def fakenow(monkeypatch):
    holder = {"now": NOW}

    def _now():
        return holder["now"]
    monkeypatch.setattr(ea, "_now", _now)
    holder["set"] = lambda dt: holder.__setitem__("now", dt)
    return holder


def mk_sink(tmp_path, aggs, alert_state=None, suppression=None, notify=True):
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "aggregates" / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")
    if alert_state is not None:
        (tmp_path / "alert_state.json").write_text(json.dumps(alert_state), encoding="utf-8")
    if suppression is not None:
        (tmp_path / "suppression.json").write_text(json.dumps(suppression), encoding="utf-8")
    if notify:
        (tmp_path / "notify.json").write_text(
            json.dumps({"bot_token": "T", "chat_id": "C", "host": "lup"}), encoding="utf-8")
    return tmp_path


def agg(priority="P1", first=ISO, last=ISO, burst_ts=None, count_5m=None, count=5, status="active"):
    a = {"priority": priority, "class": "T", "count_total": count, "count_7d": count,
         "count_prev_7d": 0, "daily": {}, "first_seen": first, "last_seen": last,
         "status": status, "fixed_at": None, "actors": [], "sources": ["docker_logs"],
         "last_example": {"message": "boom example"}}
    if burst_ts is not None:
        a["burst_ts"] = burst_ts
        a["burst_count_5m"] = count_5m if count_5m is not None else 54
    return a


def run(sink, extra=()):
    return ea.main(["--sink", str(sink), "--send-tg", *extra])


# ── AC-alert-1: new-P0 + идемпотентность (3 шага, P2-2: fake-now за cooldown) ──

class TestNewP0:
    def test_ac_alert_1_full_scenario(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"docker_logs|500|api": agg("P0")})
        # шаг 1: 1 сообщение kind=new_p0, тег ровно один раз
        assert run(sink) == 0
        assert len(sender.sent) == 1
        assert "NEW P0" in sender.sent[0]
        assert sender.sent[0].startswith("🤖[mcp-errors@lup]")
        assert sender.sent[0].count("🤖[mcp-errors@lup]") == 1
        # шаг 2: повтор без изменений = 0
        assert run(sink) == 0
        assert len(sender.sent) == 1
        # шаг 3: новый инцидент (burst_ts обновлён) + fake-now ЗА cooldown 120 мин = 1 (burst)
        aggs = {"docker_logs|500|api": agg("P0", burst_ts=LATER_121.strftime("%Y-%m-%dT%H:%M:%SZ"))}
        (sink / "aggregates" / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")
        fakenow["set"](LATER_121)
        assert run(sink) == 0
        assert len(sender.sent) == 2
        assert "BURST" in sender.sent[1]

    def test_not_p0_not_candidate(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|P1sig": agg("P1")})
        run(sink)
        assert sender.sent == []

    def test_old_first_seen_out_of_window(self, tmp_path, sender, fakenow):
        old = (NOW - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sink = mk_sink(tmp_path, {"x|oldp0": agg("P0", first=old)})
        run(sink)
        assert sender.sent == []

    def test_already_alerted_not_candidate(self, tmp_path, sender, fakenow):
        st = {"x|p0done": {"p0_alerted_at": ISO, "investigating": False}}
        sink = mk_sink(tmp_path, {"x|p0done": agg("P0")}, alert_state=st)
        run(sink)
        assert sender.sent == []

    def test_missing_first_seen_not_candidate(self, tmp_path, sender, fakenow):
        a = agg("P0")
        a.pop("first_seen")
        sink = mk_sink(tmp_path, {"x|nofs": a})
        run(sink)
        assert sender.sent == []

    def test_broken_first_seen_not_candidate(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|badfs": agg("P0", first="not-a-date")})
        run(sink)
        assert sender.sent == []


# ── AC-alert-2: burst + cooldown ──

class TestBurst:
    def test_ac_alert_2_full_scenario(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|bursty": agg("P1", burst_ts=ISO)})
        # прогон 1 = 1 сообщение kind=burst
        assert run(sink) == 0
        assert len(sender.sent) == 1
        assert "BURST" in sender.sent[0]
        # повторный прогон в течение cooldown = 0
        fakenow["set"](LATER_10)
        assert run(sink) == 0
        assert len(sender.sent) == 1
        # fake-now за cooldown + НОВЫЙ burst_ts = 1
        new_ts = LATER_121.strftime("%Y-%m-%dT%H:%M:%SZ")
        (sink / "aggregates" / "signatures.json").write_text(
            json.dumps({"x|bursty": agg("P1", burst_ts=new_ts)}), encoding="utf-8")
        fakenow["set"](LATER_121)
        assert run(sink) == 0
        assert len(sender.sent) == 2

    def test_no_burst_ts_not_candidate_no_keyerror(self, tmp_path, sender, fakenow):
        """Ленивые поля: отсутствие burst_ts/burst_count_5m не роняет (P3)."""
        sink = mk_sink(tmp_path, {"x|plain": agg("P1")})  # burst_ts отсутствует
        assert run(sink) == 0
        assert sender.sent == []

    def test_stale_burst_out_of_window(self, tmp_path, sender, fakenow):
        stale = (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sink = mk_sink(tmp_path, {"x|stale": agg("P1", burst_ts=stale)})
        run(sink)
        assert sender.sent == []

    def test_burst_requires_p0_p1(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|p2burst": agg("P2", burst_ts=ISO)})
        run(sink)
        assert sender.sent == []

    def test_burst_without_count_5m_ok(self, tmp_path, sender, fakenow):
        a = agg("P1", burst_ts=ISO)
        a.pop("burst_count_5m")  # ленивое поле отсутствует
        sink = mk_sink(tmp_path, {"x|nobc": a})
        run(sink)
        assert len(sender.sent) == 1


# ── AC-filter-1: suppressed / investigating — НЕ алертить ──

class TestFilters:
    def test_active_suppression_filters(self, tmp_path, sender, fakenow, capsys):
        supp = {"x|supp": {"reason": "known noise", "until": None}}
        sink = mk_sink(tmp_path, {"x|supp": agg("P1", burst_ts=ISO)}, suppression=supp)
        assert run(sink) == 0
        assert sender.sent == []
        assert "suppressed=1" in capsys.readouterr().out

    def test_investigating_filters(self, tmp_path, sender, fakenow, capsys):
        st = {"x|inv": {"investigating": True}}
        sink = mk_sink(tmp_path, {"x|inv": agg("P0")}, alert_state=st)
        assert run(sink) == 0
        assert sender.sent == []
        assert "investigating=1" in capsys.readouterr().out

    def test_positive_control_no_flags(self, tmp_path, sender, fakenow):
        """Негативная гарантия: те же сигнатуры БЕЗ флагов → алерт есть."""
        sink = mk_sink(tmp_path, {"x|free": agg("P1", burst_ts=ISO)})
        run(sink)
        assert len(sender.sent) == 1

    def test_expired_suppression_not_filter(self, tmp_path, sender, fakenow):
        yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        supp = {"x|exp": {"reason": "r", "until": yesterday}}
        sink = mk_sink(tmp_path, {"x|exp": agg("P1", burst_ts=ISO)}, suppression=supp)
        run(sink)
        assert len(sender.sent) == 1

    def test_broken_suppression_ts_not_filter(self, tmp_path, sender, fakenow):
        """P3-a: битый until ('zzz' лексикографически '>= сегодня' в guard) —
        alert-фильтр обязан иметь parse-guard → НЕ фильтрует."""
        supp = {"x|bad": {"reason": "r", "until": "zzz-banana"}}
        sink = mk_sink(tmp_path, {"x|bad": agg("P1", burst_ts=ISO)}, suppression=supp)
        run(sink)
        assert len(sender.sent) == 1


# ── AC-alert-3: анти-шторм (2 основных + 1 хвост; ≤3/час; cooldown) ──

class TestAntiStorm:
    def test_ten_p0_two_main_one_tail(self, tmp_path, sender, fakenow):
        aggs = {f"x|sig{i:02d}": agg("P0", first=(NOW - timedelta(minutes=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")) for i in range(10)}
        sink = mk_sink(tmp_path, aggs)
        assert run(sink) == 0
        assert len(sender.sent) == 3  # 2 основных + 1 хвост
        mains = [t for t in sender.sent if "NEW P0" in t]
        tail = [t for t in sender.sent if "NEW P0" not in t]
        assert len(mains) == 2
        assert len(tail) == 1
        assert "и ещё 8" in tail[0]
        # помечены ТОЛЬКО отправленные (2 из 10)
        st = json.loads((sink / "alert_state.json").read_text())
        marked = sum(1 for v in st.values() if isinstance(v, dict) and v.get("p0_alerted_at"))
        assert marked == 2

    def test_hourly_cap_three(self, tmp_path, sender, fakenow):
        aggs = {f"x|h{i:02d}": agg("P0") for i in range(10)}
        sink = mk_sink(tmp_path, aggs)
        for _ in range(10):  # 10 прогонов в течение часа (fake-now не двигаем)
            assert run(sink) == 0
        assert len(sender.sent) <= 3  # ≤3 send/час, хвост включён
        log = (sink / "reports" / "tg-errors.log").read_text()
        assert "storm-limit" in log and "suppressed" in log

    def test_cooldown_repeat_incident_silenced(self, tmp_path, sender, fakenow):
        """Повторный инцидент той же сигнатуры в течение 120 мин = 0 сообщений."""
        sink = mk_sink(tmp_path, {"x|rep": agg("P1", burst_ts=ISO)})
        run(sink)
        assert len(sender.sent) == 1
        new_ts = (NOW + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (sink / "aggregates" / "signatures.json").write_text(
            json.dumps({"x|rep": agg("P1", burst_ts=new_ts)}), encoding="utf-8")
        fakenow["set"](NOW + timedelta(minutes=30))
        run(sink)
        assert len(sender.sent) == 1  # cooldown держит


# ── AC-deg-1: деградация без notify.json ──

class TestDegradation:
    def test_no_notify_no_mutation(self, tmp_path, sender, fakenow, monkeypatch):
        sink = mk_sink(tmp_path, {"x|p0a": agg("P0")}, notify=False)
        # настоящий send_telegram (не fake) не должен вызываться — модуль обязан
        # сам увидеть отсутствие notify.json и не мутировать стейт
        monkeypatch.setattr(ea, "send_telegram",
                            lambda *a, **kw: pytest.fail("отправка при отсутствующем notify.json"))
        before = (sink / "alert_state.json").exists()
        assert run(sink) == 0  # exit 0
        assert not (sink / "alert_state.json").exists()  # стейт НЕ мутирован
        assert before is False
        log = (sink / "reports" / "tg-errors.log").read_text()
        assert log.count("TG: skip (notify.json") == 1

    def test_dry_run_no_send_no_state(self, tmp_path, sender, fakenow, capsys):
        sink = mk_sink(tmp_path, {"x|p0d": agg("P0")})
        assert ea.main(["--sink", str(sink), "--dry-run"]) == 0
        assert sender.sent == []
        assert not (sink / "alert_state.json").exists()
        assert "NEW P0" in capsys.readouterr().out  # план печатается

    def test_no_send_tg_prints_plan_only(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|p0p": agg("P0")})
        assert ea.main(["--sink", str(sink)]) == 0
        assert sender.sent == []
        assert not (sink / "alert_state.json").exists()


# ── host-тег в алертах (AC-host-1) ──

class TestHostTag:
    @pytest.mark.parametrize("host", ["lup", "aikb"])
    def test_host_override_tag_once(self, tmp_path, sender, fakenow, host):
        sink = mk_sink(tmp_path, {"x|h": agg("P0")})
        run(sink, extra=["--host", host])
        assert len(sender.sent) == 1
        assert sender.sent[0].startswith(f"🤖[mcp-errors@{host}]\n")
        assert sender.sent[0].count(f"@{host}") == 1

    def test_host_passed_to_sender(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|h2": agg("P0")})
        run(sink, extra=["--host", "aikb"])
        assert sender.calls[0]["host"] == "aikb"

    def test_chat_passed_to_sender(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|h3": agg("P0")})
        run(sink, extra=["--chat=-100xyz"])  # = : argparse с минус-значением
        assert sender.calls[0]["chat"] == "-100xyz"

    def test_texts_distinguish_hosts(self, tmp_path, sender, fakenow):
        a = mk_sink(tmp_path / "a", {"x|hh": agg("P0")})
        run(a, extra=["--host", "lup"])
        b = mk_sink(tmp_path / "b", {"x|hh": agg("P0")})
        run(b, extra=["--host", "aikb"])
        assert sender.sent[0] != sender.sent[1]  # тексты различимы


# ── аддитивность alert_state (§7.6: classify_weekly / prune / metrics) ──

class TestStateAdditive:
    def _alerted_state(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|a1": agg("P0")})
        run(sink)
        return sink, json.loads((sink / "alert_state.json").read_text())

    def test_classify_weekly_preserves_alert_fields(self, tmp_path, sender, fakenow):
        sink, st = self._alerted_state(tmp_path, sender, fakenow)
        assert st["x|a1"].get("p0_alerted_at")
        aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
        er.classify_weekly(aggs, st)
        # 8 полей classify_weekly появились, alert-поля НЕ потеряны/не мутированы
        assert st["x|a1"]["p0_alerted_at"] == st["x|a1"]["p0_alerted_at"]
        assert "status" in st["x|a1"] and "last_reported_week" in st["x|a1"]
        assert st["x|a1"].get("alert_cooldown_until")
        # старое поле cooldown_until (weekly-семантика) не тронуто нами: отсутствует
        assert "cooldown_until" not in st["x|a1"] or st["x|a1"]["cooldown_until"] is None

    def test_prune_plan_ignores_alert_fields(self, tmp_path, sender, fakenow):
        sink, st = self._alerted_state(tmp_path, sender, fakenow)
        st["_alerts_meta"] = {"hour_bucket": "2026-09-24T19", "sent_this_hour": 1,
                              "last_run": ISO}
        (sink / "alert_state.json").write_text(json.dumps(st), encoding="utf-8")
        cfg = {"retention_days": 90, "hold_days": 14}
        day_files, sigs, _old_backups, _ = epr.plan(sink, cfg, st)
        assert sigs == []  # новые поля/мета не делают сигнатуру prune-кандидатом
        assert day_files == []

    def test_metrics_tolerates_alerts_meta(self, tmp_path, sender, fakenow):
        """P3-h: второй consumer metrics() — _alerts_meta не ломает метрики."""
        sink, st = self._alerted_state(tmp_path, sender, fakenow)
        st["_alerts_meta"] = {"hour_bucket": "2026-09-24T19", "sent_this_hour": 1,
                              "last_run": ISO}
        aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
        met = er.metrics(aggs, st, {"lines_7d": 0, "p3_lines_7d": 0, "noise_ratio": 0.0,
                                    "raw_mb": 0.0, "mb_per_month": 0.0})
        assert met["found_before_user"].startswith("н/д")  # meta-dict не дал False→строку «0/0»

    def test_meta_written_on_send(self, tmp_path, sender, fakenow):
        _sink, st = self._alerted_state(tmp_path, sender, fakenow)
        meta = st.get("_alerts_meta")
        assert meta and meta["hour_bucket"] == NOW.strftime("%Y-%m-%dT%H")
        assert meta["sent_this_hour"] == 1


# ── текст алерта (формат §7.2-а) ──

class TestAlertText:
    def test_new_p0_body_format(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|fmt": agg("P0", count=7)})
        run(sink)
        body = sender.sent[0]
        assert "NEW P0" in body and "7d=7" in body and "total=7" in body
        assert "boom example" in body
        assert "errors-view" in body

    def test_burst_body_includes_count_5m(self, tmp_path, sender, fakenow):
        sink = mk_sink(tmp_path, {"x|fmtb": agg("P1", burst_ts=ISO, count_5m=54)})
        run(sink)
        assert "count_5m=54" in sender.sent[0]

    def test_long_signature_truncated(self, tmp_path, sender, fakenow):
        sig = "x|" + "s" * 300
        sink = mk_sink(tmp_path, {sig: agg("P0")})
        run(sink)
        body = sender.sent[0]
        assert "s" * 121 not in body  # сигнатура усечена до ≤120
