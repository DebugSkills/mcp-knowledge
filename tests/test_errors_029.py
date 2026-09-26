r"""Тесты трассы 029 (`.boardData.md` §7.18 v2 + §7.18.10) — блоки A/B/C.

A — routine-шторм guard'а → P2 без TG (сохраняемый `burst_routine`, честный декей);
B — гистограмма `exit_codes` (инкремент, cap top-8 + __other__, -1 → __unknown__);
C — `ISO_TS_RE` в `deep_normalize` (до DUR_RE/NUM_RE): ISO-склейка не дробит ключи.

Герметичность: temp-sink, синтетика `make_event`, 0 docker / 0 сети.
AC-1 формулируется GIN-only (F2-1): жертва с цикловыми событиями (все expected)
самолечится в P2 даже при залипшем lifetime-флаге `has_non_routine`.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("ec029", "scripts/errors_collect.py")
eg = _load("eg029", "scripts/errors_guard.py")
er = _load("er029", "scripts/errors_report.py")
ea = _load("ea029", "scripts/errors_alert.py")

NOW = "2026-09-26T11:00:00Z"
BURST_TS = "2026-09-26T10:00:00Z"     # в окне 7d
OLD_TS = "2026-09-01T10:00:00Z"       # окно истекло
BURST_CFG = {"guard": {"enabled": True, "burst_abs": 50, "burst_ratio": 10,
                       "burst_window_cycles": 12, "state_ttl_days": 7}}


def _agg(**kw):
    base = {"priority": "P1", "class": "T", "burst": True, "burst_ts": BURST_TS,
            "priority_base": "P1", "status": "active"}
    base.update(kw)
    return base


def load_aggs(tmp_path):
    return json.loads((tmp_path / "aggregates" / "signatures.json").read_text())


def _seed(tmp_path, aggs):
    p = tmp_path / "aggregates"
    p.mkdir(parents=True, exist_ok=True)
    (p / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")


# ── A: пост-шаг (A3) ──

def test_a01_poststep_routine_p2_even_base_p1():
    aggs = {"s": _agg(burst_routine=True, priority="P1", priority_base="P1")}
    ec._apply_burst_priority(aggs, ec.parse_ts(NOW))
    assert aggs["s"]["priority"] == "P2"          # routine всегда P2 (N-1)


def test_a02_poststep_nonroutine_p1():
    aggs = {"s": _agg(burst_routine=False, priority="P3")}
    ec._apply_burst_priority(aggs, ec.parse_ts(NOW))
    assert aggs["s"]["priority"] == "P1"          # error-шторм — как было (I1)


def test_a03_poststep_p0_not_downgraded():
    for routine in (True, False):
        aggs = {"s": _agg(burst_routine=routine, priority="P0")}
        ec._apply_burst_priority(aggs, ec.parse_ts(NOW))
        assert aggs["s"]["priority"] == "P0"      # F2-3: по ТЕКУЩЕМУ priority


def test_a04_poststep_window_expired_returns_base():
    aggs = {"s": _agg(burst_routine=True, burst_ts=OLD_TS,
                      priority="P2", priority_base="P3")}
    ec._apply_burst_priority(aggs, ec.parse_ts(NOW))
    assert aggs["s"]["priority"] == "P3"          # декей к base (N-3)


def test_a05_poststep_no_base_skips():
    a = _agg(burst_routine=True, burst_ts=OLD_TS, priority="P2")
    a.pop("priority_base")
    aggs = {"s": a}
    ec._apply_burst_priority(aggs, ec.parse_ts(NOW))   # не должно бросать
    assert aggs["s"]["priority"] == "P2"               # SKIP (N-3)


def test_a06_poststep_bad_ts_skipped():
    aggs = {"s": _agg(burst_ts="not-a-ts")}
    ec._apply_burst_priority(aggs, ec.parse_ts(NOW))
    assert aggs["s"]["priority"] == "P1"               # ValueError → continue


# ── A: лестница (A4/I3) и сохранение флага (A1) ──

def test_a07_ladder_routine_burst_hint_p2(tmp_path):
    ev = ec.make_event(BURST_TS, "guard",
                       "[GUARD] burst_routine: docker_logs|GIN|POST <path> count=100/5min (threshold)",
                       level="ERROR", marker="GUARD", priority_hint="burst_routine")
    ec.update_aggregates(tmp_path, [ev], {})
    a = load_aggs(tmp_path)[ev["signature"]]
    assert (a["priority"], a["class"]) == ("P2", "T")   # аддитивная ветвь
    assert a["priority_base"] == "P2"


def test_a08_backfill_gin_with_sticky_non_routine(tmp_path):
    """AC-1 (GIN-only): залипший has_non_routine не мешает бэкфиллу."""
    evs = [ec.make_event(BURST_TS, "docker_logs", "[MCP] tool=search ok fast",
                         marker="MCP", actor_id=f"actor{i}", expected=True)
           for i in range(2)]
    sig = evs[0]["signature"]
    _seed(tmp_path, {sig: {
        "priority": "P1", "class": "T", "burst": True, "burst_ts": BURST_TS,
        "status": "active", "first_seen": BURST_TS, "last_seen": BURST_TS,
        "count_total": 1, "daily": {}, "actors": [], "sources": ["docker_logs"],
        "last_example": None, "fixed_at": None, "has_non_routine": True}})
    ec.update_aggregates(tmp_path, evs, {})
    a = load_aggs(tmp_path)[sig]
    assert a["burst_routine"] is True            # критерий — события ЦИКЛА, не lifetime
    assert a["priority"] == "P2"                 # routine-жертва → P2, не P1


def test_a09_no_events_no_backfill(tmp_path):
    sig = "docker_logs|REQ|[REQ] GET <path>"
    _seed(tmp_path, {sig: {
        "priority": "P1", "class": "T", "burst": True, "burst_ts": BURST_TS,
        "status": "active", "first_seen": BURST_TS, "last_seen": BURST_TS,
        "count_total": 1, "daily": {}, "actors": [], "sources": [],
        "last_example": None, "fixed_at": None}})
    ec.update_aggregates(tmp_path, [], {"x": 1}, suppressed_delta={sig: 1})
    a = load_aggs(tmp_path)[sig]
    assert "burst_routine" not in a              # F2-2: не пересчитывается без evs
    assert a["priority"] == "P1"                 # F2-1: REQ/MCP остаются P1


# ── A: detect_bursts (A4) ──

def test_a10_detect_bursts_routine_marker():
    sig = "docker_logs|GIN|[GIN] POST <path>"
    markers, bd = eg.detect_bursts({sig: 60}, {}, BURST_CFG, BURST_TS,
                                   routine_sigs={sig})
    assert markers[0]["priority_hint"] == "burst_routine"
    assert markers[0]["message"].startswith("[GUARD] burst_routine: ")
    assert bd[sig]["routine"] is True


def test_a11_detect_bursts_error_marker_unchanged():
    sig = "docker_logs|MCP|[MCP] tool=x"
    markers, bd = eg.detect_bursts({sig: 60}, {}, BURST_CFG, BURST_TS)
    assert markers[0]["priority_hint"] == "burst"
    assert markers[0]["message"].startswith("[GUARD] burst: ")
    assert bd[sig]["routine"] is False


def test_a12_detect_bursts_positional_compat():
    """Параметр routine_sigs optional — позиционный вызов (прецедент 008)."""
    markers, _ = eg.detect_bursts({"s": 60}, {}, BURST_CFG, BURST_TS)
    assert markers and markers[0]["priority_hint"] == "burst"


# ── A: отчёт (A5) ──

def test_a13_report_pri_label_routine():
    assert er._pri_label(_agg(burst_routine=True, priority="P2")) == "P2/routine"
    assert er._pri_label(_agg(burst_routine=False, priority="P1")) == "P1"


def test_a14_weekly_report_marks_routine(tmp_path):
    sig = "docker_logs|GIN|[GIN] POST <path>"
    _seed(tmp_path, {sig: {
        "priority": "P2", "class": "T", "burst": True, "burst_routine": True,
        "burst_ts": BURST_TS, "burst_count_5m": 100, "status": "active",
        "first_seen": BURST_TS, "last_seen": BURST_TS, "count_total": 5,
        "count_7d": 5, "daily": {BURST_TS[:10]: 5}, "actors": [], "sources": [],
        "last_example": {"ts": BURST_TS, "message": "boom"}, "fixed_at": None}})
    (tmp_path / "alert_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    er.cmd_weekly(tmp_path, None)
    text = next((tmp_path / "reports").glob("report-*.md")).read_text()
    assert "[P2/routine]" in text


# ── B: гистограмма exit_codes (B1/B2) ──

def test_b01_bump_increment_and_unknown():
    a = {}
    ec._bump_exit_codes(a, [{"exit_code": 1}, {"exit_code": 1},
                            {"exit_code": 137}, {"exit_code": -1}])
    assert a["exit_codes"] == {"1": 2, "137": 1, "__unknown__": 1}


def test_b02_bump_accumulates_across_cycles():
    a = {}
    ec._bump_exit_codes(a, [{"exit_code": 1}])
    ec._bump_exit_codes(a, [{"exit_code": 1}, {"exit_code": 7}])
    assert a["exit_codes"] == {"1": 2, "7": 1}


def test_b03_bump_cap_and_other():
    a = {}
    ec._bump_exit_codes(a, [{"exit_code": i} for i in range(12)])
    codes = a["exit_codes"]
    assert len(codes) == ec.EXIT_CODES_KEEP + 1          # 8 + __other__
    assert codes["__other__"] == 4
    assert "0" in codes and "9" not in codes             # tie → по ключу


def test_b04_bump_skips_none():
    a = {}
    ec._bump_exit_codes(a, [{"exit_code": None}, {"exit_code": "5"}])
    assert a["exit_codes"] == {"5": 1}


def test_b05_report_exit_codes_top():
    assert er._exit_codes_top({"exit_codes": {"137": 1, "1": 5}}) == [("1", 5), ("137", 1)]


def test_b06_alert_body_shows_codes():
    cand = {"kind": "burst", "sig": "cron_log|CRON|[CRON] job=x exit=<n>",
            "ts": datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
            "agg": {"priority": "P1", "count_7d": 3, "count_total": 3,
                    "burst_count_5m": 60, "exit_codes": {"137": 1},
                    "last_example": {"message": "boom"}}}
    body = ea.alert_body(cand)
    assert "коды:1×137" in body


# ── C: ISO-склейка (C1) ──

def test_c01_iso_hour_stable_single_key():
    m1 = "[CRON] job=prune exit=1 dur=2s ts=2026-09-26T13:05:00Z"
    m2 = "[CRON] job=prune exit=1 dur=2s ts=2026-09-26T14:05:00Z"
    assert ec.deep_normalize(m1) == ec.deep_normalize(m2)   # один ключ на джобу
    assert "<ts>" in ec.deep_normalize(m1)


def test_c02_iso_fraction_and_offset():
    assert "<ts>" in ec.deep_normalize("at 2026-09-26T13:05:00.493503Z ok")
    assert "<ts>" in ec.deep_normalize("at 2026-09-26T13:05:00+03:00 ok")


def test_c03_signature_stable_across_hours():
    s1 = ec.make_signature("cron_log", "CRON", None,
                           "[CRON] job=prune exit=1 dur=2s ts=2026-09-26T13:05:00Z")
    s2 = ec.make_signature("cron_log", "CRON", None,
                           "[CRON] job=prune exit=1 dur=2s ts=2026-09-26T14:05:00Z")
    assert s1 == s2


def test_c04_no_overeating_comma_and_kb():
    out = ec.deep_normalize("search ok 200,123 used 9035.6 KB")
    assert "<ts>" not in out
    assert "," in out          # MCP-формат … ,123 сохранён
    assert "KB" in out         # байтовые величины различимы (027-D1)


def test_c05_dur_fixtures_unchanged():
    for s in ("7.8s", "85.209578ms", "2m0s"):
        assert "<dur>" in ec.deep_normalize(f"[MCP] ok {s}")


def test_c06_plain_date_not_timestamp():
    """Дата без времени — НЕ ISO-ts (граница регулярки)."""
    out = ec.deep_normalize("release 2026-09-26 done")
    assert "<ts>" not in out
