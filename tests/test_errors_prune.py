"""Юнит-тесты errors_prune.py: ретенция sink Error→Rule.

T27-7 (трасса code-2026-09-26-027, блок D3): истечение «мёртвых» сигнатур
(status ∈ {new, known, resolved} + last_seen старше stale_sig_days) из
alert_state И aggregates; regressed/investigating — НИКОГДА; dry-run не пишет;
--confirm пишет + бэкап обоих файлов.

Живой dry-run на молОДОМ sink (17 суток < 45d) даёт 0 ПО ОПРЕДЕЛЕНИЮ —
поэтому проверка синтетическая (см. §7.16.4 п.4 спеки: «не выдавать 0 за успех»).
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("errors_collect", "scripts/errors_collect.py")
pr = _load("errors_prune", "scripts/errors_prune.py")


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _sink(tmp_path, alert, aggs=None):
    """Синтетический sink: config (kill-switch ON) + alert_state + aggregates."""
    (tmp_path / "aggregates").mkdir(parents=True, exist_ok=True)
    ec.atomic_write_json(tmp_path / "aggregates" / "signatures.json", aggs or {})
    ec.atomic_write_json(tmp_path / "alert_state.json", alert)
    ec.atomic_write_json(tmp_path / "config.json",
                         {"prune": {"enabled": True}, "retention_days": 90,
                          "hold_days": 14, "stale_sig_days": 45})
    return tmp_path


def test_expiry_scope_statuses(tmp_path):
    """Истекают только new/known/resolved со старым last_seen."""
    alert = {
        "old-new": {"status": "new", "last_seen": _iso(46), "investigating": False},
        "old-known": {"status": "known", "last_seen": _iso(60), "investigating": False},
        "old-resolved": {"status": "resolved", "last_seen": _iso(90), "investigating": False},
        "fresh-new": {"status": "new", "last_seen": _iso(3), "investigating": False},
        "old-regressed": {"status": "regressed", "last_seen": _iso(80), "investigating": False},
        "old-investigating": {"status": "known", "last_seen": _iso(80), "investigating": True},
        "_alerts_meta": {"anything": 1},
    }
    sink = _sink(tmp_path, alert)
    _, sigs, stale, _, _ = pr.plan(sink, {"stale_sig_days": 45}, alert)
    assert sorted(stale) == ["old-known", "old-new", "old-resolved"]
    assert "fresh-new" not in stale
    assert "old-regressed" not in stale and "old-investigating" not in stale
    assert "_alerts_meta" not in stale


def test_dry_run_writes_nothing(tmp_path, capsys):
    alert = {"old-new": {"status": "new", "last_seen": _iso(50), "investigating": False}}
    aggs = {"old-new": {"status": "active", "last_seen": _iso(50), "count_total": 3}}
    sink = _sink(tmp_path, alert, aggs)
    before_a = (sink / "alert_state.json").read_bytes()
    before_g = (sink / "aggregates" / "signatures.json").read_bytes()
    rc = pr.main(["--sink", str(sink)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 истёкших сигнатур" in out and "DRY-RUN" in out
    assert (sink / "alert_state.json").read_bytes() == before_a
    assert (sink / "aggregates" / "signatures.json").read_bytes() == before_g


def test_confirm_removes_both_halves_and_backups(tmp_path, capsys):
    alert = {"old-new": {"status": "new", "last_seen": _iso(50), "investigating": False},
             "keep": {"status": "regressed", "last_seen": _iso(99), "investigating": False}}
    aggs = {"old-new": {"status": "active", "last_seen": _iso(50), "count_total": 3},
            "keep": {"status": "active", "last_seen": _iso(99), "count_total": 1}}
    sink = _sink(tmp_path, alert, aggs)
    rc = pr.main(["--sink", str(sink), "--confirm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PRUNED" in out and "1 истёкших (D3)" in out
    left_a = json.loads((sink / "alert_state.json").read_text())
    left_g = json.loads((sink / "aggregates" / "signatures.json").read_text())
    assert "old-new" not in left_a and "old-new" not in left_g
    assert "keep" in left_a and "keep" in left_g
    import tarfile
    tars = list((sink / "events").glob("prune-backup-*.tar.gz"))
    assert len(tars) == 1
    with tarfile.open(tars[0]) as tar:
        names = tar.getnames()
        assert "alert-state-pruned-signatures.json" in names
        snap = json.loads(tar.extractfile("alert-state-pruned-signatures.json").read())
        assert list(snap) == ["old-new"]


def test_confirm_without_killswitch_refuses(tmp_path):
    alert = {"old-new": {"status": "new", "last_seen": _iso(50), "investigating": False}}
    sink = _sink(tmp_path, alert)
    ec.atomic_write_json(sink / "config.json",
                         {"prune": {"enabled": False}, "stale_sig_days": 45})
    rc = pr.main(["--sink", str(sink), "--confirm"])
    assert rc == 1
    assert "old-new" in json.loads((sink / "alert_state.json").read_text())


def test_clean_sink_is_noop(tmp_path, capsys):
    sink = _sink(tmp_path, {"fresh": {"status": "new", "last_seen": _iso(1)}})
    rc = pr.main(["--sink", str(sink), "--confirm"])
    assert rc == 0 and "нечего удалять" in capsys.readouterr().out
