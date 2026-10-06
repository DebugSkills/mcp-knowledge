"""Страница «Чат» — стриминг ответов LiteLLM (Ф2 ai-workspace, шаг #1).

Спека §8 «Ф2-спека» E: NiceGUI server-side async-generator — дельты SSE
инкрементально дописываются в элемент-носитель (``set_content`` по чанкам =
``element.update()``). H «ложные зелёные»: «стриминг без носителя» запрещён —
каждая дельта обязана отражаться на UI (тест считает set_content-вызовы).

Персист в ConversationStore — Ф2 #2 (стора готова, сюда не тянется): история
живёт в замыкании страницы (память процесса), контекст передаётся на каждый
ход целиком.

I5/I6-транспарентность: зона выборки — ``ws_zone.zone_for_identity`` (reuse
identity-стека kb-console, семантика legacy=admin бит-ин-бит), роль —
``current_role()``. Гейт доступа: ROUTES min_role="contributor" (все роли
консоли; header скрывает пункт ниже min_role).
"""

from __future__ import annotations

from typing import Any

import httpx
from nicegui import ui

from ..core.identity import _has_users, current_identity, current_role
from ..core.llm_stream import LLMStreamError, stream_chat
from ..core.ws_zone import zone_for_identity


def _zone_caption(role: str, zone: str) -> str:
    """Подпись прозрачности I5/I6: роль + зона выборки пользователя."""
    return f"Роль: {role} · зона выборки: {zone}"


async def _run_turn(
    history: list[dict[str, str]],
    output: Any,
    status: Any,
) -> None:
    """Один ход диалога: user-сообщение уже в history → стрим → носитель.

    - каждая дельта → ``output.set_content`` (видимый носитель; на реальном
      стриме ≥2 обновлений — приёмка Ф2);
    - успех → assistant-ответ дописывается в history (контекст след. хода);
    - сбой (LLMStreamError вкл. 429 / httpx-сеть / timeout) → ``ui.notify``,
      оборванный ход откатывается из history, страница НЕ падает.
    """
    acc = ""
    output.set_content("")
    status.set_text("стрим…")
    try:
        async for delta in stream_chat(list(history)):
            acc += delta
            output.set_content(acc)
    except (LLMStreamError, httpx.HTTPError, TimeoutError) as exc:
        history.pop()  # оборванный ход не запоминаем
        status.set_text("")
        ui.notify(f"Ошибка стрима: {exc}", type="negative")
        return
    if acc:
        history.append({"role": "assistant", "content": acc})
    status.set_text("готово")


def build_chat() -> None:
    """Построить страницу «Чат» (стриминг от LiteLLM, Ф2 #1)."""
    role = current_role()
    identity = current_identity()
    zone = zone_for_identity(identity, has_users=_has_users())

    ui.label("Чат с локальной моделью").classes("text-h4 q-mb-xs")
    ui.label(_zone_caption(role, zone)).classes("text-caption text-grey q-mb-md")

    history: list[dict[str, str]] = []  # персист — Ф2 #2 (ConversationStore)
    answer = ui.markdown("").classes("w-full q-mt-md")
    status = ui.label("").classes("text-caption text-grey")
    msg_input = ui.input(
        label="Сообщение", placeholder="Спросите что-нибудь…"
    ).classes("w-96")

    async def send() -> None:
        text = (msg_input.value or "").strip()
        if not text:
            ui.notify("Введите сообщение", type="warning")
            return
        history.append({"role": "user", "content": text})
        msg_input.value = ""
        await _run_turn(history, answer, status)

    ui.button("Отправить", icon="send", on_click=send).props("color=primary")
    msg_input.on("keydown.enter", lambda: send())
