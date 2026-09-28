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

kb-console-roles Ф2: непустой users-стор перекрывает пароль — auto и
required дают per-user auth ON; CONSOLE_PASSWORD игнорируется с warning.
"""

CONSOLE_USERS_FILE: str = os.environ.get("CONSOLE_USERS_FILE", "/app/data/console/users.jsonl")
"""Путь к users.jsonl (учётные записи консоли; default — docker-volume).

Volume `./data/console:/app/data/console` в compose (паттерн data/tokens):
файл переживает пересоздание контейнера. Секретов нет — только pbkdf2-хэши.
Ручные правки файла подхватываются через TTL (~5 мин) без рестарта.
"""

CONSOLE_ADMIN_USER: str = os.environ.get("CONSOLE_ADMIN_USER", "")
"""Bootstrap-админ (kb-console-roles Ф2): seed при отсутствии активного админа.

Идемпотентно: есть активный админ → env игнорируется. Пароль
CONSOLE_ADMIN_PASSWORD обязателен, иначе bootstrap пропускается.
"""

CONSOLE_ADMIN_PASSWORD: str = os.environ.get("CONSOLE_ADMIN_PASSWORD", "")
"""Пароль bootstrap-админа CONSOLE_ADMIN_USER (НЕ логируется).

Ротация: /users-страница (reset-password) — env-пароль больше не нужен.
"""

CONSOLE_STORAGE_SECRET: str = os.environ.get("CONSOLE_STORAGE_SECRET", "")
"""Секрет подписи cookie-сессий (035; пусто → файл/автогенерация).

Разрешение (core/storage_secret.py): env → файл `<dir(CONSOLE_USERS_FILE)>/
storage_secret` (0600, volume, air-gap) → ephemeral + WARNING. Ротация
(env/удаление файла) = logout-all всех сессий — штатный ответ на инцидент.
Значение НЕ логируется.
"""

CONSOLE_ADMIN_CONTACT: str = os.environ.get("CONSOLE_ADMIN_CONTACT", "")
"""Контакт администратора для заявки на доступ (Telegram-username, 035).

Пусто (air-gap default) → t.me-блок на /login скрыт; канал заявки —
копирование шаблона и mailto. Без @, напр. `oksigen_07`.
"""

CONSOLE_TRUST_XFF: bool = os.environ.get("CONSOLE_TRUST_XFF", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
"""Доверять X-Forwarded-For для ключа rate-limit /api/login (035, N2).

Default on: единственный вход — наш Caddy-фасад (ставит XFF, incoming
значения игнорирует по дефолту → спуфинг закрыт). Прямой доступ без
фасада (air-gap) → можно `0` (ключ = transport client host).
"""

# ── 035: заявка на доступ — SSOT-константы (план §4) ────────

ACCESS_REQUEST_EMAIL: str = os.environ.get(
    "CONSOLE_ACCESS_REQUEST_EMAIL", "oksigen_07@bk.ru"
)
"""Адрес администратора для mailto-канала заявки на доступ."""

ACCESS_REQUEST_SUBJECT: str = "Заявка на доступ kb-console"
"""Тема письма-заявки (mailto и t.me используют общий шаблон тела)."""

ACCESS_REQUEST_FIELDS: tuple[str, ...] = (
    "ФИО (фамилия, имя, отчество)",
    "Отдел",
    "Телефон для связи",
    "Перечень проводимых работ",
)
"""Обязательные поля заявки на доступ (требование оператора 2026-09-28).

SSOT: все каналы (textarea-шаблон, mailto body, t.me ?text=) строятся
из access_request_template(); тест-контракт сверяет каналы с этим
кортежем (рассинхрон = RED).
"""


def access_request_template() -> str:
    """Шаблон заявки на доступ — единый источник для всех каналов (035 §4)."""
    lines = [
        "Здравствуйте!",
        "Прошу предоставить доступ к консоли управления базой знаний (kb-console).",
        "",
    ]
    lines += [f"{field}: " for field in ACCESS_REQUEST_FIELDS]
    return "\n".join(lines)


# ── kb-console-roles Ф3.1: маппинг роль→MCP-ключ ────────────

MCP_API_KEY_ADMIN: str = os.environ.get("MCP_API_KEY_ADMIN", "")
"""Write-ключ для роли admin (env сервера; пусто → fallback MCP_API_KEY)."""

MCP_API_KEY_EDITOR: str = os.environ.get("MCP_API_KEY_EDITOR", "")
"""Editor-ключ (mcp_e*-префикс, Ф1) для роли editor; пусто → fallback."""

MCP_API_KEY_CONTRIBUTOR: str = os.environ.get("MCP_API_KEY_CONTRIBUTOR", "")
"""Import-ключ для роли contributor; пусто → fallback MCP_API_KEY."""


def api_key_for_role(
    role: str,
    *,
    base: str = MCP_API_KEY,
    admin: str = MCP_API_KEY_ADMIN,
    editor: str = MCP_API_KEY_EDITOR,
    contributor: str = MCP_API_KEY_CONTRIBUTOR,
) -> str:
    """Ключ MCP-сервера по роли пользователя консоли (Ф3.1).

    Fallback-цепочка: per-role env → MCP_API_KEY (legacy-инсталляции с
    одним ключом работают бит-в-бит — все роли получают его). Неизвестная
    роль → base (безопасный дефолт, не пустота). Ключи НЕ логировать.
    """
    per_role = {"admin": admin, "editor": editor, "contributor": contributor}
    return per_role.get(role) or base
