"""Страница «Токены» — управление subscriber/read/import/write токенами (W5).

Фичи:
- Таблица токенов: бейджи уровня/зоны/статуса (v1.3), цветной префикс
  mcp_<l><z>_ + тултип расшифровки (v1.5), expiry-подсветка.
- Create-диалог: живой предпросмотр префикса; plaintext показывается
  ОДИН раз (диалог с копированием).
- Revoke / rotate / edit note+expiry.
- Баннер «скоро деактивация» (Q9 v1.7): subscriber-токены активные,
  созданные/использованные 83+ дней назад (90 - 7 warning-окно).
- Фильтры по уровню/зоне/статусу; автообновление (ui.refreshable + ui.timer).

Backend: GET/POST /tokens, POST /tokens/{id}/revoke|rotate, PATCH /tokens/{id}
(mcp_server/tokens_api.py, W5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from nicegui import ui

from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.mcp_client import MCPClient

# Q9 (v1.7): авто-деактивация subscriber через 90 дней, warning за 7 (83+)
DEACTIVATE_DAYS = 90
WARNING_WINDOW_DAYS = DEACTIVATE_DAYS - 7

LEVEL_META: dict[str, dict] = {
    "subscriber": {"label": "🟢 подписчик", "color": "green"},
    "read": {"label": "🔵 чтение", "color": "blue"},
    "import": {"label": "🟠 импорт", "color": "orange"},
    "write": {"label": "🔴 запись", "color": "red"},
}
ZONE_META: dict[str, dict] = {
    "public": {"label": "public", "color": "teal"},
    "private": {"label": "private", "color": "purple"},
    "both": {"label": "обе зоны", "color": "grey"},
}
LEVEL_CODE = {"subscriber": "s", "read": "r", "import": "i", "write": "w"}
ZONE_CODE = {"public": "a", "private": "b", "both": "x"}


def _now() -> datetime:
    return datetime.now(UTC)


def _days_old(ts_str: str | None, base: datetime) -> float | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))  # noqa: FURB162 — py3.11 fromisoformat не парсит 'Z'
    except ValueError:
        return None
    return (base - dt).total_seconds() / 86400.0


def _status_badge(rec: dict) -> tuple[str, str]:
    """(label, color) для статуса токена.

    days = (now - ts) в днях: >0 — ts в прошлом (expired), <0 — в будущем.
    """
    base = _now()
    if not rec.get("active"):
        return "⛔ revoked", "grey"
    exp = rec.get("expires_at")
    if exp:
        days = _days_old(exp, base)
        if days is not None and days > 0:
            return "⏳ expired", "grey"
        if days is not None and days >= -7:
            return f"⚠️ expires в {int(-days)}д", "orange"
    # Q9: subscriber без использования 83+ дней → предупреждение
    if rec.get("level") == "subscriber":
        last = _days_old(rec.get("last_used_at"), base)
        if last is None:
            last = _days_old(rec.get("created_at"), base)
        if last is not None and last >= WARNING_WINDOW_DAYS:
            return f"⚠️ неактивен {int(last)}д", "orange"
    return "✅ active", "green"


def _prefix(rec: dict) -> str:
    """Цветной префикс mcp_<l><z>_ (v1.5)."""
    lc = LEVEL_CODE.get(rec.get("level", ""), "?")
    zc = ZONE_CODE.get(rec.get("zone", ""), "?")
    return f"mcp_{lc}{zc}_"


def _prefix_decode(rec: dict) -> str:
    """Однострочная расшифровка префикса (тултип, v1.5)."""
    lvl = LEVEL_META.get(rec.get("level", ""), {}).get("label", rec.get("level"))
    zn = ZONE_META.get(rec.get("zone", ""), {}).get("label", rec.get("zone"))
    return f"{_prefix(rec)} → {lvl}, {zn}"


def _stale_banner_rows(tokens: list[dict]) -> list[dict]:
    """Subscriber-токены в warning-окне Q9 (83+ дней без использования)."""
    base = _now()
    out = []
    for rec in tokens:
        if rec.get("level") != "subscriber" or not rec.get("active"):
            continue
        last = _days_old(rec.get("last_used_at"), base)
        if last is None:
            last = _days_old(rec.get("created_at"), base)
        if last is not None and last >= WARNING_WINDOW_DAYS:
            out.append({"rec": rec, "days": int(last)})
    return sorted(out, key=lambda x: -x["days"])


def build_tokens() -> None:
    """Построить страницу «Токены»."""

    # ── состояние ────────────────────────────────────────────
    _tokens: list[dict] = []
    _error: str = ""
    _filters = {"level": "", "zone": "", "status": ""}
    _timer = None

    async def load() -> None:
        nonlocal _tokens, _error
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            _tokens = await client.list_tokens()
            _error = ""
        except Exception as exc:
            _error = f"Не удалось загрузить токены: {exc}"
        finally:
            await client.close()
        render.refresh()
        render_stale_banner.refresh()  # W5 gate P1: баннер Q9 живёт в своём refreshable

    # ── баннер Q9 ────────────────────────────────────────────
    @ui.refreshable
    def render_stale_banner() -> None:
        stale = _stale_banner_rows(_tokens)
        if not stale:
            return
        with ui.banner(type="warning").classes("w-full"):
            ui.label(
                f"⚠️ Авто-деактивация (Q9): {len(stale)} subscriber-токен(ов) "
                f"неактивны {WARNING_WINDOW_DAYS}+ дней — будут деактивированы "
                f"после {DEACTIVATE_DAYS} дней без использования."
            )
            for item in stale:
                rec = item["rec"]
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"• {rec['id']} ({rec.get('note') or 'без заметки'}) — {item['days']} дн.")
                    ui.button(
                        "Отозвать", size="sm", color="negative",
                        on_click=lambda tid=rec["id"]: _confirm_revoke(tid),
                    )

    # ── таблица ─────────────────────────────────────────────
    @ui.refreshable
    def render() -> None:
        if _error:
            ui.label(_error).classes("text-negative")
            return

        rows = _tokens
        if _filters["level"]:
            rows = [r for r in rows if r.get("level") == _filters["level"]]
        if _filters["zone"]:
            rows = [r for r in rows if r.get("zone") == _filters["zone"]]
        if _filters["status"] == "active":
            rows = [r for r in rows if r.get("active")]
        elif _filters["status"] == "revoked":
            rows = [r for r in rows if not r.get("active")]

        with ui.column().classes("w-full gap-2"):
            if not rows:
                ui.label("Токенов нет. Создайте первый через «Создать токен».")
                return
            for rec in rows:
                with ui.card().classes("w-full"), ui.row().classes("items-center gap-2 w-full"):
                        # цветной префикс + id (v1.5)
                        lvl = LEVEL_META.get(rec.get("level", ""), {})
                        with ui.row().classes("items-center gap-1"):
                            ui.label(_prefix(rec)).classes(
                                f"text-bold text-{lvl.get('color', 'grey')}-8"
                            ).tooltip(_prefix_decode(rec))
                            ui.label(rec["id"]).classes("text-grey-8")
                        # бейджи
                        ui.badge(
                            lvl.get("label", rec.get("level", "?")),
                            color=lvl.get("color", "grey"),
                        )
                        zn = ZONE_META.get(rec.get("zone", ""), {})
                        ui.badge(zn.get("label", rec.get("zone", "?")), color=zn.get("color", "grey"))
                        st_label, st_color = _status_badge(rec)
                        ui.badge(st_label, color=st_color)
                        if rec.get("expires_at"):
                            ui.label(f"до {rec['expires_at'][:10]}").classes("text-grey-6")
                        if rec.get("note"):
                            ui.label(rec["note"]).classes("text-grey-6")
                        if rec.get("source") == "env":
                            ui.badge("env", color="grey-5").tooltip("Задан через env-переменную; управляется только через env/revoke")
                        ui.space()
                        ui.button("Edit", size="sm", on_click=lambda r=rec: _edit_dialog(r))
                        ui.button("Rotate", size="sm", color="warning",
                                  on_click=lambda r=rec: _confirm_rotate(r))
                        ui.button("Отозвать", size="sm", color="negative",
                                  on_click=lambda r=rec: _confirm_revoke(r["id"]))

    # ── create: живой предпросмотр префикса (v1.5) ──────────
    _create_state: dict = {"level": "subscriber", "zone": "public"}

    def _preview_prefix() -> str:
        lc = LEVEL_CODE.get(_create_state["level"], "?")
        zc = ZONE_CODE.get(_create_state["zone"], "?")
        return f"mcp_{lc}{zc}_"

    def _refresh_preview() -> None:
        lvl = LEVEL_META.get(_create_state["level"], {}).get("label", _create_state["level"])
        zn = ZONE_META.get(_create_state["zone"], {}).get("label", _create_state["zone"])
        color = LEVEL_META.get(_create_state["level"], {}).get("color", "grey")
        _create_preview.text = f"{_preview_prefix()} → {lvl}, {zn}"
        _create_preview.classes(f"text-bold text-{color}-8")

    async def _do_create() -> None:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            note = _create_note.value or ""
            expires = _create_expires.value
            result = await client.create_token(
                level=_create_state["level"], zone=_create_state["zone"],
                note=note, expires_at=expires,
            )
        finally:
            await client.close()
        if result is None:
            ui.notify("Не удалось создать токен (проверьте write-ключ kb-console)", type="negative")
            return
        # plaintext — ОДИН раз
        with ui.dialog() as dlg, ui.card():
            ui.label("🔑 Новый токен (показывается один раз)").classes("text-bold")
            ui.label(result["plaintext"]).classes("text-bold text-primary").tooltip("Скопируйте и сохраните")
            with ui.row().classes("items-center gap-2"):
                ui.button("📋 Копировать", on_click=lambda: ui.clipboard.write(result["plaintext"]))
                ui.button("Закрыть", on_click=dlg.close)
        dlg.open()
        _create_dialog.close()
        ui.notify(f"Токен создан: {result['id']}", type="positive")
        await load()

    with ui.dialog() as _create_dialog, ui.card():
        ui.label("Создать токен").classes("text-h6")
        _create_level_select = ui.select(
            list(LEVEL_META.keys()), value="subscriber", label="Уровень",
            on_change=lambda v: (
                _create_state.update(level=v.value),
                # W5 gate P2: subscriber принудительно public (формат mcp_sa_,
                # token_store форсит на сервере — предпросмотр должен не врать)
                _create_state.update(zone="public") if v.value == "subscriber" else None,
                # W5 gate P3: синхронизируем дропдаун зоны с форсом
                _create_zone_select.set_value("public") if v.value == "subscriber" else None,
                _refresh_preview(),
            ),
        )
        _create_zone_select = ui.select(
            list(ZONE_META.keys()), value="public", label="Зона",
            on_change=lambda v: (
                _create_state.update(zone="public" if _create_state["level"] == "subscriber" else v.value),
                _refresh_preview(),
            ),
        )
        _create_note = ui.input("Заметка (напр. «Boosty Практик: Иван»)")
        _create_expires = ui.input("Срок действия (YYYY-MM-DD, опционально)")
        ui.label("Предпросмотр префикса:").classes("text-grey-7")
        _create_preview = ui.label("").classes("text-bold")
        _refresh_preview()
        with ui.row().classes("gap-2"):
            ui.button("Создать", color="positive", on_click=_do_create)
            ui.button("Отмена", on_click=_create_dialog.close)

    # ── revoke / rotate / edit ───────────────────────────────
    async def _do_revoke(token_id: str) -> None:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            ok = await client.revoke_token(token_id)
        finally:
            await client.close()
        ui.notify(f"Токен {token_id} отозван" if ok else "Ошибка отзыва", type="positive" if ok else "negative")
        await load()

    def _confirm_revoke(token_id: str) -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Отозвать токен {token_id}?").classes("text-h6")
            ui.label("Доступ немедленно прекратится.")
            with ui.row().classes("gap-2"):
                ui.button("Отозвать", color="negative",
                          on_click=lambda: (_do_revoke(token_id), dlg.close()))
                ui.button("Отмена", on_click=dlg.close)
        dlg.open()

    async def _do_rotate(rec: dict) -> None:
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            result = await client.rotate_token(rec["id"])
        finally:
            await client.close()
        if result is None:
            ui.notify("Ошибка rotate", type="negative")
            return
        with ui.dialog() as dlg, ui.card():
            ui.label("🔑 Новый ключ (один раз)").classes("text-bold")
            ui.label(result["plaintext"]).classes("text-bold text-primary")
            ui.button("Закрыть", on_click=dlg.close)
        dlg.open()
        await load()

    def _confirm_rotate(rec: dict) -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Rotate {rec['id']}?").classes("text-h6")
            ui.label("Старый ключ будет отозван, новый — показан один раз.")
            with ui.row().classes("gap-2"):
                ui.button("Rotate", color="warning", on_click=lambda: (_do_rotate(rec), dlg.close()))
                ui.button("Отмена", on_click=dlg.close)
        dlg.open()

    def _edit_dialog(rec: dict) -> None:
        async def _save(note_input, exp_input) -> None:
            client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
            try:
                await client.patch_token(rec["id"], note=note_input.value or None,
                                         expires_at=exp_input.value or None)
            finally:
                await client.close()
            ui.notify(f"Токен {rec['id']} обновлён", type="positive")
            await load()

        with ui.dialog() as dlg, ui.card():
            ui.label(f"Изменить {rec['id']}").classes("text-h6")
            note_input = ui.input("Заметка", value=rec.get("note") or "")
            exp_input = ui.input("Срок (YYYY-MM-DD)", value=(rec.get("expires_at") or "")[:10])
            with ui.row().classes("gap-2"):
                ui.button("Сохранить", color="positive",
                          on_click=lambda: (_save(note_input, exp_input), dlg.close()))
                ui.button("Отмена", on_click=dlg.close)
        dlg.open()

    # ── фильтры ──────────────────────────────────────────────
    with ui.row().classes("items-center gap-2"):
        ui.button("🔄 Обновить", on_click=load)
        ui.button("➕ Создать токен", color="positive", on_click=_create_dialog.open)
        ui.select(["", "subscriber", "read", "import", "write"], value="",
                  label="Уровень", on_change=lambda v: (_filters.update(level=v.value), render.refresh()))
        ui.select(["", "public", "private", "both"], value="",
                  label="Зона", on_change=lambda v: (_filters.update(zone=v.value), render.refresh()))
        ui.select(["", "active", "revoked"], value="",
                  label="Статус", on_change=lambda v: (_filters.update(status=v.value), render.refresh()))

    render_stale_banner()
    render()

    # ── автообновление ───────────────────────────────────────
    _timer = ui.timer(REFRESH_SECONDS, load)

    def _cleanup() -> None:
        if _timer is not None:
            _timer.cancel()

    ui.context.client.on_disconnect(_cleanup)
