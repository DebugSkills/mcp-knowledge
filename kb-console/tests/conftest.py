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
    from nicegui.client import Client
    from nicegui.testing.user_simulation import user_simulation

    async with user_simulation() as user:
        yield user
        logs = [r for r in caplog.get_records("call") if r.levelname == "ERROR"]
        if logs:
            pytest.fail(f"unexpected ERROR logs: {[r.message for r in logs]}", pytrace=False)
    # Гигиена глобального состояния (arch-2026-10-09-calib-admin-ui Ф2):
    # харнесс не закрывает ВСЕ свои Client'ы → Client.instances остаётся
    # непустым → у nicegui ломается script-mode fallback (context.slot_stack
    # создаёт script-client только при ПУСТОМ instances) для ПОЗЖЕ
    # запускаемых mock-ui тестов, рисующих реальный ui вне страницы
    # (chat.build_chat → attach_upload). Инцидент порядка: файлы с ui_user-
    # тестами, сортирующиеся РАНЬШЕ test_chat_stream.py (до сих пор
    # маскировалось алфавитным порядком: requests > chat). Возвращаем
    # fallback-условие — suite становится order-independent.
    Client.instances.clear()
