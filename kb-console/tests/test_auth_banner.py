"""Тесты auth_banner — тексты, show/hide, resume-пинг (007 §7.4, AC#1/AC#2)."""

from __future__ import annotations

import pytest

import kb_console.components.auth_banner as ab
from kb_console.components.auth_banner import AuthBanner
from kb_console.core.auth_state import (
    AuthenticationError,
    ForbiddenError,
    TransportError,
    get_auth_state,
)


@pytest.fixture(autouse=True)
def _clean_state():
    get_auth_state().clear()
    yield
    get_auth_state().clear()


class TestBannerTexts:
    def test_401_banner_mentions_key_and_resume(self):
        banner = AuthBanner(key_ref="global")
        banner.show(AuthenticationError("401"))
        assert "401" in banner.message
        assert "Проверить и продолжить" in banner.message or "F5" in banner.message

    def test_403_banner_has_no_key_hint(self):
        """§7.2b: у 403 текст БЕЗ «введите ключ»."""
        banner = AuthBanner(key_ref="global")
        banner.show(ForbiddenError("403"))
        assert "403" in banner.message
        assert "введите ключ" not in banner.message.lower()

    def test_hide_clears(self):
        banner = AuthBanner(key_ref="global")
        banner.show(AuthenticationError("401"))
        assert banner.visible
        banner.hide()
        assert not banner.visible
        assert banner.message == ""


class TestResumePing:
    async def test_resume_success_unblocks_and_calls_on_resume(self, monkeypatch):
        """200 от /data-version → блок снят, on_resume вызван."""
        state = get_auth_state()  # баннер работает с per-process singleton
        state.block("AuthenticationError", 1.0, key_ref="global")
        resumed = {"called": False}

        class FakeClient:
            async def get_data_version(self) -> int:
                return 7

            async def close(self) -> None:
                pass

        banner = AuthBanner(key_ref="global", on_resume=lambda: resumed.__setitem__("called", True))
        banner._visible = True
        monkeypatch.setattr(ab, "MCPClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(ab.ui, "notify", lambda msg, type: None)

        await banner._on_check_and_continue()

        assert not state.is_blocked(key_ref="global")
        assert resumed["called"] is True
        assert not banner.visible

    async def test_resume_still_401_keeps_block(self, monkeypatch):
        state = get_auth_state()  # per-process singleton
        state.block("AuthenticationError", 1.0, key_ref="global")

        class FakeClient:
            async def get_data_version(self) -> int:
                raise AuthenticationError("401")

            async def close(self) -> None:
                pass

        banner = AuthBanner(key_ref="global")
        monkeypatch.setattr(ab, "MCPClient", lambda **kwargs: FakeClient())
        notified: list[tuple[str, str]] = []
        monkeypatch.setattr(ab.ui, "notify", lambda msg, type: notified.append((msg, type)))

        await banner._on_check_and_continue()

        assert state.is_blocked(key_ref="global")  # ключ всё ещё мёртв
        assert notified and notified[0][1] == "warning"

    async def test_resume_transport_error_keeps_block(self, monkeypatch):
        state = get_auth_state()  # per-process singleton
        state.block("AuthenticationError", 1.0, key_ref="global")

        class FakeClient:
            async def get_data_version(self) -> int:
                raise TransportError("Сервер недоступен")

            async def close(self) -> None:
                pass

        banner = AuthBanner(key_ref="global")
        monkeypatch.setattr(ab, "MCPClient", lambda **kwargs: FakeClient())
        monkeypatch.setattr(ab.ui, "notify", lambda msg, type: None)

        await banner._on_check_and_continue()

        assert state.is_blocked(key_ref="global")
