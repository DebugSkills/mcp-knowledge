"""S2 (Ф6-2, arch-2026-10-10-ai-ws-acceptance): /queue — витрина ws-контура.

Закреплённые решения (задание Ф6 сессия 3):
- backend страницы «Очередь» — ws-redis НАПРЯМУЮ (pages/queue.py::
  make_ws_redis, env WS_REDIS_URL; консоль НЕ ходит в MCP-сервер);
- два контура e2e:
  * legacy self-инстанс БЕЗ WS_REDIS_URL (conftest: пустая строка) →
    fail-soft баннер «ws-redis недоступен», страница не падает, 0 ошибок
    консоли (задание: «если очередь недоступна — страница не должна
    падать, детектор ошибок не должен ложно срабатывать»);
  * per-user self-инстанс С WS_REDIS_URL=redis://127.0.0.1:6390/0 —
    живой dev ws-redis (test-only overlay compose.workspace.test.yml,
    «make ws-up-test»; прецедент WS_TEST_REDIS_URL);
- «1 тест-задача» (мутация очереди): посев одной безопасной задачи
  (ключи контракта queue.py, имена e2e-s2-*) → реакция UI: строка job в
  таблице полки; UI-мутация prio-override («Применить» → high) →
  бэкенд-ответ фиксируется напрямую (GET ws:prio:{job} == "high",
  TTL > 0 — реальный контракт SET EX, SSOT ai_workspace prio.py) +
  бейдж «⚡ (job-override)» и positive-notify в UI;
- негатив «роль»: contributor видит витрину (ROUTES /queue:
  min_role contributor — pages/__init__.py), но БЕЗ admin-секции
  override (гейт can_manage_priority режет КОНТРОЛЬ, не страницу).

Уборка — fixture ws_queue (только свои ключи; ZREM своего member из
общего ZSET). Побочный эффект: одно append-only событие
job_priority_set в стриме ws:quota:events (maxlen 10k, actor=e2e-admin,
job=e2e-s2-*) — наблюдаемый контракт UI-мутации, не разрушение.

Положительный контроль детектора ошибок консоли — test_detector_control.py
(не дублируется). Проводка @ui.page("/queue") — app.py:96 (была на месте,
в отличие от /calibration — прецедент Ф6-1).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

#: Класс ошибки fail-soft баннера: make_ws_redis без WS_REDIS_URL →
#: RuntimeError (core/redis_client.py). В баннере — КЛАСС, не str(exc)
#: (redis-ошибки несут host:port — гигиена core/redis_client.py).
_FAILSOFT_ERROR_CLASS = "RuntimeError"

#: Один тик таймера страницы — 2.0 с (pages/queue.py REFRESH_SECONDS):
#: ждём чуть больше, чтобы поймать ошибки консоли цикла обновления.
_TIMER_TICK_MS = 2600


def test_s2_failsoft_banner_without_ws_redis(
    page: Page, base_url: str, login, console_errors
) -> None:
    """S2 fail-soft: очередь недоступна → баннер, страница жива, 0 ошибок.

    Legacy self-инстанс (conftest принудительно WS_REDIS_URL="") —
    операторская консоль без ws-redis, штатный кейс pages/queue.py.
    """
    login(page)
    page.goto(f"{base_url}/queue")

    # Заголовок страницы + баннер с КЛАССОМ ошибки (не host:port).
    page.get_by_text("Очередь верстака (ws-контур)").wait_for(state="visible")
    page.get_by_text("ws-redis недоступен", exact=False).wait_for(state="visible")
    body = page.inner_text("body")
    assert _FAILSOFT_ERROR_CLASS in body, body[:400]

    # Fail-soft = ранний выход _render: витрина полок НЕ рендерится.
    assert "Полка «local»" not in body, body[:400]

    # Тик таймера (2 с): страница продолжает попытки, ошибок консоли нет —
    # детектор не срабатывает ложно на живую fail-soft ветку.
    page.wait_for_timeout(_TIMER_TICK_MS)
    assert console_errors.errors == [], console_errors.summary()


def test_s2_admin_readonly_live(console_as_admin, ws_queue, console_errors) -> None:
    """S2 read-only (живой ws-redis): admin открывает /queue — витрина
    рендерится (полки/метрики), 0 ошибок консоли (в т.ч. после тика
    таймера обновления)."""
    page: Page = console_as_admin.page
    page.goto(f"{console_as_admin.url}/queue")

    page.get_by_text("Очередь верстака (ws-контур)").wait_for(state="visible")
    # Контурные полки показываются всегда (BASE_SHELVES queue.py).
    for shelf in ("local", "ext"):
        page.get_by_text(f"Полка «{shelf}»", exact=False).wait_for(state="visible")
    # Блок метрик Ф6 4б (К4) — рендерится в безошибочной ветке (fail-soft
    # метрик — отдельная ветка, здесь только видимость карточки).
    page.get_by_text("Метрики узлов ws-контура (ws:metrics)", exact=False).wait_for(
        state="visible"
    )

    # Один тик таймера: пере-рендер по websocket без ошибок консоли.
    page.wait_for_timeout(_TIMER_TICK_MS)
    assert console_errors.errors == [], console_errors.summary()


def test_s2_test_task_visible_and_priority_mutation(
    console_as_admin, ws_queue, console_errors
) -> None:
    """S2 «1 тест-задача»: посев → появление в таблице; UI-мутация
    prio-override → positive-notify + бейдж; бэкенд-ответ зафиксирован
    напрямую (SET ws:prio:{job} EX — реальный контракт)."""
    page: Page = console_as_admin.page
    task = ws_queue.seed_test_task()

    page.goto(f"{console_as_admin.url}/queue")
    page.get_by_text("Очередь верстака (ws-контур)").wait_for(state="visible")

    # Реакция UI на тест-задачу: строка job в таблице полки (первый сбор
    # синхронный — но даём запас на websocket-хендшейк). admin-контур:
    # имя job видно ДВАЖДЫ (ячейка таблицы + label строки контролей) —
    # поэтому .first (strict mode PW).
    page.get_by_text(task.job, exact=False).first.wait_for(
        state="visible", timeout=8000
    )

    # До мутации override отсутствует (бэкенд-факт, не UI-предположение).
    assert ws_queue.get_prio_override(task) is None

    # Admin-секция контроля (Ф4.5b): заголовок + строка контролей job'а.
    page.get_by_text("Override приоритета job (только admin)").wait_for(state="visible")
    row = page.locator("div.row").filter(has_text=task.job).last
    row.locator(".q-select").click()
    page.locator(".q-menu .q-item", has_text="high").first.click()
    row.get_by_role("button", name="Применить").click()

    # Реакция UI: positive-notify (обработчик _apply_priority_from_ui)
    # и пере-рендер таблицы с бейджем job-override.
    page.get_by_text(f"Приоритет {task.job} → high", exact=False).wait_for(
        state="visible", timeout=8000
    )
    page.get_by_text("high ⚡ (job-override)", exact=False).wait_for(
        state="visible", timeout=8000
    )

    # Бэкенд-ответ мутации (реальный контракт prio.py: SET EX):
    # значение + живой TTL (24 ч, продлевается установкой).
    assert ws_queue.get_prio_override(task) == "high"
    assert ws_queue.prio_override_ttl(task) > 0

    # Тик таймера после мутации — страница стабильна, 0 ошибок консоли.
    page.wait_for_timeout(_TIMER_TICK_MS)
    assert console_errors.errors == [], console_errors.summary()


def test_s2_negative_contributor_readonly_no_controls(
    console_as_contributor, ws_queue, console_errors
) -> None:
    """S2 негатив «роль»: contributor видит витрину очереди (min_role
    contributor), но admin-секция override приоритета НЕ рендерится."""
    page: Page = console_as_contributor.page
    task = ws_queue.seed_test_task()

    page.goto(f"{console_as_contributor.url}/queue")
    page.get_by_text("Очередь верстака (ws-контур)").wait_for(state="visible")

    # Витрина доступна contributor'у: тест-задача видна в таблице.
    page.get_by_text(task.job, exact=False).wait_for(state="visible", timeout=8000)

    # Гейт на КОНТРОЛЕ (can_manage_priority → admin): ни секции,
    # ни кнопок мутации на странице нет.
    body = page.inner_text("body")
    assert "Override приоритета job" not in body, body[:400]
    assert page.locator("button", has_text="Применить").count() == 0
    assert page.locator("button", has_text="Сбросить").count() == 0

    page.wait_for_timeout(_TIMER_TICK_MS)
    assert console_errors.errors == [], console_errors.summary()
