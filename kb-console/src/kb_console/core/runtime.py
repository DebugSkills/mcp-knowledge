"""Глобальные рантайм-объекты kb-console (без side-effects при импорте).

Ф3.2: identity-хелперы нуждаются в USERS_STORE, но НЕ могут импортировать
app.py (module-level ui.run → nicegui требует NICEGUI_SCREEN_TEST_PORT в
pytest-контексте — прецедент KeyError в test_pages/test_roles_ui). app.py
кладёт сюда стор при старте; юнит-контекст (app не импортировался) видит
None → legacy-режим (бит-ин-бит 002).
"""

from __future__ import annotations

from typing import Any

USERS_STORE: Any = None
"""UserStore, созданный app.py при старте (None в unit/CLI-контексте)."""
