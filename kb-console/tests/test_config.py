"""Юнит-тесты конфигурации kb-console — CONSOLE_HOST (audit P1 У-1, code-2026-09-20-001).

CONSOLE_HOST по умолчанию 127.0.0.1 (loopback, security-by-default);
переопределение через env читается на импорте модуля.

Проверки через подпроцесс (как test_app_smoke): config.py читает env
на импорте → изоляция без importlib.reload и утечки состояния между тестами.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _config_value(var: str, env: dict[str, str] | None = None) -> str:
    """Получить значение переменной модуля kb_console.config в чистом env."""
    code = f"from kb_console import config; print(getattr(config, '{var}'))"
    run_env = os.environ.copy()
    run_env.pop("CONSOLE_HOST", None)
    if env:
        run_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=run_env,
    )
    return result.stdout.strip()


def test_console_host_default_loopback():
    """Без env CONSOLE_HOST консоль слушает loopback (security-by-default)."""
    assert _config_value("CONSOLE_HOST") == "127.0.0.1"


def test_console_host_env_override():
    """CONSOLE_HOST из env читается (0.0.0.0 — bridge docker run на клиентских хостах)."""
    assert _config_value("CONSOLE_HOST", env={"CONSOLE_HOST": "0.0.0.0"}) == "0.0.0.0"


def test_console_host_custom_bind():
    """Произвольный адрес привязки тоже читается из env."""
    assert _config_value("CONSOLE_HOST", env={"CONSOLE_HOST": "192.168.1.10"}) == "192.168.1.10"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
