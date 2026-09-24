"""Юнит-тесты errors_notify.py — общий TG-sender (code-2026-09-24-016, В2-блок б).

Спека .boardData.md §7.2(б)/§7.6: чанки ≤4096 без разрыва строк · маскировка
<token>/<proxy> · ProxyHandler при непустом proxy (AC-proxy-1) · direct-urlopen
при пустом (AC-proxy-2, 0 вызовов ProxyHandler) · host-тег 🤖[mcp-errors@<host>]
ровно один раз в каждом чанке (AC-host-1/2, приоритет env > notify.host >
gethostname) · деградация без notify.json (AC-deg-1) · continue-on-fail чанков.

Герметичность (AC-std): каждый сетевой тест очищает env HTTPS_PROXY/HTTP_PROXY/
NO_PROXY (+lowercase) — реальные прокси окружения не влияют; все urllib-вызовы
через мок-opener/urlopen (0 реальных сетевых запросов).
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("errors_notify", ROOT / "scripts" / "errors_notify.py")
en = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(en)
_spec_r = importlib.util.spec_from_file_location("errors_report", ROOT / "scripts" / "errors_report.py")
er = importlib.util.module_from_spec(_spec_r)
_spec_r.loader.exec_module(er)

TOKEN = "1234567890:AAtest_TOKEN_value-xYz"
CHAT = "-1009999999"
PROXY = "http://proxy.local:3128"


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    """AC-std: прокси-env НЕ влияет ни на один тест (direct-path идёт чистым)."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sink(tmp_path):
    (tmp_path / "reports").mkdir()
    return tmp_path


def write_notify(sink, **over):
    notify = {"bot_token": TOKEN, "chat_id": CHAT}
    notify.update(over)
    (sink / "notify.json").write_text(json.dumps(notify), encoding="utf-8")
    return notify


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Мок opener'аProxyHandler-пути: пишет full_url каждого open()."""

    def __init__(self):
        self.opened = []

    def open(self, req, timeout=None):
        self.opened.append(req.full_url)
        return FakeResponse()


# ── чанкование (R2: без чанков тест красный) ──

class TestBuildChunks:
    def test_short_text_single_chunk_with_tag(self):
        chunks = en.build_chunks("hello\nworld", "🤖[mcp-errors@lup]")
        assert len(chunks) == 1
        assert chunks[0].startswith("🤖[mcp-errors@lup]\n")

    def test_long_text_splits_no_line_break(self):
        lines = [f"line-{i:04d} " + "x" * 80 for i in range(200)]  # ~17KB
        text = "\n".join(lines)
        chunks = en.build_chunks(text, "🤖[mcp-errors@lup]")
        assert len(chunks) >= 2
        for ch in chunks:
            assert len(ch) <= en.TG_CHUNK
            assert ch.startswith("🤖[mcp-errors@lup]\n")
        # ни одна строка не разорвана: каждая строка целиком в одном чанке
        joined = "\n".join(c.split("\n", 1)[1] for c in chunks)
        assert joined == text

    def test_every_chunk_prefix_tag_once(self):
        text = "\n".join(f"row{i} " + "y" * 100 for i in range(100))
        chunks = en.build_chunks(text, "🤖[mcp-errors@aikb]")
        for ch in chunks:
            assert ch.count("🤖[mcp-errors@aikb]") == 1


# ── host-резолюция (AC-host-1: env > notify.host > gethostname) ──

class TestResolveHost:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("MCP_ERRORS_HOST", "envhost")
        assert en.resolve_host({"host": "confhost"}) == "envhost"

    def test_notify_host_second(self, monkeypatch):
        monkeypatch.delenv("MCP_ERRORS_HOST", raising=False)
        assert en.resolve_host({"host": "confhost"}) == "confhost"

    def test_gethostname_fallback_short(self, monkeypatch):
        monkeypatch.delenv("MCP_ERRORS_HOST", raising=False)
        monkeypatch.setattr(en.socket, "gethostname", lambda: "full.name.example")
        assert en.resolve_host({}) == "full"

    def test_override_beats_all(self, monkeypatch):
        monkeypatch.setenv("MCP_ERRORS_HOST", "envhost")
        assert en.resolve_host({"host": "confhost"}, override="cliovr") == "cliovr"

    def test_host_tag_format(self):
        assert en.host_tag("lup") == "🤖[mcp-errors@lup]"


# ── прокси-слой (AC-proxy-1 / AC-proxy-2) ──

class TestProxyLayer:
    def test_proxy_used_opener_path(self, sink, monkeypatch):
        """AC-proxy-1: непустой proxy → ProxyHandler + opener.open (urlopen НЕ зовётся)."""
        write_notify(sink, proxy=PROXY)
        captured = {}
        opener = FakeOpener()

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return opener

        def fail_urlopen(*a, **kw):
            raise AssertionError("urlopen не должен вызываться при proxy")

        monkeypatch.setattr(en.urllib.request, "build_opener", fake_build_opener)
        monkeypatch.setattr(en.urllib.request, "urlopen", fail_urlopen)
        sent = en.send_telegram(sink, "прокси-тест")
        assert sent == 1
        ph = [h for h in captured["handlers"] if isinstance(h, en.urllib.request.ProxyHandler)]
        assert len(ph) == 1
        assert ph[0].proxies == {"https": PROXY, "http": PROXY}
        assert opener.opened == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]

    def test_empty_proxy_direct_urlopen(self, sink, monkeypatch):
        """AC-proxy-2: proxy отсутствует/пустой → urlopen, 0 вызовов ProxyHandler."""
        write_notify(sink, proxy="")

        def fail_build_opener(*a, **kw):
            raise AssertionError("ProxyHandler-opener не должен создаваться при пустом proxy")

        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "build_opener", fail_build_opener)
        monkeypatch.setattr(en.urllib.request, "urlopen", fake_urlopen)
        sent = en.send_telegram(sink, "direct-тест")
        assert sent == 1
        assert calls == [f"https://api.telegram.org/bot{TOKEN}/sendMessage"]

    def test_null_proxy_direct_urlopen(self, sink, monkeypatch):
        write_notify(sink, proxy=None)
        monkeypatch.setattr(
            en.urllib.request, "build_opener",
            lambda *a, **kw: pytest.fail("ProxyHandler при proxy=null"))
        monkeypatch.setattr(en.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse())
        assert en.send_telegram(sink, "ok") == 1


# ── маскировка секретов (R5) ──

class TestMasking:
    def _failing_net(self, monkeypatch, exc_text):
        def boom(req, timeout=None):
            raise OSError(exc_text)
        monkeypatch.setattr(en.urllib.request, "urlopen", boom)

    def test_token_masked_in_error(self, sink, monkeypatch, capsys):
        write_notify(sink)
        self._failing_net(monkeypatch, f"connect failed for {TOKEN}")
        en.send_telegram(sink, "текст")
        out = capsys.readouterr().out + (sink / "reports" / "tg-errors.log").read_text()
        assert TOKEN not in out
        assert "<token>" in out

    def test_proxy_masked_in_error_with_proxy_cfg(self, sink, monkeypatch, capsys):
        write_notify(sink, proxy=PROXY)
        opener = FakeOpener()
        opener.open = lambda req, timeout=None: (_ for _ in ()).throw(
            OSError(f"tunnel {PROXY} refused auth user:pass"))
        monkeypatch.setattr(en.urllib.request, "build_opener", lambda *h: opener)
        en.send_telegram(sink, "текст")
        out = capsys.readouterr().out + (sink / "reports" / "tg-errors.log").read_text()
        assert PROXY not in out
        assert "<proxy>" in out


# ── деградация (AC-deg-1) и continue-on-fail ──

class TestDegradation:
    def test_no_notify_skip_log_exit_zero(self, sink, capsys):
        """AC-deg-1: нет notify.json → 0 отправок, skip-строка в tg-errors.log, return 0."""
        sent = en.send_telegram(sink, "текст")
        assert sent == 0
        log = (sink / "reports" / "tg-errors.log").read_text()
        assert log.count("TG: skip (notify.json") == 1
        assert "TG: skip (notify.json" in capsys.readouterr().out

    def test_empty_notify_skip(self, sink):
        (sink / "notify.json").write_text("{}", encoding="utf-8")
        assert en.send_telegram(sink, "x") == 0
        assert "TG: skip" in (sink / "reports" / "tg-errors.log").read_text()

    def test_notify_ready_helper(self, sink):
        assert en.notify_ready(sink) is False
        write_notify(sink)
        assert en.notify_ready(sink) is True
        write_notify(sink, bot_token="")
        assert en.notify_ready(sink) is False

    def test_chunk_fail_continues(self, sink, monkeypatch, capsys):
        """Сбой чанка 1 → лог + продолжение; чанк 2 доставлен (sent=1)."""
        write_notify(sink)
        text = "\n".join(f"строка {i} " + "z" * 90 for i in range(120))
        state = {"n": 0}

        def flaky_urlopen(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError(f"net down for {TOKEN}")
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", flaky_urlopen)
        sent = en.send_telegram(sink, text)
        assert sent >= 1 and state["n"] >= 2
        log = (sink / "reports" / "tg-errors.log").read_text()
        assert "chunk 1/" in log
        out = capsys.readouterr().out
        assert TOKEN not in out and TOKEN not in log


# ── host-тег в отправке (AC-host-1 на уровне sender) ──

class TestSendHostTag:
    def _capture(self, monkeypatch):
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode())["text"])
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", fake_urlopen)
        return sent

    @pytest.mark.parametrize("host", ["lup", "aikb"])
    def test_tag_exactly_once_parameterized(self, sink, monkeypatch, host):
        """AC-host-1: @host ровно один раз, первая строка = тег; хосты различимы."""
        write_notify(sink, host=host)
        sent = self._capture(monkeypatch)
        en.send_telegram(sink, "тело сообщения")
        assert len(sent) == 1
        text = sent[0]
        assert text.count(f"@{host}") == 1
        assert text.startswith(f"🤖[mcp-errors@{host}]\n")

    def test_env_host_override_in_send(self, sink, monkeypatch):
        write_notify(sink, host="confhost")
        monkeypatch.setenv("MCP_ERRORS_HOST", "envhost")
        sent = self._capture(monkeypatch)
        en.send_telegram(sink, "x")
        assert sent[0].startswith("🤖[mcp-errors@envhost]\n")

    def test_multichunk_tag_in_both_parts(self, sink, monkeypatch):
        """AC-host-2: 2+ чанка → тег в КАЖДОЙ части."""
        write_notify(sink, host="lup")
        sent = self._capture(monkeypatch)
        text = "\n".join(f"line-{i} " + "a" * 90 for i in range(150))
        en.send_telegram(sink, text)
        assert len(sent) >= 2
        for part in sent:
            assert part.startswith("🤖[mcp-errors@lup]\n")
            assert part.count("🤖[mcp-errors@lup]") == 1

    def test_chat_override(self, sink, monkeypatch):
        write_notify(sink)
        chats = []

        def fake_urlopen(req, timeout=None):
            chats.append(json.loads(req.data.decode())["chat_id"])
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", fake_urlopen)
        en.send_telegram(sink, "x", chat_override="-100override")
        assert chats == ["-100override"]


# ── weekly-интеграция через shared-модуль (AC-weekly-1) ──

def _mk_agg(priority="P1", count=3, first="2026-09-20T10:00:00Z", last="2026-09-23T12:00:00Z"):
    return {"priority": priority, "class": "T", "count_total": count, "count_7d": count,
            "count_prev_7d": 0, "daily": {}, "first_seen": first, "last_seen": last,
            "status": "active", "fixed_at": None, "actors": [], "sources": ["docker_logs"],
            "last_example": {"message": "boom"}}


class TestWeeklyIntegration:
    def _run_weekly(self, sink, monkeypatch):
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data.decode())["text"])
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", fake_urlopen)
        rc = er.main(["--weekly", "--send-tg", "--sink", str(sink)])
        assert rc == 0
        return sent

    def test_weekly_six_sections_and_tag(self, tmp_path, monkeypatch):
        """AC-weekly-1: 6 секций в report-*.md; TG-сообщение с тегом ровно 1 раз."""
        write_notify(tmp_path, host="lup")
        (tmp_path / "aggregates").mkdir()
        (tmp_path / "aggregates" / "signatures.json").write_text(
            json.dumps({"docker_logs|500|api": _mk_agg("P0"),
                        "docker_logs|warn|x": _mk_agg("P2")}), encoding="utf-8")
        sent = self._run_weekly(tmp_path, monkeypatch)
        reports = list((tmp_path / "reports").glob("report-*.md"))
        assert len(reports) == 1
        body = reports[0].read_text()
        for i in range(1, 7):
            assert f"## {i}." in body  # 6 секций
        assert len(sent) >= 1
        for text in sent:
            assert text.startswith("🤖[mcp-errors@lup]\n")
            assert text.count("🤖[mcp-errors@lup]") == 1
            assert len(text) <= en.TG_CHUNK

    def test_weekly_two_chunks_tag_in_both(self, tmp_path, monkeypatch):
        """AC-host-2: раздутый weekly → 2 чанка, тег в ОБЕИХ частях."""
        write_notify(tmp_path, host="lup")
        long_sig = "docker_logs|" + "k" * 60
        aggs = {}
        for i in range(12):  # секция P0 (все активные P0)
            aggs[f"{long_sig}-p0-{i:02d}|tail"] = _mk_agg("P0")
        for i in range(12):  # секция топ-P1/P2 (actors)
            a = _mk_agg("P1")
            a["actors"] = [f"actor{i}", "b"]
            aggs[f"{long_sig}-p1-{i:02d}|tail"] = a
        for i in range(8):  # burst-подсекция (burst_ts свежий)
            a = _mk_agg("P1")
            a["burst_ts"] = "2026-09-23T12:00:00Z"
            a["burst_count_5m"] = 54
            aggs[f"{long_sig}-burst-{i:02d}|tail"] = a
        for i in range(10):  # секция 6 кандидаты (resolved + fixed_at)
            a = _mk_agg("P0")
            a["status"], a["fixed_at"] = "resolved", "2026-09-22T00:00:00Z"
            aggs[f"{long_sig}-res-{i:02d}|tail"] = a
        (tmp_path / "aggregates").mkdir()
        (tmp_path / "aggregates" / "signatures.json").write_text(json.dumps(aggs), encoding="utf-8")
        sent = self._run_weekly(tmp_path, monkeypatch)
        assert len(sent) >= 2
        for part in sent:
            assert part.startswith("🤖[mcp-errors@lup]\n")

    def test_weekly_degradation_no_notify(self, tmp_path, monkeypatch, capsys):
        """AC-weekly-1/AC-deg-1: без notify.json weekly всё равно exit 0 + skip-лог."""
        (tmp_path / "aggregates").mkdir()
        (tmp_path / "aggregates" / "signatures.json").write_text(
            json.dumps({"a|b|c": _mk_agg()}), encoding="utf-8")
        sent = self._run_weekly(tmp_path, monkeypatch)
        assert sent == []
        assert "TG: skip" in (tmp_path / "reports" / "tg-errors.log").read_text()
