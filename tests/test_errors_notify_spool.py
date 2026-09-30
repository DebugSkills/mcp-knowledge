"""Тесты durable-доставки TG в errors_notify.py (code-2026-09-30-039, scope C).

Спека: in-run ретраи (retry_attempts/retry_backoff_sec) · durable spool
(reports/tg-pending/, 0600, без токена/прокси-кредов) · auto-flush (tg_flush_max)
· лимит tg_pending_max (прунинг старых) · notify_ready()==False → skip без spool ·
CLI --flush (rc/counter-контракт).

Герметичность: 0 реальной сети (urlopen/build_opener мокаются), backoff-сон
заглушён (autouse no_sleep), прокси-env очищен (autouse clean_env).
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "errors_notify", ROOT / "scripts" / "errors_notify.py")
en = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(en)

TOKEN = "1234567890:AAtest_TOKEN_value-xYz"
CHAT = "-1009999999"
PROXY = "http://proxy.local:3128"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "no_proxy", "MCP_ERRORS_HOST"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Ретраи никогда не спят реально (детерминизм + скорость)."""
    monkeypatch.setattr(en.time, "sleep", lambda s: None)


@pytest.fixture
def sink(tmp_path):
    return tmp_path


def write_notify(sink, **over):
    notify = {"bot_token": TOKEN, "chat_id": CHAT}
    notify.update(over)
    (sink / "notify.json").write_text(json.dumps(notify), encoding="utf-8")
    return notify


def pending(sink):
    d = Path(sink) / "reports" / "tg-pending"
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FailingOpener:
    def __init__(self, msg):
        self.msg = msg

    def open(self, req, timeout=None):
        raise OSError(self.msg)


def _boom(msg="connect failed"):
    def _raise(req, timeout=None):
        raise OSError(msg)
    return _raise


# ── (a)+(c)+(d): финальный фейл → spool; без кредов; last_error замаскирован ──

class TestSpool:
    def test_fail_spools_file(self, sink, monkeypatch):
        write_notify(sink)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        sent = en.send_telegram(sink, "текст-чанка")
        files = pending(sink)
        assert sent == 0
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["text"].startswith("🤖[mcp-errors@")
        assert "текст-чанка" in payload["text"]
        assert payload["chat_id"] == CHAT
        assert payload["attempts"] == en.DEFAULT_RETRY_ATTEMPTS  # 3 попытки исчерпаны

    def test_spool_mode_0600(self, sink, monkeypatch):
        write_notify(sink)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        en.send_telegram(sink, "x")
        files = pending(sink)
        assert len(files) == 1
        assert oct(files[0].stat().st_mode & 0o777)[2:] == "600"

    def test_spool_no_credentials(self, sink, monkeypatch):
        write_notify(sink, proxy=PROXY)
        monkeypatch.setattr(
            en.urllib.request, "build_opener",
            lambda *h: FailingOpener(f"tunnel {PROXY} refused"))
        en.send_telegram(sink, "текст")
        files = pending(sink)
        assert len(files) == 1
        raw = files[0].read_text()
        assert TOKEN not in raw
        assert PROXY not in raw

    def test_last_error_masked(self, sink, monkeypatch):
        write_notify(sink, proxy=PROXY)
        monkeypatch.setattr(
            en.urllib.request, "build_opener",
            lambda *h: FailingOpener(f"auth failed {TOKEN} via {PROXY}"))
        en.send_telegram(sink, "текст")
        payload = json.loads(pending(sink)[0].read_text())
        assert TOKEN not in payload["last_error"]
        assert PROXY not in payload["last_error"]
        assert "<token>" in payload["last_error"]
        assert "<proxy>" in payload["last_error"]


# ── (b): auto-flush доставляет pending от старых к новым ──

class TestAutoFlush:
    def test_flush_delivers_pending_then_removes(self, sink, monkeypatch):
        write_notify(sink)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom("down"))
        en.send_telegram(sink, "первый чанк")
        assert len(pending(sink)) == 1

        sent_texts = []

        def ok_urlopen(req, timeout=None):
            sent_texts.append(json.loads(req.data.decode())["text"])
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", ok_urlopen)
        sent = en.send_telegram(sink, "второй чанк")
        assert len(pending(sink)) == 0
        assert sent == 2  # 1 pending-флаш + 1 текущий
        assert len(sent_texts) == 2
        assert "первый чанк" in sent_texts[0]  # pending ПЕРВЫМ (старые → новые)
        assert "второй чанк" in sent_texts[1]

    def test_flush_max_caps_per_call(self, sink, monkeypatch):
        write_notify(sink, tg_flush_max=1)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        for i in range(3):
            en.send_telegram(sink, f"чанк {i}")
        assert len(pending(sink)) == 3
        # рабочий транспорт, но tg_flush_max=1 → доставится только 1 за вызов
        monkeypatch.setattr(en.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse())
        en.send_telegram(sink, "ещё чанк")
        assert len(pending(sink)) == 2  # 3 старых + 1 новый успешен; флашнёт 1 из 3


# ── (e): лимит tg_pending_max → прунинг самых старых ──

class TestPendingMax:
    def test_prunes_oldest_and_logs(self, sink, monkeypatch):
        write_notify(sink, tg_pending_max=2)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        for i in range(4):
            en.send_telegram(sink, f"чанк {i}")
        files = pending(sink)
        assert len(files) == 2
        joined = "\n".join(json.loads(f.read_text())["text"] for f in files)
        assert "чанк 2" in joined and "чанк 3" in joined  # два самых новых остались
        assert "чанк 0" not in joined and "чанк 1" not in joined
        log = (sink / "reports" / "tg-errors.log").read_text()
        assert "tg-pending overflow" in log


# ── (f): notify_ready()==False → skip, spool не создаётся ──

class TestNotReady:
    def test_no_notify_skip_no_spool(self, sink, monkeypatch):
        monkeypatch.setattr(en.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse())
        assert en.send_telegram(sink, "текст") == 0
        assert not (Path(sink) / "reports" / "tg-pending").exists()
        assert "TG: skip" in (sink / "reports" / "tg-errors.log").read_text()

    def test_empty_notify_skip_no_spool(self, sink):
        (sink / "notify.json").write_text("{}", encoding="utf-8")
        assert en.send_telegram(sink, "x") == 0
        assert not (Path(sink) / "reports" / "tg-pending").exists()


# ── (g): ретраи транспортных/серверных ошибок ──

class TestRetry:
    @pytest.mark.parametrize("code", [500, 429])
    def test_server_error_retried_then_sent(self, sink, monkeypatch, code):
        write_notify(sink)
        state = {"n": 0}

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise en.urllib.error.HTTPError(
                    "https://api.telegram.org/botX/sendMessage", code, "Err", {}, None)
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", flaky)
        sent = en.send_telegram(sink, "текст")
        assert sent == 1
        assert state["n"] == 2  # серверная ошибка → ретрай → 200
        assert len(pending(sink)) == 0

    @pytest.mark.parametrize("code", [400, 403])
    def test_client_4xx_not_retried(self, sink, monkeypatch, code):
        write_notify(sink)
        state = {"n": 0}

        def always_4xx(req, timeout=None):
            state["n"] += 1
            raise en.urllib.error.HTTPError(
                "https://api.telegram.org/botX/sendMessage", code, "Err", {}, None)

        monkeypatch.setattr(en.urllib.request, "urlopen", always_4xx)
        en.send_telegram(sink, "текст")
        assert state["n"] == 1  # 4xx (кроме 429) не ретраится
        assert len(pending(sink)) == 1  # заспулено после первого фейла

    def test_transport_error_retried(self, sink, monkeypatch):
        write_notify(sink)
        state = {"n": 0}

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] < 3:
                raise OSError("connect failed")
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", flaky)
        sent = en.send_telegram(sink, "текст")
        assert sent == 1
        assert state["n"] == 3  # 2 транспортных фейла → ретрай → 3-я попытка 200
        assert len(pending(sink)) == 0


# ── CLI --flush (rc + счётчики) ──

class TestFlushCli:
    def test_flush_no_pending_rc0(self, sink, capsys):
        write_notify(sink)
        assert en.main(["--flush", "--sink", str(sink)]) == 0
        assert "доставлено 0" in capsys.readouterr().out

    def test_flush_json_valid_counters(self, sink, capsys):
        write_notify(sink)
        assert en.main(["--flush", "--json", "--sink", str(sink)]) == 0
        data = json.loads(capsys.readouterr().out)
        assert {"sent", "spooled", "pending", "flushed", "failed", "skipped"} <= set(data)
        assert data["flushed"] == 0 and data["pending"] == 0
        assert data["skipped"] is False

    def test_flush_failed_counts_only_attempted(self, sink, monkeypatch, capsys):
        """P3-1: failed считает только реально проваленные попытки (не все 3)."""
        write_notify(sink, tg_flush_max=1)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        for i in range(3):
            en.send_telegram(sink, f"чанк {i}")
        assert len(pending(sink)) == 3
        capsys.readouterr()  # сброс stdout от send_telegram
        rc = en.main(["--flush", "--json", "--sink", str(sink)])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["failed"] == 1  # попытался 1 (tg_flush_max=1), не 3
        assert data["flushed"] == 0
        assert data["pending"] == 3

    def test_flush_delivers_pending(self, sink, monkeypatch):
        write_notify(sink)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        en.send_telegram(sink, "спулено")
        assert len(pending(sink)) == 1
        monkeypatch.setattr(en.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse())
        assert en.main(["--flush", "--sink", str(sink)]) == 0
        assert len(pending(sink)) == 0

    def test_flush_no_notify_rc0(self, sink, capsys):
        assert en.main(["--flush", "--sink", str(sink)]) == 0
        assert "доставлено 0" in capsys.readouterr().out


# ── P1-1: взаимное исключение flush (flock) — дублей нет, воскрешения нет ──

class TestFlushLock:
    def test_lock_busy_skip_no_duplicate(self, sink, monkeypatch):
        """Занятый лок → второй вызов skip: 0 сетевых вызовов, 0 дублей."""
        write_notify(sink)
        calls = []

        def rec(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse()

        monkeypatch.setattr(en.urllib.request, "urlopen", rec)
        with en._flush_lock(sink) as held:
            assert held is True
            sent = en.send_telegram(sink, "текст")
            assert sent == 0
            assert calls == []  # транспорт не вызывался — дубликатов 0

    def test_lock_busy_no_resurrection(self, sink, monkeypatch):
        """Параллельный flush с занятым локом НЕ write-back'ит доставленный файл."""
        write_notify(sink)
        monkeypatch.setattr(en.urllib.request, "urlopen", _boom())
        en.send_telegram(sink, "спулено")
        files = pending(sink)
        assert len(files) == 1
        before = json.loads(files[0].read_text())
        with en._flush_lock(sink) as held:
            assert held is True
            # рабочий транспорт, но лок занят → flush не выполняется вовсе
            monkeypatch.setattr(en.urllib.request, "urlopen",
                                lambda req, timeout=None: FakeResponse())
            assert en.send_telegram(sink, "второй чанк") == 0
            files = pending(sink)
            assert len(files) == 1  # не доставлен, не воскрешён
            after = json.loads(files[0].read_text())
            assert after["attempts"] == before["attempts"]  # write-back не произошёл

    def test_flush_cli_lock_busy_skipped(self, sink, monkeypatch, capsys):
        write_notify(sink)
        with en._flush_lock(sink) as held:
            assert held is True
            assert en.main(["--flush", "--json", "--sink", str(sink)]) == 0
            data = json.loads(capsys.readouterr().out)
            assert data["skipped"] is True
            assert data["flushed"] == 0 and data["failed"] == 0


# ── P2-4: битый spool → карантин + лог ──

class TestCorrupt:
    def test_corrupt_spool_quarantined(self, sink, monkeypatch):
        write_notify(sink)
        d = sink / "reports" / "tg-pending"
        d.mkdir(parents=True, exist_ok=True)
        bad = d / "20260101T000000.000000Z-1-1.json"
        bad.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(en.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResponse())
        en.send_telegram(sink, "текст")  # триггерит flush → карантин
        assert len(pending(sink)) == 0  # битый не занимает слот
        assert (d / ".corrupt" / bad.name).exists()
        assert "corrupt spool quarantined" in (sink / "reports" / "tg-errors.log").read_text()
