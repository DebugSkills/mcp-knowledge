"""Конфигурация kb-console через переменные окружения.

Простой подход через os.environ (без pydantic-settings).
"""

from __future__ import annotations

import os

MCP_SERVER_URL: str = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
"""URL MCP Knowledge Server (по умолчанию http://localhost:8000)."""

MCP_API_KEY: str = os.environ.get("MCP_API_KEY", "")
"""API-ключ для доступа к MCP Knowledge Server.
Если пустой — заголовок X-API-Key не отправляется (auth отключена).
"""

CONSOLE_PORT: int = int(os.environ.get("CONSOLE_PORT", "8085"))
"""Порт, на котором работает NiceGUI-консоль (по умолчанию 8085)."""

REFRESH_SECONDS: int = int(os.environ.get("REFRESH_SECONDS", "10"))
"""Интервал автообновления страницы «Статус» в секундах."""
