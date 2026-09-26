r"""Юнит-тесты storm-guard 008 (scripts/errors_guard.py + интеграция errors_collect).

Спека .boardData.md §7 «storm-guard» (В2 + дельта-фикс iter1 + P2-new-1 iter2),
тест-план §7.6 — 17 намерений, все без docker: temp-sink + синтетика make_event;
сквозной прогон — реальный errors_collect.main() с подменёнными коллекторами
(единственная точка интеграции main:839-841 проверяется end-to-end).

RED-инъекции §7.9 (каждая роняет РОВНО свой тест): R1→тест 2 · R2→тест 5 ·
R3→тест 9 · R4→тесты 7-8 · R5→тесты 3-4, 14.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("errors_collect", ROOT / "scripts" / "errors_collect.py")
eg = _load("errors_guard", ROOT / "scripts" / "errors_guard.py")
er = _load("errors_report", ROOT / "scripts" / "errors_report.py")

import mcp_server.tools  # noqa: F401  (регистрирует пакет — прецедент test_masking_parity)

eq_tool = sys.modules["mcp_server.tools.errors_query"]

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
GUARD_CFG = {
    "containers": [], "cron_logs": [], "health_urls": [],
    "thresholds": {"df_warn_pct": 100, "df_crit_pct": 100, "ram_avail_min_pct": 0,
                   "load15_factor": 99999, "vram_warn_pct": 100},
    "own_log": "unused.log",
    "guard": {"enabled": True, "cap_per_minute": 5, "burst_abs": 50,
              "burst_ratio": 10, "burst_window_cycles": 12, "state_ttl_days": 7},
}


def ts(minute, sec=0):
    return f"{TODAY}T10:{minute:02d}:{sec:02d}Z"


def ev_401(t, actor=None, msg='[REQ] GET /imports/active HTTP/1.1" 401'):
    return ec.make_event(t, "docker_logs", msg, status=401, marker="REQ",
                         actor_id=actor)


def run_collector(tmp_path, synth, monkeypatch, cfg=None):
    """Реальный main() на temp-sink; коллекторы подменены (детерминизм без docker)."""
    conf = dict(GUARD_CFG)
    conf.update(cfg or {})
    (tmp_path / "config.json").write_text(json.dumps(conf), encoding="utf-8")
    monkeypatch.setattr(ec, "collect_docker_logs", lambda *a: list(synth))
    for name in ("collect_cron_logs", "collect_docker_events", "collect_host",
                 "collect_health"):
        monkeypatch.setattr(ec, name, lambda *a: [])
    assert ec.main(["--sink", str(tmp_path)]) == 0


def raw_events(tmp_path):
    p = tmp_path / "events" / "raw" / f"{TODAY}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def aggregates(tmp_path):
    return json.loads((tmp_path / "aggregates" / "signatures.json").read_text())


# ── 1. Cap: минутные ведра ──

def test_01_cap_five_per_minute(tmp_path, monkeypatch):
    synth = [ev_401(ts(0, i)) for i in range(60)]
    run_collector(tmp_path, synth, monkeypatch)
    raw = [e for e in raw_events(tmp_path) if e.get("status") == 401]
    assert len(raw) == 5, "raw ≤5 строк/мин на сигнатуру"
    assert [e["ts"] for e in raw] == [ts(0, i) for i in range(5)], "разрешены ПЕРВЫЕ 5"
    a = aggregates(tmp_path)["docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"]
    assert a["suppressed_total"] == 55


# ── 2. Перенос: следующее разрешённое несёт suppressed_count ──

def test_02_transfer_to_next_allowed(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    run_collector(tmp_path, [ev_401(ts(1, 0))], monkeypatch)  # следующая минута
    raw = [e for e in raw_events(tmp_path) if e.get("status") == 401]
    transferred = [e for e in raw if e.get("sampled")]
    assert len(transferred) == 1
    assert transferred[0]["suppressed_count"] == 55
    state = json.loads((tmp_path / "collector_state.json").read_text())
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    assert state["guard"][sig]["suppressed_pending"] == 0


# ── 3. Двухканальность + P1-2: полностью-подавленная сигнатура ──

def test_03_two_channel_and_fully_suppressed_stub(tmp_path, monkeypatch):
    # (а) замолкшая после шторма: подавленное суммируется полностью
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    run_collector(tmp_path, [ev_401(ts(1, i)) for i in range(6)], monkeypatch)
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    a = aggregates(tmp_path)[sig]
    assert a["suppressed_total"] == 56  # 55 + 1 (6-е в новой минуте)
    assert a["count_total"] == 66  # полный поток
    # (б) P1-2: suppression.json, 0 разрешённых за цикл ⇒ stub-агрегат растёт
    tmp2 = tmp_path / "sub"
    tmp2.mkdir()
    sig2 = "docker_logs|REQ|known-noise <n>"
    assert eg.main(["--sink", str(tmp2), "add", sig2, "--reason", "тест"]) == 0
    run_collector(tmp2, [ec.make_event(ts(2, i), "docker_logs", "known-noise 7",
                                       status=404, marker="REQ") for i in range(10)],
                  monkeypatch)
    assert not [e for e in raw_events(tmp2) if e.get("message", "").startswith("known-noise")]
    stub = aggregates(tmp2)[sig2]
    assert stub["suppressed_total"] == 10
    assert stub["suppressed_daily"][TODAY] == 10
    assert stub["count_total"] == 10  # рос даже при 0 разрешённых


# ── 4. count_total/daily/count_7d = ПОЛНЫЙ поток (P2-new-1) ──

def test_04_count_total_full_truth(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    a = aggregates(tmp_path)[sig]
    assert a["count_total"] == 60, "метрики §8 не врут: не 5"
    assert a["daily"][TODAY] == 60
    assert a["count_7d"] == 60, "count_7d из daily — подавленная дельта учтена"


# ── 5. Иммунитет 4xx-с-актором + P2-1 (иммунные не выжигают cap) ──

def test_05_immunity_actor_4xx(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i), actor="key=abcd1234") for i in range(60)],
                  monkeypatch)
    raw = [e for e in raw_events(tmp_path) if e.get("status") == 401]
    assert len(raw) == 60, "user-impact глушить нельзя"
    assert not any(e.get("sampled") for e in raw)
    # P2-1: 30 иммунных той же сигнатуры не съедают cap базлайна
    synth = ([ev_401(ts(3, i), actor="key=abcd1234") for i in range(30)]
             + [ev_401(ts(3, 30 + i)) for i in range(6)])
    run_collector(tmp_path, synth, monkeypatch)
    raw2 = [e for e in raw_events(tmp_path) if e["ts"].startswith(ts(3)[:16])]
    baseline = [e for e in raw2 if not e.get("actor_id")]
    assert len(baseline) == 5, "cap базлайна цел: 5 разрешены"
    assert len([e for e in raw2 if e.get("actor_id")]) == 30, "иммунные все в raw"


# ── 6. Иммунитет traceback — абсолютный ──

def test_06_immunity_traceback(tmp_path, monkeypatch):
    sig = "docker_logs|None|Traceback (most recent call last): <path>"
    assert eg.main(["--sink", str(tmp_path), "add", sig, "--reason", "попробовать"]) == 0
    synth = [ec.make_event(ts(0, i), "docker_logs",
                           "Traceback (most recent call last):\n  File \"/x.py\"",
                           level="ERROR", priority_hint="traceback") for i in range(30)]
    run_collector(tmp_path, synth, monkeypatch)
    tb = [e for e in raw_events(tmp_path) if e.get("priority_hint") == "traceback"]
    assert len(tb) == 30, "не глушится ни cap, ни suppression-листом"
    assert not any(e.get("sampled") for e in tb)


# ── 7. Suppression CLI: add/audit/актор/until/remove ──

def test_07_suppression_cli_and_audit(tmp_path, monkeypatch):
    sig = "docker_logs|REQ|noise-a <n>"
    assert eg.main(["--sink", str(tmp_path), "add", sig, "--reason", "известный шум",
                    "--until", "2099-01-01"]) == 0
    sup = json.loads((tmp_path / "suppression.json").read_text())
    assert sup[sig]["reason"] == "известный шум" and sup[sig]["until"] == "2099-01-01"
    audit = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "add" and audit[-1]["sig"] == sig
    assert "actor" in audit[-1] and "reason" in audit[-1]
    # глушится ДАЖЕ с актором (осознанное решение оператора)
    synth = [ec.make_event(ts(0, i), "docker_logs", "noise-a 3", status=401,
                           marker="REQ", actor_id="key=ff00") for i in range(3)]
    run_collector(tmp_path, synth, monkeypatch)
    assert not [e for e in raw_events(tmp_path) if e.get("message", "").startswith("noise-a")]
    # until истёк → не глохнится
    tmp2 = tmp_path / "exp"
    tmp2.mkdir()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    eg.main(["--sink", str(tmp2), "add", sig, "--reason", "r", "--until", yesterday])
    run_collector(tmp2, [ec.make_event(ts(0, 0), "docker_logs", "noise-a 3",
                                       status=401, marker="REQ")], monkeypatch)
    assert len([e for e in raw_events(tmp2) if e.get("message", "").startswith("noise-a")]) == 1
    # remove → аудит
    assert eg.main(["--sink", str(tmp_path), "remove", sig]) == 0
    assert sig not in json.loads((tmp_path / "suppression.json").read_text())
    audit = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "remove"


# ── 8. Фильтр-инвариант: ключ = ТОЛЬКО точная сигнатура ──

def test_08_exact_signature_key_invariant(tmp_path, monkeypatch):
    eg.main(["--sink", str(tmp_path), "add", "docker_logs|REQ|victim-a <n>",
             "--reason", "r"])
    synth = [ec.make_event(ts(0, i), "docker_logs", "victim-a 1", status=401, marker="REQ")
             for i in range(3)]
    synth += [ec.make_event(ts(0, i), "docker_logs", "other-b 2", status=401, marker="REQ")
              for i in range(3)]  # тот же статус 401, ДРУГАЯ сигнатура
    run_collector(tmp_path, synth, monkeypatch)
    raw = raw_events(tmp_path)
    assert not [e for e in raw if "victim-a" in e["message"]]
    assert len([e for e in raw if "other-b" in e["message"]]) == 3, \
        "глушить «все 401» по статусу невозможно — чужая сигнатура не задета"


# ── 9. Burst: маркер [GUARD] + эскалация + held на втором прогоне ──

def test_09_burst_marker_escalation_held(tmp_path, monkeypatch):
    synth = [ev_401(ts(0, i)) for i in range(60)]
    run_collector(tmp_path, synth, monkeypatch)  # прогон 1
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    markers = [e for e in raw_events(tmp_path) if e.get("marker") == "GUARD"]
    assert len(markers) == 1
    assert markers[0]["priority_hint"] == "burst"
    assert markers[0]["source"] == "guard"
    a = aggregates(tmp_path)
    assert a[sig]["burst"] is True and a[sig]["burst_ts"]
    assert a[sig]["burst_count_5m"] == 60
    assert a[sig]["priority"] == "P1", "baseline P3 → эскалация P1"
    marker_agg = next(k for k in a if k.startswith("guard|GUARD|"))
    assert a[marker_agg]["priority"] == "P1"
    # M7: немедленных алертов нет — в коде нет отправки
    for f in ("errors_collect.py", "errors_guard.py"):
        src = (ROOT / "scripts" / f).read_text()
        assert "api.telegram" not in src and "sendMessage" not in src
    # P1-1: ВТОРОЙ прогон — held: маркер не дублируется, P1 держится, burst_ts стабилен
    run_collector(tmp_path, synth, monkeypatch)
    assert len([e for e in raw_events(tmp_path) if e.get("marker") == "GUARD"]) == 1
    a2 = aggregates(tmp_path)
    assert a2[sig]["priority"] == "P1", "пост-шаг каждый цикл"
    assert a2[sig]["burst_ts"] == a[sig]["burst_ts"]


# ── 10. Burst относительный: mean 1 → 15 (ratio 10) ──

def test_10_burst_relative_ratio():
    bs, cfg = {}, {"guard": {"burst_abs": 50, "burst_ratio": 10}}
    m1, _ = eg.detect_bursts({"s|x|1": 1}, bs, cfg, now=ts(0))
    assert not m1, "спокойная история — без маркера"
    m2, d2 = eg.detect_bursts({"s|x|1": 15}, bs, cfg, now=ts(1))
    assert len(m2) == 1 and "s|x|1" in d2, "×10 к среднему → маркер"


# ── 11. Без ложных + held-дедуп + re-arm/кулдаун (P2-2) ──

def test_11_no_false_burst_held_rearm_cooldown():
    cfg = {"guard": {"burst_abs": 50, "burst_ratio": 10}}
    bs = {}
    m, _ = eg.detect_bursts({"s|x|1": 30}, bs, cfg, now=ts(0))  # история пустая
    assert not m
    m, _ = eg.detect_bursts({"s|x|1": 30}, bs, cfg, now=ts(1))  # mean 30 → нет
    assert not m and len(bs["s|x|1"]["history"]) == 2
    # held: второй цикл подряд с count ≥ порога → ровно 1 маркер
    m1, d1 = eg.detect_bursts({"s|x|1": 60}, bs, cfg, now=ts(2))
    m2, d2 = eg.detect_bursts({"s|x|1": 60}, bs, cfg, now=ts(3))
    assert len(m1) == 1 and not m2 and not d2, "дедуп внутри held безусловен"
    assert bs["s|x|1"]["state"] == "held"
    # спад → re-armed; повторный шторм в пределах кулдауна 24 → НОВОГО маркера нет
    eg.detect_bursts({"s|x|1": 10}, bs, cfg, now=ts(4))
    assert bs["s|x|1"]["state"] == "re-armed"
    m3, d3 = eg.detect_bursts({"s|x|1": 60}, bs, cfg, now=ts(5))
    assert not m3 and not d3, "анти-флаппинг: кулдаун 24 цикла"
    # спад возвращает re-armed; реальные циклы копят cycles_since_fire до 24
    # (без ручной хирургии state — проверяем фактическое накопление кулдауна)
    for minute in range(6, 27):
        eg.detect_bursts({"s|x|1": 10}, bs, cfg, now=ts(minute))
    assert bs["s|x|1"]["cycles_since_fire"] >= 24
    # после кулдауна — НОВОЕ срабатывание с НОВЫМ burst_ts
    m4, d4 = eg.detect_bursts({"s|x|1": 60}, bs, cfg, now=ts(27))
    assert len(m4) == 1 and d4["s|x|1"]["burst_ts"] == ts(27) != d1["s|x|1"]["burst_ts"]


# ── 12. Kill-switch: бит-в-бит прежнее поведение ──

def test_12_kill_switch(tmp_path, monkeypatch):
    synth = [ev_401(ts(0, i)) for i in range(60)]
    run_collector(tmp_path, synth, monkeypatch,
                  cfg={"guard": {"enabled": False, "cap_per_minute": 5}})
    raw = [e for e in raw_events(tmp_path) if e.get("status") == 401]
    assert len(raw) == 60, "все пишутся, гварда нет"
    assert not [e for e in raw_events(tmp_path) if e.get("marker") == "GUARD"]
    state = json.loads((tmp_path / "collector_state.json").read_text())
    assert "guard" not in state and "burst" not in state, "state не тронут"


# ── 13. State-persistence: два прогона + TTL-чистка ──

def test_13_state_persistence_and_ttl(tmp_path, monkeypatch):
    synth = [ev_401(ts(0, i)) for i in range(60)]
    run_collector(tmp_path, synth, monkeypatch)
    run_collector(tmp_path, synth, monkeypatch)
    state = json.loads((tmp_path / "collector_state.json").read_text())
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    # контракт: pending АККУМУЛИРУЕТ до следующего разрешённого — ведро той же
    # минуты персистит (allowed=5 уже исчерпаны) → 2-й прогон: 60 подавлены,
    # pending = 55 + 60 = 115; состояние пережило запуск (state-persistence)
    assert state["guard"][sig]["suppressed_pending"] == 115
    assert state["burst"][sig]["history"] == [60, 60]
    assert state["burst"][sig]["state"] == "held"
    # TTL: pending==0 и last_seen старше state_ttl_days → запись удалена
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = {"guard": {"s|old|z": {"minute": "x", "allowed": 5,
                                "suppressed_pending": 0, "last_seen": old}}}
    eg.apply_write_guard([], st, {"guard": {"enabled": True, "state_ttl_days": 7}},
                         {}, now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert "s|old|z" not in st["guard"]


# ── 14. errors_query: suppressed/burst в выдаче + пример с sampled ──

def test_14_errors_query_visibility(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    run_collector(tmp_path, [ev_401(ts(1, 0))], monkeypatch)  # перенос sampled=true
    sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    agg = aggregates(tmp_path)[sig]
    r = eq_tool._render_aggregate(sig, agg)
    assert r["suppressed_total"] == 55
    assert r["suppressed_7d"] == 55  # вычисляемое из suppressed_daily
    assert r["burst"] is True and r["burst_ts"]
    flt = {"query": None, "include_audit": False, "source": None,
           "examples_limit": 3, "window_days": 7}
    examples, _meta = eq_tool._scan_raw_examples(tmp_path, [(sig, agg)], flt)
    assert examples, "примеры находятся"
    assert any(e.get("sampled") and e.get("suppressed_count") == 55 for e in examples)
    # старый агрегат без guard-полей не ломает выдачу (R6)
    r_old = eq_tool._render_aggregate("s|o|d", {"priority": "P2"})
    assert r_old["suppressed_total"] == 0 and r_old["burst"] is False


# ── 15. Weekly: suppressed-строка + burst-подсекция в окне 7d ──

def test_15_weekly_visibility(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    assert er.main(["--sink", str(tmp_path), "--weekly"]) == 0
    report = max((tmp_path / "reports").glob("report-*.md")).read_text()
    assert "suppressed за 7d (гвард): 55" in report
    sec = report.split("### Burst-инциденты за 7d")[1].split("##")[0]
    assert "docker_logs|REQ|" in sec and "count_5m=60" in sec


# ── 16. Baseline-приоритеты без burst не меняются ──

def test_16_baseline_priorities_unchanged(tmp_path):
    evs = [
        ec.make_event(ts(0, 0), "docker_logs", "[REQ] GET /ok", marker="REQ",
                      status=200, expected=True),  # routine → P3
        ec.make_event(ts(0, 1), "docker_logs", "oom killed", level="ERROR",
                      priority_hint="oom"),  # P0-hint
        ec.make_event(ts(0, 2), "docker_logs", "[MCP] tool=x ok 65000 ms",
                      marker="MCP", priority_hint="slow"),  # slow → P1
    ]
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    a = aggregates(tmp_path)
    assert a["docker_logs|REQ|[REQ] GET <path>"]["priority"] == "P3"
    assert a["docker_logs|-|oom killed"]["priority"] == "P0"
    # 027-D1: длительности маскируются ⇒ "<n> ms" → "<dur>" (E2-заморозка изменена осознанно)
    slow_sig = "docker_logs|MCP|[MCP] tool=x ok <dur>"
    assert a[slow_sig]["priority"] == "P1" and a[slow_sig]["slow"] is True


# ── 17. Декей burst: старше 7d — без эскалации и вне weekly-подсечки ──

def test_17_burst_decay_7d(tmp_path, monkeypatch):
    run_collector(tmp_path, [ev_401(ts(0, i)) for i in range(60)], monkeypatch)
    # подменяем burst_ts жертвы на 8-дневной давности
    agg_path = tmp_path / "aggregates" / "signatures.json"
    aggs = json.loads(agg_path.read_text())
    old_sig = "docker_logs|REQ|[REQ] GET <path> HTTP/<n>.<n>\" <n>"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aggs[old_sig]["burst_ts"] = old_ts
    agg_path.write_text(json.dumps(aggs))
    # следующий цикл: НОВОЕ событие жертвы в НОВОЙ минуте → лестница пересчитает
    # P3; пост-шаг НЕ эскалирует (burst_ts старше 7d — декей) ⇒ P3 остаётся
    run_collector(tmp_path, [ev_401(ts(2, 0))], monkeypatch)
    assert aggregates(tmp_path)[old_sig]["priority"] == "P3", "burst_ts старше 7d"
    er.main(["--sink", str(tmp_path), "--weekly"])
    report = max((tmp_path / "reports").glob("report-*.md")).read_text()
    sec = report.split("### Burst-инциденты за 7d")[1].split("##")[0]
    assert "docker_logs|REQ|" not in sec, "декей исключает из weekly-подсекции"
