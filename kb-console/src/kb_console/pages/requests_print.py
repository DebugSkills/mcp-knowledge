"""Печатная карточка заявки — `GET /requests/print?id=` (036 Ф2, план §5).

Отдельный GET-маршрут (НЕ @ui.page): чистый HTML без NiceGUI-обвязки —
нет SPA-скриптов/сокета, печатается как есть.

Инварианты (R4/air-gap, iter3):
- **admin-only**: гейт В ХЕНДЛЕРЕ (печать без гейта — частая дыра:
  страница скрыта в навигации ≠ запрет URL);
- **0 внешних ресурсов**: инлайн-CSS, системные шрифты, ни одного
  http(s)://-URL (контракт фиксирует тест);
- `@media print` + `@page` + кнопка `window.print()` (класс no-print).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..core.access_requests import AccessRequestError

STATUS_LABELS: dict[str, str] = {
    "new": "Новая",
    "in_progress": "В работе",
    "access_granted": "Доступ выдан",
    "rejected": "Отклонена",
}
"""RU-метки статусов (SSOT для печати и окна админа)."""

_EVENT_LABELS: dict[str, str] = {
    "created": "Создана",
    "status_change": "Смена статуса",
}

_CARD_CSS = """body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
color:#1a1a1a;margin:0;padding:16px;background:#fff}
.card{max-width:190mm;margin:0 auto}
h1{font-size:18px;margin:0 0 2px}
.sub{color:#555;font-size:13px;margin-bottom:14px}
table.fields{border-collapse:collapse;width:100%;margin-bottom:14px}
table.fields td{border:1px solid #999;padding:6px 8px;font-size:13px;
vertical-align:top}
table.fields td.k{width:38mm;background:#f2f2f2;font-weight:600}
.works{white-space:pre-wrap}
h2{font-size:14px;margin:12px 0 6px}
table.hist{border-collapse:collapse;width:100%}
table.hist th,table.hist td{border:1px solid #aaa;padding:4px 6px;
font-size:12px;text-align:left}
table.hist th{background:#f2f2f2}
.badge{display:inline-block;border:1px solid #333;border-radius:4px;
padding:1px 8px;font-size:12px;font-weight:600}
button{padding:8px 18px;font-size:14px;margin-top:10px}
@media print{.no-print{display:none}body{padding:0}}
@page{size:A4;margin:15mm}"""


def _esc(value: Any) -> str:
    from html import escape

    return escape(str(value if value is not None else ""))


def _fmt_ts(ts: str) -> str:
    """`2026-09-28T21:00:00.123Z` → `2026-09-28 21:00` (локальная читаемость)."""
    return (ts or "").replace("T", " ")[:16]


def _error_html(code: str, message: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{code}</title></head><body><h1>{code}</h1>"
        f"<p>{_esc(message)}</p></body></html>"
    )


def render_request_card_html(req: Any, events: list[dict[str, Any]]) -> str:
    """Чистый HTML карточки заявки (все поля + статус + история).

    Pure-функция: контент-контракт печатается в тестах без сервера.
    """
    status_label = STATUS_LABELS.get(req.status, req.status)
    rows = [
        ("Дата заявки", _fmt_ts(req.created_at)),
        ("Статус", f"{status_label} ({req.status})"),
        ("ФИО", req.fio),
        ("Отдел", req.department),
        ("Телефон", req.phone),
        ("Почта", req.email),
        ("Перечень проводимых работ", req.work_summary),
        ("Согласие на обработку ПДн", f"дано ({_fmt_ts(req.consent_at)})"),
    ]
    if req.decided_at:
        rows.append(
            ("Решение", f"{_fmt_ts(req.decided_at)}, {req.decided_by}"
                        + (f" — {req.decision_note}" if req.decision_note else ""))
        )
    fields = "".join(
        f"<tr><td class='k'>{_esc(k)}</td><td class='works'>{_esc(v)}</td></tr>"
        for k, v in rows
    )
    hist = "".join(
        "<tr>"
        f"<td>{_esc(_fmt_ts(e.get('ts', '')))}</td>"
        f"<td>{_esc(e.get('actor', ''))}</td>"
        f"<td>{_esc(_EVENT_LABELS.get(e.get('event', ''), e.get('event', '')))}</td>"
        f"<td>{_esc(e.get('old_status') or '—')} → "
        f"{_esc(e.get('new_status') or '—')}</td>"
        f"<td>{_esc(e.get('note', ''))}</td>"
        "</tr>"
        for e in events
    )
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>Заявка {_esc(req.id)}</title><style>{_CARD_CSS}</style></head>"
        "<body><div class='card'>"
        "<h1>Заявка на доступ к базе знаний</h1>"
        f"<p class='sub'>ID: {_esc(req.id)} · "
        f"<span class='badge'>{_esc(status_label)}</span></p>"
        f"<table class='fields'>{fields}</table>"
        "<h2>История</h2>"
        "<table class='hist'><tr><th>Время</th><th>Кто</th><th>Событие</th>"
        "<th>Статус</th><th>Заметка</th></tr>"
        f"{hist}</table>"
        "<div class='no-print'><button onclick=\"window.print()\">"
        "&#128424; Печать</button></div>"
        "</div></body></html>"
    )


def _print_impl(store: Any, request_id: str, *, is_admin: bool) -> Response:
    """Контракт печати: 403 (не-admin) / 404 (нет/id пуст) / 200 HTML."""
    if not is_admin:
        return Response(
            _error_html("403", "Печать заявки доступна только администраторам."),
            status_code=403, media_type="text/html; charset=utf-8",
        )
    if store is None:
        return Response(
            _error_html("503", "Хранилище заявок недоступно."),
            status_code=503, media_type="text/html; charset=utf-8",
        )
    try:
        req = store.get(request_id)
        events = store.events(request_id)
    except AccessRequestError as exc:
        if exc.code == "not_found":
            return Response(
                _error_html("404", "Заявка не найдена (возможно, удалена по retention)."),
                status_code=404, media_type="text/html; charset=utf-8",
            )
        return Response(
            _error_html("500", "Ошибка хранилища заявок."),
            status_code=500, media_type="text/html; charset=utf-8",
        )
    return Response(
        render_request_card_html(req, events),
        status_code=200, media_type="text/html; charset=utf-8",
    )


def register_print_route(nicegui_app: Any) -> None:
    """Зарегистрировать GET /requests/print (гейт admin в хендлере)."""
    from ..core import runtime
    from ..core.identity import effective_role, identity_from_request

    def _is_admin(request: Request) -> bool:
        users = runtime.USERS_STORE
        ident = identity_from_request(request, users)
        has_users = bool(users is not None and users.has_users())
        return effective_role(ident, has_users=has_users) == "admin"

    async def print_card(request: Request, id: str = "") -> Response:
        return _print_impl(runtime.REQUESTS_STORE, id.strip(), is_admin=_is_admin(request))

    nicegui_app.add_api_route("/requests/print", print_card, methods=["GET"])
