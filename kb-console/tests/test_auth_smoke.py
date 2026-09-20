"""Smoke-тест HTTP Basic auth kb-console через подпроцесс с паролем.

code-2026-09-20-002, Ф2.1 (порт 9878 — отдельный от test_app_smoke).
Проверяет Е2E-поведение с CONSOLE_PASSWORD=test:
  - /status без кредов → 401 (+ WWW-Authenticate);
  - /status с кредами → 200;
  - статика /_nicegui/* и socket.io-polling /_nicegui_ws/* без кредов → 401
    (skip-путей нет: JS-каркас не должен отдаваться без аутентификации).

Sync-версия: как test_app_smoke (NiceGUI/uvicorn vs pytest-asyncio).
Env-leak (P1-4): pop CONSOLE_PASSWORD/CONSOLE_AUTH из копии os.environ
ДО установки своих значений (прецедент test_config.py).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

_SMOKE_PORT = 9878
_PASSWORD = "test"


def _start_console(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    # P1-4: чистим auth-env до установки своих значений.
    env.pop("CONSOLE_PASSWORD", None)
    env.pop("CONSOLE_AUTH", None)
    env["CONSOLE_PORT"] = str(port)
    env["CONSOLE_HOST"] = "127.0.0.1"
    env["MCP_SERVER_URL"] = "http://localhost:8000"
    env["CONSOLE_PASSWORD"] = _PASSWORD
    env["NICEGUI_SCREEN_TEST_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb_console.app"],
        env=env,
        start_new_session=True,
    )
    return proc


def _stop_console(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


def _wait_for_server(port: int, timeout: float = 15.0) -> None:
    """Ждать любой HTTP-ответ (401 тоже: сервер с auth на /status жив)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            httpx.get(f"http://localhost:{port}/status", timeout=3.0)
            return
        except httpx.HTTPError as e:
            last_exc = e
    raise RuntimeError(f"Server did not start within {timeout}s: {last_exc}") from last_exc


_proc: subprocess.Popen | None = None


def _get_or_start_console() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = _start_console(_SMOKE_PORT)
        _wait_for_server(_SMOKE_PORT)
    return _proc


# ── Tests ───────────────────────────────────────────────────


def test_status_without_credentials_401():
    """GET /status без кредов → 401 + Basic-челлендж."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/status", timeout=5.0)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == 'Basic realm="kb-console"'


def test_status_with_credentials_200():
    """GET /status с верными кредами → 200 HTML."""
    _get_or_start_console()
    r = httpx.get(
        f"http://localhost:{_SMOKE_PORT}/status",
        auth=("", _PASSWORD),
        timeout=5.0,
    )
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_nicegui_static_without_credentials_401():
    """Статика /_nicegui/* без кредов → 401 (JS-каркас за auth)."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/_nicegui/nicegui.js", timeout=5.0)
    assert r.status_code == 401


def test_socketio_polling_without_credentials_401():
    """socket.io-polling /_nicegui_ws/* без кредов → 401 (обход через polling закрыт)."""
    _get_or_start_console()
    r = httpx.get(
        f"http://localhost:{_SMOKE_PORT}/_nicegui_ws/socket.io/"
        "?EIO=4&transport=polling",
        timeout=5.0,
    )
    assert r.status_code == 401


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
