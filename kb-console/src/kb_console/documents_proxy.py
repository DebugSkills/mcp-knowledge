"""Консоль-прокси выдачи документов + viewer-ссылки (bibliography Ф4c, §3.4:177).

`GET/HEAD /documents/{sha256}` на NiceGUI-app (raw starlette-роут по образцу
`pages/requests_page.register_api`) → upstream `GET/HEAD /documents/{sha256}`
MCP-сервера (Ф4a) с роль-ключом сессии консоли.

Гейты (порядок строго до похода upstream):
1. Синтаксис id — 64 hex → иначе 400 (зеркало серверного R7; заодно закрывает
   path-инъекцию в upstream-URL).
2. Роль сессии — `identity_from_request` + `effective_role` (raw-роут
   эквивалент `current_role()`: session → Basic-fallback; legacy-пустой-стор
   → admin бит-в-бит с остальной консолью).
3. Зона документа — query-параметр `?zone=private` (маркер viewer-ссылок
   консоли): private → только admin (`ROLE_LEVEL`), иначе 404 (НЕ 403 —
   существование не раскрываем, семантика сервера §3.4:175). Без маркера —
   public-умолчание: доступность уровня (б) уже проверена на сервере ключом
   вызывающего при построении citation.

Upstream-запрос: ключ `api_key_for_role(role)` (M2: НЕ base-хардкод);
`Range` пробрасывается как есть; статусы 200/206/401/404/416 — как есть;
transport-сбой → 502 (не 500). Заголовки — whitelist (hop-by-hop мимо):
`Content-Type`, `X-Content-Type-Options: nosniff`, `Content-Disposition`,
`Content-Range`, `Accept-Ranges`, `Content-Length`; прокси ДОБАВЛЯЕТ
`Cache-Control: no-store` (политика: документы консоли не кешируются).
Тело — стриминг `httpx` stream → `StreamingResponse` (M4: без буферизации).

viewer-хелпер: PDF (kind=page) → `#page=N`, media (kind=timestamp) →
`#t=<sec>`, прочие/URL-only → без фрагмента; `?zone=private` ДО фрагмента.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import (
    MCP_API_KEY,
    MCP_API_KEY_ADMIN,
    MCP_API_KEY_CONTRIBUTOR,
    MCP_API_KEY_EDITOR,
    MCP_SERVER_URL,
)
from .core.identity import (
    ROLE_LEVEL,
    api_key_for_request,
    effective_role,
    identity_from_request,
)

__all__ = [
    "DOCUMENTS_ROUTE",
    "citation_viewer_url",
    "make_documents_handler",
    "register_documents_proxy",
    "viewer_url",
]

DOCUMENTS_ROUTE = "/documents/{sha256}"

# Строгий формат id — зеркально серверу (main.py:1221, oracle-имени R7).
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_VALID_ZONES = frozenset({"public", "private"})

# Whitelist пробрасываемых upstream-заголовков: только безопасные к копированию;
# всё прочее (вкл. hop-by-hop и server-специфику) не переносится.
_PASSTHROUGH_HEADERS = (
    "content-type",
    "content-length",
    "content-disposition",
    "content-range",
    "accept-ranges",
    "x-content-type-options",
)

# Документы — большие: connect/read-таймауты мягкие (read — на чанк).
_UPSTREAM_TIMEOUT = httpx.Timeout(30.0, read=300.0)


# ── Viewer-ссылки ───────────────────────────────────────────


def viewer_url(
    sha256: str, locator: dict[str, Any] | None = None, *, zone: str | None = None
) -> str:
    """URL просмотра документа: `/documents/{sha256}[?zone=private][#фрагмент]`.

    Фрагмент по locator-оси (план §3.2): kind=page (PDF) → `#page=N`
    (N = start, 1-based canonical-PDF); kind=timestamp (media) → `#t=<sec>`
    (целые секунды без `.0`); прочие виды (image/sheet_row) и URL-only —
    без фрагмента. Битый locator — fail-soft без фрагмента (ссылка жива).

    zone="private" — маркер для гейта прокси (см. модуль-docstring) —
    ставится ДО фрагмента; unknown-зона → ValueError (fail loud,
    не размываем зону тихо).
    """
    if zone is not None and zone not in _VALID_ZONES:
        raise ValueError(f"unknown zone: {zone!r}")
    url = f"/documents/{sha256}"
    if zone == "private":
        url += "?zone=private"
    return url + _fragment(locator)


def _fragment(locator: dict[str, Any] | None) -> str:
    """`#page=N` / `#t=<sec>` по kind; всё прочее/битое — пусто."""
    if not isinstance(locator, dict):
        return ""
    kind = locator.get("kind")
    start = locator.get("start")
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return ""  # Л1: спанов/оси нет — фрагмент не выдумываем
    if kind == "page":
        try:
            return f"#page={int(start)}"
        except (TypeError, ValueError):
            return ""
    if kind == "timestamp":
        seconds = float(start)
        text = str(int(seconds)) if seconds.is_integer() else str(seconds)
        return f"#t={text}"
    return ""


def citation_viewer_url(citation: dict[str, Any] | None) -> str | None:
    """URL просмотра из citation (уровень (б) §3.4:186): sha256 + locator + zone.

    None ⇔ citation нет/нет уровня (б) (viewer_url/sha256 отсутствуют —
    частичный рендер запрещён, ссылку не строим).

    P1-1 (Ф4-fix1): citation несёт read-time `zone` (сервер, content/citation)
    → private-ссылка получает маркер `?zone=private` для гейта прокси; без
    поля/`zone="public"` — без маркера (public-умолчание). Зона вне
    {public, private} → fail-soft без маркера: гейт прокси и upstream-ключ
    остаются гейтами (существование не раскрывается).
    """
    if not isinstance(citation, dict):
        return None
    sha256 = citation.get("sha256")
    if not citation.get("viewer_url") or not sha256:
        return None
    locator = citation.get("locator")
    zone = citation.get("zone")
    return viewer_url(
        str(sha256),
        locator if isinstance(locator, dict) else None,
        zone=zone if zone in _VALID_ZONES else None,
    )


# ── Прокси-хендлер ──────────────────────────────────────────


def _guard_response(status: int, error: str) -> JSONResponse:
    """Ответ прокси-гейта/сбоя: JSON + гигиена (no-store, nosniff)."""
    return JSONResponse(
        {"error": error},
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _proxied_headers(upstream: httpx.Response) -> dict[str, str]:
    """Whitelist upstream-заголовков + обязательный `Cache-Control: no-store`."""
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _PASSTHROUGH_HEADERS
    }
    headers["cache-control"] = "no-store"
    return headers


def _runtime_users() -> Any:
    """USERS_STORE из core.runtime (app.py кладёт при старте)."""
    from .core import runtime

    return runtime.USERS_STORE


def make_documents_handler(
    *,
    users_store: Any = None,
    base_url: str = MCP_SERVER_URL,
    base_key: str = MCP_API_KEY,
    admin_key: str = MCP_API_KEY_ADMIN,
    editor_key: str = MCP_API_KEY_EDITOR,
    contributor_key: str = MCP_API_KEY_CONTRIBUTOR,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout | None = None,
) -> Callable[[Request, str], Awaitable[Response]]:
    """Собрать хендлер `GET/HEAD /documents/{sha256}` (инжекция — для тестов).

    Все зависимости — параметрами: прод-регистрация берёт env-дефолты
    конфига и runtime.USERS_STORE; тесты подставляют MockTransport/UserStore.
    """

    async def documents(request: Request, sha256: str) -> Response:
        # 1. Синтаксис id → 400 без похода upstream.
        if _SHA256_RE.match(sha256) is None:
            return _guard_response(400, "invalid document id")

        # 2. Роль сессии (session → Basic; legacy → admin; fail-closed → contributor).
        users = users_store if users_store is not None else _runtime_users()
        identity = identity_from_request(request, users) if users is not None else None
        has_users = bool(users is not None and users.has_users())
        role = effective_role(identity, has_users=has_users)

        # 3. Гейт зоны: private → admin-only; отказ = 404 (не раскрываем существование).
        zone = request.query_params.get("zone", "")
        if zone and zone not in _VALID_ZONES:
            return _guard_response(400, "invalid zone")
        if zone == "private" and ROLE_LEVEL.get(role, 0) < ROLE_LEVEL["admin"]:
            return _guard_response(404, "document not found")

        # 4. Ключ по identity (роль-ключ; legacy без identity → base,
        #    бит-в-бит с api_key_for_request в identity.py) + проброс Range.
        api_key = api_key_for_request(
            identity,
            base=base_key,
            has_users=has_users,
            admin=admin_key,
            editor=editor_key,
            contributor=contributor_key,
        )
        upstream_headers: dict[str, str] = {}
        if api_key:
            upstream_headers["X-API-Key"] = api_key
        range_header = request.headers.get("range")
        if range_header:
            upstream_headers["Range"] = range_header

        method = "HEAD" if request.method.upper() == "HEAD" else "GET"
        client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout if timeout is not None else _UPSTREAM_TIMEOUT,
        )
        path = f"/documents/{sha256}"

        if method == "HEAD":
            try:
                upstream = await client.head(path, headers=upstream_headers)
            except httpx.HTTPError:
                return _guard_response(502, "upstream unavailable")
            headers_out = _proxied_headers(upstream)
            await client.aclose()
            return Response(status_code=upstream.status_code, headers=headers_out)

        try:
            upstream = await client.send(
                client.build_request("GET", path, headers=upstream_headers),
                stream=True,
            )
        except httpx.HTTPError:
            await client.aclose()
            return _guard_response(502, "upstream unavailable")

        async def _proxy_bytes():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _proxy_bytes(),
            status_code=upstream.status_code,
            headers=_proxied_headers(upstream),
        )

    return documents


def register_documents_proxy(nicegui_app: Any, **handler_kwargs: Any) -> None:
    """Зарегистрировать `GET/HEAD /documents/{sha256}` на NiceGUI(FastAPI)-app.

    Вызывается из app.py на module-level (паттерн requests_page.register_api);
    auth-редиректы неаутентифицированных — ConsoleAuthMiddleware (302 /login).
    """
    nicegui_app.add_api_route(
        DOCUMENTS_ROUTE,
        make_documents_handler(**handler_kwargs),
        methods=["GET", "HEAD"],
    )
