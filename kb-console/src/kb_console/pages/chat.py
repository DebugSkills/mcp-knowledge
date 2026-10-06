"""Страница «Чат» — ход через tool-loop (MCP search) + персист (Ф2 #2b-2a).

Спека §8 «Ф2-спека» E: NiceGUI server-side async-generator — дельты
инкрементально дописываются в элемент-носитель (``set_content`` по чанкам =
``element.update()``). H «ложные зелёные»: «стриминг без носителя» запрещён —
каждая дельта обязана отражаться на UI (тест считает set_content-вызовы).

Ф2 #2b-2a: ``_run_turn`` идёт через ``core.chat_turn.chat_turn`` — tool-loop
(``run_turn``: MCP ``search_knowledge`` + итерации LLM) с токен-стримом в
``on_delta``. Персист — ``ConversationStore`` поверх ws-Redis (fail-soft:
нет Redis → store=None, история в замыкании страницы); сессия стабильная
``"default"``, user — из identity (``username``).

I5/I6-транспарентность: зона выборки — ``ws_zone.zone_for_identity`` (reuse
identity-стека kb-console, семантика legacy=admin бит-ин-бит), роль —
``current_role()``. Гейт доступа: ROUTES min_role="contributor" (все роли
консоли; header скрывает пункт ниже min_role).
"""

from __future__ import annotations

from typing import Any

import httpx
from nicegui import ui

from ..core.chat_turn import chat_turn
from ..core.identity import _has_users, current_identity, current_role
from ..core.llm_stream import LLMStreamError
from ..core.tool_loop import ToolLoopError
from ..core.ws_zone import zone_for_identity

SESSION_ID = "default"


def _zone_caption(role: str, zone: str) -> str:
    """Подпись прозрачности I5/I6: роль + зона выборки пользователя."""
    return f"Роль: {role} · зона выборки: {zone}"


def _build_store() -> Any:
    """ConversationStore поверх ws-Redis; fail-soft (нет Redis → None)."""
    try:
        from ..core.conversations import ConversationStore
        from ..core.redis_client import get_ws_redis

        return ConversationStore(get_ws_redis())
    except Exception:
        return None


def _session_messages(
    store: Any, user: str | None, history: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Контекст хода: сохранённая сессия + текущий вопрос из history.

    Текущее user-сообщение ещё НЕ в store (chat_turn персистит его после
    хода), поэтому берём его из локальной history (``history[-1]``).
    """
    current = history[-1] if history else None
    if store is not None and user and current is not None:
        try:
            loaded = store.history(user, SESSION_ID)
        except Exception:
            loaded = None
        if loaded is not None:
            return [*loaded, current]
    return list(history)


async def _run_turn(
    history: list[dict[str, str]],
    output: Any,
    status: Any,
    *,
    store: Any = None,
    user: str | None = None,
    zone: str = "public",
) -> None:
    """Один ход диалога через tool-loop: user-сообщение уже в history.

    - каждая дельта → ``output.set_content`` (видимый носитель; на реальном
      стриме ≥2 обновлений — приёмка Ф2);
    - успех → assistant-ответ дописывается в history и персистится
      (``chat_turn`` → store, fail-soft);
    - сбой (LLMStreamError/ToolLoopError/httpx/timeout) → ``ui.notify``,
      оборванный ход откатывается из history, страница НЕ падает.
    """
    messages = _session_messages(store, user, history)
    acc = ""
    output.set_content("")
    status.set_text("стрим…")

    def on_delta(delta: str) -> None:
        nonlocal acc
        acc += delta
        output.set_content(acc)

    try:
        result = await chat_turn(
            messages,
            session_id=SESSION_ID,
            zone=zone,
            user=user,
            store=store,
            on_delta=on_delta,
        )
    except (LLMStreamError, ToolLoopError, httpx.HTTPError, TimeoutError) as exc:
        history.pop()  # оборванный ход не запоминаем
        status.set_text("")
        ui.notify(f"Ошибка стрима: {exc}", type="negative")
        return
    if not acc:  # непростримленный финал — всё равно показать носитель
        acc = result["text"]
        output.set_content(acc)
    if acc:
        history.append({"role": "assistant", "content": acc})
    status.set_text("готово")


def build_chat() -> None:
    """Построить страницу «Чат» (tool-loop + персист, Ф2 #2b-2a)."""
    role = current_role()
    identity = current_identity()
    zone = zone_for_identity(identity, has_users=_has_users())
    user = (identity or {}).get("username") or None
    store = _build_store()

    ui.label("Чат с локальной моделью").classes("text-h4 q-mb-xs")
    ui.label(_zone_caption(role, zone)).classes("text-caption text-grey q-mb-md")

    history: list[dict[str, str]] = []  # fallback-контекст при store=None
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
        await _run_turn(history, answer, status, store=store, user=user, zone=zone)

    ui.button("Отправить", icon="send", on_click=send).props("color=primary")

    # Ф2 #6b: вложения в KB (admin-only) — серверный канал core/ws_attach.
    from ..components.attach_upload import build_attach_upload
    ui.separator().classes("q-my-md")
    build_attach_upload(role)
    msg_input.on("keydown.enter", lambda: send())
