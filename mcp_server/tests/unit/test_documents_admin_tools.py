"""Ф5b1 (bibliography): source_get + documents_stats — read/admin MCP-тулы консоли.

Покрывает (acceptance Ф5b1):
- (а) source_get public+license-ok → оба блоба + sha/size (без приватных полей);
- (б) private Source чужаку-зоне (subscriber) → отказ без oracle («not found»);
- (в) license=unknown (public) → отказ fail-closed;
- (г) sparse: canonical отсутствует → ключа нет + canonical_error (persisted);
- (д) documents_stats non-admin → 403 (auth-слой), admin → агрегаты без путей;
- (е) реестр/схемы/scope: TOOLS + TOOL_HANDLERS + READ_TOOLS/WRITE_TOOLS.
"""

from __future__ import annotations

import json
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
from mcp_server.tools.documents_admin import documents_stats, source_get
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex

READ = {"level": "read"}
SUB = {"level": "subscriber"}
WRITE = {"level": "write"}


def _source_entry(source_id, *, zone="public", status="published",
                  license_value="cc-by-4.0", public_allowed=True,
                  blobs=None, content="# Paper title\n") -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=source_id,
        domain="library",
        subject="bibliography",
        content_type="source",
        format="pdf",
        zone=zone,
        status=status,
        license=license_value,
        public_allowed=public_allowed,
        blobs=blobs,
    )
    return KnowledgeEntry(frontmatter=fm, content=content)


class _FakeStore:
    """Async MarkdownStore-подобный фейк: read по knowledge_id."""

    def __init__(self, entry=None):
        self.entry = entry

    async def read(self, knowledge_id):
        if self.entry is not None and self.entry.frontmatter.knowledge_id == knowledge_id:
            return self.entry
        return None


def _app_state(entry=None, document_store=None, index=None) -> SimpleNamespace:
    return SimpleNamespace(
        store=_FakeStore(entry),
        document_store=document_store,
        source_ref_index=index,
    )


# ── (а) public + license-ok → оба блоба + sha/size ──────────────


async def test_source_get_public_licensed_returns_both_blobs(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"orig-content" * 16, mime="application/pdf", filename="paper.pdf")
    canon = ds.put(
        b"canon-content" * 16, mime="application/pdf", filename="paper.pdf",
        role="canonical", derived_from=orig.sha256, tool="libreoffice",
    )
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf",
                     "size": orig.size, "original_filename": "paper.pdf"},
        "canonical": {"sha256": canon.sha256, "role": "canonical",
                      "derived_from": orig.sha256, "tool": "libreoffice"},
        "derived": [],
    }
    sid = "src-" + "a" * 16
    entry = _source_entry(sid, blobs=blobs)

    result = await source_get(
        {"source_id": sid, "_auth": READ}, _app_state(entry=entry, document_store=ds)
    )

    assert "error" not in result
    assert result["source_id"] == sid
    assert result["title"] == "Paper title"
    assert result["license"] == "cc-by-4.0"
    assert result["zone"] == "public"
    assert result["status"] == "published"
    o = result["blobs"]["original"]
    c = result["blobs"]["canonical"]
    assert o["sha256"] == orig.sha256
    assert o["size"] == orig.size
    assert o["present"] is True and o["available"] is True
    assert c["sha256"] == canon.sha256
    assert c["size"] == canon.size
    assert c["present"] is True and c["available"] is True
    # canonical mime — из реестра стора (frontmatter canonical без mime)
    assert c["mime"] == "application/pdf"
    # нет приватных полей-секретов
    assert "original_filename" not in o
    assert "tool" not in c and "derived_from" not in c


# ── (б) private Source чужаку-зоне → отказ без oracle ───────────


async def test_source_get_private_foreign_zone_denied_no_oracle(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"priv-content" * 16, mime="application/pdf", filename="p.pdf")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf", "size": orig.size},
        "derived": [],
    }
    sid = "src-" + "b" * 16
    entry = _source_entry(sid, zone="private", license_value="own", public_allowed=True, blobs=blobs)

    result = await source_get(
        {"source_id": sid, "_auth": SUB}, _app_state(entry=entry, document_store=ds)
    )

    assert result == {"error": f"Source not found: '{sid}'"}  # без oracle


async def test_source_get_unknown_id_no_oracle(tmp_path):
    """Несуществующий id и чужая зона — одинаковый «not found» (без утечки)."""
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    result = await source_get(
        {"source_id": "src-" + "c" * 16, "_auth": READ}, _app_state(document_store=ds)
    )
    assert result == {"error": "Source not found: 'src-" + "c" * 16 + "'"}


# ── (в) license=unknown (public) → отказ fail-closed ───────────


async def test_source_get_license_unknown_denied(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"unk-content" * 16, mime="application/pdf", filename="u.pdf")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf", "size": orig.size},
        "derived": [],
    }
    sid = "src-" + "d" * 16
    entry = _source_entry(sid, zone="public", license_value="unknown",
                          public_allowed=True, blobs=blobs)

    result = await source_get(
        {"source_id": sid, "_auth": READ}, _app_state(entry=entry, document_store=ds)
    )

    assert result == {"error": f"Source not found: '{sid}'"}


async def test_source_get_deprecated_denied(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"dep-content" * 16, mime="application/pdf", filename="d.pdf")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf", "size": orig.size},
        "derived": [],
    }
    sid = "src-" + "e" * 16
    entry = _source_entry(sid, zone="private", status="deprecated",
                          license_value="own", blobs=blobs)

    result = await source_get(
        {"source_id": sid, "_auth": READ}, _app_state(entry=entry, document_store=ds)
    )

    assert result == {"error": f"Source not found: '{sid}'"}


# ── (г) sparse: canonical отсутствует → ключа нет + canonical_error ──


async def test_source_get_sparse_no_canonical_returns_canonical_error(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"orig-only" * 16, mime="application/pdf", filename="o.pdf")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf", "size": orig.size},
        "derived": [],
        "canonical_error": {"reason": "policy_pdf_only", "message": "PDF-only policy"},
    }
    sid = "src-" + "f" * 16
    entry = _source_entry(sid, zone="private", license_value="own", blobs=blobs)

    result = await source_get(
        {"source_id": sid, "_auth": WRITE}, _app_state(entry=entry, document_store=ds)
    )

    assert "error" not in result
    assert "canonical" not in result["blobs"]  # sparse: не фабрикуем отсутствующий canonical
    assert result["blobs"]["original"]["sha256"] == orig.sha256
    assert result["canonical_error"]["reason"] == "policy_pdf_only"


# ── (д) documents_stats: non-admin denied / admin агрегаты без путей ──


def test_documents_stats_non_admin_denied():
    with pytest.raises(HTTPException) as exc:
        check_tool_permission(AuthInfo(authenticated=True, key_level="read"), "documents_stats")
    assert exc.value.status_code == 403


def test_documents_stats_editor_denied():
    with pytest.raises(HTTPException) as exc:
        check_tool_permission(AuthInfo(authenticated=True, key_level="editor"), "documents_stats")
    assert exc.value.status_code == 403


async def test_documents_stats_admin_aggregates_no_paths(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    blob = ds.put(b"x" * (2 * 1024 * 1024), mime="application/pdf", filename="x.pdf")
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-x", shas=(blob.sha256,)))
    app_state = _app_state(document_store=ds, index=index)

    result = await documents_stats({"_auth": {"level": "write"}}, app_state)

    assert "error" not in result
    assert result["quota"]["used_bytes"] == 2 * 1024 * 1024
    assert result["quota"]["max_bytes"] == 1 * 1024 ** 3
    assert result["quota"]["used_pct"] > 0.0
    assert result["blobs"]["total"] == 1
    assert result["blobs"]["orphans"] == 0  # blob референсён
    assert result["jobs"] == {}
    assert result["grace_days"] == 30
    # без абсолютных путей FS (только агрегаты)
    dumped = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in dumped
    assert "/app/" not in dumped and "/tmp/" not in dumped


async def test_documents_stats_counts_orphan(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    ref = ds.put(b"referenced" * 8, mime="application/pdf", filename="r.pdf")
    ds.put(b"orphan" * 8)  # без ref
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-r", shas=(ref.sha256,)))
    app_state = _app_state(document_store=ds, index=index)

    result = await documents_stats({"_auth": {"level": "write"}}, app_state)

    assert result["blobs"]["total"] == 2
    assert result["blobs"]["orphans"] == 1


# ── (е) реестр/схемы/scope ─────────────────────────────────────


def test_tools_registered_and_scoped():
    names = {t["name"] for t in TOOLS}
    assert "source_get" in names
    assert "documents_stats" in names
    # dispatch
    assert "source_get" in TOOL_HANDLERS
    assert "documents_stats" in TOOL_HANDLERS
    # schema зарегистрирована (inputSchema присутствует)
    sg = next(t for t in TOOLS if t["name"] == "source_get")
    dschema = next(t for t in TOOLS if t["name"] == "documents_stats")
    assert sg["inputSchema"]["required"] == ["source_id"]
    assert dschema["inputSchema"] == {"type": "object", "properties": {}}
    # scope
    assert "source_get" in READ_TOOLS
    assert "source_get" not in WRITE_TOOLS
    assert "documents_stats" in WRITE_TOOLS
    assert "documents_stats" not in READ_TOOLS
    assert "documents_stats" not in EDITOR_TOOLS
    assert "documents_stats" not in IMPORT_TOOLS
    assert "documents_stats" not in SUBSCRIBER_TOOLS


def test_tool_requests_metric_covers_new_tools():
    """Generic-метрика вызовов охватывает новые тулы (без нового счётчика)."""
    from mcp_server.metrics import tool_requests

    tool_requests.labels(tool="source_get", status="success").inc()
    tool_requests.labels(tool="documents_stats", status="success").inc()
