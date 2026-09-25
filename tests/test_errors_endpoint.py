r"""Юнит-тесты path-endpoint 009 (спека .boardData.md §7 «path-endpoint»,
дельта-фикс iter1 + §6 iter2 PASS 0.85): поле события `endpoint` и агрегат
`endpoints` БЕЗ смены формулы сигнатуры E2. Все без docker: temp-sink +
uvicorn-синтетика make_event + реальный update_aggregates; сквозной прогон —
реальный errors_collect.main() с подменёнными коллекторами (гвард-интерактив).

Сигнатуры-ожидания захардкожены ЛИТЕРАЛАМИ и сверены прогоном кода ДО патча
(§6 iter2: «реальные строки dev-sink» — литералами в тесте, не чтение sink).

RED-инъекции §7.5: R1→тест 1-2 · R2→AC3-корпус (test_errors_lib) · R3→
AC4 (test_errors_lib) · R4→тест 8 · R5→тест 5.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
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

import mcp_server.tools  # noqa: F401  (регистрирует пакет — прецедент test_errors_guard)

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

# uvicorn access-формат mcp-server (реальный — эмпирика dev-sink, §7.1)
UV401 = 'INFO:     127.0.0.1:43002 - "GET {} HTTP/1.1" 401 Unauthorized'
SIG_401 = ('docker_logs|401|INFO: <n>.<n>.<n>.<n>:<n> - "GET <path> '
           'HTTP/<n>.<n>" <n> Unauthorized')
SIG_404_UUID = ('docker_logs|404|INFO: <n>.<n>.<n>.<n>:<n> - "GET <path>/<uuid><path> '
                'HTTP/<n>.<n>" <n> Not Found')


def ts(minute, sec=0):
    return f"{TODAY}T10:{minute:02d}:{sec:02d}Z"


def uv401(path, t, status=401):
    reason = {401: "Unauthorized", 404: "Not Found"}[status]
    line = f'INFO:     127.0.0.1:43002 - "GET {path} HTTP/1.1" {status} {reason}'
    return ec.make_event(t, "docker_logs", line, level="INFO",
                         error_code=str(status), status=status)


def aggregates(tmp_path):
    return json.loads((tmp_path / "aggregates" / "signatures.json").read_text())


# ── 1. AC1: различимость эндпоинтов внутри ОДНОЙ сигнатуры ──

def test_01_ac1_one_signature_endpoint_split(tmp_path):
    evs = ([uv401("/imports/active", ts(0, i)) for i in range(3)]
           + [uv401("/data-version", ts(1, i)) for i in range(2)])
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    a = aggregates(tmp_path)
    assert len(a) == 1, "все 401-эндпоинты — ОДНА сигнатура (E2 не меняется)"
    assert next(iter(a)) == SIG_401, "байт-в-байт замороженной ДО патча"
    assert a[SIG_401]["endpoints"] == {"/imports/active": 3, "/data-version": 2}


# ── 2. AC2: id-нормализация — uuid/числа в один ключ <id> ──

def test_02_ac2_id_normalized_one_key(tmp_path):
    evs = [
        uv401("/api/v1/books/550e8400-e29b-41d4-a716-446655440000/entries/42",
              ts(0, 0), status=404),
        uv401("/api/v1/books/6ba7b810-9dad-11d1-80b4-00c04fd430c8/entries/7",
              ts(0, 1), status=404),
    ]
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    a = aggregates(tmp_path)
    assert len(a) == 1 and next(iter(a)) == SIG_404_UUID
    assert a[SIG_404_UUID]["endpoints"] == {"/api/v1/books/<id>/entries/<id>": 2}


# ── 3. Cap: 25 ключей → 20 + __others__ (§7.3-2, прецедент actors[:50]) ──

def test_03_cap_twenty_plus_others(tmp_path):
    evs = [uv401(f"/ep{i:02d}", ts(i % 60, i)) for i in range(1, 26)]
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    eps = aggregates(tmp_path)[SIG_401]["endpoints"]
    assert len(eps) == 21, "20 именованных + __others__"
    assert eps["__others__"] == 5, "хвост из 5 просуммирован"


# ── 4. AC5: legacy-события/агрегаты без endpoint/endpoints — 0 KeyError ──

def test_04_ac5_legacy_no_field(tmp_path):
    legacy_ev = ec.make_event(ts(0, 0), "docker_logs", "old line 7", status=401)
    del legacy_ev["endpoint"]  # сырой формат до 009
    ec.update_aggregates(tmp_path, [legacy_ev], {"e4_window_days": 7})
    a = aggregates(tmp_path)
    sig = next(iter(a))
    assert "endpoints" not in a[sig], "старые сигнатуры не мигрируют (R6)"
    # legacy-агрегат без поля → рендер {} без KeyError
    r = eq_tool._render_aggregate("s|o|d", {"priority": "P2"})
    assert r["endpoints"] == {}
    # legacy-агрегат живёт в weekly без KeyError
    er.main(["--sink", str(tmp_path), "--weekly"])
    assert (tmp_path / "reports").exists()


# ── 5. AC6: инвариант приоритетов/шума — snapshot заморожен ДО патчи ──

AC6_CORPUS = [
    (UV401.format("/imports"), 401),
    (UV401.format("/imports/active"), 401),
    (UV401.format("/quality/scan/progress"), 401),
    (UV401.format("/data-version"), 401),
    (UV401.format("/imports?limit=10&offset=2"), 401),
    (('INFO:     172.17.0.5:33410 - "POST /api/v1/books/'
      '550e8400-e29b-41d4-a716-446655440000/entries HTTP/1.1" 503 Service Unavailable'), 503),
    ('INFO:     127.0.0.1:43002 - "GET /files/0123456789abcdef0123 HTTP/1.1" 404 Not Found', 404),
    ('INFO:     127.0.0.1:43002 - "GET http://kb.local:8420/x?y=1 HTTP/1.1" 400 Bad Request', 400),
    ('INFO:     127.0.0.1:1 - "GET / HTTP/1.1" 401 Unauthorized', 401),
    (('INFO:     127.0.0.1:43002 - "DELETE /api/v1/tokens/'
      'deadbeefdeadbeefdeadbeefdeadbeef HTTP/1.1" 404 Not Found'), 404),
]

# заморожено ДО патчи (прогон кода 008-состояния): приоритет/класс/счётчики
AC6_SNAPSHOT = {
    'docker_logs|400|INFO: <n>.<n>.<n>.<n>:<n> - "GET http://kb.local:<n>/x?y=<n> '
    'HTTP/<n>.<n>" <n> Bad Request': ("P2", "T", 1, 1),
    'docker_logs|401|INFO: <n>.<n>.<n>.<n>:<n> - "GET / HTTP/<n>.<n>" <n> Unauthorized':
        ("P3", "T", 1, 1),
    'docker_logs|401|INFO: <n>.<n>.<n>.<n>:<n> - "GET <path> HTTP/<n>.<n>" <n> Unauthorized':
        ("P3", "T", 4, 4),
    'docker_logs|401|INFO: <n>.<n>.<n>.<n>:<n> - "GET <path>?limit=<n>&offset=<n> '
    'HTTP/<n>.<n>" <n> Unauthorized': ("P3", "T", 1, 1),
    'docker_logs|404|INFO: <n>.<n>.<n>.<n>:<n> - "DELETE <path>/<secret> '
    'HTTP/<n>.<n>" <n> Not Found': ("P3", "T", 1, 1),
    'docker_logs|404|INFO: <n>.<n>.<n>.<n>:<n> - "GET <path>/<hex> HTTP/<n>.<n>" '
    '<n> Not Found': ("P3", "T", 1, 1),
    'docker_logs|503|INFO: <n>.<n>.<n>.<n>:<n> - "POST <path>/<uuid><path> '
    'HTTP/<n>.<n>" <n> Service Unavailable': ("P2", "T", 1, 1),
}


def test_05_ac6_snapshot_priorities_unchanged(tmp_path):
    # ts(i) = f"{TODAY}T10:{i:02d}:00Z" — относительные даты от текущего дня
    # (эталон test_errors_guard.py:36-48): corpus всегда внутри 7d-окна,
    # last_seen/status остаются «свежими» — снапшот активных приоритетов
    # не зависит от реальной даты прогона (time-bomb, trace 019).
    evs = [ec.make_event(ts(i), "docker_logs", line,
                         level="INFO", error_code=str(st), status=st)
           for i, (line, st) in enumerate(AC6_CORPUS)]
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    a = aggregates(tmp_path)
    assert len(a) == len(AC6_SNAPSHOT)
    for sig, (prio, cls, total, d7) in AC6_SNAPSHOT.items():
        assert a[sig]["priority"] == prio, sig
        assert a[sig]["class"] == cls, sig
        assert a[sig]["count_total"] == total and a[sig]["count_7d"] == d7, sig
    # noise_ratio — чистая функция P3/total по 7d (формула errors_report:157):
    # P3=8 из 10 → 0.8; приоритеты не изменились ⇒ ratio не меняется
    p3_7d = sum(n for s, (p, _, _, n) in AC6_SNAPSHOT.items() if p == "P3")
    total_7d = sum(n for _, (_, _, _, n) in AC6_SNAPSHOT.items())
    assert round(p3_7d / total_7d, 3) == 0.8


# ── 6. Гвард-интерактив: suppressed не добавляют endpoints (§7.0-8) ──

def test_06_guard_interplay_suppressed_not_counted(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(GUARD_CFG), encoding="utf-8")
    synth = [uv401("/imports/active", ts(0, i)) for i in range(60)]
    monkeypatch.setattr(ec, "collect_docker_logs", lambda *a: list(synth))
    for name in ("collect_cron_logs", "collect_docker_events", "collect_host",
                 "collect_health"):
        monkeypatch.setattr(ec, name, lambda *a: [])
    assert ec.main(["--sink", str(tmp_path)]) == 0
    a = aggregates(tmp_path)
    # разрешено 5 из 60: endpoints считают ТОЛЬКО разрешённые (честное
    # ограничение: suppressed_delta = {sig: int} без путей)
    assert a[SIG_401]["endpoints"] == {"/imports/active": 5}
    assert a[SIG_401]["count_total"] == 60, "частота — полная правда (008)"
    # [GUARD]-маркер: не access-строка → endpoint=None, агрегат без endpoints
    marker_sig = next(k for k in a if k.startswith("guard|GUARD|"))
    assert "endpoints" not in a[marker_sig]
    raw = [json.loads(l) for l in
           (tmp_path / "events" / "raw" / f"{TODAY}.jsonl").read_text().splitlines()]
    guard_ev = next(e for e in raw if e.get("marker") == "GUARD")
    assert guard_ev["endpoint"] is None


# ── 7. Негатив: [REQ]-строки kb-console → endpoint=None ВСЕГДА (P1-2) ──

def test_07_kb_console_req_negative(tmp_path):
    ev = ec.make_event(ts(0, 0), "docker_logs", "[REQ] GET /")
    assert ev["endpoint"] is None
    ec.update_aggregates(tmp_path, [ev] * 3, {"e4_window_days": 7})
    a = aggregates(tmp_path)
    assert "endpoints" not in a[next(iter(a))], "endpoints не растёт от [REQ]"


# ── 8. Рендер errors_query: сортировка count desc + legacy {} ──

def test_08_render_sorted_and_legacy_empty(tmp_path):
    evs = ([uv401("/imports/active", ts(0, i)) for i in range(3)]
           + [uv401("/data-version", ts(1, i)) for i in range(2)])
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    r = eq_tool._render_aggregate(SIG_401, aggregates(tmp_path)[SIG_401])
    assert list(r["endpoints"].items()) == [("/imports/active", 3), ("/data-version", 2)]
    assert eq_tool._render_aggregate("s|o|d", {})["endpoints"] == {}


# ── 9. view: суффикс ep=/…×N(+M) — топ-3, (+M)=сумма вне топ-3 ──

def test_09_view_suffix_top3_plus_n(tmp_path, capsys):
    evs = ([uv401("/imports/active", ts(0, i)) for i in range(3)]
           + [uv401("/data-version", ts(1, i)) for i in range(2)]
           + [uv401("/quality/scan/progress", ts(2, i)) for i in range(2)]
           + [uv401("/files/x", ts(3, 0))])
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    assert er.main(["--sink", str(tmp_path), "--view"]) == 0
    out = capsys.readouterr().out
    assert "ep=/imports/active×3,/data-version×2,/quality/scan/progress×2(+1)" in out
    # без endpoints (например P0-сигнатура) — суффикса нет
    assert "ep=" not in out.split(SIG_401)[0].splitlines()[-1]


# ── 10. weekly P3-baseline: суффикс «· ep: /a×3, /b×2» (топ-3) ──

def test_10_weekly_p3_ep_suffix(tmp_path):
    evs = ([uv401("/imports/active", ts(0, i)) for i in range(3)]
           + [uv401("/data-version", ts(1, i)) for i in range(2)])
    ec.update_aggregates(tmp_path, evs, {"e4_window_days": 7})
    assert er.main(["--sink", str(tmp_path), "--weekly"]) == 0
    report = max((tmp_path / "reports").glob("report-*.md")).read_text()
    sec = report.split("## 4. P3-baseline")[1].split("##")[0]
    assert "ep: /imports/active×3, /data-version×2" in sec
