"""Ф4-fix1 (P1-1, e2e): private-гейт консоли достижим из собственных ссылок.

Политика §3.4:177 «private → только admin» в реальном потоке поиск → «Документ»:
- editor + private-source citation → в выдаче НЕТ private-ссылки (кнопки нет);
- admin → ссылка есть и несёт ?zone=private (M1/M3-детекторы);
- editor, дойдя до прокси по сгенерированной ссылке ?zone=private → 404;
- contributor + public citation → кнопка есть, ссылка без маркера, прокси 200;
- поиск шлёт роль-ключ (mcp_api_key), НЕ base-ключ (M2-детектор утечки).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from kb_console.documents_proxy import citation_viewer_url

SHA = "cd" * 32

CITE_PRIVATE = {
    "source_id": "src-priv0000000001",
    "zone": "private",
    "title": "Внутренний регламент",
    "authors": ["Иванов И."],
    "formatted": "Иванов И. Внутренний регламент",
    "viewer_url": f"/documents/{SHA}",
    "sha256": SHA,
    "locator": {"kind": "page", "start": 3},
}

CITE_PUBLIC = {**CITE_PRIVATE, "zone": "public", "source_id": "src-pub00000000001"}

HIT_PRIVATE = {
    "title": "Запись с private-источником",
    "knowledge_id": "k-1",
    "content": "текст",
    "score": 0.9,
    "citation": dict(CITE_PRIVATE),
}


async def _render(items: list[dict], *, role: str, key: str):
    """Прогнать search._do_search с моком UI/клиента; вернуть (ctor, ui)."""
    from kb_console.pages import search

    client = MagicMock()
    client.tools_call = AsyncMock(return_value=items)
    client.close = AsyncMock()
    container = MagicMock()
    with (
        patch.object(search, "MCPClient", return_value=client) as ctor,
        patch.object(search, "mcp_api_key", return_value=key),
        patch.object(search, "current_role", return_value=role),
        patch.object(search, "_load_book_titles", AsyncMock(return_value={})),
        patch.object(search, "ui") as ui,
    ):
        await search._do_search("запрос", 5, container)
    return ctor, ui


def _doc_buttons(ui) -> list:
    """Вызовы ui.button с подписью «Документ» (viewer-ссылка)."""
    return [c for c in ui.button.call_args_list if c.args and c.args[0] == "Документ"]


def _doc_url(ui) -> str:
    """URL из on_click-замыкания кнопки «Документ» (единственная в выдаче).

    Без вызова хендлера (patch-контекст уже закрыт): читаем default-аргумент
    лямбды `lambda u=doc_url: ui.open(u, new_tab=True)`.
    """
    assert _doc_buttons(ui), "кнопка «Документ» не отрендерена"
    handler = ui.button.return_value.props.return_value.on_click.call_args.args[0]
    return handler.__defaults__[0]


# ── e2e: выдача поиска ──────────────────────────────────────


async def test_editor_private_link_hidden():
    """editor + private source → в выдаче консоли нет private-ссылки."""
    _, ui = await _render([HIT_PRIVATE], role="editor", key="k-editor")
    assert _doc_buttons(ui) == []


async def test_admin_private_link_with_zone_marker():
    """admin → private-ссылка есть и несёт ?zone=private (M1/M3-детектор)."""
    _, ui = await _render([HIT_PRIVATE], role="admin", key="k-admin")
    assert len(_doc_buttons(ui)) == 1
    assert _doc_url(ui) == f"/documents/{SHA}?zone=private#page=3"


async def test_contributor_public_works():
    """contributor + public source → кнопка есть, ссылка без маркера."""
    hit = {**HIT_PRIVATE, "citation": dict(CITE_PUBLIC)}
    _, ui = await _render([hit], role="contributor", key="k-contrib")
    assert len(_doc_buttons(ui)) == 1
    assert _doc_url(ui) == f"/documents/{SHA}#page=3"


async def test_search_uses_role_key_not_base():
    """M2-детектор: base-ключ (admin-эквивалент) в поиске = private-утечка."""
    from kb_console.config import MCP_API_KEY

    ctor, _ = await _render([HIT_PRIVATE], role="editor", key="k-editor")
    used = ctor.call_args.kwargs["api_key"]
    assert used == "k-editor"
    assert used != MCP_API_KEY


# ── e2e: прокси по сгенерированной ссылке ───────────────────


async def test_editor_via_generated_private_link_gets_404(tmp_path):
    """Editor, дойдя до прокси по сгенерированной console-ссылке → 404."""
    from test_documents_proxy import _handler, _request, _session, _Upstream

    link = citation_viewer_url(dict(CITE_PRIVATE))
    assert "?zone=private" in link, "ссылка консоли обязана нести маркер зоны"
    path = link.split("#", 1)[0]
    base, _, query = path.partition("?")

    up = _Upstream()
    handler = _handler(tmp_path, up)
    resp = await handler(
        _request(path=base, query=query.encode(), session=_session("editor")), SHA
    )
    assert resp.status_code == 404
    assert up.calls == []


async def test_contributor_public_proxied_via_generated_link(tmp_path):
    """Contributor + public-ссылка (без маркера) → прокси отдаёт документ."""
    from test_documents_proxy import _handler, _request, _session, _Upstream

    link = citation_viewer_url(dict(CITE_PUBLIC))
    assert "zone=" not in link
    path = link.split("#", 1)[0]

    up = _Upstream()
    handler = _handler(tmp_path, up)
    resp = await handler(
        _request(path=path, session=_session("contributor")), SHA
    )
    assert resp.status_code == 200
    assert len(up.calls) == 1


# ── mcp_api_key (identity) ──────────────────────────────────


def test_mcp_api_key_identity_role_key(monkeypatch):
    """identity есть → роль-ключ (editor), НЕ base."""
    from kb_console.core import identity

    monkeypatch.setattr(identity, "current_identity", lambda: {"id": "1", "username": "e", "role": "editor"})
    monkeypatch.setattr(identity, "_has_users", lambda: True)
    monkeypatch.setattr(identity, "MCP_API_KEY", "k-base", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_ADMIN", "k-admin", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_EDITOR", "k-editor", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_CONTRIBUTOR", "k-contrib", raising=False)
    assert identity.mcp_api_key() == "k-editor"


def test_mcp_api_key_legacy_base(monkeypatch):
    """Legacy (пустой стор / вне page-context) → base, бит-в-бит с 002."""
    from kb_console.core import identity

    monkeypatch.setattr(identity, "current_identity", lambda: None)
    monkeypatch.setattr(identity, "_has_users", lambda: False)
    monkeypatch.setattr(identity, "MCP_API_KEY", "k-base", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_ADMIN", "k-admin", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_EDITOR", "k-editor", raising=False)
    monkeypatch.setattr(identity, "MCP_API_KEY_CONTRIBUTOR", "k-contrib", raising=False)
    assert identity.mcp_api_key() == "k-base"
