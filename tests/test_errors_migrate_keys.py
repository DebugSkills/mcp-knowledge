"""Юнит-тесты errors_migrate_keys_027.py (M-A′, трасса code-2026-09-26-027).

T27-8: (а) merge aggregates с сохранением счётчиков; (б) alert-половина по карте
из aggregates + merge статусов; (в) орфан остаётся и попадает в манифест;
(г) идемпотентность (повторный прогон = no-op); (д) инвариант alert ⊆ aggs ∪ orphans.

Механика: у alert_state НЕТ last_example ⇒ карта строится ТОЛЬКО по aggregates
(ключевое решение Critic iter1 P1-1 / iter2 N-1).
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mg = _load("errors_migrate_keys_027", "scripts/errors_migrate_keys_027.py")


def _agg(msg, count, daily, status="active", priority="P2", **kw):
    a = {"count_total": count, "daily": daily, "suppressed_total": 0,
         "first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-25T00:00:00Z",
         "actors": [], "sources": ["docker_logs"], "status": status,
         "fixed_at": None, "priority": priority, "class": "T",
         "last_example": {"ts": "2026-09-25T00:00:00Z", "message": msg}}
    a.update(kw)
    return a


def test_merge_aggregates_keeps_counters():
    """(а) два ключа, различавшиеся ТОЛЬКО длительностью → один, счётчики суммируются."""
    old1 = 'docker_logs|EMBED|[EMBED] SLOW <n>.4s n=<n>'
    old2 = 'docker_logs|EMBED|[EMBED] SLOW <n>.6s n=<n>'
    aggs = {
        old1: _agg("[EMBED] SLOW 7.4s n=1", 5, {"2026-09-24": 2, "2026-09-25": 3}),
        old2: _agg("[EMBED] SLOW 9.6s n=1", 7, {"2026-09-25": 7}, priority="P1"),
    }
    new_aggs, _, st = mg.migrate(aggs, {})
    assert st["agg_before"] == 2 and st["agg_after"] == 1
    key = next(iter(new_aggs))
    a = new_aggs[key]
    assert a["count_total"] == 12                      # 5 + 7 (не потерян)
    assert a["daily"] == {"2026-09-24": 2, "2026-09-25": 10}   # сумма по датам
    assert a["priority"] == "P1"                       # severity — максимум
    assert key.endswith("[EMBED] SLOW <dur> n=<n>")    # ключ нормализован D1


def test_alert_half_renamed_and_status_merged():
    """(б) alert без last_example: переименование по карте из aggregates + merge статусов."""
    old1 = 'docker_logs|EMBED|[EMBED] SLOW <n>.4s n=<n>'
    old2 = 'docker_logs|EMBED|[EMBED] SLOW <n>.6s n=<n>'
    aggs = {old1: _agg("[EMBED] SLOW 7.4s n=1", 5, {}),
            old2: _agg("[EMBED] SLOW 9.6s n=1", 7, {})}
    alert = {
        old1: {"status": "resolved", "first_seen": "2026-09-01T00:00:00Z",
               "last_seen": "2026-09-10T00:00:00Z", "investigating": False,
               "reported_by_user": False, "last_reported_week": "2026-W37",
               "fixed_at": "2026-09-10T00:00:00Z"},
        old2: {"status": "regressed", "first_seen": "2026-09-02T00:00:00Z",
               "last_seen": "2026-09-25T00:00:00Z", "investigating": False,
               "reported_by_user": True, "last_reported_week": "2026-W39",
               "fixed_at": None},
    }
    new_aggs, new_alert, st = mg.migrate(aggs, alert)
    assert st["alert_after"] == 1 and st["alert_groups"] == 1
    key = next(iter(new_alert))
    st_a = new_alert[key]
    assert st_a["status"] == "regressed"               # приоритет статусов
    assert st_a["first_seen"] == "2026-09-01T00:00:00Z"  # min
    assert st_a["last_seen"] == "2026-09-25T00:00:00Z"   # max
    assert st_a["reported_by_user"] is True              # OR
    assert st_a["last_reported_week"] == "2026-W39"      # max
    assert st_a["fixed_at"] is None                      # рецидив обнуляет
    assert set(new_alert) <= set(new_aggs)               # инвариант (д)


def test_orphan_kept_and_reported():
    """(в) orphan alert-ключ (нет пары в aggregates) — остаётся и в манифесте."""
    aggs = {"docker_logs|EMBED|[EMBED] SLOW <n>.4s": _agg("[EMBED] SLOW 7.4s", 1, {})}
    alert = {"docker_logs|EMBED|[EMBED] SLOW <n>.4s":
             {"status": "known", "last_seen": "2026-09-01T00:00:00Z"},
             "cron_log|CRON|legacy-orphan":
             {"status": "known", "last_seen": "2026-09-01T00:00:00Z"}}
    _, new_alert, st = mg.migrate(aggs, alert)
    assert "cron_log|CRON|legacy-orphan" in new_alert      # не удалён
    assert st["orphans"] == ["cron_log|CRON|legacy-orphan"]


def test_no_last_example_key_left_as_is():
    """Ключ без last_example пересчитать нельзя — остаётся как есть (не теряется)."""
    aggs = {"docker_logs|X|old": {"count_total": 1, "status": "active"}}
    new_aggs, _, st = mg.migrate(aggs, {})
    assert "docker_logs|X|old" in new_aggs
    assert st["underivable"] == ["docker_logs|X|old"]


def test_idempotent_second_run_is_noop(tmp_path, capsys):
    """(г) повторный прогон не меняет файлы (идемпотентность)."""
    old1 = 'docker_logs|EMBED|[EMBED] SLOW <n>.4s n=<n>'
    old2 = 'docker_logs|EMBED|[EMBED] SLOW <n>.6s n=<n>'
    sink = tmp_path
    (sink / "aggregates").mkdir(parents=True)
    mg.atomic_write_json(sink / "aggregates" / "signatures.json", {
        old1: _agg("[EMBED] SLOW 7.4s n=1", 1, {"2026-09-24": 1}),
        old2: _agg("[EMBED] SLOW 9.6s n=1", 1, {"2026-09-25": 1})})
    mg.atomic_write_json(sink / "alert_state.json", {})
    rc = mg.main(["--sink", str(sink), "--confirm", "--trash-dir", str(sink / "trash")])
    assert rc == 0
    after1 = (sink / "aggregates" / "signatures.json").read_bytes()
    assert len(json.loads(after1)) == 1
    capsys.readouterr()
    rc2 = mg.main(["--sink", str(sink), "--confirm", "--trash-dir", str(sink / "trash")])
    assert rc2 == 0 and "NO-OP" in capsys.readouterr().out
    assert (sink / "aggregates" / "signatures.json").read_bytes() == after1


def test_lock_requires_force(tmp_path, capsys):
    """Свежий collector_state (< 300 с) блокирует запись без --force."""
    old1 = 'docker_logs|EMBED|[EMBED] SLOW <n>.4s'
    sink = tmp_path
    (sink / "aggregates").mkdir(parents=True)
    mg.atomic_write_json(sink / "aggregates" / "signatures.json",
                         {old1: _agg("[EMBED] SLOW 7.4s", 1, {})})
    mg.atomic_write_json(sink / "alert_state.json", {})
    mg.atomic_write_json(sink / "collector_state.json", {"fresh": True})  # только что
    rc = mg.main(["--sink", str(sink), "--confirm", "--trash-dir", str(sink / "trash")])
    assert rc == 1 and "ЛОК" in capsys.readouterr().out

def test_meta_key_not_orphan_and_invariant_ok(tmp_path, capsys):
    """Служебный `_alerts_meta` — не орфан и НЕ ломает инвариант (exit 0)."""
    old1 = 'docker_logs|EMBED|[EMBED] SLOW <n>.4s'
    sink = tmp_path
    (sink / "aggregates").mkdir(parents=True)
    mg.atomic_write_json(sink / "aggregates" / "signatures.json",
                         {old1: _agg("[EMBED] SLOW 7.4s", 1, {})})
    mg.atomic_write_json(sink / "alert_state.json", {
        "_alerts_meta": {"hour_bucket": "2026-09-26T11", "sent_this_hour": 1},
        old1: {"status": "known", "last_seen": "2026-09-25T00:00:00Z"},
    })
    rc = mg.main(["--sink", str(sink), "--confirm", "--force",
                  "--trash-dir", str(sink / "trash")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "инвариант alert ⊆ aggregates ∪ orphans — соблюдён" in out
    assert "_alerts_meta" in json.loads((sink / "alert_state.json").read_text())
