"""Ф5d: механическая e2e-матрица роль×зона консольного слоя (bibliography).

SSOT-политика §3.4 «private → только admin» механически, по 4 поверхностям:
  1. viewer-прокси GET/HEAD /documents/{sha256} (documents_proxy);
  2. Source-карточка (render_source_card ← source_get серверный гейт);
  3. admin-страница /documents (ROUTES min_role=admin + runtime 403-лейбл);
  4. поиск (private-citation ниже admin → нет «Документ» И «Источник»).

Матрица роль × зона = {admin, editor, contributor} × {public, private}.
Инварианты: public доступен всем ролям; private — только admin (ниже admin:
ссылки нет / прокси 404 без похода upstream / карточка нейтральна / страница
403); запросы консоли идут с роль-ключом (mcp_api_key), не base; ни в одном
ответе ниже admin нет «zone=private»/license-деталей приватного источника.

Мутационные детекторы (демо+откат):
  M1 снять гейт zone=private в прокси → private-editor/contributor 200 (падает);
  M2 показать private-«Документ» editor'у в поиске → кнопка есть (падает);
  M2' показать private-«Источник» ниже admin → кнопка есть (падает);
  M3 открыть /documents editor'у (min_role→editor) → страница строится (падает);
  M4 показывать zone/license при server-error в карточке → leak-ассерт падает.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Прокси-хелперы (бит-в-бит с test_documents_proxy: тот же хендлер/стор/транспорт).
from test_documents_proxy import (
    _body,
    _handler,
    _request,
    _session,
    _Upstream,
)

from kb_console.config import MCP_API_KEY
from kb_console.core.utils import render_source_card
from kb_console.pages import ROUTES

ROLES = ("admin", "editor", "contributor")
ZONES = ("public", "private")
KEY_BY_ROLE = {"admin": "k-admin", "editor": "k-editor", "contributor": "k-contrib"}

SHA = "ef" * 32  # валидный 64-hex, отличный от прокси-фикстуры

PRIVATE_TITLE = "Внутренний регламент"
PRIVATE_LICENSE = "nda-internal"


def _source_full(zone: str) -> dict:
    """Полный source (что сервер вернёт ключу, у которого есть доступ)."""
    return {
        "source_id": "src-matrix00000001",
        "title": "Лекция по MCP" if zone == "public" else PRIVATE_TITLE,
        "domain": "ai",
        "subject": "mcp",
        "format": "pdf",
        "license": "cc-by" if zone == "public" else PRIVATE_LICENSE,
        "zone": zone,
        "status": "published",
        "blobs": {
            "original": {
                "sha256": "ab" * 32, "size": 1048576, "mime": "application/pdf",
                "present": True, "available": True,
            },
            "canonical": {
                "sha256": "cd" * 32, "size": 1572864, "mime": "application/pdf",
                "present": True, "available": True,
            },
        },
    }


def _source_for(role: str, zone: str) -> dict:
    """Модель серверного гейта source_get по (роль, зона).

    Чужая зона (private ниже admin) → сервер отвечает {"error": ...} без oracle
    (Ф5b1). Моделируем ХУДШИЙ случай: враждебный/дефектный сервер добавляет к
    error метаданные (zone/license/title) — консоль обязана отрисовать нейтрально
    и НЕ рендерить эти поля (M4-детектор утечки зоны/лицензии).
    """
    if zone == "public" or role == "admin":
        return _source_full(zone)
    return {
        "error": "Source not found: 'src-x'",
        "zone": zone,
        "license": PRIVATE_LICENSE,
        "title": PRIVATE_TITLE,
    }


def _citation(zone: str) -> dict:
    return {
        "source_id": "src-search-000001",
        "zone": zone,
        "title": "Лекция по MCP" if zone == "public" else PRIVATE_TITLE,
        "authors": ["Иванов И."],
        "formatted": "Иванов И. Лекция",
        "viewer_url": f"/documents/{SHA}",
        "sha256": SHA,
        "locator": {"kind": "page", "start": 3},
    }


# ── 1. viewer-прокси: роль × зона (6 кейсов) ─────────────────


class TestProxyMatrix:
    @pytest.mark.parametrize("role", ROLES)
    @pytest.mark.parametrize("zone", ZONES)
    async def test_proxy(self, tmp_path, role, zone):
        """private → admin 200; иначе (editor/contributor) 404 БЕЗ похода upstream."""
        up = _Upstream()
        query = b"zone=private" if zone == "private" else b"zone=public"
        resp = await _handler(tmp_path, up)(
            _request(session=_session(role), query=query), SHA
        )
        if zone == "public" or role == "admin":
            assert resp.status_code == 200
            assert await _body(resp) == up.body
            assert len(up.calls) == 1
        else:
            assert resp.status_code == 404
            assert resp.headers["cache-control"] == "no-store"
            assert up.calls == [], "отказ не должен дёргать upstream (M1)"

    @pytest.mark.parametrize("role", ["editor", "contributor"])
    async def test_proxy_role_key_not_base(self, tmp_path, role):
        """Запрос к upstream идёт с роль-ключом (не base) — публичная зона."""
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session(role), query=b"zone=public"), SHA
        )
        assert resp.status_code == 200
        assert up.calls[0].headers.get("X-API-Key") == KEY_BY_ROLE[role]
        assert up.calls[0].headers.get("X-API-Key") != "k-base"


# ── 2. Source-карточка: роль × зона (6 кейсов) ───────────────


def _render_card(source):
    container = MagicMock()
    with patch("kb_console.core.utils.ui") as mock_ui:
        render_source_card(source, container)
    return mock_ui


def _label_texts(mock_ui) -> list[str]:
    return [str(c.args[0]) for c in mock_ui.label.call_args_list if c.args]


class TestSourceCardMatrix:
    @pytest.mark.parametrize("role", ROLES)
    @pytest.mark.parametrize("zone", ZONES)
    def test_source_card(self, role, zone):
        """admin/private → карточка с обоими блобами; ниже admin private → нейтрально."""
        mock_ui = _render_card(_source_for(role, zone))
        texts = _label_texts(mock_ui)
        joined = " ".join(texts)
        if zone == "public" or role == "admin":
            assert any("original" in t and "present" in t for t in texts)
            assert any("canonical" in t and "present" in t for t in texts)
        else:
            assert any("нет данных" in t for t in texts), "ниже admin private → нейтрально"
            for leak in ("license", "private", "public", PRIVATE_LICENSE, PRIVATE_TITLE):
                assert leak not in joined, f"утечка {leak!r} в нейтральной карточке"


# ── 3. admin-страница /documents: роль (3 кейса) ─────────────


class TestDocumentsPageMatrix:
    def test_route_min_role_admin(self):
        """`/documents` в ROUTES с min_role="admin" (скрыто из навигации ниже admin)."""
        matches = [r for r in ROUTES if r[0] == "/documents"]
        assert len(matches) == 1
        assert matches[0][3] == "admin"

    @pytest.mark.parametrize("role", ROLES)
    def test_documents_page(self, role):
        """admin → страница строится; editor/contributor → 403-лейбл, MCPClient не создаётся."""
        from kb_console.pages import documents

        with (
            patch.object(documents, "is_admin", return_value=(role == "admin")),
            patch.object(documents, "ui") as mock_ui,
            patch.object(documents, "MCPClient") as mock_client_cls,
        ):
            documents.build_documents()
        labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
        if role == "admin":
            assert any("Документы (хранилище)" in t for t in labels)
        else:
            assert any("403" in t for t in labels)
            mock_client_cls.assert_not_called()


# ── 4. поиск: роль × зона (6 кейсов) + роль-ключ ─────────────


async def _render_search(citation: dict, *, role: str, key: str):
    from kb_console.pages import search

    hit = {
        "title": "Запись с источником",
        "knowledge_id": "k-1",
        "content": "текст",
        "score": 0.9,
        "citation": dict(citation),
    }
    client = MagicMock()
    client.tools_call = AsyncMock(return_value=[hit])
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


def _buttons(ui, label: str) -> list:
    return [c for c in ui.button.call_args_list if c.args and c.args[0] == label]


class TestSearchMatrix:
    @pytest.mark.parametrize("role", ROLES)
    @pytest.mark.parametrize("zone", ZONES)
    async def test_search(self, role, zone):
        """private ниже admin → нет «Документ»/«Источник»; иначе оба есть (M2/M2')."""
        key = KEY_BY_ROLE[role]
        ctor, ui = await _render_search(_citation(zone), role=role, key=key)
        doc_buttons = _buttons(ui, "Документ")
        src_buttons = _buttons(ui, "Источник")
        if zone == "public" or role == "admin":
            assert len(doc_buttons) == 1
            assert len(src_buttons) == 1
        else:
            assert doc_buttons == [], "private-ссылка «Документ» не должна презентоваться ниже admin"
            assert src_buttons == [], "private-источник «Источник» не должен презентоваться ниже admin"
        # роль-ключ (не base) на уровне вызова
        assert ctor.call_args.kwargs["api_key"] == key
        assert ctor.call_args.kwargs["api_key"] != MCP_API_KEY

    def test_admin_private_doc_url_has_zone_marker(self):
        """admin + private → ссылка несёт ?zone=private (гейт прокси)."""
        from kb_console.documents_proxy import citation_viewer_url

        url = citation_viewer_url(_citation("private"))
        assert url == f"/documents/{SHA}?zone=private#page=3"

    def test_public_citation_no_zone_marker(self):
        from kb_console.documents_proxy import citation_viewer_url

        assert citation_viewer_url(_citation("public")) == f"/documents/{SHA}#page=3"


# ── Сводный ассерт: утечка запрещена ниже admin ──────────────


class TestNoLeakBelowAdmin:
    @pytest.mark.parametrize("role", ["editor", "contributor"])
    def test_no_private_zone_or_license_leak(self, role):
        """Ни один рендер ниже admin не содержит zone=private/лицензии приватного."""
        card = _render_card(_source_for(role, "private"))
        texts = " ".join(_label_texts(card))
        for leak in ("zone=private", "private", PRIVATE_LICENSE, "license", PRIVATE_TITLE):
            assert leak not in texts, f"{role}: утечка {leak!r}"

    @pytest.mark.parametrize("role", ["editor", "contributor"])
    async def test_no_private_url_in_search(self, role):
        """Ниже admin поиск не отдаёт URL с ?zone=private (даже в замыкании кнопки)."""
        _, ui = await _render_search(_citation("private"), role=role, key=KEY_BY_ROLE[role])
        assert _buttons(ui, "Документ") == []
        assert _buttons(ui, "Источник") == []
