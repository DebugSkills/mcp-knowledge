"""Юнит-тесты errors_collect.py: нормализация/маскирование/сигнатуры/приоритеты E3.

Ф1 спеки code-2026-09-22-003 (.boardData.md §7): фикстуры «строка с UUID/hex/
путём/токеном → сигнатура стабильна, секрет замаскирован». Тесты root-level
(образ mcp-server не видит всё дерево — прецедент P1-3в trace 002).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("errors_collect", ROOT / "scripts" / "errors_collect.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)


# ── deep_normalize (E2, порядок фиксирован) ──

class TestDeepNormalize:
    def test_uuid_replaced(self):
        assert ec.deep_normalize("point 550e8400-e29b-41d4-a716-446655440000 done") == \
            "point <uuid> done"

    def test_hex16_replaced(self):
        assert ec.deep_normalize("hash=0123456789abcdef0123") == "hash=<hex>"

    def test_short_hex_kept(self):
        # key=<hex> схлопывается всегда (актор НЕ в сигнатуре — P1 «≥2 акторов»
        # требует одинаковых сигнатур у разных акторов); голый hex без key= —
        # только ≥16 символов (HEX16)
        assert ec.deep_normalize("key=abcd1234") == "key=<key>"
        assert ec.deep_normalize("hash=abcd1234") == "hash=abcd1234"

    def test_path_replaced(self):
        assert ec.deep_normalize("failed to read /app/knowledge/README.md") == \
            "failed to read <path>"

    def test_numbers_replaced(self):
        # \b\d+\b: «1» заменён, «4» в «1.4s» — часть слова (нет границы) — стабильность ок
        assert ec.deep_normalize("elapsed=1.4s count=42") == "elapsed=<n>.4s count=<n>"

    def test_whitespace_collapsed(self):
        assert ec.deep_normalize("a   b\t c") == "a b c"


# ── mask_secrets (E7/P2-10) ──

class TestMaskSecrets:
    def test_mcp_key_pattern(self):
        masked = ec.mask_secrets("auth key mcp_re_abc123XYZ failed")
        assert "mcp_re_abc123XYZ" not in masked
        assert "<secret>" in masked

    def test_hex32(self):
        masked = ec.mask_secrets("token=0123456789abcdef0123456789abcdef")
        assert "0123456789abcdef0123456789abcdef" not in masked

    def test_authorization_header(self):
        masked = ec.mask_secrets("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in masked

    def test_password_kv(self):
        masked = ec.mask_secrets("password=hunter2 login")
        assert "hunter2" not in masked

    def test_bearer(self):
        masked = ec.mask_secrets("Bearer eyJhbGciOi.payload.sig")
        assert "eyJhbGciOi" not in masked


# ── сигнатура стабильна (E2: 171→47 в пилоте — нормализация схлопывает варианты) ──

class TestSignatureStable:
    def test_same_signature_for_different_uuids(self):
        s1 = ec.make_signature("docker_logs", "IMPORT", None,
                               "import book 550e8400-e29b-41d4-a716-446655440000 failed")
        s2 = ec.make_signature("docker_logs", "IMPORT", None,
                               "import book 123e4567-e89b-12d3-a456-426614174000 failed")
        assert s1 == s2

    def test_same_signature_for_different_actors(self):
        # key-hash схлопывается: одинаковая tool-операция разных акторов — ОДНА
        # сигнатура, агрегат собирает 2 акторов → P1 (E3 работает только так)
        s1 = ec.make_signature("docker_logs", "MCP", None, "[MCP] tool=search ok 12.1 ms key=aaaa1111")
        s2 = ec.make_signature("docker_logs", "MCP", None, "[MCP] tool=search ok 15.9 ms key=bbbb2222")
        assert s1 == s2

    def test_marker_or_error_code_slot(self):
        s = ec.make_signature("cron_log", "CRON", None, "job=backup exit=1")
        assert s.startswith("cron_log|CRON|")

    def test_error_code_used_when_no_marker(self):
        s = ec.make_signature("docker_logs", None, "500", "GET /x 500")
        assert s.startswith("docker_logs|500|")

    def test_secret_never_in_signature(self):
        # make_event маскирует ДО вычисления сигнатуры — секрет не попадает ни в
        # message, ни в signature (make_signature сам не маскирует — by design)
        ev = ec.make_event("2026-09-22T10:00:00Z", "docker_logs",
                           "tool=x key=mcp_re_SuperSecret1 denied")
        assert "SuperSecret1" not in ev["signature"]
        assert "SuperSecret1" not in ev["message"]


# ── приоритизация E3 (замороженный словарь) ──

def _ev(**kw):
    """Событие через make_event — гарантированно имеет signature (как в реальном потоке)."""
    kw.setdefault("ts", "2026-09-22T10:00:00Z")
    kw.setdefault("source", "docker_logs")
    kw.setdefault("message", "msg")
    return ec.make_event(**kw)


class TestPriorityHints:
    def test_p0_hints_frozen(self):
        assert ec.P0_HINTS == frozenset({
            "traceback", "critical", "5xx", "oom", "restart", "cron_nonzero",
            "health_degraded", "hang", "disk_critical"})

    def test_baseline_4xx_frozen(self):
        assert ec.BASELINE_4XX == frozenset({401, 403, 404, 429})


class TestExpectedRestarts:
    """P2-8: плановые restart в окне ±15 мин от [CRON] job=prod-update → expected, НЕ P0."""

    def _aggregated_priority(self, events):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sink = Path(td)
            ec.atomic_write_json(sink / "aggregates" / "signatures.json", {})
            ec.update_aggregates(sink, events, {})
            return json.loads((sink / "aggregates" / "signatures.json").read_text())

    def test_unexpected_restart_is_p0(self):
        aggs = self._aggregated_priority([
            _ev(priority_hint="restart", message="docker event: restart container=x")])
        pri = next(a["priority"] for a in aggs.values())
        assert pri == "P0"

    def test_expected_restart_not_p0(self):
        events = [
            _ev(source="cron_log", marker="CRON", message="[CRON] job=prod-update exit=0 dur=90s ts=2026-09-22T09:55:00Z"),
            _ev(ts="2026-09-22T10:00:00Z", priority_hint="restart", message="docker event: restart container=x"),
        ]
        events = ec.mark_expected_restarts(events)
        assert events[1]["expected"] is True
        aggs = self._aggregated_priority([events[1]])
        pri = next(a["priority"] for a in aggs.values())
        assert pri != "P0"

    def test_far_restart_stays_p0(self):
        events = [
            _ev(source="cron_log", ts="2026-09-22T09:00:00Z", marker="CRON",
                message="[CRON] job=prod-update exit=0 dur=90s ts=2026-09-22T09:00:00Z"),
            _ev(ts="2026-09-22T10:00:00Z", priority_hint="restart", message="restart"),
        ]
        events = ec.mark_expected_restarts(events)
        assert events[1].get("expected") is not True


class TestAggregatePriority:
    def _run(self, events):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sink = Path(td)
            ec.atomic_write_json(sink / "aggregates" / "signatures.json", {})
            ec.update_aggregates(sink, events, {})
            return json.loads((sink / "aggregates" / "signatures.json").read_text())

    def test_two_actors_make_p1(self):
        # реальные access-строки актора в теле НЕ содержат (актор из key= отдельно)
        # → одинаковые сигнатуры, агрегат собирает обоих акторов
        aggs = self._run([
            _ev(status=400, actor_id="aaaa1111", message='"POST /vote HTTP/1.1" 400 Bad Request'),
            _ev(status=400, actor_id="bbbb2222", message='"POST /vote HTTP/1.1" 400 Bad Request'),
        ])
        a = next(iter(aggs.values()))
        assert a["priority"] == "P1"
        assert a["class"] == "U"
        assert sorted(a["actors"]) == ["aaaa1111", "bbbb2222"]

    def test_baseline_4xx_without_actor_is_p3(self):
        aggs = self._run([_ev(status=401, message="401 unauthorized")])
        a = next(iter(aggs.values()))
        assert a["priority"] == "P3"

    def test_traceback_is_p0(self):
        aggs = self._run([_ev(priority_hint="traceback", message="Traceback ...")])
        a = next(iter(aggs.values()))
        assert a["priority"] == "P0"

    def test_single_400_with_actor_is_p2(self):
        # 400/409/422 при наличии актора → U-класс; одиночка → P2 (P1 только при ≥2/росте)
        aggs = self._run([_ev(status=400, actor_id="cccc3333",
                              message='"POST /vote HTTP/1.1" 400 Bad Request')])
        a = next(iter(aggs.values()))
        assert a["priority"] == "P2"
        assert a["class"] == "U"

    def test_health_snapshot_is_p3(self):
        aggs = self._run([_ev(marker="HEALTH", message="health url → ok [snapshot≤1/h]")])
        a = next(iter(aggs.values()))
        assert a["priority"] == "P3"


class TestAtomicWrite:
    """P2-3: state-файлы пишутся tmp + os.replace (weekly-report не порвёт 5-мин цикл)."""

    def test_atomic_write_uses_replace(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(ec.os, "replace", lambda s, d: calls.append((s, d)))
        ec.atomic_write_json(tmp_path / "state.json", {"a": 1})
        assert calls and calls[0][0].name == "state.json.tmp"

    def test_atomic_write_real(self, tmp_path):
        target = tmp_path / "state.json"
        ec.atomic_write_json(target, {"a": 1})
        assert target.exists() and json.loads(target.read_text()) == {"a": 1}
        assert not target.with_name(target.name + ".tmp").exists()


class TestParseDockerLogs:
    def _lines(self, *rows):
        return [f"2026-09-22T15:40:0{i}.437236190Z {r}" for i, r in enumerate(rows)]

    def test_marker_line_captured_with_level(self):
        evs = ec.parse_docker_log_events(
            "mcp-knowledge-server",
            self._lines('2026-09-22 15:40:01,826 [WARNING] mcp_knowledge.reconcile: [RECONCILE] Failed to parse /app/knowledge/README.md: no frontmatter'),
            last_ts="")
        assert len(evs) == 1
        assert evs[0]["marker"] == "RECONCILE"
        assert evs[0]["level"] == "WARNING"

    def test_2xx_access_skipped_4xx_captured(self):
        evs = ec.parse_docker_log_events(
            "mcp-knowledge-server",
            self._lines('INFO:     127.0.0.1:56286 - "GET /health/live HTTP/1.1" 200 OK',
                        'INFO:     127.0.0.1:56287 - "GET /api/v1/nope HTTP/1.1" 404 Not Found'),
            last_ts="")
        assert len(evs) == 1
        assert evs[0]["status"] == 404

    def test_traceback_block_single_event(self):
        evs = ec.parse_docker_log_events(
            "mcp-knowledge-server",
            self._lines("ERROR:    something broke",
                        "Traceback (most recent call last):",
                        '  File "/app/src/x.py", line 10, in f',
                        "    return g()",
                        "ValueError: bad value"),
            last_ts="")
        tbs = [e for e in evs if e["priority_hint"] == "traceback"]
        assert len(tbs) == 1
        assert "ValueError" in tbs[0]["message"]

    def test_dedup_window_last_ts(self):
        lines = [f"2026-09-22T15:40:0{i}.100000000Z [INFO] x: [START] begin" for i in (1, 2)]
        evs = ec.parse_docker_log_events("c", lines, last_ts="2026-09-22T15:40:02Z")
        assert evs == []


class TestCronWrap:
    def test_wrap_line_format(self):
        # синтетический [CRON]-парсер: regex из коллектора
        line = "[CRON] job=backup exit=3 dur=12s ts=2026-09-22T03:00:12+03:00"
        m = ec.CRON_LINE_RE.search(line)
        assert m and m.group(1) == "backup" and m.group(2) == "3"

    def test_wrap_exit_code_semantics(self):
        import subprocess
        wrap = ROOT / "scripts" / "cron_wrap.sh"
        r_ok = subprocess.run(["bash", str(wrap), "t", "/dev/null", "--", "true"], check=False)
        r_fail = subprocess.run(["bash", str(wrap), "t", "/dev/null", "--", "false"], check=False)
        assert r_ok.returncode == 0
        assert r_fail.returncode == 1


class TestMakeEvent:
    def test_message_truncated_and_masked(self):
        ev = ec.make_event("2026-09-22T10:00:00Z", "docker_logs",
                           "x" * 5000 + " key=mcp_re_Secret123")
        assert len(ev["message"]) <= 2000
        assert "Secret123" not in ev["message"]
        assert ev["trace_id"] is None  # зарезервировано (в логах пока не эмитится)

    def test_actor_key_hash(self):
        # actor вычисляет classify_actor (make_event принимает actor_id параметром)
        assert ec.classify_actor("[MCP] tool=search start key=abcd1234", "docker_logs") == "abcd1234"


# ── Ф5 (iter2): routine-класс D1, slow_ms, скоуп docker events D2 ──

LOG_LINE = "2026-09-22T10:00:00Z {}"


class TestRoutineClassification:
    """D1: routine → expected=True → P3; slow/ошибки НЕ routine."""

    def _events(self, rest, slow_ms=60000):
        return ec.parse_docker_log_events(
            "mcp-knowledge-server", [LOG_LINE.format(rest)], "", slow_ms)

    def _prio(self, events):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sink = Path(td)
            ec.update_aggregates(sink, events, {})
            aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
            return next(iter(aggs.values()))

    def test_mcp_ok_fast_is_routine_p3(self):
        evs = self._events("2026-09-22 10:00:00,123 [INFO] mcp_knowledge.mcp: "
                           "[MCP] tool=search_knowledge ok 157.8 ms key=f7708f878c65e1be")
        assert len(evs) == 1 and evs[0]["expected"] is True
        a = self._prio(evs)
        assert a["priority"] == "P3" and a["class"] == "T"

    def test_mcp_ok_slow_is_p1_slow(self):
        # реальный кейс дев-стенда: import_content ok 724667.9 ms (12 минут)
        evs = self._events("2026-09-22 10:00:00,123 [INFO] mcp_knowledge.mcp: "
                           "[MCP] tool=import_content ok 724667.9 ms key=f7708f878c65e1be")
        assert len(evs) == 1 and evs[0]["expected"] is False
        assert evs[0]["priority_hint"] == "slow"
        a = self._prio(evs)
        assert a["priority"] == "P1" and a.get("slow") is True

    def test_mcp_start_is_routine_p3(self):
        # hang-детектор не задет: start всё ещё пишется в raw (capture-first)
        evs = self._events("2026-09-22 10:00:00,123 [INFO] mcp_knowledge.mcp: "
                           "[MCP] tool=search_knowledge start args=['_auth', 'query'] key=f7708f878c65e1be")
        assert len(evs) == 1 and evs[0]["expected"] is True
        assert self._prio(evs)["priority"] == "P3"

    def test_mcp_error_not_routine(self):
        evs = self._events("2026-09-22 10:00:00,123 [ERROR] mcp_knowledge.mcp: "
                           "[MCP] tool=search_knowledge error: qdrant timeout")
        assert len(evs) == 1 and evs[0]["expected"] is False
        assert self._prio(evs)["priority"] in ("P0", "P1", "P2")

    def test_5xx_not_routine_p0(self):
        evs = self._events('INFO:     127.0.0.1:36936 - "GET /health HTTP/1.1" 503 Service Unavailable')
        assert len(evs) == 1 and evs[0]["expected"] is False
        assert evs[0]["priority_hint"] == "5xx"
        assert self._prio(evs)["priority"] == "P0"

    def test_warning_not_routine(self):
        evs = self._events("2026-09-22 10:00:00,123 [WARNING] mcp_knowledge.pipeline: "
                           "Очередь переполнена — blocking put")
        assert len(evs) == 1 and evs[0]["expected"] is False

    def test_req_2xx_is_routine_p3(self):
        # реальный кейс: '[REQ] GET /' без кода ответа → рутина (не P2-шум)
        evs = self._events("[REQ] GET /")
        assert len(evs) == 1 and evs[0]["expected"] is True
        assert self._prio(evs)["priority"] == "P3"

    def test_routine_actors_do_not_make_p1(self):
        # «≥2 акторов ⇒ P1» к routine НЕ применяется — два актора ok-fast → P3
        evs = self._events("2026-09-22 10:00:00,1 [INFO] mcp: [MCP] tool=t ok 10.0 ms key=aaaa1111")
        evs2 = self._events("2026-09-22 10:00:00,2 [INFO] mcp: [MCP] tool=t ok 20.0 ms key=bbbb2222")
        # сигнатуры совпадают (ключ нормализован) — агрегат в одном батче
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sink = Path(td)
            ec.update_aggregates(sink, evs + evs2, {})
            aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
            a = next(iter(aggs.values()))
            assert len(a["actors"]) >= 2 and a["priority"] == "P3"

    def test_non_routine_sticks(self):
        # инкрементальность: одна сигнатура, батч 1 — routine (expected),
        # батч 2 — та же сигнатура, но не-routine → has_non_routine залипает,
        # приоритет уже НЕ откатится в P3 (даже если батч 3 снова routine)
        import tempfile
        ev_kw = dict(ts="2026-09-22T10:00:00Z", source="docker_logs",
                     message="[REQ] GET /", marker="REQ")
        with tempfile.TemporaryDirectory() as td:
            sink = Path(td)
            ec.update_aggregates(sink, [_ev(expected=True, **ev_kw)], {})
            aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
            assert next(iter(aggs.values()))["priority"] == "P3"
            ec.update_aggregates(sink, [_ev(expected=False, **ev_kw)], {})
            aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
            assert next(iter(aggs.values()))["priority"] != "P3"
            ec.update_aggregates(sink, [_ev(expected=True, **ev_kw)], {})
            aggs = json.loads((sink / "aggregates" / "signatures.json").read_text())
            assert next(iter(aggs.values()))["priority"] != "P3"  # залипло


class TestDockerEventsScope:
    """D2: события чужих контейнеров (donation_bot*) полностью игнорируются."""

    def test_foreign_container_filtered(self, monkeypatch):
        payload = {
            "status": "die", "Action": "die",
            "Actor": {"Attributes": {"name": "donation_bot", "exitCode": "1"}},
            "Time": 1758554400,
        }

        class FakeProc:
            returncode = 0
            stdout = json.dumps(payload) + "\n"

        cmd_seen = {}

        def fake_run(cmd, **kw):
            cmd_seen["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(ec.subprocess, "run", fake_run)
        cfg = {"containers": ["mcp-knowledge-server", "kb-console"]}
        out = ec.collect_docker_events(Path("/tmp/kilo/nonexistent-sink"), {}, cfg)
        assert out == []  # чужой контейнер отброшен пост-фильтром
        # и фильтры container= в команде присутствуют (двойная защита)
        flat = " ".join(cmd_seen["cmd"])
        assert "container=mcp-knowledge-server" in flat

    def test_own_container_passes(self, monkeypatch):
        payload = {
            "status": "die", "Action": "die",
            "Actor": {"Attributes": {"name": "mcp-knowledge-server", "exitCode": "1"}},
            "Time": 1758554400,
        }

        class FakeProc:
            returncode = 0
            stdout = json.dumps(payload) + "\n"

        monkeypatch.setattr(ec.subprocess, "run", lambda cmd, **kw: FakeProc())
        out = ec.collect_docker_events(Path("/tmp/kilo/nonexistent-sink"), {},
                                       {"containers": ["mcp-knowledge-server"]})
        assert len(out) == 1 and "mcp-knowledge-server" in out[0]["message"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
