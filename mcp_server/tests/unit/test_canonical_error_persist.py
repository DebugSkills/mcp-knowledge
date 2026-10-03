"""Ф4b3: write-path persist blobs.canonical_error (план §3.4:192 reason-классификация).

Writer = ingest (ingest_source), reader = citation.classify_reason (whitelist-маппинг).
Контракты:
- CanonicalizationError/quota на write-path → blobs.canonical_error {reason, message, at} в SSOT;
- успешная канонизация (retry) → canonical_error снят (не stale);
- субкод вне whitelist → persisted как есть, classify_reason → conversion_failed;
- round-trip: 5 whitelist-значений → classify_reason возвращает тот же код.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mcp_server.config import settings
from mcp_server.content.canonicalizer import CanonicalizationError
from mcp_server.content.ingest import ingest_source
from mcp_server.storage.document_store import DocumentStore
from mcp_server.tools.source_ref_index import SourceRefIndex  # noqa: F401 — первым: primes mcp_server.tools (иначе цикл)

from mcp_server.content.citation import classify_reason


class _FakeMarkdownStore:
    """Async MarkdownStore-фейк (Source-запись в память)."""

    def __init__(self):
        self.entries = {}

    async def read(self, knowledge_id):
        return self.entries.get(knowledge_id)

    async def write_entry(self, entry):
        self.entries[entry.frontmatter.knowledge_id] = entry
        return entry


def _make_state(tmp_path):
    state = MagicMock()
    state.document_store = DocumentStore(tmp_path / "documents", max_gb=10)
    state.store = _FakeMarkdownStore()
    return state


def _fm_from_blobs(blobs: dict) -> dict:
    """Source-frontmatter dict для classify_reason (reader принимает SSOT-dict)."""
    return {"format": "docx", "ingest_policy_applied": "normalize", "blobs": blobs}


# ── 1. Persist на write-path ───────────────────────────────


async def test_canonical_error_persisted_on_write_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("conversion_timeout", "sidecar timed out after 120s")

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fail)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 docx", filename="doc.docx",
    )
    # fail-closed: ingest не упал, canonical нет, причина известна
    assert res["canonical_present"] is False
    assert res["reason"] == "conversion_timeout"
    blobs = state.store.entries[res["source_id"]].frontmatter.blobs
    err = blobs["canonical_error"]
    assert err["reason"] == "conversion_timeout"
    assert err["message"]  # краткое сообщение
    datetime.fromisoformat(err["at"])  # валидный ISO-8601 ts
    assert "canonical" not in blobs
    assert state.document_store.count() == 1  # original жив


async def test_quota_exceeded_persisted(tmp_path, monkeypatch):
    """quota_exceeded — whitelist-причина, тоже persist-ится."""
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")

    def _quota_put(self, data, **kw):
        if kw.get("role") == "canonical":
            from mcp_server.storage.document_store import QuotaExceededError

            raise QuotaExceededError(1, 0, 0)
        return type("R", (), {"to_dict": lambda s: {
            "sha256": __import__("hashlib").sha256(data).hexdigest(), "size": len(data)}})()

    monkeypatch.setattr(DocumentStore, "put", _quota_put)
    state = _make_state(tmp_path)

    async def _ok(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _ok)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 docx", filename="doc.docx",
    )
    assert res["reason"] == "quota_exceeded"
    blobs = state.store.entries[res["source_id"]].frontmatter.blobs
    assert blobs["canonical_error"]["reason"] == "quota_exceeded"


# ── 2. Снятие при успехе (retry) ───────────────────────────


async def test_canonical_error_cleared_on_success_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    state = _make_state(tmp_path)
    data = b"PK\x03\x04 docx bytes"

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("converter_unavailable", "no sidecar")

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fail)
    r1 = await ingest_source(state, format="docx", domain="library", subject="bibliography",
                             data=data, filename="doc.docx")
    assert "canonical_error" in state.store.entries[r1["source_id"]].frontmatter.blobs

    async def _ok(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _ok)
    r2 = await ingest_source(state, format="docx", domain="library", subject="bibliography",
                             data=data, filename="doc.docx")
    assert r2["reused"] is True and r2["attached"] is True
    assert r2["canonical_present"] is True
    # stale-причина снята из SSOT
    blobs = state.store.entries[r2["source_id"]].frontmatter.blobs
    assert blobs.get("canonical", {}).get("sha256") == r2["canonical"]["sha256"]
    assert "canonical_error" not in blobs


# ── 3. Субкод вне whitelist ────────────────────────────────


async def test_unknown_subcode_persisted_and_mapped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("input_too_large", "123456 bytes > max input")

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fail)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 docx", filename="doc.docx",
    )
    blobs = state.store.entries[res["source_id"]].frontmatter.blobs
    # persisted как есть (диагностика), reader мапит в whitelist
    assert blobs["canonical_error"]["reason"] == "input_too_large"
    assert classify_reason(
        _fm_from_blobs(blobs), exists_fn=lambda sha: True, index=SourceRefIndex()
    ) == "conversion_failed"


# ── 4. Round-trip: whitelist → classify_reason ─────────────


@pytest.mark.parametrize(
    "reason",
    ["queued", "conversion_failed", "conversion_timeout", "converter_unavailable", "quota_exceeded"],
)
def test_roundtrip_whitelist_reasons(reason):
    blobs = {
        "original": {"sha256": "a" * 64, "mime": "text/plain", "size": 10, "original_filename": "d.docx"},
        "derived": [],
        "canonical_error": {"reason": reason, "message": "m", "at": "2026-10-03T00:00:00+00:00"},
    }
    assert classify_reason(
        _fm_from_blobs(blobs), exists_fn=lambda sha: True, index=SourceRefIndex()
    ) == reason


# ── 5. Контракт решения: ось/политика НЕ persist-ятся ──────


async def test_outside_pdf_axis_no_canonical_error(tmp_path):
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="audio", domain="library", subject="bibliography",
        data=b"ID3 fake mp3", filename="talk.mp3", locator_kind="timestamp",
    )
    assert res["reason"] == "outside_pdf_axis"
    blobs = state.store.entries[res["source_id"]].frontmatter.blobs
    assert "canonical_error" not in blobs  # причина выводима из format (классиф. шаг 4)
