"""037 Ф0: контракт темы kb-console (SSOT components/theme.py).

Проверяется на СГЕНЕРИРОВАННОМ CSS и PALETTE — до любого рендера:
- полнота палитры (нет «…», все токены — валидный #rrggbb);
- все hex в CSS принадлежат PALETTE (нет побочных цветов вне SSOT);
- WCAG-контраст ВСЕХ фактических пар: relative luminance считается ЗДЕСЬ,
  результат не хардкодится; порог текст ≥4.5:1, focus-ring/нестекст ≥3:1
  (WCAG 1.4.11 non-text contrast; критика 037 P1-1/P1-2);
- air-gap: 0 внешних URL в сгенерированном CSS;
- prefers-reduced-motion отключает анимацию; @media print — белый фон;
- структурные подстроки login (регресс test_login_page.py:187,217-220;
  критика 037 P1-3 — все 5, не только .split);
- quasar_brand: полный набор ключей app.colors(), семантика не тронута.
"""

from __future__ import annotations

import re
import types
from typing import ClassVar

import pytest

from kb_console.components.theme import (
    PALETTE,
    QUASAR_SEMANTIC_DEFAULTS,
    console_css,
    login_css,
    quasar_brand,
)

_HEX_RE = re.compile(r"#[0-9a-f]{6}\b", re.IGNORECASE)

# ── WCAG relative luminance / contrast (считается, не хардкодится) ──


def _srgb_to_linear(channel_pct: float) -> float:
    c = channel_pct / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


# ── полнота палитры ──────────────────────────────────────────

REQUIRED_TOKENS = {
    # светлые поверхности
    "page_bg", "surface",
    # текст на светлом
    "text_main", "text_area", "text_muted", "text_soft", "text_toggle",
    # бренд (тёмно-зелёный)
    "brand_900", "brand_800", "brand_700", "brand_600", "secondary",
    # акценты на тёмной панели login
    "text_on_dark", "text_on_dark_soft", "text_on_dark_muted", "accent_on_dark",
    # функциональные
    "focus_ring", "ghost_bg", "on_brand", "ok", "err",
    "input_border", "border_light",
    # деликатный градиент-фон консоли
    "console_wash_min", "console_wash_mid", "console_wash_max",
}


class TestPalette:
    def test_all_required_tokens_present(self):
        missing = REQUIRED_TOKENS - set(PALETTE)
        assert not missing, f"в PALETTE нет токенов: {missing}"

    def test_values_are_valid_hex_no_ellipsis(self):
        assert "…" not in "".join(PALETTE.values())
        for token, value in PALETTE.items():
            assert re.fullmatch(r"#[0-9a-f]{6}", value), f"{token}={value!r} не #rrggbb"

    def test_focus_ring_is_wcag_green_not_light(self):
        """P1-1: #86efac (1.3-1.4:1 на светлом) запрещён; ring = #15803d."""
        assert PALETTE["focus_ring"].lower() != "#86efac"
        assert PALETTE["focus_ring"].lower() == "#15803d"

    def test_brand_hex_from_plan(self):
        """План §3.2: якорные оттенки тёмно-зелёного бренда."""
        expect = {
            "brand_900": "#0b3d2e",
            "brand_800": "#0f5132",
            "brand_700": "#14532d",
            "brand_600": "#166534",
        }
        for token, hex_value in expect.items():
            assert PALETTE[token].lower() == hex_value, token


class TestNoHexOutsidePalette:
    """SSOT-свойство: каждый hex в сгенерированном CSS — из PALETTE."""

    @pytest.mark.parametrize("css_fn", [login_css, console_css], ids=["login", "console"])
    def test_css_hex_subset_of_palette(self, css_fn):
        css = css_fn()
        allowed = {v.lower() for v in PALETTE.values()}
        stray = {h.lower() for h in _HEX_RE.findall(css)} - allowed
        assert not stray, f"hex вне PALETTE в CSS: {stray}"


# ── контраст-матрица ВСЕХ фактических пар (P1-2) ─────────────
# (fg-токен, bg-токен, порог, где пара живёт в UI)
CONTRAST_PAIRS = [
    # /login: светлые поверхности
    ("text_main", "page_bg", 4.5, "body-текст /login"),
    ("text_main", "surface", 4.5, "текст в карточке login"),
    ("text_soft", "page_bg", 4.5, ".copy — подпись в футере login"),
    ("text_muted", "surface", 4.5, ".hint/.req-note/.consent-text/неактивный таб"),
    ("text_toggle", "surface", 4.5, ".toggle «показать пароль»"),
    ("text_area", "surface", 4.5, "textarea/.fld-подписи полей заявки"),
    ("err", "surface", 4.5, ".err — сообщение об ошибке"),
    ("ok", "surface", 4.5, ".ok — успех заявки"),
    ("brand_600", "surface", 4.5, "активный таб login (текст)"),
    ("brand_600", "ghost_bg", 4.5, ".btn.ghost — текст ghost-кнопки"),
    ("on_brand", "brand_800", 4.5, ".btn — текст основной кнопки"),
    ("on_brand", "brand_700", 4.5, ".btn:hover — текст кнопки при hover"),
    # /login: тёмная бренд-панель (worst-stop градиента = brand_600)
    ("text_on_dark", "brand_900", 4.5, ".brand h1 на нижнем стопе"),
    ("text_on_dark", "brand_600", 4.5, ".brand h1 на верхнем стопе (worst)"),
    ("text_on_dark_soft", "brand_600", 4.5, ".brand p на worst-стопе"),
    ("text_on_dark_muted", "brand_600", 4.5, ".brand ul на worst-стопе"),
    ("accent_on_dark", "brand_600", 4.5, "✓-галочки .brand li на worst-стопе"),
    # консоль
    ("text_main", "console_wash_max", 4.5, "текст консоли на самом тёмном стопе фона"),
    # non-text (WCAG 1.4.11)
    ("focus_ring", "page_bg", 3.0, "focus-ring на фоне страницы"),
    ("focus_ring", "surface", 3.0, "focus-ring на карточке"),
    ("brand_800", "page_bg", 3.0, "primary-кнопка/полоса на фоне страницы"),
]


class TestContrastMatrix:
    @pytest.mark.parametrize("fg,bg,threshold,where", CONTRAST_PAIRS)
    def test_pair_meets_threshold(self, fg, bg, threshold, where):
        ratio = contrast_ratio(PALETTE[fg], PALETTE[bg])
        assert ratio >= threshold, (
            f"{where}: {PALETTE[fg]} на {PALETTE[bg]} = {ratio:.2f}:1 "
            f"< {threshold}:1 (токены {fg}/{bg})"
        )

    def test_matrix_is_nonvacuous(self):
        assert len(CONTRAST_PAIRS) >= 20
        text_pairs = [p for p in CONTRAST_PAIRS if p[2] == 4.5]
        assert len(text_pairs) >= 15


# ── air-gap ──────────────────────────────────────────────────


class TestAirGap:
    @pytest.mark.parametrize("css_fn", [login_css, console_css], ids=["login", "console"])
    def test_no_external_vectors(self, css_fn):
        css = css_fn()
        for banned in ("http://", "https://", "//fonts", "@import", "url("):
            assert banned not in css, f"внешний вектор {banned!r} в CSS"

    def test_system_font_stack_only(self):
        assert "system-ui" in login_css() and "system-ui" in console_css()


# ── анимация: тайминг + reduced-motion (Q3) ──────────────────


class TestAnimation:
    @pytest.mark.parametrize("css_fn", [login_css, console_css], ids=["login", "console"])
    def test_kb_shift_timing_26_30s(self, css_fn):
        m = re.search(r"animation:kb-shift (\d+)s", css_fn())
        assert m, "нет animation:kb-shift <N>s"
        assert 26 <= int(m.group(1)) <= 30, f"тайминг {m.group(1)}s вне 26-30s"

    @pytest.mark.parametrize("css_fn", [login_css, console_css], ids=["login", "console"])
    def test_keyframes_by_background_position(self, css_fn):
        assert "@keyframes kb-shift{from{background-position:0% 0%}" in css_fn()
        assert "to{background-position:100% 100%}}" in css_fn()

    @pytest.mark.parametrize("css_fn", [login_css, console_css], ids=["login", "console"])
    def test_reduced_motion_disables_animation(self, css_fn):
        assert "prefers-reduced-motion:reduce" in css_fn()
        assert "animation:none" in css_fn()

    def test_gradient_enlarged_for_shift(self):
        assert "background-size:220%" in login_css()
        assert "background-size:200%" in console_css()


# ── печать: белый лист, без анимации (инвариант №2) ──────────


class TestPrintGuard:
    def test_console_print_is_white_and_static(self):
        css = console_css()
        assert "@media print" in css
        assert "body::before{display:none}" in css
        assert re.search(r"body\{background:#ffffff", css)

    def test_login_brand_not_needed_in_print(self):
        """Печать login не является контуром задачи; guard не требуется,
        но градиент консоли не должен утекать в печать — см. тест выше."""
        assert "prefers-reduced-motion" in login_css()


# ── структурные подстроки login (P1-3: все 5) ────────────────


class TestLoginCssStructure:
    SUBSTRINGS: ClassVar[list[str]] = [
        ".split{display:flex;width:100%;flex:1}",
        "flex-direction:column",
        "white-space:nowrap",
        ".copy",
        "input[type=text],input[type=password]",
    ]

    @pytest.mark.parametrize("fragment", SUBSTRINGS)
    def test_structural_substring_preserved(self, fragment):
        assert fragment in login_css(), f"потеряна структурная подстрока {fragment!r}"


# ── Quasar brand ─────────────────────────────────────────────


class TestQuasarBrand:
    EXPECTED_KEYS: ClassVar[set[str]] = {
        "primary", "secondary", "accent", "dark", "dark_page",
        "positive", "negative", "info", "warning",
    }

    def test_full_key_set(self):
        brand = quasar_brand()
        assert set(brand) == self.EXPECTED_KEYS

    def test_brand_colors_mapped(self):
        brand = quasar_brand()
        assert brand["primary"] == PALETTE["brand_800"]
        assert brand["secondary"] == PALETTE["secondary"]
        assert brand["accent"] == PALETTE["brand_600"]

    def test_semantic_colors_untouched(self):
        """Инвариант №6: success/warn/error-семантика Quasar не меняется."""
        brand = quasar_brand()
        for key, value in QUASAR_SEMANTIC_DEFAULTS.items():
            assert brand[key] == value, f"{key} отклонился от дефолта Quasar"


# ── apply(): одна точка подключения ──────────────────────────


class TestApply:
    def test_apply_wires_colors_and_shared_head(self, monkeypatch):
        from kb_console.components import theme

        calls: dict[str, object] = {}

        class FakeApp:
            def colors(self, **kwargs):
                calls["colors"] = kwargs

        fake_ui = types.SimpleNamespace(
            add_head_html=lambda code, *, shared=False: calls.update(head=(code, shared))
        )
        monkeypatch.setattr(theme, "ui", fake_ui)

        theme.apply(FakeApp())

        assert calls["colors"] == quasar_brand()
        code, shared = calls["head"]
        assert shared is True, "add_head_html без shared=True не доставит CSS на @ui.page"
        assert code.startswith("<style>") and code.endswith("</style>")
        assert console_css() in code
