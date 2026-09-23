"""errors_query (006): фильтры/лимиты/view/маскирование/graceful/audit/0-мутаций.

Спека .boardData.md §7 (code-2026-09-22-006) §8. Sink — tmp_path, без docker.
Auth-матрица — отдельно в test_auth_errors_query.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.tools.errors_query import (
    RAW_SCAN_CAP_BYTES,
    errors_query,
)

NOW = datetime.now(timezone.utc)
ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
LAST_MONTH = (NOW - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")

AGGS = {
    "docker_logs|MCP|timeout after <n>": {
        "priority": "P1", "class": "U", "status": "active",
        "count_7d": 12, "count_prev_7d": 3, "count_total": 57,
        "actors": ["1a2b3c4d", "cron:errors-collect"], "sources": ["docker_logs"],
        "first_seen": LAST_MONTH, "last_seen": ISO, "fixed_at": None,
        "last_example": {"ts": ISO, "message": "[MCP] raw"},
    },
    "cron_log|CRON|backup finished": {
        "priority": "P3", "class": "T", "status": "active",
        "count_7d": 2, "count_prev_7d": 2, "count_total": 10,
        "actors": [], "sources": ["cron_log"],
        "first_seen": LAST_MONTH, "last_seen": YESTERDAY, "fixed_at": None,
        "last_example": None,
    },
    "host|MCP|db gone": {
        "priority": "P2", "class": "T", "status": "resolved",
        "count_7d": 0, "count_prev_7d": 5, "count_total": 5,
        "actors": [], "sources": ["host"],
        "first_seen": LAST_MONTH, "last_seen": LAST_MONTH, "fixed_at": LAST_MONTH,
        "last_example": None,
    },
    "docker_logs|ERRORS_QUERY|audit": {
        "priority": "P3", "class": "T", "status": "active",
        "count_7d": 9, "count_prev_7d": 0, "count_total": 9,
        "actors": ["1a2b3c4d"], "sources": ["docker_logs"],
        "first_seen": ISO, "last_seen": ISO, "fixed_at": None,
        "last_example": None,
    },
}

EVENTS = [
    # normalized содержит "<n>", сырой message — "30" (OQ-⑤: ищем по normalized)
    {"ts": ISO, "source": "docker_logs", "container": "mcp-knowledge-server",
     "level": "ERROR", "marker": "MCP", "actor_id": "1a2b3c4d",
     "message": "Timeout after 30 seconds",
     "normalized_message": "timeout after <n> seconds",
     "signature": "docker_logs|MCP|timeout after <n>"},
    {"ts": YESTERDAY, "source": "docker_logs", "container": "mcp-knowledge-server",
     "level": "ERROR", "marker": "MCP", "actor_id": None,
     "message": "Timeout after 31 seconds", 
     "normalized_message": "timeout after <n> seconds",
     "signature": "docker_logs|MCP|timeout after <n>"},
    {"ts": ISO, "source": "cron_log", "container": None,
     "level": None, "marker": "CRON", "actor_id": "cron:errors-collect",
     "message": "password=hunter2 backup ok",
     "normalized_message": "password=<secret> backup ok",
     "signature": "cron_log|CRON|backup finished"},
    {"ts": ISO, "source": "docker_logs", "container": "mcp-knowledge-server",
     "level": "INFO", "marker": "ERRORS_QUERY", "actor_id": "1a2b3c4d",
     "message": "[ERRORS_QUERY] view=aggregates results=1",
     "normalized_message": "[errors_query] view=aggregates results=<n>",
     "signature": "docker_logs|ERRORS_QUERY|audit"},
]


def _make_sink(root: Path, aggs: dict | None = None, events: list | None = None,
               with_aggs: bool = True) -> Path:
    sink = root / "sink"
    (sink / "events" / "raw").mkdir(parents=True)
    (sink / "aggregates").mkdir(parents=True)
    if with_aggs:
        (sink / "aggregates" / "signatures.json").write_text(
            json.dumps(aggs if aggs is not None else AGGS), encoding="utf-8")
    day = NOW.strftime("%Y-%m-%d")
    with open(sink / "events" / "raw" / f"{day}.jsonl", "w", encoding="utf-8") as f:
        for ev in events if events is not None else EVENTS:
            f.write(json.dumps(ev) + "\n")
    return sink


@pytest.fixture
def sink(tmp_path, monkeypatch):
    s = _make_sink(tmp_path)
    monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
    return s


def _sha_dir(path: Path) -> dict[str, str]:
    out = {}
    for f in sorted(path.rglob("*")):
        if f.is_file():
            out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


async def _call(**params) -> dict:
    return await errors_query(params, app_state=None)


# ── view / состав ответа ────────────────────────────────────

class TestViews:
    async def test_default_view_aggregates(self, sink):
        r = await _call()
        assert r["view"] == "aggregates"
        assert r["aggregates"] and r["examples"] == []
        assert r["meta"]["examples_returned"] == 0
        assert r["meta"]["raw_files_scanned"] == 0  # raw не сканируется

    async def test_view_examples(self, sink):
        r = await _call(view="examples")
        assert r["aggregates"] == [] and r["examples"]
        assert r["meta"]["raw_files_scanned"] >= 1

    async def test_view_both(self, sink):
        r = await _call(view="both")
        assert r["aggregates"] and r["examples"]

    async def test_aggregate_shape_no_last_example_no_actors(self, sink):
        r = await _call()
        agg = r["aggregates"][0]
        assert "last_example" not in agg and "actors" not in agg
        assert agg["actors_count"] == 2  # длина, не список (Critic ③/P2-2)
        assert set(agg) == {
            "signature", "priority", "class", "status", "count_7d",
            "count_prev_7d", "count_total", "trend", "actors_count",
            "sources", "first_seen", "last_seen", "fixed_at",
            # 008 storm-guard: аддитивные поля видимости suppressed/burst (§7.5)
            "suppressed_total", "suppressed_7d", "burst", "burst_ts",
            # 009 path-endpoint: топ-эндпоинты сигнатуры (§7.3-3, аддитивно)
            "endpoints"}

    async def test_endpoints_legacy_fixture_renders_empty(self, sink):
        # 009: фикстура AGGS — legacy-агрегаты без «endpoints» → {} (R6 .get)
        r = await _call()
        assert all(a["endpoints"] == {} for a in r["aggregates"])

    async def test_trend_semantics(self, sink):
        r = await _call(signature="docker_logs|MCP|timeout after <n>")
        assert r["aggregates"][0]["trend"] == "up"       # 12 > 3
        r = await _call(signature="cron_log|CRON|backup finished")
        assert r["aggregates"][0]["trend"] == "flat"     # 2 == 2
        r = await _call(signature="host|MCP|db gone")
        assert r["aggregates"][0]["trend"] == "down"     # 0 < 5
        r = await _call(signature="docker_logs|ERRORS_QUERY|audit", include_audit=True)
        assert r["aggregates"][0]["trend"] is None       # prev == 0


# ── Фильтры ─────────────────────────────────────────────────

class TestFilters:
    async def test_priority_filter(self, sink):
        r = await _call(priority=["P1"])
        assert [a["priority"] for a in r["aggregates"]] == ["P1"]

    async def test_source_filter(self, sink):
        r = await _call(source="cron_log")
        assert {a["signature"] for a in r["aggregates"]} == {"cron_log|CRON|backup finished"}

    async def test_signature_exact(self, sink):
        r = await _call(signature="host|MCP|db gone")
        assert len(r["aggregates"]) == 1 and r["aggregates"][0]["status"] == "resolved"

    async def test_query_normalized_not_raw(self, sink):
        # "<n>" есть в normalized, нет в сыром message — OQ-⑤
        r = await _call(view="both", query="after <n>")
        assert any("timeout" in a["signature"] for a in r["aggregates"])
        assert r["examples"]

    async def test_since_filter(self, sink):
        r = await _call(since=(NOW - timedelta(hours=1)).isoformat())
        sigs = {a["signature"] for a in r["aggregates"]}
        assert "host|MCP|db gone" not in sigs  # last_seen месяц назад
        assert "docker_logs|MCP|timeout after <n>" in sigs

    async def test_period_filter(self, sink):
        r = await _call(period="30d")
        # исключены: audit (P1-1г, дефолт) и host-сигнатура (last_seen 31d > окно 30d)
        assert {a["signature"] for a in r["aggregates"]} == set(AGGS) - {
            "docker_logs|ERRORS_QUERY|audit", "host|MCP|db gone"}

    async def test_since_and_period_rejected(self, sink):
        r = await _call(since=ISO, period="7d")
        assert r.get("error", "").startswith("invalid_params")

    async def test_audit_excluded_by_default(self, sink):
        r = await _call(view="both")
        assert all("ERRORS_QUERY" not in a["signature"] for a in r["aggregates"])
        assert all(e["marker"] != "ERRORS_QUERY" for e in r["examples"])

    async def test_include_audit_true(self, sink):
        r = await _call(include_audit=True)
        assert any(a["signature"] == "docker_logs|ERRORS_QUERY|audit"
                   for a in r["aggregates"])

    async def test_filters_applied_echoed(self, sink):
        r = await _call(priority=["P1"], source="docker_logs")
        assert r["filters_applied"]["priority"] == ["P1"]
        assert r["filters_applied"]["source"] == "docker_logs"


# ── Лимиты/капы (P2-3, P2-new-1) ────────────────────────────

class TestLimits:
    async def test_limit_truncation_flag(self, sink):
        r = await _call(limit=1)
        assert len(r["aggregates"]) == 1
        assert r["meta"]["signatures_truncated"] is True

    async def test_limit_caps_at_100(self, sink):
        r = await _call(limit=1000)  # schema cap 100 → не падает
        assert r["filters_applied"]["limit"] == 100

    async def test_examples_limit_cap_10(self, tmp_path, monkeypatch):
        sig = "docker_logs|MCP|flood"
        evs = [dict(EVENTS[0], message=f"flood {i}", normalized_message=f"flood <n> {i}",
                    signature=sig, ts=ISO) for i in range(15)]
        s = _make_sink(tmp_path, events=evs,
                       aggs={sig: {**AGGS["docker_logs|MCP|timeout after <n>"], "actors": []}})
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
        r = await _call(view="examples", signature=sig, examples_limit=99)
        assert r["filters_applied"]["examples_limit"] == 10  # schema cap
        assert len(r["examples"]) == 10  # 15 событий → не больше 10 на сигнатуру

    async def test_message_truncated_500(self, tmp_path, monkeypatch):
        sig = "docker_logs|MCP|long"
        long_ev = dict(EVENTS[0], message="z" * 900, normalized_message="long",
                       signature=sig)
        s = _make_sink(tmp_path, events=[long_ev],
                       aggs={sig: {**AGGS["docker_logs|MCP|timeout after <n>"], "actors": []}})
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
        r = await _call(view="examples", signature=sig)
        assert len(r["examples"]) == 1
        msg = r["examples"][0]["message"]
        assert len(msg) <= 500 + len("…[truncated]")
        assert msg.endswith("…[truncated]")

    async def test_raw_cap_truncated_flag(self, sink, monkeypatch):
        # имя модуля затенено одноимённой функцией в пакете → sys.modules
        import sys
        eq = sys.modules["mcp_server.tools.errors_query"]
        monkeypatch.setattr(eq, "RAW_SCAN_CAP_BYTES", 10)  # меньше файла
        r = await _call(view="examples")
        assert r["meta"]["raw_scan_truncated"] is True
        assert any("truncated by" in w for w in r["meta"]["warnings"])

    async def test_raw_cap_default_64mb(self):
        assert RAW_SCAN_CAP_BYTES == 64 * 1024 * 1024


# ── Маскирование на отдаче (P2-5) ───────────────────────────

class TestMasking:
    async def test_secrets_masked_in_examples(self, sink):
        r = await _call(view="examples", query="backup")
        assert r["examples"]
        msg = r["examples"][0]["message"]
        assert "hunter2" not in msg and "password=<secret>" in msg


# ── Структурная проверка / graceful (P1-2, P2-6) ────────────

class TestGraceful:
    async def test_empty_dir_not_available(self, tmp_path, monkeypatch):
        empty = tmp_path / "auto"  # Docker авто-создаёт bind-каталог
        empty.mkdir()
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(empty))
        r = await _call()
        assert r["sink_available"] is False
        assert r["error"] == "sink_unavailable"
        assert "структуры sink нет" in r["hint"]

    async def test_full_sink_no_data_hint(self, tmp_path, monkeypatch):
        s = _make_sink(tmp_path, aggs={}, events=[])
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
        r = await _call()
        assert r["sink_available"] is True
        assert r["aggregates"] == [] and r["examples"] == []
        assert "hint" in r["meta"]

    async def test_broken_aggregates_json_warning(self, tmp_path, monkeypatch):
        s = _make_sink(tmp_path)
        (s / "aggregates" / "signatures.json").write_text("{broken")
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
        r = await _call()
        assert r["sink_available"] is True
        assert r["aggregates"] == []
        assert any("broken JSON" in w for w in r["meta"]["warnings"])

    @pytest.mark.skipif(os.geteuid() == 0, reason="root игнорирует chmod 000")
    async def test_permission_denied(self, tmp_path, monkeypatch):
        s = _make_sink(tmp_path)
        os.chmod(s / "aggregates", 0o000)
        monkeypatch.setattr(settings, "ERRORS_SINK_DIR", str(s))
        try:
            r = await _call()
            assert r["sink_available"] is False
            assert r["error"] == "permission_denied"
        finally:
            os.chmod(s / "aggregates", 0o755)

    async def test_meta_sink_dir(self, sink):
        r = await _call()
        assert r["meta"]["sink_dir"] == str(sink)


# ── Audit-маркер (P1-1а: без значений) ──────────────────────

class TestAuditMarker:
    async def test_marker_format_no_query_text(self, sink, caplog):
        with caplog.at_level(logging.INFO, logger="mcp_knowledge.tools.errors_query"):
            await _call(view="both", query="secret-needle-42", priority=["P1"])
        line = next(r for r in caplog.messages if r.startswith("[ERRORS_QUERY]"))
        assert "secret-needle-42" not in line          # текст запроса НЕ логируется
        assert "q_len=16" in line                       # только длина
        assert "q_hash=" in line and "prio=P1" in line
        assert "view=both" in line and "key=" in line

    async def test_marker_q_hash_is_sha256_prefix(self, sink, caplog):
        with caplog.at_level(logging.INFO, logger="mcp_knowledge.tools.errors_query"):
            await _call(query="needle")
        line = next(r for r in caplog.messages if r.startswith("[ERRORS_QUERY]"))
        expect = hashlib.sha256(b"needle").hexdigest()[:8]
        assert f"q_hash={expect}" in line


# ── 0-мутаций sink (приёмка №3) ─────────────────────────────

class TestNoMutation:
    async def test_sink_not_mutated_by_calls(self, sink):
        before = _sha_dir(sink)
        for _ in range(10):
            await _call(view="both")
        assert _sha_dir(sink) == before


# ── actor_id (P2-2) ─────────────────────────────────────────

class TestActorId:
    async def test_actor_id_namespace_and_null(self, sink):
        r = await _call(view="examples")
        cron = [e for e in r["examples"] if e["actor_id"] == "cron:errors-collect"]
        assert cron, "cron:<job> namespace должен проходить как есть"
        assert any(e["actor_id"] == "1a2b3c4d" for e in r["examples"])
