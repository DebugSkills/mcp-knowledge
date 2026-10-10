"""Positive-control сборщика ошибок консоли (Ф6): детектор обязан ловить.

Доказывает два свойства фикстуры console_errors:
  1) не вакуумный — инъекция console.error/pageerror ПОПАДАЕТ в список;
  2) не инертный/не насыщенный — ДО инъекции список пуст (S0: пуст и после).
Ожидаемые ошибки снимаются mark_expected() → teardown-гейт остаётся зелёным.
"""

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]


def test_detector_positive_control(
    page: Page, base_url: str, console_errors
) -> None:
    page.goto(f"{base_url}/login")
    page.wait_for_url("**/login**")

    # До инъекции детектор пуст (чистая статическая страница /login).
    assert console_errors.errors == [], console_errors.summary()

    # Инъекция обоих каналов: console.error + необработанное исключение.
    page.evaluate("console.error('detector-control: console.error boom')")
    page.evaluate(
        "setTimeout(() => { throw new Error('detector-control: pageerror boom') }, 0)"
    )

    console_errors.wait_for(minimum=2, timeout=5.0)
    joined = console_errors.summary()
    assert any("console.error boom" in e.detail for e in console_errors.errors), joined
    assert any("pageerror boom" in e.detail for e in console_errors.errors), joined

    # Ошибки ожидаемые (контроль) — снимаем: teardown-гейт должен остаться зелёным.
    console_errors.mark_expected()
    assert console_errors.errors == []
