"""Серверный (доверенный) канал «вложение чата верстака → KB» (Ф2 #6a).

Трасса: arch-2026-10-05-ai-workspace (plans/arch-2026-10-05-ai-workspace-plan.md,
строка #6). Часть 6a — ТОЛЬКО серверный модуль + тесты; UI-обвязка (pages/chat.py) — 6b.

Зачем отдельный канал: клиент (UI) НЕ может задать цель/зону импорта. Все
target-поля (collection_id, replace_collection_id, reimport_in_place, zone)
физически отсутствуют в сигнатуре attach_to_kb, а params для MCP строятся
СТРОГО allowlist-функцией build_import_params. Зона ``private`` инжектится
СЕРВЕРОМ (роль → ws_zone.zone_for_role), не вызывающим.

Гейты (строгий порядок, ВСЕ до любого MCP-вызова):

1. РОЛЬ (403): ``zone_for_role(role) == PRIVATE_ZONE`` — admin-only v1 (D7).
   У contributor/editor зона public: импорт в private был бы эскалацией.
   Сервисный импорт-ключ (env ``WS_MCP_IMPORT_KEY``) — НЕ обход этого гейта:
   гейт стоит ДО использования mcp_client (а клиент в 6b создаётся только
   после прохождения роли) — contributor не эскалирует, даже имея доступ
   к ключу через окружение.
2. ТИП (415): расширение filename ∈ ALLOWED_EXTS. v1 — только текст
   (.md/.txt); PDF-канал (multipart upload/convert) — вне этой части.
3. РАЗМЕР/ПУСТОТА (413/400): len(raw) > MAX_FILE_SIZE → 413; пустой или
   whitespace-only файл → 400.
4. ДЕКОД (400): utils._read_uploaded_file → (None, error) → отказ.

Отказы — ТОЛЬКО типизированные AttachError(code, message); silent-return
нет. Ответ MCP (реальный контракт import_content: dict с collection_id/
imported/failed/partial_success) проверяется на 'error' и
partial_success+failed>0 — отказы НЕ проглатываются: серверный отказ зоны
→ 403, прочее → 400. Сырые исключения клиента пробрасываются вызывающему.
"""

from __future__ import annotations

import os
from os.path import splitext
from typing import Any

from .mcp_client import MCPClient
from .utils import MAX_FILE_SIZE, _read_uploaded_file, _sanitize_title
from .ws_zone import PRIVATE_ZONE, zone_for_role

IMPORT_TOOL_NAME = "import_content"

ALLOWED_EXTS = frozenset({".md", ".txt"})
"""v1 — текстовые вложения; PDF и прочие бинарники — вне 6a (415)."""

DEFAULT_MCP_TIMEOUT_S = 1800.0
"""Импорт больших книг идёт минутами (batch-запись + git) — per-call timeout."""


class AttachError(Exception):
    """Типизированный отказ канала вложений.

    code — HTTP-класс отказа: 403 (роль/серверный отказ зоны), 415 (тип),
    413 (размер), 400 (контент/прочий серверный отказ); message —
    безопасный для UI текст (без ключей и внутренних деталей).
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_import_params(
    filename: str, text: str, *, domain: str, subject: str
) -> dict[str, Any]:
    """ЧИСТАЯ функция: params для MCP ``import_content`` — СТРОГО allowlist.

    Возвращает ровно ключи: content, content_type='book', domain, subject,
    title=_sanitize_title(filename), zone='private', wait_for_index=False.

    НИКОГДА не содержит replace_collection_id / collection_id /
    reimport_in_place и вообще ни одного target/zone-поля вызывающего:
    ``zone`` — серверная инъекция (PRIVATE_ZONE), а не параметр.
    """
    return {
        "content": text,
        "content_type": "book",
        "domain": domain,
        "subject": subject,
        "title": _sanitize_title(filename),
        "zone": PRIVATE_ZONE,  # серверная инъекция, НЕ выбор вызывающего
        "wait_for_index": False,
    }


def default_import_client() -> MCPClient:
    """Прод-дефолт (ленивый): сервисный импорт-ключ верстака из env.

    ``WS_MCP_IMPORT_KEY`` — ключ уровня ``import`` (не read); ``WS_MCP_URL`` —
    адрес MCP-сервера (паттерн env — как core/tool_loop._default_mcp_client).
    Клиент создаётся вызывающим (6b) ТОЛЬКО после прохождения гейта роли —
    attach_to_kb сам клиентов не создаёт (тестируемость через инжект).
    """
    return MCPClient(
        base_url=os.environ.get("WS_MCP_URL", "http://localhost:8000"),
        api_key=os.environ.get("WS_MCP_IMPORT_KEY", ""),
    )


def _is_zone_denial(message: str) -> bool:
    """Серверный отказ зоны (zone-scope ключа / import-уровня) → класс 403."""
    return "zone" in message.lower()


async def attach_to_kb(
    *,
    filename: str,
    raw: bytes,
    role: str | None,
    mcp_client: MCPClient,
    domain: str = "attachments",
    subject: str = "uploads",
    timeout: float = DEFAULT_MCP_TIMEOUT_S,
) -> dict[str, Any]:
    """Импортировать вложение чата в KB private (серверный канал, admin-only).

    Гейты — СТРОГО до любого MCP-вызова (см. docstring модуля): роль → 403,
    тип → 415, размер/пустота → 413/400, декод → 400. Успех → sparse-dict
    ``{"collection_id", "imported", "failed", "zone": "private"}`` (зона —
    серверная, возвращается для отображения, не выбирается клиентом).

    Raises:
        AttachError: любой отказ канала (code 400/403/413/415); тексты
            безопасны для показа в UI.
        Exception: сырые исключения mcp_client (транспорт/таймаут) —
            пробрасываются, НЕ глотаются и НЕ маскируются под успех.
    """
    # Гейт 1 — роль: ДО любого MCP-вызова (сервисный ключ не обход).
    if zone_for_role(role) != PRIVATE_ZONE:
        raise AttachError(
            403,
            "Загрузка вложений в базу знаний доступна только администратору",
        )
    # Гейт 2 — тип файла (v1: только текст).
    ext = splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise AttachError(
            415,
            f"Неподдерживаемый тип вложения «{ext or 'без расширения'}» "
            f"(ожидаются: {', '.join(sorted(ALLOWED_EXTS))})",
        )
    # Гейт 3 — размер и пустота.
    if len(raw) > MAX_FILE_SIZE:
        raise AttachError(
            413,
            f"Вложение слишком большое (макс. {MAX_FILE_SIZE // 1_048_576} МБ)",
        )
    if not raw.strip():
        raise AttachError(400, "Вложение пустое")
    # Гейт 4 — декод (utf-8 → windows-1251 fallback, reuse utils).
    text, decode_error = _read_uploaded_file(filename, raw)
    if text is None or decode_error is not None:
        raise AttachError(400, decode_error or "Не удалось прочитать файл")

    params = build_import_params(filename, text, domain=domain, subject=subject)
    result = await mcp_client.tools_call(IMPORT_TOOL_NAME, params, timeout=timeout)

    if not isinstance(result, dict):
        raise AttachError(400, f"Неожиданный ответ MCP: {type(result).__name__}")
    err = result.get("error")
    if err:
        message = str(err)
        code = 403 if _is_zone_denial(message) else 400
        raise AttachError(code, message)
    if result.get("partial_success") and result.get("failed", 0) > 0:
        raise AttachError(
            400,
            f"Импорт завершён с ошибками: failed={result.get('failed', 0)}",
        )
    return {
        "collection_id": result.get("collection_id"),
        "imported": result.get("imported", 0),
        "failed": result.get("failed", 0),
        "zone": PRIVATE_ZONE,  # серверная зона — константа канала
    }
