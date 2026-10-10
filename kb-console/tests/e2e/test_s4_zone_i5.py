"""S4 (Ф6-3, arch-2026-10-10-ai-ws-acceptance): зонный гейт I5 — read-only.

Инвариант I5 (политика D7 «private admin-only v1», core/ws_zone.py): роль
без private-зоны НЕ видит private-контент. Исходная форма S4 портфолио
(«subscriber → private») в UI неисполнима — subscriber уровень ТОКЕНА
(tokens.py LEVEL_CODE), не роль консоли (iter2/iter3 портфолио, W1);
исполнимая форма (задание Ф6 сессии 4): contributor — реальная роль
без private-зоны (WS_ZONE_BY_ROLE) — не получает private-контент, admin
(контроль) — получает.

Слои проверки (все read-only, мутаций нет):

1. UI-гейт /chat (browser, per-user self-инстанс):
   - contributor: upload-виджет private-зоны НЕ создаётся вовсе
     (components/attach_upload.py — гейт attach_allowed(role) поверх
     ws_zone.zone_for_role; «виджет просто не создаётся у non-admin»),
     вместо него заглушка-подпись; зонный бейдж прозрачности I5
     («зона выборки», pages/chat.py::_zone_caption) = public;
   - admin (контроль): виджет есть, бейдж = private.
2. Серверный канал core/ws_attach.attach_to_kb (Python, БЕЗ MCP):
   попытка private-действия ролью без private-зоны → AttachError 403
   ДО любого MCP-вызова (сентинел-клиент); admin роль-гейт проходит —
   отказ приходит со следующего гейта (415, тип файла), т.е. гейт
   ДИСКРИМИНИРУЕТ роли (negative-control метрики).
3. zone-UI книг (pages/books.py::_apply_zone_ui): зона бейджа книги —
   из серверных данных по роль-ключу mcp_api_key() (identity.py);
   self-инстанс не получает MCP-ключей → список обычно не строится
   (fail-soft баннер). Условная проверка (прецедент S3 _table_headers):
   ЕСЛИ список построен (внешний контур KB_CONSOLE_URL с ключами) —
   у contributor на странице нет 🔒 private-бейджей.

Ожидания строятся из РЕАЛЬНЫХ функций (_zone_caption, zone_for_role,
attach_allowed, WS_ZONE_BY_ROLE) — не хардкодом в тесте.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

# ── Реальные контракты (якоря импортом, НЕ копии в тесте) ──────────
from kb_console.components.attach_upload import attach_allowed
from kb_console.core.ws_attach import AttachError, attach_to_kb
from kb_console.core.ws_zone import PRIVATE_ZONE, WS_ZONE_BY_ROLE, zone_for_role
from kb_console.pages.chat import _zone_caption

#: Подпись upload-виджета и заглушки — из components/attach_upload.py
#: (стабильные page-маркеры, паттерн S2 «Очередь верстака»).
_UPLOAD_LABEL = "Прикрепить файл в базу знаний"
_STUB_LABEL = "Вложения доступны только администратору"

#: Маркеры терминальных состояний списка книг (pages/books.py::_show_list).
_BOOKS_LIST_BUILT = "Найдено книг:"
_BOOKS_EMPTY = "Книг пока нет"
_BOOKS_ERROR = "Ошибка загрузки списка книг"


class _McpSentinel:
    """Сентинел MCP-клиента: любое касание = провал теста.

    Гейт роли обязан срабатывать ДО использования mcp_client (ws_attach:
    «гейт стоит ДО использования mcp_client — сервисный ключ не обход»).
    Если канал тронул сентинел — порядок гейтов нарушен.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"MCP-клиент использован до гейта роли: .{name}()")


def _attach_outcome(**kwargs: Any) -> dict[str, Any]:
    """Прогнать attach_to_kb в отдельном потоке со СВОИМ event loop.

    pytest-playwright держит event loop в главном потоке сессии —
    asyncio.run() там запрещён («cannot be called from a running event
    loop»). Отдельный поток даёт чистый loop; исход транспортируется
    словарём: result (успех) | attach_error (AttachError) | unexpected
    (любое иное исключение — например, касание сентинела).
    """
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = asyncio.run(attach_to_kb(**kwargs))
        except AttachError as exc:
            outcome["attach_error"] = exc
        except BaseException as exc:  # noqa: BLE001 — транспорт исхода из потока
            outcome["unexpected"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "attach_to_kb завис (>10s) — гейт не сработал?"
    return outcome


def test_s4_contributor_private_invisible_chat(
    console_as_contributor, console_errors
) -> None:
    """S4 негатив: contributor — ни зонного бейджа private, ни виджета."""
    page: Page = console_as_contributor.page
    role = console_as_contributor.role

    # Анти-вакуум роли фикстуры: эта роль ДЕЙСТВИТЕЛЬНО без private-зоны
    # (реальный SSOT-резолвер; если маппинг сменится — тест честно уйдёт
    # в admin-ветку и падением на отрицаниях ниже).
    assert zone_for_role(role) != PRIVATE_ZONE, f"роль {role} получила private"
    assert attach_allowed(role) is False, f"attach_allowed({role!r}) должен быть False"

    page.goto(f"{console_as_contributor.url}/chat")
    # Зонный бейдж I5 (реальный форматтер + реальный резолвер):
    # «Роль: contributor · зона выборки: public» — private в бейдже НЕТ.
    page.get_by_text(_zone_caption(role, zone_for_role(role)), exact=False).wait_for(
        state="visible"
    )

    # Upload-виджет private-зоны НЕ создаётся (гейт attach_allowed):
    # ни Quasar-аплоадера, ни подписи виджета.
    assert page.locator(".q-uploader").count() == 0, "upload-виджет у non-admin создан"
    assert page.get_by_text(_UPLOAD_LABEL, exact=False).count() == 0

    # Вместо виджета — заглушка (текст из attach_upload.py, defense-in-depth
    # к СЕРВЕРНОМУ гейту 403, который остаётся авторитетным).
    page.get_by_text(_STUB_LABEL, exact=False).wait_for(state="visible")

    assert console_errors.errors == [], console_errors.summary()


def test_s4_admin_private_visible_control_chat(
    console_as_admin, console_errors
) -> None:
    """S4 контроль: admin — зонный бейдж private, upload-виджет создан."""
    page: Page = console_as_admin.page
    role = console_as_admin.role

    assert zone_for_role(role) == PRIVATE_ZONE, f"роль {role} без private"
    assert attach_allowed(role) is True

    page.goto(f"{console_as_admin.url}/chat")
    page.get_by_text(_zone_caption(role, zone_for_role(role)), exact=False).wait_for(
        state="visible"
    )
    # Виджет private-зоны создан: подпись + Quasar-аплоадер.
    page.get_by_text(f"{_UPLOAD_LABEL} (private, .md/.txt)", exact=False).wait_for(
        state="visible"
    )
    assert page.locator(".q-uploader").count() >= 1, "upload-виджет у admin не создан"

    assert console_errors.errors == [], console_errors.summary()


def test_s4_server_channel_private_action_403() -> None:
    """S4 серверный негатив: private-действие ролью без private → 403 (до MCP).

    Data-driven по SSOT WS_ZONE_BY_ROLE: каждая роль с базовой зоной ≠
    private (плюс None — fail-closed ветка «неизвестная роль → public»)
    получает 403 на ВАЛИДНОМ вложении — единственная причина отказа =
    роль, эскалации привилегий нет. Admin-контроль: роль-гейт пройден —
    отказ со СЛЕДУЮЩЕГО гейта (тип файла → 415, не 403): гейт не инертен
    и не насыщен (разные роли → разные исходы).
    """
    non_admin = [r for r, z in WS_ZONE_BY_ROLE.items() if z != PRIVATE_ZONE]
    assert non_admin, "в SSOT нет ролей без private — проверка выродилась"
    # Fail-closed: None/неизвестная роль → public → тоже 403 (D7/I6).
    roles: list[str | None] = [*non_admin, None]

    for role in roles:
        outcome = _attach_outcome(
            filename="e2e-s4.md",
            raw=b"# e2e s4\n",
            role=role,
            mcp_client=_McpSentinel(),
        )
        err = outcome.get("attach_error")
        assert isinstance(err, AttachError), (
            f"role={role!r}: ожидаем AttachError, исход={outcome!r}"
        )
        assert err.code == 403, f"role={role!r}: {err.code} != 403"

    # Admin проходит роль-гейт (доказательство — иной класс отказа).
    outcome = _attach_outcome(
        filename="e2e-s4.exe",
        raw=b"binary",
        role="admin",
        mcp_client=_McpSentinel(),
    )
    err = outcome.get("attach_error")
    assert isinstance(err, AttachError), (
        f"admin: ожидаем AttachError, исход={outcome!r}"
    )
    assert err.code == 415, err.code


def test_s4_books_zone_ui_no_private_badges_contributor(
    console_as_contributor, console_errors
) -> None:
    """S4 zone-UI книг: у contributor нет 🔒 private-бейджей (условная).

    Зона бейджа книги (pages/books.py::_apply_zone_ui ← entry['zone'])
    приходит сервером по роль-ключу mcp_api_key(); self-инстанс без
    MCP-ключей → fail-soft баннер (список не строится). Проверка условная
    по терминальному состоянию списка (прецедент S3 _table_headers):
    построен (внешний контур KB_CONSOLE_URL) → private-бейджей быть не
    должно. В обоих исходах — 0 ошибок консоли (страница жива).
    """
    page: Page = console_as_contributor.page
    page.goto(f"{console_as_contributor.url}/books")

    # Терминальное состояние списка: построен | пуст | fail-soft баннер.
    terminal = (
        page.get_by_text(_BOOKS_LIST_BUILT, exact=False)
        .or_(page.get_by_text(_BOOKS_EMPTY, exact=False))
        .or_(page.get_by_text(_BOOKS_ERROR, exact=False))
    )
    terminal.first.wait_for(state="visible", timeout=15_000)

    body = page.inner_text("body")
    if _BOOKS_LIST_BUILT in body:
        # Зонные бейджи карточек («🌍 public» / «🔒 private», books.py) —
        # у роли без private-зоны private-бейдж не рендерится.
        assert "🔒 private" not in body, body[:600]

    assert console_errors.errors == [], console_errors.summary()
