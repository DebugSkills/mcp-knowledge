"""Ф5b3 (bibliography): documents_gc (mark-and-sweep) + documents_retry (реканонизация).

Покрывает (acceptance Ф5b3):
- (а) оба тула non-admin → 403; admin (write) → доступ; без ключа → 401;
- (б) dry-run по умолчанию: кандидаты найдены, НИ ОДИН файл не удалён;
- (в) referenced никогда не удаляется (даже старше grace);
- (г) свежий orphan сохраняется (grace);
- (д) dry_run=False удаляет только кандидатов, freed_bytes>0, повтор → 0;
- (е) retry при наличии canonical → no-op already_present;
- (ж) retry успех → canonical_present=true, canonical_error снят;
- (з) retry провал конвертера → reason в whitelist, original цел;
- (и) регистрация/схемы/scopes.
"""

from __future__ import annotations

import os
import time
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
from mcp_server.config import settings
from mcp_server.content.canonicalizer import CanonicalizationError
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.document_store import DocumentStore
from mcp_server.tools import TOOLS, TOOL_HANDLERS
from mcp_server.tools.documents_admin import documents_gc, documents_retry
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex

WHITELIST = {"conversion_failed", "converter_unavailable", "conversion_timeout"}


def _source_entry(kid, *, fmt="docx", blobs=None, policy="normalize") -> KnowledgeEntry:
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="source",
        zone="private",
        status="published",
        format=fmt,
        ingest_policy_applied=policy,
        blobs=blobs,
    )
    return KnowledgeEntry(frontmatter=fm, content=f"# {kid}\n")


class _FakeStore:
    """Async MarkdownStore-фейк: read + write_entry (Source-запись в память)."""

    def __init__(self, entry=None):
        self.entries = {entry.frontmatter.knowledge_id: entry} if entry else {}

    async def read(self, knowledge_id):
        return self.entries.get(knowledge_id)

    async def write_entry(self, entry):
        self.entries[entry.frontmatter.knowledge_id] = entry
        return entry


def _backdate(ds: DocumentStore, sha: str, days: float) -> None:
    """Сдвинуть mtime блоба в прошлое (GC grace-возраст)."""
    old = time.time() - days * 86400
    os.utime(ds._shard_path(sha), (old, old))


def _gc_state(tmp_path, index=None) -> SimpleNamespace:
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    return SimpleNamespace(document_store=ds, source_ref_index=index)


# ── (а) auth matrix ─────────────────────────────────────────


class TestGcRetryAuthMatrix:
    @pytest.mark.parametrize("tool", ["documents_gc", "documents_retry"])
    def test_write_key_allowed(self, tool):
        check_tool_permission(AuthInfo(authenticated=True, key_level="write"), tool)

    @pytest.mark.parametrize("tool", ["documents_gc", "documents_retry"])
    @pytest.mark.parametrize("level", ["editor", "read", "import", "subscriber"])
    def test_non_write_forbidden_403(self, tool, level):
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(AuthInfo(authenticated=True, key_level=level), tool)
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("tool", ["documents_gc", "documents_retry"])
    def test_no_key_401(self, tool):
        with pytest.raises(HTTPException) as exc:
            check_tool_permission(AuthInfo(authenticated=False), tool)
        assert exc.value.status_code == 401


# ── (б) dry-run по умолчанию: кандидаты найдены, ничего не удалено ──


async def test_gc_dry_run_default_no_deletion(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    referenced = ds.put(b"ref-content" * 8, mime="application/pdf", filename="r.pdf")
    orphan = ds.put(b"orphan-content" * 8)  # без ref
    _backdate(ds, orphan.sha256, 40)  # старый orphan → кандидат
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-r", shas=(referenced.sha256,)))
    app_state = _gc_state(tmp_path, index=index)

    result = await documents_gc({}, app_state)

    assert result["dry_run"] is True
    assert any(c["sha"] == orphan.sha256 for c in result["candidates"])
    assert all(c["sha"] != referenced.sha256 for c in result["candidates"])
    assert result["reclaimable_bytes"] == orphan.size
    assert result["kept_referenced"] == 1
    # НИ ОДИН файл не удалён (dry-run)
    assert ds.exists(orphan.sha256) is True
    assert ds.exists(referenced.sha256) is True


# ── (в) referenced никогда не удаляется ─────────────────────


async def test_gc_referenced_never_deleted(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    referenced = ds.put(b"ref-old" * 8, mime="application/pdf", filename="r.pdf")
    _backdate(ds, referenced.sha256, 40)  # старый, НО referenced
    orphan = ds.put(b"orphan-old" * 8)
    _backdate(ds, orphan.sha256, 40)
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-r", shas=(referenced.sha256,)))
    app_state = _gc_state(tmp_path, index=index)

    result = await documents_gc({"dry_run": False}, app_state)

    assert result["deleted"] == 1
    assert ds.exists(referenced.sha256) is True  # referenced цел
    assert ds.exists(orphan.sha256) is False  # кандидат сметён


# ── (г) свежий orphan сохраняется (grace) ───────────────────


async def test_gc_fresh_orphan_kept_by_grace(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    referenced = ds.put(b"ref" * 8, mime="application/pdf", filename="r.pdf")
    fresh = ds.put(b"fresh-orphan" * 8)  # mtime = now — кандидат, но свежий
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-r", shas=(referenced.sha256,)))
    app_state = _gc_state(tmp_path, index=index)

    result = await documents_gc({"dry_run": False}, app_state)

    assert result["deleted"] == 0
    assert result["kept_fresh"] == 1
    assert result["kept_referenced"] == 1
    assert ds.exists(fresh.sha256) is True


# ── (д) dry_run=False: только кандидаты + идемпотентность ────


async def test_gc_delete_only_candidates_and_idempotent(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    referenced = ds.put(b"ref" * 8, mime="application/pdf", filename="r.pdf")
    old_orphan = ds.put(b"old-orphan" * 100)
    _backdate(ds, old_orphan.sha256, 40)
    fresh = ds.put(b"fresh-orphan" * 8)
    index = SourceRefIndex()
    index.add(SourceRef(source_id="src-r", shas=(referenced.sha256,)))
    app_state = _gc_state(tmp_path, index=index)

    first = await documents_gc({"dry_run": False}, app_state)
    assert first["deleted"] == 1
    assert first["freed_bytes"] == old_orphan.size
    assert first["freed_bytes"] > 0
    assert ds.exists(referenced.sha256) is True
    assert ds.exists(fresh.sha256) is True
    assert ds.exists(old_orphan.sha256) is False

    second = await documents_gc({"dry_run": False}, app_state)
    assert second["deleted"] == 0
    assert second["freed_bytes"] == 0


# ── R2 (fail-closed): индекс None/пуст + физические блобы → НЕ удалять ──


async def test_gc_index_none_fail_closed(tmp_path):
    """Индекс None (старт-скан упал) + старый блоб → отказ, ничего не удалено."""
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    live = ds.put(b"live-doc" * 8, mime="application/pdf", filename="l.pdf")
    _backdate(ds, live.sha256, 40)  # старый — был бы кандидатом при пустом referenced
    app_state = _gc_state(tmp_path, index=None)

    result = await documents_gc({"dry_run": False}, app_state)

    assert result["error"] == "index_unavailable"
    assert result["deleted"] == 0
    assert ds.exists(live.sha256) is True  # живые блобы целы


async def test_gc_empty_index_fail_closed(tmp_path):
    """Пустой индекс + физические блобы → отказ, ничего не удалено."""
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    live = ds.put(b"live-doc" * 8, mime="application/pdf", filename="l.pdf")
    _backdate(ds, live.sha256, 40)
    index = SourceRefIndex()  # пустой
    app_state = _gc_state(tmp_path, index=index)

    result = await documents_gc({"dry_run": False}, app_state)

    assert result["error"] == "index_unavailable"
    assert result["deleted"] == 0
    assert ds.exists(live.sha256) is True


async def test_gc_allow_empty_index_deletes(tmp_path):
    """Явный allow_empty_index=True → удаление в деградации разрешено."""
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orphan = ds.put(b"orphan" * 8)
    _backdate(ds, orphan.sha256, 40)
    index = SourceRefIndex()  # пустой, но allow_empty_index=True
    app_state = _gc_state(tmp_path, index=index)

    result = await documents_gc({"dry_run": False, "allow_empty_index": True}, app_state)

    assert "error" not in result
    assert result["deleted"] == 1
    assert ds.exists(orphan.sha256) is False


# ── (е) retry при наличии canonical → no-op ──────────────────


async def test_retry_already_present_noop(tmp_path):
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"orig" * 8, mime="application/pdf", filename="o.pdf")
    canon = ds.put(
        b"%PDF-canon" * 8, mime="application/pdf", filename="o.pdf",
        role="canonical", derived_from=orig.sha256,
    )
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf",
                     "size": orig.size, "original_filename": "o.pdf"},
        "canonical": {"sha256": canon.sha256, "role": "canonical",
                      "derived_from": orig.sha256},
        "derived": [],
    }
    entry = _source_entry("src-1", fmt="docx", blobs=blobs)
    store = _FakeStore(entry)
    app_state = SimpleNamespace(store=store, document_store=ds, source_ref_index=None)

    result = await documents_retry({"source_id": "src-1"}, app_state)

    assert result == {"status": "already_present", "canonical_sha256": canon.sha256}
    # SSOT не тронут (no-op)
    assert store.entries["src-1"] is entry


# ── (ж) retry успех → canonical_present=true, canonical_error снят ──


async def test_retry_success_clears_canonical_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"PK\x03\x04 docx" * 8, mime="application/pdf", filename="d.docx")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf",
                     "size": orig.size, "original_filename": "d.docx"},
        "derived": [],
        "canonical_error": {"reason": "conversion_failed", "message": "boom",
                            "at": "2026-10-03T00:00:00+00:00"},
    }
    entry = _source_entry("src-2", fmt="docx", blobs=blobs)
    store = _FakeStore(entry)

    async def _ok(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF-converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    app_state = SimpleNamespace(store=store, document_store=ds,
                                source_ref_index=None, canonicalizer=_ok)

    result = await documents_retry({"source_id": "src-2"}, app_state)

    assert result["status"] == "ok"
    canon_sha = result["canonical_sha256"]
    assert ds.exists(canon_sha) is True  # canonical_present
    updated = store.entries["src-2"].frontmatter.blobs
    assert updated["canonical"]["sha256"] == canon_sha
    assert "canonical_error" not in updated


# ── (з) retry провал конвертера → whitelist-reason, original цел ──


async def test_retry_converter_failure_whitelist_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"PK\x03\x04 docx" * 8, mime="application/pdf", filename="d.docx")
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf",
                     "size": orig.size, "original_filename": "d.docx"},
        "derived": [],
        "canonical_error": {"reason": "conversion_failed", "message": "boom",
                            "at": "2026-10-03T00:00:00+00:00"},
    }
    entry = _source_entry("src-3", fmt="docx", blobs=blobs)
    store = _FakeStore(entry)

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("converter_unavailable", "no sidecar")

    app_state = SimpleNamespace(store=store, document_store=ds,
                                source_ref_index=None, canonicalizer=_fail)

    result = await documents_retry({"source_id": "src-3"}, app_state)

    assert result["status"] == "failed"
    assert result["reason"] in WHITELIST
    # original цел, canonical не появился
    assert ds.exists(orig.sha256) is True
    assert ds.count() == 1
    updated = store.entries["src-3"].frontmatter.blobs
    assert updated["canonical_error"]["reason"] in WHITELIST
    assert "canonical" not in updated


# ── R3 (CR2): canonical в SSOT есть, blob потерян → НЕ misleading ok ──


async def test_retry_canonical_blob_lost_replaces_ssot(tmp_path, monkeypatch):
    """CR2: canonical sha в SSOT, blob физически потерян → retry переканонизирует
    и ЗАМЕНЯЕТ запись SSOT валидным sha (не frozen-ok с новым sha и сиротой)."""
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    ds = DocumentStore(tmp_path / "documents", max_gb=1)
    orig = ds.put(b"PK\x03\x04 docx" * 8, mime="application/pdf", filename="d.docx")
    lost_sha = "c" * 64  # canonical в SSOT, blob НЕ в сторе (потерян)
    blobs = {
        "original": {"sha256": orig.sha256, "mime": "application/pdf",
                     "size": orig.size, "original_filename": "d.docx"},
        "canonical": {"sha256": lost_sha, "role": "canonical",
                      "derived_from": orig.sha256},
        "derived": [],
    }
    entry = _source_entry("src-4", fmt="docx", blobs=blobs)
    store = _FakeStore(entry)

    async def _ok(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF-recovered", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    app_state = SimpleNamespace(store=store, document_store=ds,
                                source_ref_index=None, canonicalizer=_ok)

    result = await documents_retry({"source_id": "src-4"}, app_state)

    assert result["status"] == "ok"
    new_sha = result["canonical_sha256"]
    assert new_sha != lost_sha
    assert ds.exists(new_sha) is True  # новый canonical физически жив
    updated = store.entries["src-4"].frontmatter.blobs
    assert updated["canonical"]["sha256"] == new_sha  # SSOT обновлён валидным sha
    assert "canonical_error" not in updated


# ── (и) регистрация/схемы/scopes ─────────────────────────────


def test_gc_retry_registered_and_scoped():
    names = {t["name"] for t in TOOLS}
    assert "documents_gc" in names
    assert "documents_retry" in names
    assert "documents_gc" in TOOL_HANDLERS
    assert "documents_retry" in TOOL_HANDLERS

    gc_schema = next(t for t in TOOLS if t["name"] == "documents_gc")
    retry_schema = next(t for t in TOOLS if t["name"] == "documents_retry")
    assert gc_schema["inputSchema"]["properties"]["dry_run"]["default"] is True
    assert retry_schema["inputSchema"]["required"] == ["source_id"]

    for tool in ("documents_gc", "documents_retry"):
        assert tool in WRITE_TOOLS
        assert tool not in READ_TOOLS
        assert tool not in EDITOR_TOOLS
        assert tool not in IMPORT_TOOLS
        assert tool not in SUBSCRIBER_TOOLS
