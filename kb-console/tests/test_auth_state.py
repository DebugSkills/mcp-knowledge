"""Тесты auth_state — иерархия исключений + key-scoped AuthState (007 §7.2)."""

from __future__ import annotations

import pytest

from kb_console.core.auth_state import (
    AuthenticationError,
    AuthError,
    AuthState,
    ForbiddenError,
    TransportError,
    get_auth_state,
    record_auth_error,
)

# ── Иерархия (N-1: канон имён; OQ-2: back-compat RuntimeError) ──


class TestExceptionHierarchy:
    def test_auth_errors_are_runtime_errors(self):
        """AuthError-иерархия наследует RuntimeError (back-compat, R4)."""
        assert issubclass(AuthError, RuntimeError)
        assert issubclass(AuthenticationError, AuthError)
        assert issubclass(ForbiddenError, AuthError)
        assert issubclass(TransportError, RuntimeError)

    def test_transport_is_not_auth(self):
        """TransportError — НЕ AuthError: бэкофф, а не стоп."""
        assert not issubclass(TransportError, AuthError)

    def test_forbidden_text_has_no_key_hint(self):
        """403-баннер НЕ предлагает «ввести ключ» (спека §7.2b)."""
        exc = ForbiddenError("Доступ запрещён: недостаточно прав (403)")
        assert "ключ" not in str(exc).lower() or "введите" not in str(exc).lower()

    def test_key_ref_attribute(self):
        exc = AuthenticationError("x", key_ref="role:editor")
        assert exc.key_ref == "role:editor"


# ── Key-scoped AuthState (OQ-1/OQ-8) ────────────────────────────


class TestAuthStateKeyScope:
    def test_block_and_is_blocked(self):
        state = AuthState()
        state.block("AuthenticationError", 123.0, key_ref="global")
        assert state.is_blocked(key_ref="global")
        assert state.blocked_kind(key_ref="global") == "AuthenticationError"
        assert state.blocked_since(key_ref="global") == 123.0

    def test_block_key_a_does_not_block_key_b(self):
        """AC#4: блок ключа A не мешает поллеру ключа B."""
        state = AuthState()
        state.block("AuthenticationError", 1.0, key_ref="key-A")
        assert state.is_blocked(key_ref="key-A")
        assert not state.is_blocked(key_ref="key-B")

    def test_key_ref_is_required_kwarg(self):
        """OQ-8: key_ref обязательный kwarg без дефолта (P2-D)."""
        state = AuthState()
        with pytest.raises(TypeError):
            state.block("AuthenticationError", 1.0)
        with pytest.raises(TypeError):
            state.is_blocked()

    def test_empty_key_ref_rejected(self):
        state = AuthState()
        with pytest.raises(ValueError):
            state.block("AuthenticationError", 1.0, key_ref="")

    def test_unblock(self):
        state = AuthState()
        state.block("AuthenticationError", 1.0, key_ref="global")
        state.unblock(key_ref="global")
        assert not state.is_blocked(key_ref="global")

    def test_unblock_absent_is_noop(self):
        state = AuthState()
        state.unblock(key_ref="missing")  # не падает

    def test_clear(self):
        state = AuthState()
        state.block("AuthenticationError", 1.0, key_ref="a")
        state.block("ForbiddenError", 2.0, key_ref="b")
        state.clear()
        assert not state.is_blocked(key_ref="a")
        assert not state.is_blocked(key_ref="b")


# ── Singleton + record_auth_error ───────────────────────────────


class TestSingletonAndRecord:
    def test_get_auth_state_is_singleton(self):
        assert get_auth_state() is get_auth_state()

    def test_record_auth_error_blocks_key(self):
        state = AuthState()
        exc = AuthenticationError("401", key_ref="global")
        record_auth_error(exc, state=state)
        assert state.is_blocked(key_ref="global")
        assert state.blocked_kind(key_ref="global") == "AuthenticationError"

    def test_record_auth_error_defaults_to_global(self):
        state = AuthState()
        exc = ForbiddenError("403")  # key_ref не задан
        record_auth_error(exc, state=state)
        assert state.is_blocked(key_ref="global")
        assert state.blocked_kind(key_ref="global") == "ForbiddenError"
