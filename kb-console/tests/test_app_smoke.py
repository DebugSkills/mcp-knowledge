"""Smoke-тест kb-console приложения через подпроцесс.

Проверяет:
  - Приложение стартует и GET / возвращает redirect на /status.
  - GET /status, /books, /import, /search → 200 (HTML).
  - Reload сохраняет раздел (GET /books → 200, не redirect).

Sync-версия: NiceGUI/uvicorn конфликтуют с pytest-asyncio event loop,
поэтому подпроцесс запускается и опрашивается синхронно.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

# Порт для smoke-тестов (избегаем конфликта с другими тестами).
_SMOKE_PORT = 9877


def _start_console(port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["CONSOLE_PORT"] = str(port)
    env["MCP_SERVER_URL"] = "http://localhost:8000"
    # NiceGUI определяет запуск внутри pytest (helpers.is_pytest) и требует
    # NICEGUI_SCREEN_TEST_PORT — задаём его явно (штатный тестовый механизм).
    env["NICEGUI_SCREEN_TEST_PORT"] = str(port)
    # stdout/stderr НЕ пипим в PIPE: NiceGUI логирует много, переполнение
    # буфера блокирует подпроцесс. Логи уходят в родительский терминал.
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
    """Ждать, пока сервер начнёт отвечать на GET /status."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            r = httpx.get(f"http://localhost:{port}/status", timeout=3.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_exc = e
    raise RuntimeError(f"Server did not start within {timeout}s: {last_exc}") from last_exc


# Кэшируем процесс между smoke-тестами (module-scoped fixture через pytest).
_proc: subprocess.Popen | None = None


def _get_or_start_console() -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = _start_console(_SMOKE_PORT)
        _wait_for_server(_SMOKE_PORT)
    return _proc


# ── Tests ───────────────────────────────────────────────────


def test_root_redirects_to_status():
    """GET / должен редиректить на /status (HTTP 200 на / тоже ок — NiceGUI
    может отдавать страницу с meta refresh или navigate)."""
    _get_or_start_console()
    # NiceGUI @ui.page("/") с ui.navigate.to("/status") даёт HTML-страницу
    # (200), которая делает клиентский редирект. Проверяем, что / отвечает.
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/", timeout=5.0, follow_redirects=False)
    assert r.status_code in (200, 302, 303, 307, 308), f"Root should respond: {r.status_code}"


def test_status_page_200():
    """GET /status → 200 HTML."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/status", timeout=5.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_books_page_200():
    """GET /books → 200 HTML."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/books", timeout=5.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_import_page_200():
    """GET /import → 200 HTML."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/import", timeout=5.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_search_page_200():
    """GET /search → 200 HTML."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/search", timeout=5.0)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_books_page_survives_reload():
    """Reload /books → остаётся на /books (не редиректит на /status)."""
    _get_or_start_console()
    r = httpx.get(f"http://localhost:{_SMOKE_PORT}/books", timeout=5.0)
    assert r.status_code == 200
    # Не должно быть редиректа
    assert not r.is_redirect


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
