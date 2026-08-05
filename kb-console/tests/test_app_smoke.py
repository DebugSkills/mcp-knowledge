"""Smoke-тест kb-console приложения через подпроцесс.

Проверяет, что приложение стартует и GET / возвращает 200 (HTML).
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


def test_app_serves_home_page():
    """GET / должен вернуть 200 (NiceGUI отдаёт HTML)."""
    port = 9877  # Избегаем конфликта с другими тестами
    proc = _start_console(port)
    try:
        # Пробуем подключиться с retry (NiceGUI стартует ~1-3 сек)
        last_exc: Exception | None = None
        for _ in range(30):
            time.sleep(0.5)
            try:
                r = httpx.get(f"http://localhost:{port}/", timeout=3.0)
                assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
                assert "<html" in r.text.lower(), "Response should be HTML"
                return
            except httpx.HTTPError as e:
                last_exc = e
        raise RuntimeError(f"Server did not start: {last_exc}") from last_exc
    finally:
        _stop_console(proc)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
