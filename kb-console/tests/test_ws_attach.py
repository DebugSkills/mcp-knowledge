"""Тесты серверного канала вложений «чат верстака → KB» (Ф2 #6a, ws_attach).

Трасса: arch-2026-10-05-ai-workspace (plans/arch-2026-10-05-ai-workspace-plan.md,
строка #6). Часть 6a — только серверный модуль + тесты; UI-обвязка — отдельно (6b).

Negative-first инварианты:
- private admin-only v1 (D7): contributor/editor/None/"" → AttachError(403)
  ДО любого MCP-вызова (сервисный импорт-ключ — НЕ обход гейта роли);
- params для MCP — строгий allowlist (build_import_params), zone всегда private;
- структурный негатив: сигнатура attach_to_kb НЕ принимает target/zone-полей;
- MCP-отказы ('error' / partial_success+failed>0) и исключения НЕ проглатываются.

Форма ответа import_content — реальный контракт сервера (mcp_server/tests/
integration/test_import_flow.py): dict с collection_id/imported/failed/
partial_success; словарь с 'error' — отказ.
"""

from __future__ import annotations

import inspect

import pytest

from kb_console.core.mcp_client import MCPClient
from kb_console.core.utils import MAX_FILE_SIZE, _sanitize_title
from kb_console.core.ws_attach import (
    ALLOWED_EXTS,
    AttachError,
    attach_to_kb,
    build_import_params,
)


class FakeImportMCP(MCPClient):
    """Fake MCP-клиента: наследник НАСТОЯЩЕГО MCPClient (правило 10 —
    контракт tools_call проверяется на реальном типе, не на MagicMock).

    Записывает вызовы ``calls: list[(name, params)]``; ответ/исключение
    настраиваются (result / exc).
    """

    def __init__(self, result: object = None, exc: BaseException | None = None) -> None:
        super().__init__(base_url="http://fake.local", api_key="")
        self.calls: list[tuple[str, dict]] = []
        self.result = result
        self.exc = exc

    async def tools_call(
        self,
        name: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append((name, dict(params or {})))
        if self.exc is not None:
            raise self.exc
        return self.result


# ── 1. build_import_params: строгий allowlist ────────────────────────


def test_build_import_params_exact_allowlist_keys() -> None:
    params = build_import_params(
        "Заметки сессии.md", "текст", domain="attachments", subject="uploads"
    )
    assert set(params) == {
        "content",
        "content_type",
        "domain",
        "subject",
        "title",
        "zone",
        "wait_for_index",
    }
    assert params["zone"] == "private"
    assert params["content_type"] == "book"
    assert params["wait_for_index"] is False
    assert params["content"] == "текст"
    assert params["domain"] == "attachments"
    assert params["subject"] == "uploads"


def test_build_import_params_no_target_keys() -> None:
    """Ни один target/zone-ключ вызывающего не попадает в params."""
    params = build_import_params("a.md", "x", domain="d", subject="s")
    for forbidden in (
        "replace_collection_id",
        "collection_id",
        "reimport_in_place",
        "target",
        "replace_on_partial",
    ):
        assert forbidden not in params


def test_build_import_params_title_sanitized() -> None:
    raw_name = "##   Заголовок вложения.md  "
    params = build_import_params(raw_name, "x", domain="d", subject="s")
    assert params["title"] == _sanitize_title(raw_name)


def test_allowed_exts_v1_text_only() -> None:
    assert ALLOWED_EXTS == {".md", ".txt"}


# ── 2. Гейт роли: не-admin не доходит до MCP (403) ───────────────────


@pytest.mark.parametrize("role", ["contributor", "editor", None, "", "unknown-role"])
async def test_non_admin_forbidden_before_mcp(role: str | None) -> None:
    """Ролевой гейт — ДО любого MCP-вызова: клиент не вызывается ни разу."""
    fake = FakeImportMCP(result={"collection_id": "c", "imported": 1})
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(
            filename="a.md", raw=b"text content", role=role, mcp_client=fake
        )
    assert ei.value.code == 403
    assert fake.calls == []


# ── 3. Oversize → 413 (MCP не вызван) ────────────────────────────────


async def test_oversize_413_not_called() -> None:
    fake = FakeImportMCP()
    big = b"x" * (MAX_FILE_SIZE + 1)
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename="big.md", raw=big, role="admin", mcp_client=fake)
    assert ei.value.code == 413
    assert fake.calls == []


# ── 4. Плохой тип → 415 (MCP не вызван) ──────────────────────────────


@pytest.mark.parametrize("filename", ["virus.exe", "data.bin", "noext"])
async def test_bad_ext_415_not_called(filename: str) -> None:
    fake = FakeImportMCP()
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename=filename, raw=b"data", role="admin", mcp_client=fake)
    assert ei.value.code == 415
    assert fake.calls == []


# ── 5. Пустой/whitespace/нечитаемый → 400 ────────────────────────────


@pytest.mark.parametrize("raw", [b"", b"   \n\t  "])
async def test_empty_or_whitespace_400(raw: bytes) -> None:
    fake = FakeImportMCP()
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename="a.md", raw=raw, role="admin", mcp_client=fake)
    assert ei.value.code == 400
    assert fake.calls == []


async def test_undecodable_400() -> None:
    """Гейт декода: байты, невалидные и в utf-8, и в windows-1251 → 400."""
    fake = FakeImportMCP()
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(
            filename="a.md", raw=b"\x98\x81", role="admin", mcp_client=fake
        )
    assert ei.value.code == 400
    assert fake.calls == []


# ── 6. admin happy-path ──────────────────────────────────────────────


async def test_admin_happy_path() -> None:
    fake = FakeImportMCP(
        result={
            "collection_id": "book-123-collection",
            "imported": 3,
            "failed": 0,
            "partial_success": False,
        }
    )
    res = await attach_to_kb(
        filename="Заметки.md", raw=b"# hello", role="admin", mcp_client=fake
    )
    # ровно один вызов import_content с серверной зоной private
    assert len(fake.calls) == 1
    name, params = fake.calls[0]
    assert name == "import_content"
    assert params["zone"] == "private"
    assert params["title"] == _sanitize_title("Заметки.md")
    assert params["content"] == "# hello"
    # возвращённый dict — sparse-результат + серверная зона
    assert res["zone"] == "private"
    assert res["collection_id"] == "book-123-collection"
    assert res["imported"] == 3
    assert res["failed"] == 0
    # правило 10: fake — наследник реального MCPClient (контракт настоящий)
    assert isinstance(fake, MCPClient)


async def test_admin_custom_domain_subject_passed() -> None:
    fake = FakeImportMCP(result={"collection_id": "c", "imported": 1})
    await attach_to_kb(
        filename="a.txt",
        raw=b"text",
        role="admin",
        mcp_client=fake,
        domain="custom-domain",
        subject="custom-subject",
    )
    _, params = fake.calls[0]
    assert params["domain"] == "custom-domain"
    assert params["subject"] == "custom-subject"


# ── 7. MCP-отказы/исключения не проглатываются ───────────────────────


async def test_mcp_zone_error_403() -> None:
    """Серверный отказ зоны → AttachError(403), текст не проглочен."""
    fake = FakeImportMCP(result={"error": "zone denied"})
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename="a.md", raw=b"text", role="admin", mcp_client=fake)
    assert ei.value.code == 403
    assert "zone" in ei.value.message.lower()


async def test_mcp_generic_error_400() -> None:
    fake = FakeImportMCP(result={"error": "quota exceeded"})
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename="a.md", raw=b"text", role="admin", mcp_client=fake)
    assert ei.value.code == 400
    assert "quota" in ei.value.message


async def test_mcp_partial_success_failed_400() -> None:
    """partial_success=True и failed>0 → AttachError (молчаливый успех запрещён)."""
    fake = FakeImportMCP(
        result={
            "collection_id": "c",
            "imported": 2,
            "failed": 1,
            "partial_success": True,
        }
    )
    with pytest.raises(AttachError) as ei:
        await attach_to_kb(filename="a.md", raw=b"text", role="admin", mcp_client=fake)
    assert ei.value.code == 400
    assert "1" in ei.value.message


async def test_mcp_exception_propagates() -> None:
    """Сырое исключение клиента НЕ глотается и НЕ заворачивается в AttachError."""
    fake = FakeImportMCP(exc=RuntimeError("transport boom"))
    with pytest.raises(RuntimeError, match="transport boom"):
        await attach_to_kb(filename="a.md", raw=b"text", role="admin", mcp_client=fake)


# ── 8. Структурный негатив: сигнатура без target-полей ───────────────


def test_signature_has_no_target_params() -> None:
    """collection_id/target/zone/replace_* физически не принимаются."""
    sig = inspect.signature(attach_to_kb)
    forbidden = {"collection_id", "target", "zone", "replace_collection_id"}
    assert forbidden & set(sig.parameters) == set()
    # полный allowlist параметров
    assert set(sig.parameters) == {
        "filename",
        "raw",
        "role",
        "mcp_client",
        "domain",
        "subject",
        "timeout",
    }
