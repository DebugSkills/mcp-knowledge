"""Общая настройка тестов kb-console.

Импорт _local_http выставляет NO_PROXY для localhost при коллекции тестов
(trace code-2026-09-25-021): без этого локальные smoke-проверки уходят на
внешний HTTP(S)_PROXY и падают при снятом NO_PROXY.
"""
from __future__ import annotations

import pytest
from _local_http import local_get  # side-effect: NO_PROXY для localhost

__all__ = ["local_get"]


@pytest.fixture
async def ui_user(caplog: pytest.LogCaptureFixture):
    """NiceGUI-харнесс БЕЗ main_file (app.py не исполняем: env-зависимости).

    user_simulation(root=None, main_file=None) — чистый client+lifecycle;
    страницы регистрируются в самом тесте через ui.page(...). Плюс guard на
    ERROR-логи NiceGUI (как в штатном user-plugin).
    """
    from nicegui.testing.user_simulation import user_simulation

    async with user_simulation() as user:
        yield user
        logs = [r for r in caplog.get_records("call") if r.levelname == "ERROR"]
        if logs:
            pytest.fail(f"unexpected ERROR logs: {[r.message for r in logs]}", pytrace=False)
