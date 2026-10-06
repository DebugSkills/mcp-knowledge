"""Тесты UI-компонента «прикрепить файл в KB» (Ф2 #6b-1, attach_upload).

Трасса: arch-2026-10-05-ai-workspace (plans/arch-2026-10-05-ai-workspace-plan.md,
строка #6). Часть 6b-1 — ТОЛЬКО компонент + тест; интеграция в pages/chat.py — 6b-2.

Инварианты (зеркало test_ws_attach.py, UI-слой):
- гейт UI: non-admin → upload-виджет НЕ создаётся, только label-подсказка
  (defense-in-depth к серверному гейту 403 — UI не предлагает эскалацию);
- admin → upload с auto_upload, max_file_size=MAX_FILE_SIZE, callable-хендлеры;
- happy-path: attach_to_kb вызван с (filename, raw, role, mcp_client из
  client_factory); успех → notify positive с collection_id;
- AttachError → notify negative «[code] message», НЕ пробрасывается наружу;
- generic-исключение → notify negative «Ошибка вложения: …», НЕ пробрасывается;
- клиент закрывается (aclose) в finally в обоих исходах (fail-soft).

Механика: ``ui`` monkeypatch-ится НА УРОВНЕ МОДУЛЯ компонента — реальный
NiceGUI-сервер НЕ поднимается (unit-слой, без slot-context).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import kb_console.components.attach_upload as au
from kb_console.components.attach_upload import attach_allowed, build_attach_upload
from kb_console.core.mcp_client import MCPClient
from kb_console.core.utils import MAX_FILE_SIZE
from kb_console.core.ws_attach import AttachError


class FakeUI:
    """Рекордер вызовов nicegui.ui, нужных компоненту: label/upload/notify."""

    def __init__(self) -> None:
        self.labels: list[str] = []
        self.uploads: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, str | None]] = []

    def label(self, text: str) -> SimpleNamespace:
        self.labels.append(text)
        return SimpleNamespace(classes=lambda *a, **k: None)

    def upload(self, **kwargs: Any) -> SimpleNamespace:
        self.uploads.append(kwargs)
        return SimpleNamespace(props=lambda *a, **k: None, classes=lambda *a, **k: None)

    def notify(self, message: str, *, type: str | None = None) -> None:
        self.notifications.append((message, type))


class FakeFile:
    """Файл события ui.upload (NiceGUI 3.15): атрибут .name + async .read()."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeUploadEvent:
    """Событие ui.upload: e.file — FakeFile (контракт import_page.handle_upload)."""

    def __init__(self, file: FakeFile) -> None:
        self.file = file


class FakeAttachClient(MCPClient):
    """Fake MCP-клиента: наследник НАСТОЯЩЕГО MCPClient (правило 10 —
    контракт проверяется на реальном типе, не на MagicMock).

    attach_to_kb в этих тестах подменяется (его MCP-контракт покрыт
    test_ws_attach.py), но клиент передаётся через client_factory как есть:
    компонент обязан его закрыть (aclose) — рекордер closed.
    """

    def __init__(self) -> None:
        super().__init__(base_url="http://fake.local", api_key="")
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


# ── 1. attach_allowed: private admin-only (D7) ───────────────────────
class TestAttachAllowed:
    def test_admin_allowed(self):
        assert attach_allowed("admin") is True

    @pytest.mark.parametrize("role", ["editor", "contributor", None, "", "unknown"])
    def test_non_admin_denied(self, role):
        """private — только admin; None/неизвестная роль → fail-closed."""
        assert attach_allowed(role) is False


# ── 2. Гейт UI: non-admin не получает upload-виджет ──────────────────
class TestUiGate:
    def test_non_admin_no_upload_widget(self, monkeypatch):
        fake_ui = FakeUI()
        monkeypatch.setattr(au, "ui", fake_ui)

        build_attach_upload("contributor")

        assert fake_ui.uploads == []  # upload-виджет НЕ создан
        assert any(
            "Вложения доступны только администратору" in text for text in fake_ui.labels
        )

    def test_admin_gets_upload_widget(self, monkeypatch):
        fake_ui = FakeUI()
        monkeypatch.setattr(au, "ui", fake_ui)

        build_attach_upload("admin")

        assert len(fake_ui.uploads) == 1
        kwargs = fake_ui.uploads[0]
        assert kwargs["max_file_size"] == MAX_FILE_SIZE
        assert kwargs["auto_upload"] is True
        assert callable(kwargs["on_upload"])
        assert callable(kwargs["on_rejected"])


# ── 3. on_upload: вызов attach_to_kb и исходы ────────────────────────
class TestOnUpload:
    def _build(self, monkeypatch, attach_impl) -> tuple[FakeUI, FakeAttachClient]:
        """Собрать компонент под admin с подменённым attach_to_kb."""
        fake_ui = FakeUI()
        monkeypatch.setattr(au, "ui", fake_ui)
        monkeypatch.setattr(au, "attach_to_kb", attach_impl)
        client = FakeAttachClient()
        build_attach_upload("admin", client_factory=lambda: client)
        return fake_ui, client

    async def test_happy_path(self, monkeypatch):
        captured: dict[str, Any] = {}

        async def fake_attach(
            *, filename: str, raw: bytes, role: str | None, mcp_client: Any
        ) -> dict:
            captured.update(
                filename=filename, raw=raw, role=role, mcp_client=mcp_client
            )
            return {
                "collection_id": "c1",
                "imported": 3,
                "failed": 0,
                "zone": "private",
            }

        fake_ui, client = self._build(monkeypatch, fake_attach)

        await fake_ui.uploads[0]["on_upload"](
            FakeUploadEvent(FakeFile("note.md", b"# title\n"))
        )

        assert captured["filename"] == "note.md"
        assert captured["raw"] == b"# title\n"
        assert captured["role"] == "admin"
        assert isinstance(captured["mcp_client"], MCPClient)
        assert captured["mcp_client"] is client  # клиент из client_factory
        assert fake_ui.notifications == [
            ("Файл добавлен в KB (private): c1", "positive")
        ]
        assert client.closed == 1  # finally: закрыт и на успехе

    async def test_attach_error_negative_notify(self, monkeypatch):
        async def fake_attach(**_kwargs: Any) -> dict:
            raise AttachError(403, "nope")

        fake_ui, client = self._build(monkeypatch, fake_attach)

        # Исключение НЕ проглочено наружу (хендлер сам гасит) — тест доходит
        # до ассертов:
        await fake_ui.uploads[0]["on_upload"](
            FakeUploadEvent(FakeFile("note.md", b"text"))
        )

        assert len(fake_ui.notifications) == 1
        message, ntype = fake_ui.notifications[0]
        assert ntype == "negative"
        assert "403" in message and "nope" in message
        assert client.closed == 1  # finally выполняется и при AttachError

    async def test_generic_error_negative_notify_and_close(self, monkeypatch):
        async def fake_attach(**_kwargs: Any) -> dict:
            raise RuntimeError("boom")

        fake_ui, client = self._build(monkeypatch, fake_attach)

        await fake_ui.uploads[0]["on_upload"](
            FakeUploadEvent(FakeFile("note.md", b"text"))
        )

        assert fake_ui.notifications == [("Ошибка вложения: boom", "negative")]
        assert client.closed == 1  # finally: закрыт даже при generic-сбое
