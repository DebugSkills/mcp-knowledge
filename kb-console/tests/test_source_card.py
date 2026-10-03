"""Ф5c2 (kb-console): Source-карточка в search — on-demand `source_get`, оба блоба.

Контракт сервера (Ф5b1, source_get):
    {"source_id", "title", "domain", "subject", "format", "license", "zone",
     "status", "blobs": {"original": {...}, "canonical": {...}},
     "canonical_error"? {reason, message, at}}
    Отказ гейта (чужая зона / license=unknown / deprecated) →
    {"error": "Source not found: '…'"} — без oracle.

Консоль (Ф5c2):
- Source-карточка on-demand: `source_get` НЕ вызывается при рендере списка,
  только по действию пользователя (кнопка «Источник»);
- оба блоба рендерятся (present/available/size/sha256);
- canonical_error → human-текст через canonical_reason_text + pdf_only-предупреждение;
- sparse (нет canonical) → без фабрикации и без падения;
- серверный error → карточка нейтральна «нет данных» (без утечки зоны/лицензии/причины);
- роль-ключ mcp_api_key, НЕ base;
- citation без source_id → кнопки нет.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from kb_console.core.utils import (
    canonical_reason_text,
    human_size,
    render_source_card,
    short_sha,
)

SHA_O = "ab" * 32
SHA_C = "cd" * 32
SID = "src-card0000000001"

CITE = {
    "source_id": SID,
    "zone": "public",
    "title": "Лекция по MCP",
    "formatted": "Автор. Лекция по MCP",
    "viewer_url": f"/documents/{SHA_O}",
    "sha256": SHA_O,
    "locator": {"kind": "page", "start": 1},
}

HIT = {
    "title": "Секция с источником",
    "knowledge_id": "k-1",
    "content": "текст",
    "score": 0.9,
    "citation": dict(CITE),
}

SRC_FULL = {
    "source_id": SID,
    "title": "Лекция по MCP",
    "domain": "ai",
    "subject": "mcp",
    "format": "pdf",
    "license": "cc-by",
    "zone": "public",
    "status": "published",
    "blobs": {
        "original": {
            "sha256": SHA_O, "size": 1048576, "mime": "application/pdf",
            "present": True, "available": True,
        },
        "canonical": {
            "sha256": SHA_C, "size": 1572864, "mime": "application/pdf",
            "present": True, "available": True,
        },
    },
}


# ── чистые хелперы (human_size / short_sha) ──────────────────


def test_human_size_readable():
    assert human_size(512) == "512 B"
    assert human_size(1048576) == "1.0 MB"
    assert human_size(1572864) == "1.5 MB"
    assert human_size(None) == "—"
    assert human_size("junk") == "—"


def test_short_sha_prefix():
    assert short_sha(SHA_O) == "abababababab…"
    assert short_sha(None) == ""
    assert short_sha("") == ""


# ── render_source_card (уровень utils, паттерн test_import_provenance_ui) ──


def _render(source):
    container = MagicMock()
    with patch("kb_console.core.utils.ui") as mock_ui:
        render_source_card(source, container)
    return mock_ui


def _label_texts(mock_ui) -> list[str]:
    return [str(c.args[0]) for c in mock_ui.label.call_args_list if c.args]


# (б) оба блоба: present/available/size/sha ───────────────────


def test_both_blobs_rendered_present_available_size_sha():
    mock_ui = _render(dict(SRC_FULL))
    texts = _label_texts(mock_ui)
    assert any("original" in t and "present" in t and "available" in t for t in texts)
    assert any("canonical" in t and "present" in t and "available" in t for t in texts)
    assert any("1.0 MB" in t for t in texts)
    assert any("1.5 MB" in t for t in texts)
    assert any(SHA_O[:12] in t for t in texts)
    assert any(SHA_C[:12] in t for t in texts)


def test_metadata_rendered():
    mock_ui = _render(dict(SRC_FULL))
    texts = _label_texts(mock_ui)
    assert any("Лекция по MCP" in t for t in texts)
    assert any("pdf" in t and "cc-by" in t and "published" in t for t in texts)


# (в) canonical_error → human-текст + pdf_only-предупреждение ──


def test_canonical_error_badge_human_text_and_warning():
    src = dict(SRC_FULL)
    src["canonical_error"] = {"reason": "conversion_failed", "message": "x", "at": "..."}
    mock_ui = _render(src)
    texts = _label_texts(mock_ui)
    assert any("⚠ canonical недоступен" in t for t in texts)
    assert canonical_reason_text("conversion_failed") in " ".join(texts)
    assert any("pdf_only-фоллбэк" in t for t in texts)


# (г) sparse: нет canonical → без фабрикации и без падения ────


def test_sparse_no_canonical_no_fabrication_no_crash():
    src = dict(SRC_FULL)
    src["blobs"] = {"original": SRC_FULL["blobs"]["original"]}
    mock_ui = _render(src)
    texts = _label_texts(mock_ui)
    assert any("original" in t for t in texts)
    assert not any("canonical" in t for t in texts)


def test_sparse_missing_optional_fields_no_crash():
    src = {
        "source_id": SID,
        "blobs": {"original": {"sha256": SHA_O, "present": True, "available": False}},
    }
    mock_ui = _render(src)
    texts = _label_texts(mock_ui)
    assert any("original" in t for t in texts)


# (д) серверный error → нейтрально, без утечки ────────────────


def test_server_error_neutral_no_leak():
    mock_ui = _render({"error": "Source not found: 'src-x'"})
    texts = _label_texts(mock_ui)
    assert any("нет данных" in t for t in texts)
    leaked = ("license", "public", "private", "Source not found", "unknown")
    joined = " ".join(texts)
    assert not any(term in joined for term in leaked)


def test_none_source_neutral_no_crash():
    mock_ui = _render(None)
    texts = _label_texts(mock_ui)
    assert any("нет данных" in t for t in texts)


# ── search.py: on-demand + роль-ключ + гейт source_id ────────


async def _render_search(items: list[dict], *, key: str, role: str = "editor", expand: bool = False):
    """Прогнать search._do_search; expand=True — кликнуть «Источник» ВНУТРИ patch.

    Клик обязателен внутри patch-контекста: `_load_source_card` читает
    `search.MCPClient`/`mcp_api_key`/`render_source_card` по module-global
    в момент вызова (лениво), а не в момент рендера списка.
    """
    from kb_console.pages import search

    client = MagicMock()
    client.tools_call = AsyncMock(return_value=items)
    client.close = AsyncMock()
    client.source_get = AsyncMock(return_value=dict(SRC_FULL))
    container = MagicMock()
    with (
        patch.object(search, "MCPClient", return_value=client) as ctor,
        patch.object(search, "mcp_api_key", return_value=key),
        patch.object(search, "current_role", return_value=role),
        patch.object(search, "_load_book_titles", AsyncMock(return_value={})),
        patch.object(search, "ui") as ui,
        patch.object(search, "render_source_card") as render_card,
    ):
        await search._do_search("запрос", 5, container)
        buttons = [
            c for c in ui.button.call_args_list if c.args and c.args[0] == "Источник"
        ]
        if expand and buttons:
            await buttons[0].kwargs["on_click"]()
    return client, ctor, ui, render_card, buttons


def _source_buttons(ui) -> list:
    return [c for c in ui.button.call_args_list if c.args and c.args[0] == "Источник"]


async def test_on_demand_source_get_not_called_at_render():
    """(а) on-demand: source_get НЕ вызывается при построении списка."""
    client, _, _ui, render_card, buttons = await _render_search([dict(HIT)], key="k-editor")
    assert len(buttons) == 1, "кнопка «Источник» обязана отрендериться при source_id"
    client.source_get.assert_not_called()
    render_card.assert_not_called()


async def test_source_get_called_on_click_and_role_key():
    """(а)+(е): клик → source_get(source_id); ленивый клиент — роль-ключ, не base."""
    from kb_console.config import MCP_API_KEY

    client, ctor, _, render_card, _buttons = await _render_search(
        [dict(HIT)], key="k-editor", expand=True
    )
    client.source_get.assert_called_once_with(SID)
    render_card.assert_called_once()
    assert ctor.call_args.kwargs["api_key"] == "k-editor"
    assert ctor.call_args.kwargs["api_key"] != MCP_API_KEY


async def test_citation_without_source_id_no_button():
    """(ж) citation без source_id → кнопки «Источник» нет."""
    cite_no_source = {k: v for k, v in CITE.items() if k != "source_id"}
    hit = {**HIT, "citation": cite_no_source}
    _, _, _ui, _, buttons = await _render_search([hit], key="k-editor")
    assert buttons == []


async def test_no_citation_no_button():
    """citation отсутствует целиком → кнопки «Источник» нет."""
    hit = {k: v for k, v in HIT.items() if k != "citation"}
    _, _, _ui, _, buttons = await _render_search([hit], key="k-editor")
    assert buttons == []


# (е) источник не тащит base-ключ в текст модуля (паттерн import_page) ──


def test_search_module_no_base_key_constant():
    """search.py не должен ссылаться на base-ключ MCP_API_KEY (Ф4-fix1 + Ф5c2)."""
    from pathlib import Path

    from kb_console.pages import search

    src = Path(search.__file__).read_text(encoding="utf-8")
    assert "MCP_API_KEY" not in src
