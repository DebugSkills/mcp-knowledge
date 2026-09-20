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

CONSOLE_HOST: str = os.environ.get("CONSOLE_HOST", "127.0.0.1")
"""Адрес, на котором слушает NiceGUI-консоль (по умолчанию 127.0.0.1 — loopback,
security-by-default; у консоли нет собственной аутентификации).

`0.0.0.0` — ТОЛЬКО для bridge-режима `docker run` на клиентских хостах:
docker-proxy ходит на IP контейнера, приложение на 127.0.0.1 внутри контейнера
через `-p` недоступно. При этом экспозицию держим на loopback хоста:
`docker run -e CONSOLE_HOST=0.0.0.0 -p 127.0.0.1:8085:8085 kb-console:prod`.
В host-сети compose (dev/prod) всегда 127.0.0.1; внешний доступ — ssh -L.
"""

REFRESH_SECONDS: int = int(os.environ.get("REFRESH_SECONDS", "10"))
"""Интервал автообновления страницы «Статус» в секундах."""

CONSOLE_PASSWORD: str = os.environ.get("CONSOLE_PASSWORD", "")
"""Пароль HTTP Basic auth консоли (по умолчанию пуст — auth выключен).

Пустой пароль + CONSOLE_AUTH=auto → auth off (при bind≠loopback — warning
в логах). Пароль НЕ логируется; username Basic игнорируется (один оператор).
Ротация = смена env + рестарт (идентично паттерну MCP-ключей).
⚠️ Basic без TLS = креды base64 в каждом запросе → сетевой доступ
только ssh -L или TLS-фасад (см. README «Доступ и авторизация»).
"""

CONSOLE_AUTH: str = os.environ.get("CONSOLE_AUTH", "auto")
"""Режим аутентификации: auto | off | required (по умолчанию auto).

- auto: пароль задан → auth ON; пусто + loopback → off тихо;
  пусто + bind≠127.0.0.1 → off + WARNING (не ломает bridge-паттерн
  `0.0.0.0` внутри контейнера + `-p 127.0.0.1:8085:8085`);
- off: явно выключено (warn подавлен); заданный пароль игнорируется;
- required: пустой пароль → RuntimeError на старте (fail-fast, прод).
Невалидное значение → ValueError со списком допустимых (на старте).
"""
