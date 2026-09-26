"""028-C: секции weekly-отчёта — «рецидивы к разбору» (B-4) и
«кандидаты на глушение» (C-1, только совет: авто-глушение отвергнуто §7.17.4)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


er = _load("errors_report_028c", "scripts/errors_report.py")


def _iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agg(priority="P1", status="active", fixed_at=None, last_seen=None, count_7d=3, **extra):
    a = {"priority": priority, "class": "T", "status": status, "fixed_at": fixed_at,
         "first_seen": _iso(20), "last_seen": last_seen or _iso(1), "count_total": count_7d,
         "count_7d": count_7d, "count_prev_7d": 0, "daily": {}, "actors": [], "sources": [],
         "last_example": {"message": "example"}}
    a.update(extra)
    return a


def _sink(tmp_path, aggs, alert=None):
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    import json
    (tmp_path / "aggregates" / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")
    (tmp_path / "alert_state.json").write_text(json.dumps(alert or {}), encoding="utf-8")
    return tmp_path


def test_regressed_section_lists_recurrence_with_command(tmp_path, capsys):
    """B-4: рецидив (fixed_at < last_seen, age7) → секция 6 с командой разбора."""
    sig = "docker_logs|TEST|regressed-028"
    fx, ts = _iso(3), _iso(2)
    aggs = {sig: _agg(status="resolved", fixed_at=fx, last_seen=ts)}
    alert = {sig: {"status": "resolved", "fixed_at": fx, "last_seen": ts,
                   "first_seen": _iso(20)}}
    sink = _sink(tmp_path, aggs, alert)
    assert er.cmd_weekly(sink, send_tg=False) == 0
    out = capsys.readouterr().out
    assert "## 7. Рецидивы к разбору" in out
    assert "errors-guard-add" in out and sig[:40] in out


def test_regressed_section_empty(tmp_path, capsys):
    sink = _sink(tmp_path, {("s|A|%d" % 1): _agg()})
    er.cmd_weekly(sink, send_tg=False)
    out = capsys.readouterr().out
    assert "## 7. Рецидивы к разбору" in out and "- (пусто)" in out


def test_suppress_candidates_flags_401_with_command(tmp_path, capsys):
    """C-1: P0 c 401 и без не-рутинных событий → совет с готовой командой."""
    sig = "health|HEALTH|health http://localhost:<n>/ → degraded:http_401"
    aggs = {sig: _agg(priority="P0", count_7d=12, has_non_routine=False)}
    sink = _sink(tmp_path, aggs)
    er.cmd_weekly(sink, send_tg=False)
    out = capsys.readouterr().out
    assert "## 8. Кандидаты на глушение" in out
    assert "auth-required" in out and "errors-guard-add" in out


def test_suppress_candidates_skips_non_routine(tmp_path, capsys):
    """C-1: та же сигнатура с has_non_routine=True (настоящая поломка) — НЕ советуем."""
    sig = "health|HEALTH|health http://localhost:<n>/ → degraded:http_401"
    aggs = {sig: _agg(priority="P0", count_7d=12, has_non_routine=True)}
    sink = _sink(tmp_path, aggs)
    er.cmd_weekly(sink, send_tg=False)
    out = capsys.readouterr().out.split("## 8.")[1]
    assert "- (пусто)" in out


def test_suppress_candidates_skips_resolved(tmp_path, capsys):
    """C-1: уже resolved — не кандидат (нечего глушить)."""
    sig = "docker_events|LIFECYCLE|docker event: die container=x exit=<n>"
    aggs = {sig: _agg(priority="P0", status="resolved", fixed_at=_iso(1),
                      last_seen=_iso(10), count_7d=2, has_non_routine=False)}
    sink = _sink(tmp_path, aggs)
    er.cmd_weekly(sink, send_tg=False)
    out = capsys.readouterr().out.split("## 8.")[1]
    assert "- (пусто)" in out
