"""Ф2: UI страницы входа + заявка на доступ + кнопка «Выйти» (035).

Контент-контракт (план §4.5): ВСЕ обязательные поля ACCESS_REQUEST_FIELDS
присутствуют в КАЖДОМ канале (textarea, mailto-body, t.me-text) — каналы
генерируются из одних SSOT-констант, тест ловит рассинхрон.

Air-gap-контракт (P1-5/N4, невакуумный): детектор парсит векторы внешней
загрузки; позитивный контроль — мутации cdn.example ОБЯЗАНЫ ронять детектор.

UI-контракт Ф2: сплит-лейаут, табы «Вход»/«Заявка», копи-кнопка,
password-toggle, legacy-режим без поля логина.
"""

from __future__ import annotations

import html as html_mod
import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from kb_console.config import ACCESS_REQUEST_EMAIL, ACCESS_REQUEST_FIELDS
from kb_console.login_page import render_login_html

# ── air-gap детектор (N4: полный список векторов) ─────────────

_ATTR_URL = re.compile(
    r"""(?:src|href|action|poster|srcset|xlink:href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.IGNORECASE)
_META_REFRESH = re.compile(
    r"http-equiv\s*=\s*['\"]refresh['\"][^>]*url=([^'\">]+)", re.IGNORECASE
)
_JS_FETCH = re.compile(r"""(?:fetch|WebSocket|EventSource)\(\s*['"`]([^'"`]+)['"`]""")
_XHR_OPEN = re.compile(r"""\.open\(\s*['"`][A-Z]+['"`]\s*,\s*['"`]([^'"`]+)['"`]""")
_IMPORT = re.compile(r"""@import\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]""")

_KNOWN_LOCAL_SCHEMES = ("data:", "mailto:", "#", "javascript:void(0)")


def find_external_urls(page_html: str, *, allow_tme: str = "") -> list[str]:
    """Все URL, которые тянут что-то НЕ same-origin.

    Разрешены: относительные пути (same-origin), data:, mailto:, '#'-якоря.
    Абсолютные https:// — только allow_tme (t.me-контакт), ровно один раз.
    """
    urls: list[str] = []
    urls += _ATTR_URL.findall(page_html)
    urls += _CSS_URL.findall(page_html)
    urls += [u for u in _META_REFRESH.findall(page_html)]
    urls += _JS_FETCH.findall(page_html)
    urls += _XHR_OPEN.findall(page_html)
    urls += [u for pair in _IMPORT.findall(page_html) for u in pair if u]
    external: list[str] = []
    for raw in urls:
        value = raw.strip()
        if value.startswith(_KNOWN_LOCAL_SCHEMES):
            continue
        if value.startswith("/"):  # same-origin относительный
            continue
        if allow_tme and value.startswith(f"https://t.me/{allow_tme}"):
            continue
        external.append(value)
    return external


# ── контент-контракт каналов заявки (§4.5) ────────────────────


class TestAccessRequestChannels:
    @pytest.fixture()
    def page(self) -> str:
        return render_login_html(legacy=False, next_path="/books", admin_contact="admin_officer")

    def test_fields_in_textarea(self, page):
        for field in ACCESS_REQUEST_FIELDS:
            assert field in page, f"поле отсутствует в textarea: {field}"

    def test_fields_in_mailto_body(self, page):
        href = _first(page, r"href=['\"](mailto:[^'\"]+)['\"]")
        body = parse_qs(urlparse(href).query).get("body", [""])[0]
        for field in ACCESS_REQUEST_FIELDS:
            assert field in unquote(body), f"поле отсутствует в mailto-body: {field}"

    def test_fields_in_tme_text(self, page):
        href = _first(page, r"href=['\"](https://t\.me/[^'\"]+)['\"]")
        text = parse_qs(urlparse(href).query).get("text", [""])[0]
        for field in ACCESS_REQUEST_FIELDS:
            assert field in unquote(text), f"поле отсутствует в t.me-канале: {field}"

    def test_mailto_uses_ssot_email_and_subject(self, page):
        from kb_console.config import ACCESS_REQUEST_SUBJECT

        href = unquote(_first(page, r"href=['\"](mailto:[^'\"]+)['\"]"))
        assert href.startswith(f"mailto:{ACCESS_REQUEST_EMAIL}?")
        assert f"subject={ACCESS_REQUEST_SUBJECT}" in href or ACCESS_REQUEST_SUBJECT in unquote(href)


def _first(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    assert match, f"не найдено: {pattern}"
    return match.group(1)


# ── air-gap-контракт (P1-5, N4) ───────────────────────────────


class TestAirGap:
    def test_no_external_urls_when_no_contact(self):
        page = render_login_html(legacy=False, next_path="/x", admin_contact="")
        assert find_external_urls(page) == []
        assert "https://" not in page

    def test_exactly_one_tme_when_contact_set(self):
        page = render_login_html(legacy=False, next_path="/x", admin_contact="admin_officer")
        tme = re.findall(r"https://t\.me/admin_officer", page)
        assert len(tme) == 1
        # все прочие абсолютные URL запрещены
        rest = find_external_urls(page, allow_tme="admin_officer")
        assert rest == []

    @pytest.mark.parametrize(
        "mutation",
        [
            '<script src="https://cdn.example/x.js"></script>',
            '<img poster="https://cdn.example/p.jpg">',
            '<img srcset="https://cdn.example/i.png 2x">',
            '<iframe src="https://cdn.example/f"></iframe>',
            "<style>@import url('https://cdn.example/s.css');</style>",
            "<div style='background:url(https://cdn.example/b.png)'></div>",
            'fetch("https://cdn.example/api")',
            'new WebSocket("wss://cdn.example/ws")',
            '<base href="https://cdn.example/">',
        ],
    )
    def test_detector_catches_mutations(self, mutation):
        """Позитивный контроль: детектор НЕвакуумный — каждая мутация ловится."""
        page = render_login_html(legacy=False, next_path="/x", admin_contact="") + mutation
        caught = find_external_urls(page)
        assert any("cdn.example" in u for u in caught), f"мутация не поймана: {mutation}"


# ── UI-контракт Ф2: сплит + табы + копи-кнопка ────────────────


class TestLoginUiContract:
    def test_tabs_present(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "Вход" in page
        assert "Заявка на доступ" in page

    def test_copy_button_present(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "Скопировать" in page
        assert "clipboard" in page

    def test_password_toggle_and_error_inline(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "password" in page
        assert "checkbox" in page or "toggle" in page.lower()
        assert "id='err'" in page or 'id="err"' in page

    def test_per_user_shows_username_field(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "f-user" in page
        assert "Логин" in page

    def test_username_input_has_type_text_css_match(self):
        """Регресс прод-нита 035: без type атрибут не матчился CSS-селектором
        `input[type=text],input[type=password]` → поле «Логин» рендерилось
        системным (узкое, без скругления). Оба поля должны покрываться CSS."""
        page = render_login_html(legacy=False, next_path="/x")
        assert "id='f-user' type='text'" in page
        # CSS-селектор покрывает оба поля (самопроверка стилей)
        assert "input[type=text],input[type=password]" in page

    def test_legacy_hides_username_field(self):
        page = render_login_html(legacy=True, next_path="/x")
        # контракт: нет ПОЛЯ ввода логина (JS-обращение к 'f-user'
        # null-safe остаётся — username просто не читается)
        assert "id='f-user'" not in page and 'id="f-user"' not in page
        assert "placeholder='Логин'" not in page
        assert "Пароль" in page

    def test_admin_hint_present(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "выдаёт администратор" in page

    def test_submit_uses_same_origin_fetch(self):
        page = render_login_html(legacy=False, next_path="/x")
        assert "fetch('/api/login'" in page or 'fetch("/api/login"' in page

    def test_copyright_present_and_ssot(self):
        """Подпись разработчика на /login из SSOT APP_COPYRIGHT (035)."""
        from kb_console.config import APP_COPYRIGHT

        page = render_login_html(legacy=False, next_path="/x")
        assert APP_COPYRIGHT in page
        assert "footer" in page  # подпись — в подвале, не в контенте формы

    def test_copyright_footer_layout(self):
        """Регресс прод-нита: body flex-column + .split flex:1 + .copy nowrap,
        footer ПОСЛЕ .split — иначе подпись уезжает вправо и рвётся на 2 строки."""
        page = render_login_html(legacy=False, next_path="/x")
        assert "flex-direction:column" in page
        assert ".split{display:flex;width:100%;flex:1}" in page
        assert "white-space:nowrap" in page
        assert ".copy" in page
        # footer вне .split: закрывающий </footer> стоит после закрытия </main>
        # и после конца .split (</main></div>), т.е. позиция footer > позиция split-close
        assert page.rfind("</footer>") > page.rfind("</main></div>")

    def test_next_sanitized_into_js(self):
        page = render_login_html(legacy=False, next_path="https://evil.example")
        assert "evil.example" not in html_mod.unescape(page)


# ── кнопка «Выйти» (header) ───────────────────────────────────


class TestLogoutControl:
    def test_should_show_logout(self):
        from kb_console.components.header import should_show_logout

        ident = {"username": "alice"}
        assert should_show_logout(ident, "on") is True
        assert should_show_logout(None, "on") is False
        assert should_show_logout(ident, "off") is False

    def test_logout_js_posts_api_and_redirects(self):
        from kb_console.components.header import LOGOUT_JS

        assert "/api/logout" in LOGOUT_JS
        assert "/login" in LOGOUT_JS

    def test_session_identity_reads_scope(self):
        from kb_console.core.identity import session_identity

        # вне page-context → None (не падает)
        assert session_identity() is None
