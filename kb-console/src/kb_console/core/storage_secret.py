"""storage_secret для cookie-сессий kb-console (035, план §3б / P2-5).

Лестница разрешения секрета сессий:
  1. env CONSOLE_STORAGE_SECRET (приоритет, для инсталляций с vault);
  2. файл <dir(CONSOLE_USERS_FILE)>/storage_secret — volume ./data/console
     (docker-compose), переживает пересоздание контейнера, air-gap ок;
  3. ephemeral в памяти + WARNING (RO fs / нет volume): сессии живут до
     рестарта — деградация, не крах.

Права файла 0600, запись атомарная (tmp + os.replace, без symlink-follow).
Ротация (env-смена или удаление файла) = все сессии недействительны —
штатный ответ на инцидент (README kb-console).

Секрет НИКОГДА не логируется (значение — только факт генерации/отказа).
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile

logger = logging.getLogger("kb_console.storage_secret")

_SECRET_FILENAME = "storage_secret"
"""Имя файла рядом с users.jsonl (та же директория = смонтированный volume)."""

_MIN_SECRET_LEN = 32
"""Минимальная длина token_urlsafe(32) ≈ 43 символа; проверка env-значения."""


def _generate() -> str:
    return secrets.token_urlsafe(32)


def _read_file(path: str) -> str | None:
    try:
        value = open(path, encoding="utf-8").read().strip()  # noqa: SIM115
    except OSError:
        return None
    return value or None


def _write_file_atomic(path: str, value: str) -> bool:
    """Атомарная запись 0600 (tmp в той же директории + os.replace).

    Родительскую директорию НЕ создаём: смонтированный volume обязан
    существовать; отсутствие = деградация в ephemeral (не молча создаю
    пути вне volume). False → вызывающий код уходит в ephemeral.
    """
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        return False
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".storage_secret.")
    except OSError:
        return False
    try:
        try:
            os.write(fd, (value + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # атомарно, symlink не преследуем
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def resolve_storage_secret(
    env_value: str = "",
    *,
    base_path: str | None = None,
    users_file_env: str | None = None,
) -> str:
    """Разрешить секрет сессий: env → файл → ephemeral+WARNING.

    base_path — путь-якорь (CONSOLE_USERS_FILE): секрет кладётся рядом.
    users_file_env — fallback чтения env CONSOLE_USERS_FILE (для вызовов
    без явного base_path, напр. тесты). Секрет не логируется.
    """
    if env_value:
        if len(env_value) < _MIN_SECRET_LEN:
            logger.warning(
                "CONSOLE_STORAGE_SECRET короче %d символов — рекомендуется "
                "перегенерировать (слабый ключ подписи cookie)",
                _MIN_SECRET_LEN,
            )
        return env_value

    anchor = base_path or users_file_env or os.environ.get("CONSOLE_USERS_FILE", "")
    secret_file = (
        os.path.join(os.path.dirname(anchor), _SECRET_FILENAME) if anchor else ""
    )

    if secret_file:
        stored = _read_file(secret_file)
        if stored:
            return stored

    secret = _generate()
    if not secret_file or not _write_file_atomic(secret_file, secret):
        logger.warning(
            "storage_secret: файл %s недоступен для записи — секрет ephemeral "
            "(сессии не переживут рестарт; смонтируйте volume data/console)",
            secret_file or "<не определён>",
        )
    return secret
