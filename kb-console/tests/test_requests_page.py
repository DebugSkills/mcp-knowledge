"""Тесты окна админа «Заявки» (036 Ф2): статусы, печать, гейты.

Контракты по плану §5-§6 + §9 Фаза 2:
- статусный переход `new→in_progress→access_granted|rejected` пишет events
  (аудит actor/note); откат и перескок запрещены (409);
- API `_status_impl`: не-admin → 403 (гейт В ХЕНДЛЕРЕ, не в UI);
- печать `_print_impl`: admin → 200 HTML со всеми полями/статусом/датой,
  `@media print`, **0 внешних URL** (air-gap, iter3), не-admin → 403,
  неизвестный id → 404;
- ROUTES: `/requests` min_role=admin (скрыт не-admin в навигации).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kb_console.core.access_requests import AccessRequestError, AccessRequestStore
from kb_console.pages.requests_page import (
    TRANSITIONS,
    _change_status,
    _status_impl,
)
from kb_console.pages.requests_print import (
    _print_impl,
    render_request_card_html,
)


def _store(tmp_path: Path) -> AccessRequestStore:
    s = AccessRequestStore(str(tmp_path / "req" / "a.db"))
    s.migrate()
    return s


def _seed(store: AccessRequestStore, *, fio: str = "Иван Иванов") -> str:
    return store.append(
        fio=fio, department="Отдел тестирования", phone="+7 900 000-00-01",
        email="ivan@example.com", work_summary="Чтение базы знаний",
    ).id


# ── Workflow: переходы статусов (§6) ────────────────────────


class TestTransitions:
    def test_chain_writes_events(self, tmp_path):
        """new→in_progress→access_granted: каждый переход пишет status_change."""
        store = _store(tmp_path)
        rid = _seed(store)
        _change_status(store, rid, "in_progress", actor="admin")
        _change_status(store, rid, "access_granted", actor="root", note="ок")
        events = store.events(rid)
        changes = [e for e in events if e["event"] == "status_change"]
        assert [(c["old_status"], c["new_status"], c["actor"]) for c in changes] == [
            ("new", "in_progress", "admin"),
            ("in_progress", "access_granted", "root"),
        ]
        assert changes[-1]["note"] == "ок"
        rec = store.get(rid)
        assert (rec.status, rec.decided_by, rec.decision_note) == (
            "access_granted", "root", "ок",
        )

    def test_skip_forbidden(self, tmp_path):
        """Перескок new→access_granted запрещён (workflow §6)."""
        store = _store(tmp_path)
        rid = _seed(store)
        with pytest.raises(AccessRequestError) as ei:
            _change_status(store, rid, "access_granted", actor="admin")
        assert ei.value.code == "invalid_transition"

    def test_rollback_forbidden(self, tmp_path):
        """Откат in_progress→new не предусматривается (§6)."""
        store = _store(tmp_path)
        rid = _seed(store)
        _change_status(store, rid, "in_progress", actor="admin")
        with pytest.raises(AccessRequestError) as ei:
            _change_status(store, rid, "new", actor="admin")
        assert ei.value.code == "invalid_transition"

    def test_terminal_frozen(self, tmp_path):
        """Терминальный статус без переходов."""
        store = _store(tmp_path)
        rid = _seed(store)
        _change_status(store, rid, "in_progress", actor="admin")
        _change_status(store, rid, "rejected", actor="admin")
        for target in ("new", "in_progress", "access_granted"):
            with pytest.raises(AccessRequestError):
                _change_status(store, rid, target, actor="admin")
        assert TRANSITIONS.get("rejected") is None and TRANSITIONS.get(
            "access_granted"
        ) is None


# ── API-контракт статусов (гейт в хендлере) ─────────────────


class TestStatusApi:
    def test_non_admin_403_no_mutation(self, tmp_path):
        """Не-admin → 403; заявка НЕ тронута (гейт серверный)."""
        store = _store(tmp_path)
        rid = _seed(store)
        resp = _status_impl(store, rid, "in_progress", is_admin=False, actor="eve")
        assert resp.status_code == 403
        assert b"forbidden" in resp.body
        assert store.get(rid).status == "new"
        assert [e["event"] for e in store.events(rid)] == ["created"]

    def test_admin_ok_200(self, tmp_path):
        store = _store(tmp_path)
        rid = _seed(store)
        resp = _status_impl(
            store, rid, "in_progress", is_admin=True, actor="admin", note="взял",
        )
        assert resp.status_code == 200
        assert b"in_progress" in resp.body
        assert store.get(rid).status == "in_progress"

    def test_bad_status_400(self, tmp_path):
        store = _store(tmp_path)
        rid = _seed(store)
        resp = _status_impl(store, rid, "wat", is_admin=True, actor="admin")
        assert resp.status_code == 400
        assert b"bad_status" in resp.body

    def test_unknown_id_404(self, tmp_path):
        store = _store(tmp_path)
        resp = _status_impl(store, "req_nope", "in_progress", is_admin=True, actor="a")
        assert resp.status_code == 404

    def test_invalid_transition_409(self, tmp_path):
        store = _store(tmp_path)
        rid = _seed(store)
        resp = _status_impl(store, rid, "access_granted", is_admin=True, actor="a")
        assert resp.status_code == 409
        assert b"invalid_transition" in resp.body


# ── Печать: контент-контракт (air-gap) ──────────────────────

_EXTERNAL_URL_RE = re.compile(r"https?://|//[a-z0-9.-]+/", re.IGNORECASE)


class TestPrint:
    def test_admin_200_full_content(self, tmp_path):
        store = _store(tmp_path)
        rid = _seed(store)
        _change_status(store, rid, "in_progress", actor="admin")
        resp = _print_impl(store, rid, is_admin=True)
        assert resp.status_code == 200
        html = resp.body.decode("utf-8")
        # все поля + статус + дата + история (events печатаются RU-метками)
        for needle in (
            rid, "Иван Иванов", "Отдел тестирования", "+7 900 000-00-01",
            "ivan@example.com", "Чтение базы знаний", "В работе",
            "История", "Смена статуса",
        ):
            assert needle in html, f"нет «{needle}» в печатной карточке"
        assert "<script" not in html  # чистый HTML без SPA-скриптов

    def test_media_print_and_button(self, tmp_path):
        store = _store(tmp_path)
        rid = _seed(store)
        html = render_request_card_html(store.get(rid), store.events(rid))
        assert "@media print" in html
        assert "@page" in html
        assert "window.print()" in html
        assert "no-print" in html  # кнопка не попадает на бумагу

    def test_zero_external_urls(self, tmp_path):
        """Air-gap: ни одного внешнего URL (инлайн-CSS, системные шрифты)."""
        store = _store(tmp_path)
        rid = _seed(store)
        html = render_request_card_html(store.get(rid), store.events(rid))
        assert _EXTERNAL_URL_RE.search(html) is None, "внешний URL в карточке"
        assert "http:" not in html and "https:" not in html
        assert "font-family:system-ui" in html  # системные шрифты

    def test_non_admin_403(self, tmp_path):
        """Печать без гейта — частая дыра: не-admin → 403 (id существует!)."""
        store = _store(tmp_path)
        rid = _seed(store)
        resp = _print_impl(store, rid, is_admin=False)
        assert resp.status_code == 403
        # в 403-странице нет ПДн заявки
        assert "Иван Иванов" not in resp.body.decode("utf-8")

    def test_unknown_id_404(self, tmp_path):
        store = _store(tmp_path)
        resp = _print_impl(store, "req_nope", is_admin=True)
        assert resp.status_code == 404

    def test_empty_id_404(self, tmp_path):
        store = _store(tmp_path)
        assert _print_impl(store, "", is_admin=True).status_code == 404

    def test_xss_escaped(self, tmp_path):
        """Поля заявки — публичный ввод: экранирование HTML."""
        store = _store(tmp_path)
        rid = store.append(
            fio="<script>alert(1)</script>", department="d", phone="+7",
            email="e@e.e", work_summary="<b>works</b>",
        ).id
        html = render_request_card_html(store.get(rid), store.events(rid))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


# ── Навигация: /requests скрыт не-admin ─────────────────────


class TestNav:
    def test_route_min_role_admin(self):
        from kb_console.pages import ROUTES

        entry = next(r for r in ROUTES if r[0] == "/requests")
        assert entry[3] == "admin"
        assert callable(entry[2])


class TestUserPrefillLink:
    def test_users_page_accepts_prefill_kwarg(self):
        """build_users(prefill_request_id=…) — контракт Q2 (рутинг из /requests)."""
        import inspect

        from kb_console.pages.users_page import build_users

        sig = inspect.signature(build_users)
        assert "prefill_request_id" in sig.parameters
        assert sig.parameters["prefill_request_id"].default == ""


# ── Регресс визуальной проверки 036 (дефект 1, P0): первая отрисовка ──────


@pytest.mark.filterwarnings(
    # nicegui.testing-харнесс: teardown-артефакт библиотеки (Outbox.loop),
    # не наш код — подавляем точечно, глобальный фильтр не ставим
    "ignore:coroutine 'Outbox.loop' was never awaited:RuntimeWarning",
)
class TestFirstPaint:
    """`/requests` рендерил ТОЛЬКО шапку: @ui.refreshable render() не был
    вызван ни разу — render.refresh() на никогда не рисованной функции
    оставляет страницу пустой. Харнесс nicegui.testing.User ловит класс
    «страница/refreshable не отрисованы» на уровне реального DOM-дерева.
    """

    async def test_admin_sees_counters_and_table_on_first_paint(self, tmp_path, ui_user):
        from nicegui import ui

        user = ui_user
        from kb_console.core import runtime
        from kb_console.pages.requests_page import build_requests

        store = _store(tmp_path)
        rid = _seed(store)
        _change_status(store, rid, "in_progress", actor="admin")

        runtime.REQUESTS_STORE = store
        try:
            ui.page("/t-036-requests")(build_requests)
            await user.open("/t-036-requests")
            # счётчики (render_counters) + фильтры (render)
            await user.should_see("Всего: 1")
            await user.should_see("В работе: 1")
            await user.should_see("Поиск по ФИО")
            # таблица отрисована И содержит заявку (ячейки Quasar живут
            # клиентски — проверяем rows отрисанного ui.table)
            from nicegui import ui as _ui

            tables = [
                el for el in user.client.layout.descendants()
                if isinstance(el, _ui.table)
            ]
            assert tables, "ui.table не отрисован"
            all_rows = [r for t in tables for r in t._props.get("rows", [])]
            assert any(
                r.get("fio") == "Иван Иванов" and r.get("status") == "in_progress"
                for r in all_rows
            ), f"строка заявки не в таблице: {all_rows}"
        finally:
            runtime.REQUESTS_STORE = None

    async def test_empty_store_still_renders_placeholder(self, tmp_path, ui_user):
        """Пустой стор: страница не пустая — рисуется заглушка (не шапка-одна)."""
        from nicegui import ui

        user = ui_user
        from kb_console.core import runtime
        from kb_console.pages.requests_page import build_requests

        runtime.REQUESTS_STORE = _store(tmp_path)
        try:
            ui.page("/t-036-requests-empty")(build_requests)
            await user.open("/t-036-requests-empty")
            await user.should_see("Всего: 0")
            await user.should_see("Заявок нет.")  # заглушка, не пустая страница
        finally:
            runtime.REQUESTS_STORE = None
