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
from _local_http import local_get  # 021: без env-прокси

_SMOKE_PORT = 9878
_PASSWORD = "test"


def _start_console(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    # P1-4: чистим auth-env до установки своих значений.
    env.pop("CONSOLE_PASSWORD", None)
    env.pop("CONSOLE_AUTH", None)
    # kb-console-roles Ф2: bootstrap-админ/stor из окружения не должны
    # включать per-user ветку (иначе legacy-пароль test будет отклонён).
    env.pop("CONSOLE_USERS_FILE", None)
    env.pop("CONSOLE_ADMIN_USER", None)
    env.pop("CONSOLE_ADMIN_PASSWORD", None)
    # kb-console-roles Ф3.1: per-role ключи (P1-4).
    env.pop("MCP_API_KEY_ADMIN", None)
    env.pop("MCP_API_KEY_EDITOR", None)
    env.pop("MCP_API_KEY_CONTRIBUTOR", None)
    # 036: /app-дефолт БД заявок не существует вне docker → локальный tmp
    env["CONSOLE_ACCESS_REQUESTS_DB"] = "/tmp/kilo/035-users-smoke/access_requests.db"
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
            local_get(f"http://localhost:{port}/status", timeout=3.0)
            return
        except httpx.HTTPError as e:
            last_exc = e
    raise RuntimeError(f"Server did not start within {timeout}s: {last_exc}") from last_exc


_proc: subprocess.Popen | None = None


@pytest.fixture(scope="module", autouse=True)
def _teardown_console():
    """Модульный teardown: убить smoke-сервер (утечка = stale-порт →
    false-green/false-red в следующем прогоне)."""
    yield
    global _proc
    if _proc is not None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            _proc.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        _proc = None


def _get_or_start_console() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        # Stale-сервер на порту (false-green: отвечает СТАРЫЙ код) — fail fast.
        try:
            local_get(f"http://localhost:{_SMOKE_PORT}/status", timeout=1.0)
            raise RuntimeError(
                f"порт {_SMOKE_PORT} занят stale-сервером — прибейте его "
                "(pkill -f kb_console.app) перед прогоном smoke"
            )
        except httpx.HTTPError:
            pass  # порт свободен — штатно поднимаем свой сервер
        _proc = _start_console(_SMOKE_PORT)
        _wait_for_server(_SMOKE_PORT)
    return _proc


# ── Tests ───────────────────────────────────────────────────


def test_status_without_credentials_302_to_login():
    """GET /status без кредов → v3 (035): 302 + Location /login (Basic-челленджа нет)."""
    _get_or_start_console()
    r = local_get(f"http://localhost:{_SMOKE_PORT}/status", timeout=5.0)
    assert r.status_code == 302
    assert r.headers.get("location", "").startswith("/login")
    assert r.headers.get("www-authenticate") is None


def test_status_with_credentials_200():
    """GET /status с верными Basic-кредами → 200 HTML (back-compat v3)."""
    _get_or_start_console()
    r = local_get(
        f"http://localhost:{_SMOKE_PORT}/status",
        auth=("", _PASSWORD),
        timeout=5.0,
    )
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_nicegui_static_without_credentials_302():
    """Статика /_nicegui/* без кредов → 302 (JS-каркас за auth)."""
    _get_or_start_console()
    r = local_get(f"http://localhost:{_SMOKE_PORT}/_nicegui/nicegui.js", timeout=5.0)
    assert r.status_code == 302
    assert r.headers.get("location", "").startswith("/login")


def test_socketio_polling_without_credentials_401():
    """socket.io-polling /_nicegui_ws/* без кредов → 401 JSON (обход через
    polling закрыт; API-класс — не 302, P2-2)."""
    _get_or_start_console()
    r = local_get(
        f"http://localhost:{_SMOKE_PORT}/_nicegui_ws/socket.io/"
        "?EIO=4&transport=polling",
        timeout=5.0,
    )
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") is None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
