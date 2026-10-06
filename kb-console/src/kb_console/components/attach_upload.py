"""UI-компонент «прикрепить файл в базу знаний» для чата верстака (Ф2 #6b-1).

Трасса: arch-2026-10-05-ai-workspace (plans/arch-2026-10-05-ai-workspace-plan.md,
строка #6). Часть 6b-1 — ТОЛЬКО компонент + тесты; встраивание в pages/chat.py
и compose/env (WS_MCP_IMPORT_KEY) — отдельный шаг 6b-2.

Назначение: upload-виджет (.md/.txt) → серверный канал ``core.ws_attach.
attach_to_kb`` → импорт вложения в KB **зоны private**.

Why admin-only: политика D7 «private admin-only v1» — у contributor/editor
базовая зона public (``core.ws_zone.zone_for_role``), импорт из чата в private
был бы эскалацией привилегий. Гейт UI (``attach_allowed``) — defense-in-depth:
виджет просто не создаётся у non-admin; СЕРВЕРНЫЙ гейт 403 в ``attach_to_kb``
остаётся авторитетным (UI-гейт не заменяет и не ослабляет его).

Зона и цель импорта НЕ выбираются и НЕ передаются из UI: они строятся
серверно в ``ws_attach`` (``build_import_params`` — строгий allowlist,
``zone=private`` — инъекция сервера, target-поля физически отсутствуют в
сигнатуре ``attach_to_kb``). Этот компонент передаёт только filename/raw/role.

Fail-closed: любое исключение канала → ``ui.notify(type="negative")``;
silent-return нет. Клиент MCP закрывается в ``finally`` (fail-soft: aclose
или close, await если корутина — закрытие не должно ронять обработчик).
"""

from __future__ import annotations

import inspect
from typing import Any

from nicegui import ui

from ..core.utils import MAX_FILE_SIZE
from ..core.ws_attach import AttachError, attach_to_kb, default_import_client
from ..core.ws_zone import PRIVATE_ZONE, zone_for_role


def attach_allowed(role: str | None) -> bool:
    """True только для роли с зоной private (admin). Reuse ws_zone.zone_for_role.

    Чистый хелпер без NiceGUI-зависимостей — отдельная единица тестирования.
    None/неизвестная роль → public (fail-closed) → False.
    """
    return zone_for_role(role) == PRIVATE_ZONE


async def _close_quietly(client: Any) -> None:
    """Fail-soft закрытие MCP-клиента: ``aclose`` → ``close``, await если корутина.

    Ошибка закрытия НЕ пробрасывается — обработчик upload уже показал исход
    операции пользователю, закрывать его падением нельзя.
    """
    for name in ("aclose", "close"):
        method = getattr(client, name, None)
        if callable(method):
            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001,S110 — fail-soft по контракту закрытия
                pass
            return


def build_attach_upload(role: str | None, *, client_factory: Any = None) -> None:
    """Построить блок «прикрепить файл в KB» для роли ``role``.

    ``client_factory`` — фабрика MCP-клиента (по умолчанию ``ws_attach.
    default_import_client``: env ``WS_MCP_IMPORT_KEY``/``WS_MCP_URL``);
    инжектимость нужна тестам. Клиент создаётся лениво — только в момент
    upload и только после прохождения гейта роли.
    """
    if client_factory is None:
        client_factory = default_import_client

    # Гейт UI: non-admin не получает upload-виджет вовсе (D7, см. docstring).
    if not attach_allowed(role):
        ui.label("Вложения доступны только администратору (зона private)").classes(
            "text-caption text-grey"
        )
        return

    ui.label("Прикрепить файл в базу знаний (private, .md/.txt)").classes(
        "text-caption"
    )

    async def on_upload(e) -> None:
        # NiceGUI 3.15 (как pages/import_page.handle_upload): e.file —
        # загруженный файл с атрибутом ``.name`` и асинхронным ``.read()``.
        filename = e.file.name
        raw = await e.file.read()
        client = client_factory()
        try:
            res = await attach_to_kb(
                filename=filename, raw=raw, role=role, mcp_client=client
            )
            ui.notify(
                f"Файл добавлен в KB (private): {res.get('collection_id', '')}",
                type="positive",
            )
        except AttachError as exc:
            # Типизированный отказ канала: код + безопасный текст из сервера.
            ui.notify(f"[{exc.code}] {exc.message}", type="negative")
        except Exception as exc:  # noqa: BLE001 — UI-граница: показать и не уронить
            ui.notify(f"Ошибка вложения: {exc}", type="negative")
        finally:
            await _close_quietly(client)

    def on_rejected(_e) -> None:
        # Отклонение Quasar ещё до on_upload (размер/accept фильтр клиента).
        ui.notify("Файл отклонён: превышен размер или тип", type="negative")

    ui.upload(
        on_upload=on_upload,
        auto_upload=True,
        max_file_size=MAX_FILE_SIZE,
        on_rejected=on_rejected,
    ).props('accept=".md,.txt"')
