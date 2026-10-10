"""S3 (Ф6-1, arch-2026-10-10-ai-ws-acceptance): /calibration — read-only.

Закреплённые решения (задание Ф6 сессия 2):
- таргет — self-инстанс kb-console (вариант A): users-стор admin+contributor,
  секретов оператора нет; данные карточек — с живого admin-API :8700
  (CALIB_API_KEY из окружения раннера; недоступность API = fail-soft);
- S3 (admin): страница рендерится (карточки модель/GPU, история отчётов),
  0 ошибок консоли;
- негатив 1 «роль» (contributor): /calibration скрыт из навигации
  (ROUTES-гейт components/header.py) и прямой URL рендерит 403-заглушку
  (runtime-гейт pages/calibration.py::build_calibration);
- негатив 2 «leak-whitelist» (admin): в тексте страницы только whitelist
  _REPORT_FIELDS (якорь — ai_workspace/calibration/admin_api.py, НЕ копия);
  утечек нет: абсолютные пути, тексты прогона (output/text/answer),
  X-Calib-Key/секреты — отсутствуют.

Положительный контроль детектора ошибок консоли — test_detector_control.py
(не дублируется). Проводку /calibration (@ui.page в app.py) чинит та же
сессия Ф6-1: до неё роут отдавал 404 при видимой кнопке навигации.
"""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

# ── Якорь whitelist: РЕАЛЬНЫЙ контракт admin-API (не копия в тесте) ──
from ai_workspace.calibration.admin_api import _REPORT_FIELDS

#: Защищаемый минимум (задание Ф6-1): выпадение любого из этих полей из
#: контракта admin-API обязано ронять e2e (сжатие whitelist = регрессия).
_MANDATED_FIELDS: frozenset[str] = frozenset({
    "golden_median_score", "needle_rate", "rub", "wall_s", "heldout_score",
    "parse_rate", "golden_dispersion", "n_runs", "flags", "run_id",
    "model_id", "digest",
})

#: Отображения чипов/подписей метрик → поля whitelist (источник:
#: pages/calibration.py::_render_report_metrics, строки ~577-617).
#: Значения обязаны оставаться ⊆ реального _REPORT_FIELDS — иначе рендер
#: выехал за контракт (дрейф UI ↔ admin-API).
_LABEL_TO_FIELD: dict[str, str] = {
    "score": "golden_median_score",
    "needle_rate": "needle_rate",
    "ceiling": "flags",
    "wall": "wall_s",
    "heldout": "heldout_score",
    "parse": "parse_rate",
    "disp": "golden_dispersion",
    "n_runs": "n_runs",
    "run_id": "run_id",
    "model": "model_id",
    "digest": "digest",
    "rub": "rub",
}

#: Абсолютные пути хоста/контейнера (примеры задания: /kvm/, /app/).
_ABS_PATH_RE = re.compile(
    r"(?<![\w.\-/])(?:/kvm|/app|/home|/root|/opt|/var|/usr|/tmp|/etc)"
    r"(?:/[\w.\-]+)+"
)

#: Латинские маркеры полей текстов прогона (output/text/answer) — такие
#: ключи в отчётах несут сырые тексты LLM; на странице их быть не должно.
_RUN_TEXT_RE = re.compile(r"\b(?:output|answer|text)\b", re.IGNORECASE)

#: Секрет-маркеры: имя заголовка/переменной ключа admin-API + формат хэшей.
_SECRET_MARKERS: tuple[str, ...] = ("X-Calib-Key", "x-calib-key",
                                    "CALIB_API_KEY", "pbkdf2$")


def _assert_whitelist_anchor() -> None:
    """Якорь-контроль: отображения и защищаемый минимум ⊆ контракт API."""
    anchor = set(_REPORT_FIELDS)
    shrink = _MANDATED_FIELDS - anchor
    assert not shrink, f"контракт _REPORT_FIELDS сжался: {sorted(shrink)}"
    drift = set(_LABEL_TO_FIELD.values()) - anchor
    assert not drift, f"рендер метрик выехал за whitelist: {sorted(drift)}"


def _assert_no_leak(body: str, session_password: str) -> None:
    """Негатив 2: в тексте страницы нет утечек (анти-вакуумный).

    Анти-вакуумность: сперва маркеры рендера (проверяем РЕАЛЬНЫЙ текст
    страницы, а не пустую строку), затем — отсутствия.
    """
    for marker in ("Калибровка системы под текущую модель",
                   "История калибровок"):
        assert marker in body, f"анти-вакуумность: маркер рендера {marker!r} не найден"

    m = _ABS_PATH_RE.search(body)
    assert m is None, f"утечка абсолютного пути: {m.group(0)!r}"

    m = _RUN_TEXT_RE.search(body)
    assert m is None, f"утечка текстов прогона (поле {m.group(0)!r})"

    for marker in _SECRET_MARKERS:
        assert marker not in body, f"утечка секрет-маркера: {marker!r}"

    key = os.environ.get("CALIB_API_KEY", "")
    if key:  # значение НЕ печатаем ни в ассерте, ни в лог
        assert key not in body, "утечка значения CALIB_API_KEY"

    assert session_password not in body, "утечка тестового пароля"


def _table_headers(page: Page) -> list[str]:
    """Заголовки таблицы истории (если построена; fail-soft — может не быть)."""
    headers = page.locator("thead th")
    return [headers.nth(i).inner_text() for i in range(headers.count())]


def test_s3_calibration_admin_readonly(console_as_admin, console_errors) -> None:
    """S3: admin — read-only просмотр, карточки/история рендерятся, 0 ошибок.

    Никаких запусков probe/пар/approve — только загрузка страницы (read-only).
    """
    page: Page = console_as_admin.page
    page.goto(f"{console_as_admin.url}/calibration")
    page.get_by_text("Калибровка системы под текущую модель").wait_for(
        state="visible"
    )
    # Карточки + панель отчётов (заголовки секций — из build_calibration).
    for marker in ("Текущая модель", "GPU / VRAM", "История калибровок"):
        page.get_by_text(marker, exact=True).wait_for(state="visible")

    # Данные карточек приходят асинхронно (ui.timer 0.1s → admin-API :8700,
    # timeout 15s): ждём ЛИБО факт полки, ЛИБО fail-soft ветку — обе валидны.
    try:
        page.get_by_text("Полка:", exact=False).wait_for(state="visible", timeout=8000)
    except Exception:  # noqa: BLE001, S110 — fail-soft по заданию
        pass

    # 0 ошибок консоли — явный дубль teardown-гейта console_errors.
    assert console_errors.errors == [], console_errors.summary()


def test_s3_negative_role_contributor_hidden_and_403(
    console_as_contributor, console_errors
) -> None:
    """Негатив 1: contributor — навигация скрыта, прямой URL → 403-заглушка."""
    page: Page = console_as_contributor.page
    # После логина ожидаем аутентифицированную страницу (fallback /status).
    page.wait_for_url(lambda u: "/login" not in u)
    assert page.url.rstrip("/").endswith("/status"), page.url

    # ROUTES-гейт (components/header.py): пункта «Калибровка» в навигации нет.
    assert page.locator("button", has_text="Калибровка").count() == 0

    # Прямой URL: runtime-гейт build_calibration → 403-заглушка (не контент).
    page.goto(f"{console_as_contributor.url}/calibration")
    page.get_by_text("403", exact=False).wait_for(state="visible")
    body = page.inner_text("body")
    assert "калибровка доступна только администраторам" in body, body[:400]
    # Admin-контент не рендерится (гейт сработал ДО сборки страницы):
    assert "Калибровка системы под текущую модель" not in body
    assert "История калибровок" not in body
    # Навигация скрыта и на самой странице 403.
    assert page.locator("button", has_text="Калибровка").count() == 0

    assert console_errors.errors == [], console_errors.summary()


def test_s3_negative_leak_whitelist_admin(console_as_admin, console_errors) -> None:
    """Негатив 2: текст admin-страницы — только whitelist-метрики, без утечек."""
    _assert_whitelist_anchor()

    page: Page = console_as_admin.page
    page.goto(f"{console_as_admin.url}/calibration")
    page.get_by_text("Калибровка системы под текущую модель").wait_for(
        state="visible"
    )
    # Даём fail-soft/данным отработать (механика как в S3-сценарии выше).
    try:
        page.get_by_text("Полка:", exact=False).wait_for(state="visible", timeout=8000)
    except Exception:  # noqa: BLE001, S110 — fail-soft
        pass

    body = page.inner_text("body")
    _assert_no_leak(body, console_as_admin.password)

    # Whitelist-контроль таблицы истории: заголовки колонок (если таблица
    # построена — реальный ответ /calib/reports) ⊆ {ts} ∪ _REPORT_FIELDS.
    headers = _table_headers(page)
    if headers:  # «нет отчётов»/API недоступен → таблицы нет (fail-soft)
        allowed = {"ts", *_REPORT_FIELDS}
        extra = set(headers) - allowed
        assert not extra, f"колонки истории вне whitelist: {sorted(extra)}"

    assert console_errors.errors == [], console_errors.summary()
