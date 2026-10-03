"""Ф5b2 (bibliography): documents_check + documents_rebuild — admin-only MCP-тулы.

Покрывает (acceptance Ф5b2):
- (а) оба тула non-admin → 403, admin (write) → доступ; handler-успех;
- (б) documents_check дефолт create_issues=False → issues НЕ создаются;
      create_issues=True → создаются;
- (в) documents_rebuild идемпотентен (×2 → нулевые дельты);
- (г) реестр/схемы/scope (паттерн Ф5b1 test_tools_registered_and_scoped);
- (д) возврат соблюдает лимит samples (без неограниченного дампа).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mcp_server.auth import (
    EDITOR_TOOLS,
    IMPORT_TOOLS,
    READ_TOOLS,
    SUBSCRIBER_TOOLS,
    WRITE_TOOLS,
    AuthInfo,
    check_tool_permission,
)
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.document_store import DocumentStore
from mcp_server.tools import TOOLS, TOOL_HANDLERS
from mcp_server.tools.documents_admin import documents_check, documents_rebuild


def _source_entry(kid: str, blobs: dict | None) -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="source",
        zone="private",
        status="published",
        blobs=blobs,
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n")


class _FakeScanStore:
    """MarkdownStore-подобный фейк: reindex_scan + _parse_file (Source entries)."""

    def __init__(self, entries):
        self._entries = entries

    async def reindex_scan(self):
        return [Path(f"/fake/{i}.md") for i in range(len(self._entries))]

    def _parse_file(self, path: Path):
        return self._entries[int(path.stem)]


def _app_state(entries=None, document_store=None) -> SimpleNamespace:
    return SimpleNamespace(
        store=_FakeScanStore(entries or []),
        document_store=document_store,
    )


# ── (а) auth: non-admin denied / admin allowed ─────────────────


class TestAdminAuthMatrix:
    @pytest.mark.parametrize("tool", ["documents_check", "documents_rebuild"])
    def test_write_key_allowed(self, tool):
        check_tool_permission(AuthInfo(authenticated=True, key_level="write"), tool)

    @pytest.mark.parametrize("tool", ["documents_check", "documents_rebuild"])
    @pytest.mark.parametrize("level", ["editor", "read", "import", "subscriber"])
    def test_non_write_forbidden_403(self, tool, level):
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(AuthInfo(authenticated=True, key_level=level), tool)
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("tool", ["documents_check", "documents_rebuild"])
    def test_no_key_401(self, tool):
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(AuthInfo(authenticated=False), tool)
        assert exc.value.status_code == 401


# ── (б) documents_check: дефолт не создаёт issues / явный — создаёт ──


@pytest.fixture
def quality_tempdir(tmp_path):
    """Изоляция issues-store (паттерн test_documents_integrity)."""
    from mcp_server.quality.issues import set_store_dir

    set_store_dir(str(tmp_path))
    yield str(tmp_path)
    set_store_dir(None)


def _broken_links(kid: str) -> list:
    from mcp_server.quality.issues import list_issues

    return [
        i for i in list_issues(types=["broken_link"], status="open", limit=200)
        if i.knowledge_id == kid
    ]


async def test_documents_check_default_creates_no_issues(tmp_path, quality_tempdir):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    sid = "src-" + "a" * 16
    # Source со ссылкой на НЕсуществующий blob (дефект), но дефолт read-only.
    entry = _source_entry(sid, {"original": {"sha256": "a" * 64}, "derived": []})
    app_state = _app_state(entries=[entry], document_store=ds)

    result = await documents_check({}, app_state)

    assert result["ok"] is False
    assert result["issues"]["missing_blob"] == 1
    # Дефолт НЕ фиксирует issues в реестр
    assert _broken_links(sid) == []


async def test_documents_check_create_issues_true_creates(tmp_path, quality_tempdir):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    sid = "src-" + "b" * 16
    entry = _source_entry(sid, {"original": {"sha256": "b" * 64}, "derived": []})
    app_state = _app_state(entries=[entry], document_store=ds)

    result = await documents_check({"create_issues": True}, app_state)

    assert result["ok"] is False
    broken = _broken_links(sid)
    assert broken, "create_issues=True должен фиксировать broken_link"
    assert "b" * 64 in broken[0].detail


async def test_documents_check_admin_success_structure(tmp_path, quality_tempdir):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    put = ds.put(b"ok" * 64, mime="application/pdf", filename="ok.pdf")
    sid = "src-" + "c" * 16
    entry = _source_entry(sid, {
        "original": {"sha256": put.sha256, "mime": "application/pdf",
                     "size": put.size, "original_filename": "ok.pdf"},
        "derived": [],
    })
    app_state = _app_state(entries=[entry], document_store=ds)

    result = await documents_check({}, app_state)

    assert result["ok"] is True
    assert result["counts"]["sources"] == 1
    assert result["issues"]["orphans"] == 0
    assert set(result["samples"]) == {
        "missing_blob", "sha_mismatch", "canonical_missing",
        "provenance_incomplete", "dangling_source_refs", "orphans", "errors",
    }


async def test_documents_check_uninitialized():
    result = await documents_check({}, SimpleNamespace(store=None, document_store=None))
    assert result == {"error": "documents contour is not initialized"}


# ── (в) documents_rebuild: идемпотентность ×2 ──────────────────


async def test_documents_rebuild_idempotent_twice(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    put = ds.put(b"x" * 100, mime="application/pdf", filename="paper.pdf")
    sid = "src-" + "d" * 16
    entry = _source_entry(sid, {
        "original": {"sha256": put.sha256, "mime": "application/pdf",
                     "original_filename": "paper.pdf"},
        "derived": [],
    })
    app_state = _app_state(entries=[entry], document_store=ds)

    first = await documents_rebuild({}, app_state)
    second = await documents_rebuild({}, app_state)

    assert first["added"] == 0 and first["updated"] == 0 and first["removed"] == 0
    assert first["blobs"] == 1 and first["with_source_meta"] == 1
    assert second["added"] == 0 and second["updated"] == 0 and second["removed"] == 0


async def test_documents_rebuild_detects_registry_loss(tmp_path):
    """Потеря реестра (DELETE blobs) → rebuild added=1; повтор → added=0."""
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    put = ds.put(b"y" * 100, mime="application/pdf", filename="book.pdf")
    sid = "src-" + "e" * 16
    entry = _source_entry(sid, {
        "original": {"sha256": put.sha256, "mime": "application/pdf",
                     "original_filename": "book.pdf"},
        "derived": [],
    })
    app_state = _app_state(entries=[entry], document_store=ds)

    with ds._connect() as conn:
        conn.execute("DELETE FROM blobs")

    first = await documents_rebuild({}, app_state)
    second = await documents_rebuild({}, app_state)

    assert first["added"] == 1 and first["removed"] == 0
    assert second["added"] == 0 and second["removed"] == 0


async def test_documents_rebuild_uninitialized():
    result = await documents_rebuild({}, SimpleNamespace(store=None, document_store=None))
    assert result == {"error": "documents contour is not initialized"}


# ── (г) реестр/схемы/scope ─────────────────────────────────────


def test_tools_registered_and_scoped():
    names = {t["name"] for t in TOOLS}
    assert "documents_check" in names
    assert "documents_rebuild" in names
    assert "documents_check" in TOOL_HANDLERS
    assert "documents_rebuild" in TOOL_HANDLERS

    check_schema = next(t for t in TOOLS if t["name"] == "documents_check")
    rebuild_schema = next(t for t in TOOLS if t["name"] == "documents_rebuild")
    assert check_schema["inputSchema"]["properties"]["create_issues"]["default"] is False
    assert rebuild_schema["inputSchema"] == {"type": "object", "properties": {}}

    for tool in ("documents_check", "documents_rebuild"):
        assert tool in WRITE_TOOLS
        assert tool not in READ_TOOLS
        assert tool not in EDITOR_TOOLS
        assert tool not in IMPORT_TOOLS
        assert tool not in SUBSCRIBER_TOOLS


# ── (д) возврат соблюдает лимит samples ────────────────────────


async def test_documents_check_samples_limited(tmp_path, quality_tempdir):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    # 25 orphan-блобов без Source-ref → orphans=25, samples ограничены 20.
    for n in range(25):
        ds.put(f"orphan-{n}-".encode() * 8)
    app_state = _app_state(entries=[], document_store=ds)

    result = await documents_check({}, app_state)

    assert result["issues"]["orphans"] == 25
    assert len(result["samples"]["orphans"]) == 20
