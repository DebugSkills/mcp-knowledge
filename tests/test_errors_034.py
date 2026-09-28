r"""Тесты трассы 034 (plans/034-429-analysis.md §5 Variant A / §6) — anonymous 429 → routine.

F1-граница (критично): routine — СТРОГО anonymous-backpressure:
  (1) access-429 uvicorn — status==429 && marker is None (GIN-строки имеют
      marker='GIN' и исключены; гейт ERROR/5xx идёт раньше);
  (2) WARNING mcp_knowledge.rate_limit «Rate limit exceeded: key=anonymous».
Keyed (key=<hex>) — остаётся сигналом (P1-способен: ≥2 акторов / burst) —
anti-regress t3. Fail-word-гард — по low, прецедент 015-t3 (t5, критик N1:
гард применяется и к access-ветке — «GET /errors» 429 остаётся non-routine
= fail-safe, решение зафиксировано в комментариях classify_routine).

Кейсы §6: 1 access-429 · 2 anonymous-warning · 3 keyed (F1) · 4 чужой
логгер · 5 fail-word-мутанты (N1) · 6 401/403/404/500 без изменений ·
6b gin-429 (iter2 F1-в) · 7 guard-шторм → [GUARD] burst_routine, TG=0
(паттерн tests/test_errors_029.py::test_a10; TG-предикат — тот же, что
tests/test_errors_alert.py::test_burst_requires_p0_p1: burst-кандидат
требует priority ∈ (P0,P1), errors_alert.py:123-127 — критик N2) ·
8 sticky/декей инвариантами (AC — инварианты, не числа sink).

Герметичность: temp-sink, синтетика, 0 docker / 0 сети. Даты динамические
(анти-time-bomb, урок 019).
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("ec034", "scripts/errors_collect.py")
eg = _load("eg034", "scripts/errors_guard.py")
ea = _load("ea034", "scripts/errors_alert.py")

BASE = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_aggs(tmp_path) -> dict:
    return json.loads((tmp_path / "aggregates" / "signatures.json").read_text())


def _seed(tmp_path, aggs):
    p = tmp_path / "aggregates"
    p.mkdir(parents=True, exist_ok=True)
    (p / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")


# ── §6 кейсы 1–6b: classify_routine (граница anonymous/keyed) ──

ACCESS_429 = 'INFO:     127.0.0.1:56286 - "POST /mcp HTTP/1.1" 429 Too Many Requests'
ANON_WARNING = ("2026-09-27 20:14:37,853 [WARNING] mcp_knowledge.rate_limit: "
                "Rate limit exceeded: key=anonymous (available=0.17 tokens)")
KEYED_WARNING = ("2026-09-27 20:14:37,853 [WARNING] mcp_knowledge.rate_limit: "
                 "Rate limit exceeded: key=81af34651c4f (available=0.17 tokens)")


class TestRateLimitBackpressure034:
    """034 §6 (1–6b): routine строго для anonymous; keyed/чужие/мутанты — нет."""

    def _events(self, rest, slow_ms=60000):
        ts = _iso(BASE - timedelta(minutes=1))
        return ec.parse_docker_log_events(
            "mcp-knowledge-server", [f"{ts} {rest}"], "", slow_ms)

    def _prio(self, events):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ec.update_aggregates(Path(td), events, {})
            aggs = json.loads((Path(td) / "aggregates" / "signatures.json").read_text())
            return next(iter(aggs.values()))

    def test_t1_access_429_routine_p3_baseline(self):
        """§6-1: access-429 (status=429, marker=None) → (True, None); агрегат P3-baseline."""
        evs = self._events(ACCESS_429)
        assert len(evs) == 1
        assert evs[0]["status"] == 429 and evs[0]["marker"] is None
        assert evs[0]["expected"] is True
        assert evs[0]["priority_hint"] is None  # (True, None) — без маркера (прецедент 015)
        a = self._prio(evs)
        assert a["priority"] == "P3" and a["class"] == "T"

    def test_t2_anonymous_warning_routine(self):
        """§6-2: WARNING mcp_knowledge.rate_limit + key=anonymous → (True, None)."""
        evs = self._events(ANON_WARNING)
        assert len(evs) == 1
        assert evs[0]["level"] == "WARNING"
        assert evs[0]["actor_id"] is None  # anonymous не матчит KEY_HASH_RE
        assert evs[0]["expected"] is True
        assert evs[0]["priority_hint"] is None
        a = self._prio(evs)
        assert a["priority"] == "P3" and a["class"] == "T"

    def test_t3_keyed_warning_not_routine(self):
        """§6-3 (anti-regress F1): keyed-warning (key=<hex>) → False — P1-способен."""
        evs = self._events(KEYED_WARNING)
        assert len(evs) == 1
        assert evs[0]["expected"] is False
        assert evs[0]["actor_id"] == "81af34651c4f"  # user-impact виден в акторе
        a = self._prio(evs)
        assert a["priority"] != "P3"  # одиночка с актором → P2/U (P1 при ≥2/росте)

    def test_t4_foreign_logger_substring_not_routine(self):
        """§6-4: чужой логгер с «Rate limit exceeded»/«Too Many Requests», но без
        mcp_knowledge.rate_limit → False (двойное сужение, прецедент 015-t2)."""
        evs = self._events("2026-09-27 20:14:37,853 [WARNING] other_service.limiter: "
                           "Rate limit exceeded: Too Many Requests")
        assert len(evs) == 1 and evs[0]["expected"] is False

    def test_t5_fail_word_mutants_not_routine(self):
        """§6-5 (N1): fail-word-мутант с сохранённым якорем → False — ветку
        блокирует гард AUDIT_FAIL_RE, а не отсутствие подстроки (015-t3).
        Гард применён к ОБЕИМ веткам: warning-мутант и access-мутант."""
        # warning-мутант: key=anonymous сохранён + слово error
        evs = self._events("2026-09-27 20:14:37,853 [WARNING] mcp_knowledge.rate_limit: "
                           "Rate limit exceeded: key=anonymous (limiter internal error)")
        assert len(evs) == 1 and evs[0]["expected"] is False
        assert self._prio(evs)["priority"] != "P3"
        # access-мутант: 429-строка + слово failed (гард на access-ветке — fail-safe)
        evs2 = self._events('INFO:     127.0.0.1:56286 - "POST /mcp HTTP/1.1" '
                            "429 Too Many Requests (request failed)")
        assert len(evs2) == 1 and evs2[0]["expected"] is False

    def test_t6_other_4xx_5xx_unchanged(self):
        """§6-6: 401/403/404 → False (baseline P3); 500 → hint 5xx → P0."""
        for status, phrase in ((401, "Unauthorized"), (403, "Forbidden"), (404, "Not Found")):
            evs = self._events(f'INFO:     127.0.0.1:43002 - '
                               f'"GET /imports HTTP/1.1" {status} {phrase}')
            assert len(evs) == 1
            assert evs[0]["expected"] is False, status
            assert self._prio(evs)["priority"] == "P3", status
        evs5 = self._events('INFO:     127.0.0.1:43002 - '
                            '"GET /health HTTP/1.1" 500 Internal Server Error')
        assert len(evs5) == 1
        assert evs5[0]["expected"] is False and evs5[0]["priority_hint"] == "5xx"
        assert self._prio(evs5)["priority"] == "P0"

    def test_t6b_gin_429_not_routine(self):
        """iter2 F1-в: GIN-строка с 429 имеет marker='GIN' → исключена (False)."""
        evs = self._events('[GIN] 2026/09/27 - 20:14:37 | 429 | 1.2ms | '
                           '127.0.0.1 | POST "/api/embed"')
        assert len(evs) == 1
        assert evs[0]["status"] == 429 and evs[0]["marker"] == "GIN"
        assert evs[0]["expected"] is False


# ── §6 кейс 7: guard-шторм → [GUARD] burst_routine, TG-кандидатов 0 ──

GUARD_CFG = {"guard": {"enabled": True, "cap_per_minute": 5, "burst_abs": 50,
                       "burst_ratio": 10, "burst_window_cycles": 12,
                       "state_ttl_days": 7}}


class TestGuardStorm034:
    """034 §6-7: anonymous-429-шторм → [GUARD] burst_routine (НЕ burst);
    лестница :946-949 → P2; TG-кандидатов 0 (burst требует P0/P1)."""

    def test_t7_storm_burst_routine_marker_and_no_tg(self, tmp_path):
        # живой путь: parse → classify (expected=True) → guard → агрегаты
        lines = [_iso(BASE - timedelta(seconds=1)) + " " + ACCESS_429
                 for _ in range(60)]  # ≥ burst_abs(50) — шторм одной сигнатуры
        evs = ec.parse_docker_log_events("mcp-knowledge-server", lines, "")
        assert len(evs) == 60 and all(e["expected"] is True for e in evs)

        state = {}
        allowed, markers, suppressed_delta, burst_delta = eg.apply_write_guard(
            evs, state, GUARD_CFG, now=_iso(BASE))
        # guard-маркер — burst_routine, НЕ burst (паттерн test_a10; routine_sigs
        # построен из expected-событий самим apply_write_guard)
        assert len(markers) == 1
        assert markers[0]["priority_hint"] == "burst_routine"
        assert markers[0]["message"].startswith("[GUARD] burst_routine: ")
        sig = evs[0]["signature"]
        assert burst_delta[sig]["routine"] is True

        ec.update_aggregates(tmp_path, allowed + markers, {},
                             suppressed_delta=suppressed_delta,
                             burst_delta=burst_delta)
        aggs = load_aggs(tmp_path)
        guard_agg = next(a for s, a in aggs.items() if s.startswith("guard|"))
        assert (guard_agg["priority"], guard_agg["class"]) == ("P2", "T")  # :946-949
        victim = aggs[sig]
        assert victim["burst"] is True and victim["burst_routine"] is True
        assert victim["priority"] == "P2"  # burst-окно + routine → P2 (не P1)

        # TG-кандидатов 0: burst-кандидат требует priority ∈ (P0,P1) —
        # errors_alert.py:123-127 (тот же предикат, что
        # tests/test_errors_alert.py::test_burst_requires_p0_p1) — критик N2
        cands = ea.detect_candidates(aggs, {}, {}, {"new_p0_window_min": 10},
                                     BASE + timedelta(minutes=1))
        assert cands == []


# ── §6 кейс 8: sticky/декей — инварианты, не числа sink ──

STICKY_SEED_BASE = {
    "priority": "P1", "class": "T", "burst": True,
    "first_seen": _iso(BASE - timedelta(hours=2)),
    "last_seen": _iso(BASE - timedelta(hours=2)),
    "count_total": 15, "daily": {}, "actors": [], "sources": ["docker_logs"],
    "last_example": None, "status": "active", "fixed_at": None,
}


class TestStickyDecay034:
    """034 §6-8: burst=True, burst_routine=False (живой access-429 агрегат):
    бэкфилл :893-894 при первом routine-событии цикла; без событий — декей
    к base P3 по истечении окна 7d (AC — инварианты)."""

    def test_t8a_backfill_on_first_routine_event(self, tmp_path):
        # сид: burst-жертва БЕЗ ключа burst_routine (+ залипший has_non_routine
        # — критерий бэкфилла = события ЦИКЛА, не lifetime; 029-A1)
        ts = _iso(BASE - timedelta(minutes=1))
        evs = ec.parse_docker_log_events(
            "mcp-knowledge-server", [f"{ts} {ACCESS_429}"], "")
        sig = evs[0]["signature"]
        seed = dict(STICKY_SEED_BASE, burst_ts=_iso(BASE - timedelta(hours=1)),
                    has_non_routine=True)
        _seed(tmp_path, {sig: seed})
        ec.update_aggregates(tmp_path, evs, {})
        a = load_aggs(tmp_path)[sig]
        assert a["burst_routine"] is True       # бэкфилл по событию цикла
        assert a["priority"] == "P2"            # routine-жертва → P2, не P1

    def test_t8b_decay_to_base_p3_after_window(self, tmp_path):
        # без событий по сиду (цикл с чужой сигнатурой): окно 7d истекло →
        # _apply_burst_priority возвращает priority_base (base 429 = P3)
        sig_old = "docker_logs|429|old access 429"
        seed = dict(STICKY_SEED_BASE,
                    burst_ts=_iso(BASE - timedelta(days=8)),
                    burst_routine=False, priority_base="P3")
        _seed(tmp_path, {sig_old: seed})
        other = ec.make_event(_iso(BASE), "docker_logs", "[REQ] GET /",
                              marker="REQ", expected=True)
        ec.update_aggregates(tmp_path, [other], {})
        a = load_aggs(tmp_path)[sig_old]
        assert a["priority"] == "P3"            # декей к base (7d окно истекло)


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-v"]))
