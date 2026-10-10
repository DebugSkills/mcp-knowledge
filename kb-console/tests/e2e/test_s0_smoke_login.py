"""S0 (smoke-login, Ф6): / → /login → форма → /status → logout → /login.

Полный happy-path контура CONSOLE_AUTH=required на живом dev-стеке.
Требование «0 ошибок консоли» держат: явный assert (шаг 4) + teardown-гейт
фикстуры console_errors.
"""

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]


def test_s0_smoke_login(
    page: Page, base_url: str, login, console_errors
) -> None:
    # 1) Без сессии «/» редиректит на /login (middleware, CONSOLE_AUTH=required).
    page.goto(f"{base_url}/")
    page.wait_for_url("**/login**")
    assert "/login" in page.url, page.url

    # 2) DOM-контракт /login (src/kb_console/login_page.py): форма #lf + #f-pass.
    assert page.locator("#lf").count() == 1
    assert page.locator("#f-pass").count() == 1

    # 3) Логин реальной формой → аутентифицированная страница (fallback /status).
    login(page)
    assert page.url.rstrip("/").endswith("/status"), page.url
    page.get_by_text("Статус MCP Knowledge Server").wait_for(state="visible")

    # 4) Happy-path: 0 ошибок консоли (явный дубль teardown-гейта).
    assert console_errors.errors == [], console_errors.summary()

    # 5) Logout (контракт: POST /api/logout → {"ok": True}) → /status ведёт на /login.
    resp = page.request.post(f"{base_url}/api/logout")
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    page.goto(f"{base_url}/status")
    page.wait_for_url("**/login**")
