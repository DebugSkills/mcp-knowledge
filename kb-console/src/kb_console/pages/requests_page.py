"""Страница «Заявки на доступ» — admin-only окно админа (036 Ф2, §5-§6).

- Таблица заявок (дата/ФИО/отдел/телефон/почта/статус) + фильтры
  (статус/период/ФИО — клиентские: стор ограничен cap 500, один
  SQL-выборкой list(); индексы §2 работают на стороне стора);
- счётчики по статусам; диалог-деталь: все поля + история events;
- статусные переходы `new → in_progress → access_granted|rejected`
  (без отката — §6): ядро валидации `_change_status` общее для UI
  и публичного API `POST /api/requests/{id}/status` (admin-гейт в
  хендлере — UI-скрытие не защита);
- «Создать пользователя по заявке» (Q2: РУЧНОЕ создание) на
  access_granted → `/users?from_request=<id>` (предзаполнение);
- «Печать» → `/requests/print?id=` (requests_print.py).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..core.access_requests import STATUSES, AccessRequestError
from .requests_print import STATUS_LABELS

STATUS_META: dict[str, dict[str, str]] = {
    "new": {"label": "🆕 Новая", "color": "blue"},
    "in_progress": {"label": "⏳ В работе", "color": "orange"},
    "access_granted": {"label": "✅ Доступ выдан", "color": "green"},
    "rejected": {"label": "⛔ Отклонена", "color": "red"},
}

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("in_progress",),
    "in_progress": ("access_granted", "rejected"),
    # терминальные статусы без переходов (откат не предусматривается, §6)
}


def _requests_store() -> Any:
    from ..core import runtime

    return runtime.REQUESTS_STORE


def _change_status(
    store: Any, request_id: str, new_status: str, *, actor: str, note: str = "",
) -> Any:
    """Ядро перехода: валидация workflow + store.set_status (пишет events).

    Единая точка для UI-кнопок и API-хендлера (нет дрейфа правил).
    Raises AccessRequestError: bad_status / not_found / invalid_transition.
    """
    if new_status not in STATUSES:
        raise AccessRequestError("bad_status", "unknown status")
    current = store.get(request_id).status
    if new_status not in TRANSITIONS.get(current, ()):
        raise AccessRequestError(
            "invalid_transition", f"{current} -> {new_status} not allowed"
        )
    return store.set_status(request_id, new_status=new_status, actor=actor, note=note)


_STATUS_ERROR_MESSAGES = {
    "not_found": "Заявка не найдена",
    "bad_status": "Неизвестный статус",
    "invalid_transition": "Переход запрещён (workflow без отката)",
    "storage_error": "Ошибка хранилища",
}


def _status_impl(
    store: Any, request_id: str, new_status: str, *,
    is_admin: bool, actor: str, note: str = "",
) -> JSONResponse:
    """API-контракт POST /api/requests/{id}/status (pure, тестируется без сервера)."""
    if not is_admin:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if new_status not in STATUSES:
        return JSONResponse({"error": "bad_status"}, status_code=400)
    try:
        req = _change_status(store, request_id, new_status, actor=actor, note=note)
    except AccessRequestError as exc:
        code = exc.code if exc.code in _STATUS_ERROR_MESSAGES else "storage_error"
        return JSONResponse({"error": code}, status_code={
            "not_found": 404, "invalid_transition": 409,
        }.get(code, 500))
    return JSONResponse({"id": req.id, "status": req.status})


def register_api(nicegui_app: Any) -> None:
    """Зарегистрировать POST /api/requests/{request_id}/status (admin-гейт).

    Тело JSON: `{"status": "in_progress", "note": "..."}` (оба опциональны
    на уровне транспорта; валидация статуса — в _status_impl → 400).
    """
    from ..core import runtime
    from ..core.identity import effective_role, identity_from_request

    async def set_status(request_id: str, request: Request) -> JSONResponse:
        users = runtime.USERS_STORE
        ident = identity_from_request(request, users)
        has_users = bool(users is not None and users.has_users())
        is_admin = effective_role(ident, has_users=has_users) == "admin"
        actor = ident["username"] if ident else "admin"
        new_status, note = "", ""
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                new_status = str(payload.get("status") or "")
                note = str(payload.get("note") or "")[:200]
        except Exception:  # тело опционально, любые ошибки = ''
            new_status, note = "", ""
        return _status_impl(
            store=runtime.REQUESTS_STORE, request_id=request_id,
            new_status=new_status, is_admin=is_admin, actor=actor, note=note,
        )

    nicegui_app.add_api_route(
        "/api/requests/{request_id}/status", set_status, methods=["POST"]
    )


# ── Окно админа (страница) ──────────────────────────────────

_TABLE_COLUMNS = [
    {"name": "created", "label": "Дата", "field": "created", "align": "left"},
    {"name": "fio", "label": "ФИО", "field": "fio", "align": "left"},
    {"name": "department", "label": "Отдел", "field": "department", "align": "left"},
    {"name": "phone", "label": "Телефон", "field": "phone", "align": "left"},
    {"name": "email", "label": "Почта", "field": "email", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status_label", "align": "left"},
]


def _table_rows(requests: list[Any]) -> list[dict]:
    return [
        {
            "id": r.id,
            "created": r.created_at.replace("T", " ")[:16],
            "fio": r.fio,
            "department": r.department,
            "phone": r.phone,
            "email": r.email,
            "status": r.status,
            "status_label": STATUS_META.get(r.status, {}).get("label", r.status),
        }
        for r in requests
    ]


def build_requests() -> None:
    """Построить страницу «Заявки» (admin-only — паттерн tokens/users)."""
    from ..core.identity import current_actor, is_admin

    if not is_admin():
        ui.label("⛔ 403: заявки на доступ доступны только администраторам.").classes(
            "text-h6 text-negative"
        )
        ui.label("Обратитесь к администратору консоли.").classes("text-body1 text-grey")
        return

    _rows: list[Any] = []
    _error = ""
    _filters = {"status": "", "period_from": "", "period_to": "", "fio": ""}

    def load() -> None:
        """Одна SQL-выборка без фильтров (cap 500); фильтры — клиентские."""
        nonlocal _rows, _error
        store = _requests_store()
        if store is None:
            _error = "Хранилище заявок недоступно (запустите консоль через app.py)."
            return
        try:
            _rows = store.list()
            _error = ""
        except AccessRequestError:
            _error = "Ошибка загрузки заявок."

    def _filtered() -> list[Any]:
        rows = _rows
        if _filters["status"]:
            rows = [r for r in rows if r.status == _filters["status"]]
        if _filters["period_from"]:
            rows = [r for r in rows if r.created_at >= _filters["period_from"]]
        if _filters["period_to"]:
            rows = [r for r in rows if r.created_at[:10] <= _filters["period_to"]]
        if _filters["fio"]:
            needle = _filters["fio"].strip().lower()
            rows = [r for r in rows if needle in r.fio.lower()]
        return rows

    def _do_status(request_id: str, new_status: str, note_value, dlg) -> None:
        store = _requests_store()
        try:
            _change_status(
                store, request_id, new_status,
                actor=current_actor(), note=(note_value.value or "").strip(),
            )
        except AccessRequestError as exc:
            ui.notify(
                _STATUS_ERROR_MESSAGES.get(exc.code, "Ошибка"), type="negative",
            )
            return
        dlg.close()
        ui.notify(f"Статус изменён: {STATUS_LABELS.get(new_status, new_status)}",
                  type="positive")
        ui.navigate.to("/requests")  # полный reload — простой refresh

    def _detail(rec: Any) -> None:
        with ui.dialog() as dlg, ui.card().classes("w-full max-w-3xl"):
            meta = STATUS_META.get(rec.status, {})
            ui.label(f"Заявка {rec.id}").classes("text-h6")
            ui.badge(meta.get("label", rec.status), color=meta.get("color", "grey"))
            ui.label(f"Создана: {rec.created_at.replace('T', ' ')[:19]}").classes(
                "text-grey-7"
            )
            ui.separator()
            for label, value in (
                ("ФИО", rec.fio), ("Отдел", rec.department),
                ("Телефон", rec.phone), ("Почта", rec.email),
                ("Перечень проводимых работ", rec.work_summary),
                ("Согласие на обработку ПДн",
                 f"дано ({rec.consent_at.replace('T', ' ')[:19]})"),
            ):
                with ui.row().classes("w-full items-start gap-2"):
                    ui.label(f"{label}:").classes("font-bold w-56 shrink-0")
                    ui.label(value).classes("whitespace-pre-wrap")
            if rec.decided_at:
                ui.label(
                    f"Решение: {rec.decided_at.replace('T', ' ')[:16]}, "
                    f"{rec.decided_by}"
                    + (f" — {rec.decision_note}" if rec.decision_note else "")
                ).classes("text-grey-7")
            ui.separator()
            ui.label("История").classes("font-bold")
            for e in _requests_store().events(rec.id):
                change = e.get("old_status") or ""
                new = e.get("new_status") or ""
                arrow = f": {change} → {new}" if new else ""
                note = f" ({e['note']})" if e.get("note") else ""
                ui.label(
                    f"· {e['ts'].replace('T', ' ')[:16]} — {e['actor']}: "
                    f"{e['event']}{arrow}{note}"
                ).classes("text-body2 text-grey-8")
            targets = TRANSITIONS.get(rec.status, ())
            if targets:
                ui.separator()
                note_in = ui.input("Заметка к решению (опционально)").classes("w-full")
                with ui.row().classes("gap-2"):
                    for target in targets:
                        tmeta = STATUS_META.get(target, {})
                        ui.button(
                            f"→ {tmeta.get('label', target)}",
                            color=tmeta.get("color", "primary"),
                            on_click=lambda t=target: _do_status(rec.id, t, note_in, dlg),
                        ).props("dense")
            if rec.status == "access_granted":
                ui.separator()
                ui.button(
                    "👤 Создать пользователя по заявке",
                    color="positive",
                    on_click=lambda: ui.navigate.to(
                        f"/users?from_request={rec.id}"
                    ),
                ).tooltip("Q2: ручное создание — роль и пароль задаёт администратор")
            with ui.row().classes("gap-2 q-mt-md"):
                ui.button(
                    "🖨 Печать", on_click=lambda: ui.navigate.to(
                        f"/requests/print?id={rec.id}", new_tab=True,
                    ),
                ).props("flat")
                ui.button("Закрыть", on_click=dlg.close).props("flat")
        dlg.open()

    # ── счётчики + фильтры ──────────────────────────────────
    def render_counters() -> None:
        with ui.row().classes("items-center gap-2 q-mb-sm"):
            total = len(_rows)
            ui.badge(f"Всего: {total}", color="grey-6")
            for status in STATUSES:
                count = sum(1 for r in _rows if r.status == status)
                meta = STATUS_META.get(status, {})
                ui.badge(
                    f"{meta.get('label', status)}: {count}",
                    color=meta.get("color", "grey"),
                )

    @ui.refreshable
    def render() -> None:
        render_counters()
        with ui.row().classes("items-center gap-2 q-mb-sm no-print"):
            ui.select(
                ["", *STATUSES], value=_filters["status"], label="Статус",
                on_change=lambda v: (_filters.update(status=v.value), render.refresh()),
            ).classes("w-44")
            ui.input(
                "Период с (YYYY-MM-DD)", value=_filters["period_from"],
                on_change=lambda v: (_filters.update(period_from=v.value), render.refresh()),
            ).classes("w-44")
            ui.input(
                "по (YYYY-MM-DD)", value=_filters["period_to"],
                on_change=lambda v: (_filters.update(period_to=v.value), render.refresh()),
            ).classes("w-36")
            ui.input(
                "Поиск по ФИО", value=_filters["fio"],
                on_change=lambda v: (_filters.update(fio=v.value), render.refresh()),
            ).classes("w-52")
            ui.button("🔄 Обновить", on_click=lambda: _refresh_all())
        if _error:
            ui.label(_error).classes("text-negative")
            return
        rows = _filtered()
        if not rows:
            ui.label("Заявок нет.").classes("text-grey-7")
            return
        table = ui.table(
            columns=_TABLE_COLUMNS, rows=_table_rows(rows), row_key="id",
        ).classes("w-full")
        table.props("flat bordered dense")
        table.on("rowClick", lambda e: _detail_by_id(e.args[1]["id"]))
        ui.label("Клик по строке — детализация, история и действия").classes(
            "text-caption text-grey-6"
        )

    _by_id: dict[str, Any] = {}

    def _detail_by_id(request_id: str) -> None:
        rec = _by_id.get(request_id)
        if rec is not None:
            _detail(rec)

    def _refresh_all() -> None:
        """Полный цикл: SQL-выборка → реестр id → перерисовка."""
        load()
        _by_id.clear()
        _by_id.update({r.id: r for r in _rows})
        render.refresh()

    # Первичная отрисовка. ВАЖНО: `@ui.refreshable`-функцию нужно вызвать
    # ХОТЯ БЫ РАЗ самому — иначе `render.refresh()` ничего не рисует и
    # страница остаётся пустой (дефект, пойман визуальной проверкой 036).
    load()
    _by_id.update({r.id: r for r in _rows})
    render()
