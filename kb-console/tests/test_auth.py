"""Юнит-тесты HTTP Basic auth kb-console (code-2026-09-20-002, вариант A2d).

Покрытие (спецификация .boardData.md §7:4388):
  - unit: parse Basic (валидный / кривой base64 / отсутствует / не-Basic),
    verify пароля (верный / неверный, username игнорируется),
    interlock-матрица resolve_auth_mode (auto / off / required / невалидный);
  - ASGI fake-scope: http 401+WWW-Authenticate / pass-through,
    websocket close-до-accept / pass-through, lifespan → транзит,
    режим off → всё транзитом, задержка на неверный пароль.

Подпроцессов нет — импорт напрямую (auth.py не читает env на импорте;
env-контракт тестируется в test_config.py / test_auth_smoke.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

import pytest

from kb_console.auth import ConsoleAuthMiddleware, resolve_auth_mode, verify_basic_auth


def _basic(user: str, password: str) -> str:
    """Собрать валидный Authorization: Basic-заголовок."""
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# ── unit: parse / verify ─────────────────────────────────────


def test_verify_valid_password():
    assert verify_basic_auth(_basic("admin", "secret"), "secret") is True


def test_verify_wrong_password():
    assert verify_basic_auth(_basic("admin", "wrong"), "secret") is False


def test_verify_username_ignored():
    """Один оператор: username в проверке не участвует."""
    assert verify_basic_auth(_basic("anyone", "secret"), "secret") is True
    assert verify_basic_auth(_basic("", "secret"), "secret") is True


def test_verify_missing_header():
    assert verify_basic_auth("", "secret") is False


def test_verify_bad_base64():
    assert verify_basic_auth("Basic !!!!not-base64!!!!", "secret") is False


def test_verify_non_basic_scheme():
    assert verify_basic_auth("Bearer dGVzdA==", "secret") is False


def test_verify_no_colon_in_decoded():
    """Декод без ':' → пароля нет → отказ."""
    token = base64.b64encode(b"useronly").decode()
    assert verify_basic_auth("Basic " + token, "secret") is False


# ── unit: interlock-матрица (resolve_auth_mode) ─────────────


def test_interlock_auto_empty_loopback_off(caplog):
    """auto + пусто + loopback → off, старт тихий (текущее поведение 001)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "127.0.0.1") == "off"
    assert not caplog.records


def test_interlock_auto_empty_ipv6_loopback_off(caplog):
    """::1 — тоже loopback (расширение спеки: loopback-множество, не только 127.0.0.1)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "::1") == "off"
    assert not caplog.records


def test_interlock_auto_empty_nonloopback_warn(caplog):
    """auto + пусто + bind≠loopback → off + warning, старт продолжается."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "auto", "0.0.0.0") == "off"
    assert any("CONSOLE_PASSWORD" in r.getMessage() for r in caplog.records)


def test_interlock_auto_password_on():
    assert resolve_auth_mode("secret", "auto", "127.0.0.1") == "on"
    assert resolve_auth_mode("secret", "auto", "0.0.0.0") == "on"


def test_interlock_off_disabled_silent(caplog):
    """off без пароля → off, warn подавлен (bridge-рецепт без пароля)."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("", "off", "0.0.0.0") == "off"
    assert not caplog.records


def test_interlock_off_with_password_warns(caplog):
    """off + пароль задан → disabled + warning «пароль игнорируется»."""
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        assert resolve_auth_mode("secret", "off", "127.0.0.1") == "off"
    assert any("игнорируется" in r.getMessage() for r in caplog.records)


def test_interlock_required_empty_runtime_error():
    """required + пусто → RuntimeError (fail-fast, процесс не стартует)."""
    with pytest.raises(RuntimeError, match="CONSOLE_AUTH=required"):
        resolve_auth_mode("", "required", "127.0.0.1")


def test_interlock_required_password_on():
    assert resolve_auth_mode("secret", "required", "0.0.0.0") == "on"


def test_interlock_invalid_mode_value_error():
    """Невалидный CONSOLE_AUTH → ValueError со списком допустимых значений."""
    with pytest.raises(ValueError, match="auto.*off.*required"):
        resolve_auth_mode("secret", "foo", "127.0.0.1")


# ── ASGI fake-scope: ConsoleAuthMiddleware ──────────────────


class _StubApp:
    """Фиксирует вызовы внутреннего ASGI-приложения."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)


def _make_scope(
    scope_type: str,
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    scope = {"type": scope_type, "path": path, "headers": headers or []}
    if scope_type == "http":
        scope.update({"method": "GET", "http_version": "1.1", "scheme": "http"})
    return scope


def _make_mw(
    password: str = "secret",
    mode: str = "on",
    failure_delay: float = 0.0,
) -> tuple[ConsoleAuthMiddleware, _StubApp]:
    stub = _StubApp()
    mw = ConsoleAuthMiddleware(
        app=stub, password=password, mode=mode, failure_delay=failure_delay
    )
    return mw, stub


def _run(mw: ConsoleAuthMiddleware, scope: dict) -> list[dict]:
    """Прогнать middleware через asyncio.run; вернуть отправленные сообщения."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent


def test_http_no_credentials_401_with_challenge():
    """http без кредов → 401 + WWW-Authenticate: Basic realm="kb-console"; app НЕ вызван."""
    mw, stub = _make_mw()
    sent = _run(mw, _make_scope("http", "/status"))
    assert stub.calls == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401
    headers = {k.decode().lower(): v.decode() for k, v in sent[0]["headers"]}
    assert headers["www-authenticate"] == 'Basic realm="kb-console"'


def test_http_no_credentials_silent_challenge(caplog):
    """Первичный заход браузера (заголовка нет) — тихий 401, БЕЗ warning."""
    mw, _stub = _make_mw()
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        _run(mw, _make_scope("http", "/status"))
    assert not caplog.records


def test_http_with_credentials_pass_through():
    """http с верными кредами → app вызван, middleware ничего не отправляет сама."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "secret").encode())]
    )
    sent = _run(mw, scope)
    assert len(stub.calls) == 1
    assert sent == []


def test_http_wrong_password_401_and_warning(caplog):
    """Неверный пароль → 401 + logger.warning (brute-force-видимость)."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "wrong").encode())]
    )
    with caplog.at_level(logging.WARNING, logger="kb_console.auth"):
        sent = _run(mw, scope)
    assert stub.calls == []
    assert sent[0]["status"] == 401
    assert any("401" in r.getMessage() or "auth" in r.getMessage().lower() for r in caplog.records)


def test_failure_delay_sleeps_on_wrong_password():
    """Фиксированная задержка на неверный пароль (brute-force замедление)."""
    mw, _stub = _make_mw(failure_delay=0.2)
    scope = _make_scope(
        "http", "/status", headers=[(b"authorization", _basic("op", "wrong").encode())]
    )
    t0 = time.monotonic()
    _run(mw, scope)
    assert time.monotonic() - t0 >= 0.15


def test_websocket_no_credentials_close_before_accept():
    """websocket без кредов → websocket.close ДО accept, app НЕ вызван."""
    mw, stub = _make_mw()
    sent = _run(mw, _make_scope("websocket", "/_nicegui_ws/socket.io/"))
    assert stub.calls == []
    assert sent and sent[0]["type"] == "websocket.close"
    # accept не отправлялся — close строго до accept
    assert all(msg["type"] != "websocket.accept" for msg in sent)


def test_websocket_with_credentials_pass_through():
    """websocket с верными кредами → app вызван (WS-транспорт жив)."""
    mw, stub = _make_mw()
    scope = _make_scope(
        "websocket",
        "/_nicegui_ws/socket.io/",
        headers=[(b"authorization", _basic("op", "secret").encode())],
    )
    sent = _run(mw, scope)
    assert len(stub.calls) == 1
    assert sent == []


def test_lifespan_scope_transit():
    """lifespan → транзит: app вызван, никакого 401/close (иначе старт uvicorn сломан)."""
    mw, stub = _make_mw()
    sent = _run(mw, {"type": "lifespan", "message": "lifespan.startup"})
    assert len(stub.calls) == 1
    assert sent == []


def test_other_scope_types_transit():
    """Любые прочие типы scope → транзит (guard первым до парсинга)."""
    mw, stub = _make_mw()
    _run(mw, {"type": "odd.future.scope"})
    assert len(stub.calls) == 1


def test_mode_off_transits_everything():
    """Режим off → http и websocket без кредов проходят транзитом (нулевой оверхед)."""
    mw_http, stub_http = _make_mw(mode="off")
    _run(mw_http, _make_scope("http", "/status"))
    assert len(stub_http.calls) == 1

    mw_ws, stub_ws = _make_mw(mode="off")
    _run(mw_ws, _make_scope("websocket", "/_nicegui_ws/socket.io/"))
    assert len(stub_ws.calls) == 1


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
