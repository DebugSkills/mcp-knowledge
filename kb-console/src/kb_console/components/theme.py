"""SSOT тёмно-зелёной светлой темы kb-console (037, вариант C).

Один источник палитры для двух миров:
- static /login (рукописный HTML вне NiceGUI-рантайма) — ``login_css()``;
- NiceGUI/Quasar-страницы консоли — ``app.colors(**quasar_brand())`` +
  ``console_css()`` через ``ui.add_head_html(..., shared=True)``.

``shared=True`` обязателен: без него head-HTML достаётся только auto-index
client, а реальные ``@ui.page`` его не получают (L2-опыт, add-body-html).

Анимированный градиент (Q2/Q3): ``background-size`` 200-220% + keyframes по
``background-position``, 26-30s ``ease-in-out infinite alternate`` — медленное
«дыхание», а не мигание (vestibular-безопасность: полный цикл >15s).
``prefers-reduced-motion: reduce`` отключает анимацию.

Почему фон консоли — ``body::before``, а не элемент внутри q-page-container:
по L2-опыту (task-2026-06-16-frontend-audit-001) Quasar может переключать
``will-change: transform`` у контейнера, пересоздавая compositing-layer и
сбрасывая CSS-анимации дочерних элементов. На установленном Quasar механика
НЕ воспроизведена (критика 037 P2-1 — гипотеза, не факт); ``body::before``
лежит вне контейнера и от неё не зависит. Запасной вариант (не понадобился):
``.q-page-container{will-change:auto!important}``.

Печать: ``@media print`` убирает градиент и полосу шапки — бумага белая.
Печатная карточка заявок — отдельный носитель
``pages/requests_print.py::_CARD_CSS``, здесь не затрагивается.

Air-gap: только system-font stack и inline-CSS, 0 внешних URL
(контракт — tests/test_theme.py).

Контраст: все текстовые пары PALETTE ≥4.5:1, focus-ring ≥3:1 (WCAG 1.4.11);
матрица пар с посчитанным контрастом — в тесте. focus-ring = #15803d
(4.63:1 на page_bg), НЕ #86efac (1.3-1.4:1 — критика 037 P1-1).
"""

from __future__ import annotations

from string import Template

from nicegui import ui

# ── Единственный источник hex-цветов ────────────────────────

PALETTE: dict[str, str] = {
    # светлые поверхности
    "page_bg": "#f2f7f4",  # фон /login (едва зелёноватый белый)
    "surface": "#ffffff",  # карточка login, панели консоли, печать
    # текст на светлом
    "text_main": "#1f2937",  # основной текст body
    "text_area": "#374151",  # textarea / подписи .fld
    "text_muted": "#6b7280",  # .hint/.req-note/.consent-text/неактивный таб
    "text_soft": "#4b5563",  # .copy — подпись в футере (на page_bg 6.99:1)
    "text_toggle": "#546e7a",  # .toggle «показать пароль»
    # бренд — тёмно-зелёный (план §3.2)
    "brand_900": "#0b3d2e",  # нижний стоп градиента login / полоса шапки
    "brand_800": "#0f5132",  # primary: кнопки, активные вкладки, focus-border
    "brand_700": "#14532d",  # hover primary
    "brand_600": "#166534",  # accent-текст, активный таб login, верхний стоп
    "secondary": "#15803d",  # Quasar secondary, focus-ring, .ok
    # текст/акценты на тёмной бренд-панели login (worst-стоп = brand_600)
    "text_on_dark": "#f0fdf4",  # .brand h1 (11.66:1 на brand_900)
    "text_on_dark_soft": "#bbf7d0",  # .brand p (5.88:1 на brand_600)
    "text_on_dark_muted": "#d1fae5",  # .brand ul (6.29:1 на brand_600)
    "accent_on_dark": "#a7f3d0",  # ✓-галочки .brand li (5.56:1 на brand_600)
    # функциональные
    "focus_ring": "#15803d",  # outline инпутов (4.63:1 на page_bg; P1-1)
    "ghost_bg": "#ecfdf5",  # .btn.ghost фон
    "on_brand": "#ffffff",  # текст на primary-кнопках
    "ok": "#15803d",  # успех (семантика, была и осталась зелёной)
    "err": "#c62828",  # ошибки (семантика, не меняется)
    "input_border": "#cbd5e1",  # бордер инпутов/textarea
    "border_light": "#e5e7eb",  # разделитель табов
    # деликатный градиент-фон консоли (контраст между стопами <1.15:1)
    "console_wash_min": "#f4f8f5",
    "console_wash_mid": "#e9f2ec",
    "console_wash_max": "#dfece4",
}

# Семантические цвета Quasar НЕ перекрашиваются (инвариант: success/warn/error
# статусов качества/токенов/импорта). Значения — дефолты NiceGUI.
QUASAR_SEMANTIC_DEFAULTS: dict[str, str] = {
    "positive": "#21ba45",
    "negative": "#c10015",
    "info": "#31ccec",
    "warning": "#f2c037",
}


def quasar_brand() -> dict[str, str]:
    """Полный набор brand-ключей для ``app.colors()`` (NiceGUI >= 3.6).

    primary/secondary/accent — из PALETTE; семантика и dark-режим — дефолты
    Quasar (dark mode в консоли нет и не вводится).
    """
    return {
        "primary": PALETTE["brand_800"],
        "secondary": PALETTE["secondary"],
        "accent": PALETTE["brand_600"],
        "dark": "#1d1d1d",
        "dark_page": "#121212",
        **QUASAR_SEMANTIC_DEFAULTS,
    }


def _rgb_triple(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _card_shadow() -> str:
    """Тень карточки login, тонированная brand_900 (производная, не hex)."""
    r, g, b = _rgb_triple(PALETTE["brand_900"])
    return f"rgba({r},{g},{b},.14)"


# ── /login: насыщенный анимированный градиент ────────────────
# Структура побайтово повторяет прежний CSS login_page.py (структурные
# подстроки — контракт test_login_page.py); заменены только цвета,
# добавлены background-size/animation/reduced-motion. Мёртвые пустые
# строки прежнего литерала вычищены (критика 037 P2-4).

_LOGIN_CSS = Template(
    "*{box-sizing:border-box;margin:0}"
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "min-height:100vh;display:flex;flex-direction:column;"
    "background:$page_bg;color:$text_main}"
    ".split{display:flex;width:100%;flex:1}"
    ".brand{flex:1 1 46%;background:linear-gradient(160deg,$brand_900 0%,"
    "$brand_800 55%,$brand_600 130%);background-size:220% 220%;"
    "animation:kb-shift 26s ease-in-out infinite alternate;"
    "color:$text_on_dark;display:flex;flex-direction:column;"
    "justify-content:center;padding:64px;gap:14px}"
    ".brand h1{font-size:42px;letter-spacing:-.5px}.brand .logo{font-size:52px}"
    ".brand p{color:$text_on_dark_soft;line-height:1.55;max-width:44ch}"
    ".brand ul{list-style:none;margin-top:18px;display:flex;"
    "flex-direction:column;gap:10px;color:$text_on_dark_muted;font-size:15px}"
    ".brand li:before{content:'✓  ';color:$accent_on_dark;font-weight:700}"
    ".pane{flex:1 1 54%;display:flex;align-items:center;justify-content:center;padding:36px}"
    ".card{width:100%;max-width:460px;background:$surface;border-radius:18px;"
    f"box-shadow:0 12px 40px {_card_shadow()};padding:34px 34px 28px}}"
    ".tabs input[type=radio]{position:absolute;opacity:0;pointer-events:none}"
    ".tablabels{display:flex;gap:6px;margin-bottom:22px;"
    "border-bottom:1px solid $border_light}"
    ".tablabels label{flex:1;text-align:center;padding:10px 6px;cursor:pointer;"
    "font-weight:600;color:$text_muted;border-bottom:2px solid transparent;"
    "transition:color .15s,border-color .15s}"
    "#tab-login:checked~.tablabels label[for=tab-login],"
    "#tab-req:checked~.tablabels label[for=tab-req]{color:$brand_600;"
    "border-bottom-color:$brand_600}"
    ".panels section{display:none}#tab-login:checked~.panels #p-login,"
    "#tab-req:checked~.panels #p-req{display:block;animation:fade .18s ease-in}"
    "@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}"
    "input[type=text],input[type=password]{width:100%;padding:12px 14px;"
    "margin:8px 0;border:1px solid $input_border;border-radius:10px;font-size:15px}"
    "input:focus{outline:2px solid $focus_ring;border-color:$brand_800}"
    ".toggle{display:flex;align-items:center;gap:8px;font-size:14px;"
    "color:$text_toggle;margin:6px 0 4px;cursor:pointer}"
    ".btn{display:inline-flex;align-items:center;justify-content:center;"
    "width:100%;padding:12px 18px;margin-top:14px;border:0;border-radius:10px;"
    "background:$brand_800;color:$on_brand;font-size:15px;font-weight:600;"
    "cursor:pointer;transition:background .15s;text-decoration:none}"
    ".btn:hover{background:$brand_700}.btn.ghost{background:$ghost_bg;"
    "color:$brand_600}"
    ".err{color:$err;min-height:22px;font-size:14px;margin-top:10px}"
    ".hint{font-size:13.5px;color:$text_muted;margin:10px 0 2px}"
    "textarea{width:100%;min-height:190px;padding:12px;"
    "border:1px solid $input_border;border-radius:10px;"
    "font-family:ui-monospace,Consolas,monospace;font-size:13px;"
    "resize:vertical;color:$text_area}"
    ".channels{display:flex;flex-direction:column;gap:8px;margin-top:6px}"
    ".req-note{font-size:13.5px;color:$text_muted;margin:12px 0 8px;"
    "line-height:1.5}"
    ".fld{display:block;font-size:13.5px;color:$text_area;margin:10px 0 2px}"
    ".fld input,.fld textarea{width:100%;padding:12px 14px;margin-top:4px;"
    "border:1px solid $input_border;border-radius:10px;font-size:15px;"
    "font-family:inherit;resize:vertical}"
    ".fld input:focus,.fld textarea:focus{outline:2px solid $focus_ring;"
    "border-color:$brand_800}"
    ".consent-text{font-size:13px;color:$text_muted;line-height:1.45}"
    ".ok{color:$ok;min-height:20px;font-size:14px;margin-top:10px;"
    "line-height:1.5}"
    ".copy{margin:12px 0 18px;text-align:center;white-space:nowrap;"
    "font-size:12.5px;color:$text_soft}"
    "@media(max-width:860px){.split{flex-direction:column}.brand{padding:34px;"
    "flex-basis:auto}.brand h1{font-size:30px}.brand ul{display:none}}"
    "@keyframes kb-shift{from{background-position:0% 0%}"
    "to{background-position:100% 100%}}"
    "@media(prefers-reduced-motion:reduce){.brand{animation:none;"
    "background-position:0% 0%}}"
)

# ── консоль: деликатный фон-«дыхание» + полоса шапки ─────────
# Фон — body::before ВНЕ q-page-container (см. докстринг модуля).
# Полоса шапки — статичный градиент (анимация внутри контейнера
# не требуется; гипотеза will-change — P2-1). .kb-header вешается
# классом в render_header (критика 037 P2-2), не структурным селектором.

_CONSOLE_CSS = Template(
    ":root{--kb-primary:$brand_800;--kb-secondary:$secondary;"
    "--kb-accent:$brand_600;--kb-page-bg:$page_bg;"
    "--kb-wash-min:$console_wash_min;--kb-wash-mid:$console_wash_mid;"
    "--kb-wash-max:$console_wash_max}"
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "background-color:$page_bg}"
    "body::before{content:'';position:fixed;inset:0;z-index:-1;"
    "background:linear-gradient(165deg,$console_wash_min,"
    "$console_wash_mid 60%,$console_wash_max);background-size:200% 200%;"
    "animation:kb-shift 30s ease-in-out infinite alternate}"
    "@keyframes kb-shift{from{background-position:0% 0%}"
    "to{background-position:100% 100%}}"
    ".kb-header{border-top:4px solid $brand_800;"
    "border-image:linear-gradient(90deg,$brand_900,$brand_600) 1}"
    "@media(prefers-reduced-motion:reduce){body::before{animation:none;"
    "background-position:0% 0%}}"
    "@media print{body::before{display:none}body{background:$surface}"
    ".kb-header{border-image:none;border-top-color:$text_muted}}"
)


def login_css() -> str:
    """CSS static-страницы /login (цвета только из PALETTE)."""
    return _LOGIN_CSS.substitute(PALETTE)


def console_css() -> str:
    """Глобальный CSS консоли: переменные --kb-*, фон, полоса, guards."""
    return _CONSOLE_CSS.substitute(PALETTE)


def apply(app) -> None:
    """Подключить тему к NiceGUI-приложению (одна точка входа).

    Вызывается в app.py ДО ``ui.run``: перекрашивает Quasar-элементы всех
    ``@ui.page`` (кнопки/вкладки/спиннеры) и инъектирует console_css в head
    каждой страницы (``shared=True`` — обязательный, см. докстринг модуля).
    Rollback темы = убрать один этот вызов.
    """
    app.colors(**quasar_brand())
    ui.add_head_html(f"<style>{console_css()}</style>", shared=True)
