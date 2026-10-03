"""Тесты консоль-прокси выдачи документов + viewer (bibliography Ф4c).

Контракты (план §3.4:177 + задание Ф4c):
- viewer-хелпер: PDF → `#page=N`, media(timestamp) → `#t=<sec>`,
  прочие/URL-only → без фрагмента; `?zone=private` ДО фрагмента;
- гейт зоны: private → admin-only (editor/contributor → 404, НЕ 403,
  upstream НЕ дёргается); без zone — public-умолчание (доступность
  уровня (б) уже проверена ключом вызывающего на сервере);
- ключ upstream — по роли сессии (api_key_for_role), НЕ base-хардкод;
- синтаксис sha256 (64 hex) → 400 без похода upstream;
- проброс: Range (206/416), 404/401 как есть; заголовки Content-Type,
  X-Content-Type-Options: nosniff — как есть; всегда Cache-Control: no-store;
- тело — стримингом (StreamingResponse), HEAD — без тела;
- transport-сбой upstream → 502 (не 500);
- неаутентифицированный запрос → 302 /login (ConsoleAuthMiddleware, v3).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from kb_console.auth import ConsoleAuthMiddleware
from kb_console.core.users import UserStore
from kb_console.documents_proxy import (
    DOCUMENTS_ROUTE,
    citation_viewer_url,
    make_documents_handler,
    register_documents_proxy,
    viewer_url,
)

SHA = "ab" * 32  # валидный 64-hex
UPSTREAM = "http://upstream.test"
PDF_BODY = b"%PDF-1.4 fake-bytes-for-proxy-test"

K_BASE, K_ADMIN, K_EDITOR, K_CONTRIB = "k-base", "k-admin", "k-editor", "k-contrib"


# ── Хелперы ─────────────────────────────────────────────────


def _session(role: str, user: str = "alice") -> dict:
    return {"identity": {"user_id": "id-1", "username": user, "role": role}}


def _request(
    path: str = f"/documents/{SHA}",
    *,
    method: str = "GET",
    session: dict | None = None,
    headers: dict[str, str] | None = None,
    query: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    if session is not None:
        scope["session"] = session
    return Request(scope)


class _Upstream:
    """Mock-транспорт: фиксирует запросы, отвечает по сценарию."""

    def __init__(self, *, status: int = 200, body: bytes = PDF_BODY):
        self.calls: list[httpx.Request] = []
        self.status = status
        self.body = body

    def transport(self) -> httpx.MockTransport:
        upstream = self

        async def handle(request: httpx.Request) -> httpx.Response:
            upstream.calls.append(request)
            if isinstance(upstream.status, Exception):
                raise upstream.status
            headers = {
                "Content-Type": "application/pdf",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'inline; filename="doc.pdf"',
                "Accept-Ranges": "bytes",
            }
            if upstream.status == 206:
                headers["Content-Range"] = (
                    f"bytes 0-{len(upstream.body) - 1}/{len(upstream.body)}"
                )
            elif upstream.status == 416:
                headers["Content-Range"] = f"bytes */{len(upstream.body)}"
            return httpx.Response(
                upstream.status, content=upstream.body, headers=headers
            )

        return httpx.MockTransport(handle)


def _handler(tmp_path, upstream: _Upstream, *, users: UserStore | None = None):
    store = (
        users if users is not None else UserStore(users_file=str(tmp_path / "u.jsonl"))
    )
    return make_documents_handler(
        users_store=store,
        base_url=UPSTREAM,
        base_key=K_BASE,
        admin_key=K_ADMIN,
        editor_key=K_EDITOR,
        contributor_key=K_CONTRIB,
        transport=upstream.transport(),
    )


def _store_with(tmp_path, *roles: str) -> UserStore:
    store = UserStore(users_file=str(tmp_path / f"u-{'-'.join(roles)}.jsonl"))
    for i, role in enumerate(roles):
        store.create_user(f"user{i}", f"pw{i}", role)
    return store


async def _body(resp) -> bytes:
    return b"".join([chunk async for chunk in resp.body_iterator])


# ── viewer_url: fragment-контракт ───────────────────────────


class TestViewerUrl:
    def test_pdf_page_locator(self):
        assert (
            viewer_url(SHA, {"kind": "page", "start": 42, "end": 42})
            == f"/documents/{SHA}#page=42"
        )

    def test_pdf_page_range_uses_start(self):
        assert (
            viewer_url(SHA, {"kind": "page", "start": 7, "end": 9})
            == f"/documents/{SHA}#page=7"
        )

    def test_media_timestamp_int(self):
        assert (
            viewer_url(SHA, {"kind": "timestamp", "start": 61, "end": 90})
            == f"/documents/{SHA}#t=61"
        )

    def test_media_timestamp_float(self):
        assert (
            viewer_url(SHA, {"kind": "timestamp", "start": 61.5, "end": 90})
            == f"/documents/{SHA}#t=61.5"
        )

    def test_no_locator_bare(self):
        assert viewer_url(SHA) == f"/documents/{SHA}"
        assert viewer_url(SHA, None) == f"/documents/{SHA}"

    def test_other_kinds_no_fragment(self):
        assert (
            viewer_url(SHA, {"kind": "sheet_row", "start": 3, "end": 3})
            == f"/documents/{SHA}"
        )
        assert (
            viewer_url(SHA, {"kind": "image", "start": 1, "end": 1})
            == f"/documents/{SHA}"
        )

    def test_zone_private_before_fragment(self):
        url = viewer_url(SHA, {"kind": "page", "start": 3}, zone="private")
        assert url == f"/documents/{SHA}?zone=private#page=3"

    def test_zone_private_no_locator(self):
        assert viewer_url(SHA, zone="private") == f"/documents/{SHA}?zone=private"

    def test_zone_public_no_query(self):
        assert viewer_url(SHA, zone="public") == f"/documents/{SHA}"

    def test_unknown_zone_fails_loud(self):
        with pytest.raises(ValueError):
            viewer_url(SHA, zone="both")

    def test_broken_locator_fail_soft_bare(self):
        assert viewer_url(SHA, {"kind": "page", "start": "x"}) == f"/documents/{SHA}"
        assert viewer_url(SHA, {"start": 5}) == f"/documents/{SHA}"
        assert viewer_url(SHA, {}) == f"/documents/{SHA}"


class TestCitationViewerUrl:
    def test_level_b_with_locator(self):
        cite = {
            "source_id": "src-ab",
            "title": "T",
            "authors": ["A"],
            "viewer_url": f"/documents/{SHA}",
            "sha256": SHA,
            "locator": {"kind": "page", "start": 12, "end": 12},
        }
        assert citation_viewer_url(cite) == f"/documents/{SHA}#page=12"

    def test_level_b_without_locator(self):
        cite = {"viewer_url": f"/documents/{SHA}", "sha256": SHA}
        assert citation_viewer_url(cite) == f"/documents/{SHA}"

    def test_no_viewer_url_none(self):
        assert citation_viewer_url({"sha256": SHA}) is None
        assert citation_viewer_url({}) is None
        assert citation_viewer_url(None) is None


class TestCitationViewerUrlZone:
    """Ф4-fix1 (P1-1): citation несёт read-time zone → ссылка несёт маркер.

    Мутационные детекторы: снятие zone-прокидывания (M3) / убирание zone из
    citation сервера (M1) роняют test_private_zone_marker.
    """

    def _cite(self, zone: str | None) -> dict:
        cite = {
            "viewer_url": f"/documents/{SHA}",
            "sha256": SHA,
            "locator": {"kind": "page", "start": 12, "end": 12},
        }
        if zone is not None:
            cite["zone"] = zone
        return cite

    def test_private_zone_marker(self):
        """private-citation → `?zone=private` ДО фрагмента (гейт прокси)."""
        assert citation_viewer_url(self._cite("private")) == (
            f"/documents/{SHA}?zone=private#page=12"
        )

    def test_public_zone_no_query(self):
        assert citation_viewer_url(self._cite("public")) == f"/documents/{SHA}#page=12"

    def test_zone_missing_backward_compatible(self):
        """citation без zone (старый сервер) → без маркера, ссылка жива."""
        assert citation_viewer_url(self._cite(None)) == f"/documents/{SHA}#page=12"

    def test_unknown_zone_fail_soft_no_marker(self):
        """Мусорная зона → без маркера (fail-soft: upstream-ключ остаётся гейтом)."""
        assert citation_viewer_url(self._cite("weird")) == f"/documents/{SHA}#page=12"


# ── Гейт зоны + роль-ключи ──────────────────────────────────


class TestZoneGuard:
    async def test_admin_private_proxied(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("admin"), query=b"zone=private"), SHA
        )
        assert resp.status_code == 200
        assert await _body(resp) == PDF_BODY
        assert len(up.calls) == 1

    async def test_editor_private_404_no_upstream(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("editor"), query=b"zone=private"), SHA
        )
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"
        assert up.calls == []

    async def test_contributor_private_404(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("contributor"), query=b"zone=private"), SHA
        )
        assert resp.status_code == 404
        assert up.calls == []

    async def test_unknown_role_private_404(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("ghost"), query=b"zone=private"), SHA
        )
        assert resp.status_code == 404
        assert up.calls == []

    async def test_contributor_public_proxied(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("contributor"), query=b"zone=public"), SHA
        )
        assert resp.status_code == 200
        assert len(up.calls) == 1

    async def test_editor_no_zone_default_public(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(_request(session=_session("editor")), SHA)
        assert resp.status_code == 200

    async def test_legacy_empty_store_is_admin(self, tmp_path):
        """Legacy (пустой стор, без identity) → admin: бит-в-бит с консолью."""
        up = _Upstream()
        resp = await _handler(tmp_path, up)(_request(query=b"zone=private"), SHA)
        assert resp.status_code == 200

    async def test_fail_closed_store_without_identity(self, tmp_path):
        """Непустой стор без identity → contributor: private закрыт."""
        up = _Upstream()
        store = _store_with(tmp_path, "editor")
        resp = await _handler(tmp_path, up, users=store)(
            _request(query=b"zone=private"), SHA
        )
        assert resp.status_code == 404
        assert up.calls == []

    async def test_invalid_zone_value_400(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(session=_session("admin"), query=b"zone=both"), SHA
        )
        assert resp.status_code == 400
        assert up.calls == []


class TestShaSyntax:
    @pytest.mark.parametrize("bad", ["xyz", "AB" * 32, "a" * 63, "a" * 65, ""])
    async def test_bad_sha_400_no_upstream(self, tmp_path, bad):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(_request(session=_session("admin")), bad)
        assert resp.status_code == 400
        assert resp.headers["cache-control"] == "no-store"
        assert up.calls == []


class TestRoleKeys:
    async def test_admin_key_used(self, tmp_path):
        up = _Upstream()
        await _handler(tmp_path, up)(_request(session=_session("admin")), SHA)
        assert up.calls[0].headers.get("X-API-Key") == K_ADMIN

    async def test_editor_key_used(self, tmp_path):
        up = _Upstream()
        await _handler(tmp_path, up)(_request(session=_session("editor")), SHA)
        assert up.calls[0].headers.get("X-API-Key") == K_EDITOR

    async def test_contributor_key_used(self, tmp_path):
        up = _Upstream()
        await _handler(tmp_path, up)(_request(session=_session("contributor")), SHA)
        assert up.calls[0].headers.get("X-API-Key") == K_CONTRIB

    async def test_unknown_role_falls_back_to_base(self, tmp_path):
        up = _Upstream()
        await _handler(tmp_path, up)(_request(session=_session("ghost")), SHA)
        assert up.calls[0].headers.get("X-API-Key") == K_BASE

    async def test_legacy_base_key(self, tmp_path):
        up = _Upstream()
        await _handler(tmp_path, up)(_request(), SHA)
        assert up.calls[0].headers.get("X-API-Key") == K_BASE


# ── Проброс статусов/заголовков/Range + стриминг ────────────


class TestPassthrough:
    async def test_upstream_404_passthrough(self, tmp_path):
        up = _Upstream(status=404)
        resp = await _handler(tmp_path, up)(_request(session=_session("admin")), SHA)
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"

    async def test_upstream_401_passthrough(self, tmp_path):
        up = _Upstream(status=401)
        resp = await _handler(tmp_path, up)(_request(session=_session("editor")), SHA)
        assert resp.status_code == 401

    async def test_range_forwarded_206(self, tmp_path):
        up = _Upstream(status=206, body=PDF_BODY[:10])
        resp = await _handler(tmp_path, up)(
            _request(session=_session("admin"), headers={"Range": "bytes=0-9"}), SHA
        )
        assert up.calls[0].headers.get("Range") == "bytes=0-9"
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 0-9/10"
        assert await _body(resp) == PDF_BODY[:10]

    async def test_upstream_416_passthrough(self, tmp_path):
        up = _Upstream(status=416)
        resp = await _handler(tmp_path, up)(
            _request(session=_session("admin"), headers={"Range": "bytes=9999-"}), SHA
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"].startswith("bytes */")

    async def test_headers_and_no_store(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(_request(session=_session("admin")), SHA)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["content-disposition"] == 'inline; filename="doc.pdf"'
        assert resp.headers["accept-ranges"] == "bytes"

    async def test_streaming_not_buffered(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(_request(session=_session("admin")), SHA)
        assert isinstance(resp, StreamingResponse)
        assert await _body(resp) == PDF_BODY

    async def test_head_no_body(self, tmp_path):
        up = _Upstream()
        resp = await _handler(tmp_path, up)(
            _request(method="HEAD", session=_session("admin")), SHA
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["cache-control"] == "no-store"
        assert up.calls[0].method == "HEAD"

    async def test_transport_error_502(self, tmp_path):
        up = _Upstream(status=httpx.ConnectError("boom"))
        resp = await _handler(tmp_path, up)(_request(session=_session("admin")), SHA)
        assert resp.status_code == 502
        assert resp.headers["cache-control"] == "no-store"


# ── Регистрация роута (end-to-end через ASGITransport) ──────


class TestRegistration:
    async def test_route_served_end_to_end_legacy_admin(self, tmp_path):
        """Пустой стор (legacy) → эффективная роль admin → private проксируется."""
        from fastapi import FastAPI

        up = _Upstream()
        app = FastAPI()
        register_documents_proxy(
            app,
            users_store=UserStore(users_file=str(tmp_path / "empty.jsonl")),
            base_url=UPSTREAM,
            base_key=K_BASE,
            admin_key=K_ADMIN,
            editor_key=K_EDITOR,
            contributor_key=K_CONTRIB,
            transport=up.transport(),
        )
        paths = [r.path for r in app.routes]
        assert DOCUMENTS_ROUTE in paths
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console.test"
        ) as client:
            r = await client.get(f"/documents/{SHA}?zone=private")
            assert r.status_code == 200
            assert r.content == PDF_BODY
            assert r.headers["cache-control"] == "no-store"
            assert up.calls[0].headers.get("X-API-Key") == K_BASE  # legacy → base-ключ


class TestRegistrationAccess:
    async def test_no_session_fail_closed_404_private(self, tmp_path):
        from fastapi import FastAPI

        up = _Upstream()
        app = FastAPI()
        register_documents_proxy(
            app,
            users_store=_store_with(tmp_path, "editor"),
            base_url=UPSTREAM,
            base_key=K_BASE,
            admin_key=K_ADMIN,
            editor_key=K_EDITOR,
            contributor_key=K_CONTRIB,
            transport=up.transport(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console.test"
        ) as client:
            r = await client.get(f"/documents/{SHA}?zone=private")
            assert r.status_code == 404
            r2 = await client.get(f"/documents/{SHA}")
            assert r2.status_code == 200  # public-умолчание, legacy-независимо
            assert r2.headers["cache-control"] == "no-store"


# ── Неаутентифицированный → 302 /login (middleware v3) ──────


class _StubApp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)


def _run_mw(mw, scope):
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


class TestUnauthenticatedRedirect:
    def test_documents_without_session_302_login(self, tmp_path):
        """Существующее поведение консоли: нет сессии/Basic → 302 /login?next=."""
        store = _store_with(tmp_path, "editor")
        stub = _StubApp()
        mw = ConsoleAuthMiddleware(app=stub, mode="on", users=store, failure_delay=0.0)
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/documents/{SHA}",
            "headers": [],
            "query_string": b"zone=private",
            "http_version": "1.1",
            "scheme": "http",
        }
        sent = _run_mw(mw, scope)
        assert stub.calls == []
        assert sent[0]["status"] == 302
        headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
        assert headers["location"].startswith("/login?next=")
        assert "documents" in headers["location"]

    def test_documents_with_session_passes(self, tmp_path):
        store = _store_with(tmp_path, "editor")
        stub = _StubApp()
        mw = ConsoleAuthMiddleware(app=stub, mode="on", users=store, failure_delay=0.0)
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/documents/{SHA}",
            "headers": [],
            "query_string": b"",
            "session": {
                "identity": {
                    "username": "user0",
                    "role": "editor",
                    "store_version": store.store_version,
                }
            },
            "http_version": "1.1",
            "scheme": "http",
        }
        _run_mw(mw, scope)
        assert len(stub.calls) == 1
