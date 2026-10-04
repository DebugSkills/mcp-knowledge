"""Unit-тесты блока «Источник» на странице книг (bibliography P3-1).

Тестируемое:
  - _citation_view_model: чистая логика решения «рендерить ли блок»
    (нет citation → нет UI; private ниже admin → нет блока целиком,
    ни кнопки, ни намёка — §3.4, паттерн search.py:153-165);
  - _citation_pages_label: человекочитаемые страницы/время из locator;
  - source-assertion: render_book_detail рендерит блок в TOC и секции
    (паттерн test_show_book_dialog_has_zone_toggle — UI через inspect).
"""

from __future__ import annotations

from kb_console.pages.books import (
    _citation_pages_label,
    _citation_view_model,
)

SHA = "a" * 64

CITATION_PUBLIC: dict = {
    "source_id": "src-abcdef1234567890",
    "zone": "public",
    "title": "Some Book",
    "authors": ["Иванов И. И."],
    "formatted": "Иванов И. И. Некоторые книги. — М.: Прогресс, 2023.",
    "locator": {"kind": "page", "start": 12, "end": 14},
    "viewer_url": "/documents/view",
    "sha256": SHA,
}


# ── (а) citation есть → блок с formatted и URL ──────────────


def test_citation_present_builds_block():
    vm = _citation_view_model(CITATION_PUBLIC, admin_viewer=False)
    assert vm is not None, "public citation → блок есть"
    assert "Иванов" in vm["formatted"], "текст блока = formatted (ГОСТ)"
    assert vm["doc_url"] == f"/documents/{SHA}#page=12"


def test_citation_public_shown_to_non_admin():
    """Public-source citation доступен любой роли (гейт только на private)."""
    vm = _citation_view_model(CITATION_PUBLIC, admin_viewer=False)
    assert vm is not None


def test_citation_without_viewer_url_keeps_text_but_no_button():
    """Нет viewer_url/sha256 → текст блока остаётся, кнопки «Документ» нет
    (частичный рендер ссылки запрещён — паттерн search.py:149-151)."""
    partial = {k: v for k, v in CITATION_PUBLIC.items() if k not in ("viewer_url", "sha256")}
    vm = _citation_view_model(partial, admin_viewer=True)
    assert vm is not None
    assert "Иванов" in vm["formatted"]
    assert vm["doc_url"] is None


# ── (б) citation нет → блока нет ─────────────────────────────


def test_no_citation_no_block():
    assert _citation_view_model(None, admin_viewer=True) is None


def test_empty_citation_no_block():
    """citation без formatted → пустышка, блок не рендерим."""
    assert _citation_view_model({}, admin_viewer=True) is None
    assert _citation_view_model({"formatted": "   "}, admin_viewer=True) is None


def test_non_dict_citation_no_block():
    assert _citation_view_model("not-a-dict", admin_viewer=True) is None


# ── (в) private + не-admin → нет ссылки (и блока) ────────────


def test_private_non_admin_hidden_entirely():
    """§3.4: private-source ниже admin — ни кнопки, ни намёка (блока нет)."""
    citation = {**CITATION_PUBLIC, "zone": "private"}
    assert _citation_view_model(citation, admin_viewer=False) is None


# ── (г) private + admin → есть ссылка ────────────────────────


def test_private_admin_gets_zone_marked_link():
    citation = {**CITATION_PUBLIC, "zone": "private"}
    vm = _citation_view_model(citation, admin_viewer=True)
    assert vm is not None
    assert vm["doc_url"] == f"/documents/{SHA}?zone=private#page=12", (
        "admin-ссылка несёт маркер ?zone=private для гейта прокси"
    )


# ── _citation_pages_label ────────────────────────────────────


def test_pages_label_page_range():
    label = _citation_pages_label({"kind": "page", "start": 12, "end": 14})
    assert label == "С. 12–14"


def test_pages_label_page_single():
    label = _citation_pages_label({"kind": "page", "start": 5})
    assert label == "С. 5"


def test_pages_label_timestamp():
    label = _citation_pages_label({"kind": "timestamp", "start": 754})
    assert label == "12:34"


def test_pages_label_broken_locator_none():
    """Битый locator → None (страницы не выдумываем; ссылка живёт своим фрагментом)."""
    assert _citation_pages_label(None) is None
    assert _citation_pages_label({"kind": "page"}) is None
    assert _citation_pages_label({"kind": "page", "start": "many"}) is None
    assert _citation_pages_label({"kind": "unknown_kind", "start": 3}) is None


# ── Source-assertion: рендер-точки (паттерн inspect.getsource) ──


def test_render_book_detail_renders_citation_block():
    """render_book_detail строит view-model и рендерит блок «Источник»."""
    import inspect

    from kb_console.pages import books

    src = inspect.getsource(books.render_book_detail)
    assert "_citation_view_model" in src, (
        "render_book_detail должна строить _citation_view_model из payload get_entry"
    )
    assert "_render_citation_block" in src, (
        "render_book_detail должна рендерить блок «Источник»"
    )


def test_citation_block_uses_role_gate_like_search():
    """Гейт зоны = паттерн search.py: ROLE_LEVEL[current_role()] >= admin."""
    import inspect

    from kb_console.pages import books

    src = inspect.getsource(books)
    assert 'ROLE_LEVEL.get(current_role(), 0) >= ROLE_LEVEL["admin"]' in src, (
        "admin_viewer вычисляется по роли сессии (как search.py:100)"
    )


def test_citation_block_opens_new_tab():
    """Кнопка «Документ» открывает viewer-URL в новой вкладке (как search.py:165)."""
    import inspect

    from kb_console.pages import books

    src = inspect.getsource(books._render_citation_block)
    assert "new_tab=True" in src
