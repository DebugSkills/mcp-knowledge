"""Тесты интеграции ingest_source (bibliography Ф1): PDF→blob+Source; docx политики."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import MagicMock

from mcp_server.config import settings
from mcp_server.content.ingest import _get_store, ingest_source
from mcp_server.storage.document_store import DocumentStore, QuotaExceededError


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


async def test_ingest_source_pdf_blob_and_source(tmp_path):
    state = _make_state(tmp_path)
    data = b"%PDF-1.4 fake pdf bytes" * 10
    res = await ingest_source(
        state, format="pdf", domain="library", subject="bibliography",
        data=data, mime="application/pdf", filename="book.pdf", zone="private",
        license="cc-by-4.0",
    )
    assert res["source_id"].startswith("src-")
    assert res["created"] is True
    assert res["canonical_present"] is True
    # canonical == original (as-is, дедуп)
    assert res["canonical"]["sha256"] == res["original"]["sha256"]
    assert res["canonical"]["derived_from"] is None and res["canonical"]["tool"] == "as-is"
    # один физический blob
    assert state.document_store.count() == 1
    # Source-запись в MarkdownStore
    entry = state.store.entries[res["source_id"]]
    assert entry.frontmatter.content_type == "source"
    assert entry.frontmatter.format == "pdf"
    assert entry.frontmatter.blobs["original"]["sha256"] == hashlib.sha256(data).hexdigest()


async def test_ingest_source_pdf_reimport_noop(tmp_path):
    state = _make_state(tmp_path)
    data = b"%PDF" * 20
    await ingest_source(state, format="pdf", domain="library", subject="bibliography",
                        data=data, mime="application/pdf", filename="book.pdf")
    res2 = await ingest_source(state, format="pdf", domain="library", subject="bibliography",
                               data=data, mime="application/pdf", filename="book.pdf")
    assert res2["reused"] is True and res2["created"] is False
    assert state.document_store.count() == 1


async def test_ingest_source_docx_policy_pdf_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "pdf_only")
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 fake docx", mime="application/octet-stream", filename="doc.docx",
    )
    assert res["canonical_present"] is False
    assert res["reason"] == "policy_pdf_only"
    assert state.document_store.count() == 1  # только original


async def test_ingest_source_docx_normalize(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")

    async def _fake_canonicalize(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fake_canonicalize)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 fake docx", filename="doc.docx",
    )
    assert res["canonical_present"] is True
    assert res["canonical"]["derived_from"] == res["original"]["sha256"]
    assert res["canonical"]["tool"] == "libreoffice-headless"
    assert state.document_store.count() == 2  # original + canonical


async def test_ingest_source_docx_default_policy_normalize(tmp_path, monkeypatch):
    """Канон §4.1 decision-16: default INGEST_POLICY=normalize (не pdf_only).

    Без monkeypatch INGEST_POLICY — non-PDF документ с инжектированным конвертером
    идёт в canonical, а НЕ в policy_pdf_only. params_hash попадает во frontmatter.
    """
    assert settings.INGEST_POLICY == "normalize"  # default после переключения

    async def _fake_canonicalize(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "def456"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fake_canonicalize)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 fake docx", filename="doc.docx",
    )
    assert res["canonical_present"] is True
    assert res["reason"] is None  # НЕ policy_pdf_only
    assert res["canonical"]["tool"] == "libreoffice-headless"
    entry = state.store.entries[res["source_id"]]
    assert entry.frontmatter.blobs["canonical"]["params_hash"] == "def456"
    assert entry.frontmatter.ingest_policy_applied == "normalize"
    assert state.document_store.count() == 2  # original + canonical


async def test_ingest_source_docx_params_hash_registry_and_rebuild(tmp_path, monkeypatch):
    """params_hash в реестре (SQLite) сразу на ingest И переживает rebuild.

    До фикса: canonical-put не передавал params_hash в store.put → registry-строка
    canonical имела params_hash=NULL до следующего rebuild; BlobInfo/info/list_blobs
    не возвращали params_hash. Теперь: registry заполняется сразу + rebuild сохраняет.
    """
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")

    async def _fake_canonicalize(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fake_canonicalize)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 fake docx", filename="doc.docx",
    )
    canonical_sha = res["canonical"]["sha256"]

    # (а) registry-строка canonical несёт params_hash сразу (не NULL).
    info = state.document_store.info(canonical_sha)
    assert info is not None
    assert info.params_hash == "abc123"

    # (б) frontmatter (SSOT) — params_hash на месте.
    entry = state.store.entries[res["source_id"]]
    assert entry.frontmatter.blobs["canonical"]["params_hash"] == "abc123"

    # (в) rebuild из SSOT → params_hash переживает (не теряется).
    state.document_store.rebuild(sources=[{"blobs": entry.frontmatter.blobs}])
    info2 = state.document_store.info(canonical_sha)
    assert info2 is not None
    assert info2.params_hash == "abc123"


async def test_ingest_source_converter_unavailable_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    from mcp_server.content.canonicalizer import CanonicalizationError

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("converter_unavailable", "no sidecar")

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fail)
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="docx", domain="library", subject="bibliography",
        data=b"PK\x03\x04 docx", filename="doc.docx",
    )
    # fail-closed: original сохранён, canonical=null
    assert res["canonical_present"] is False
    assert res["reason"] == "converter_unavailable"
    assert state.document_store.count() == 1  # original жив


async def test_ingest_source_media_outside_pdf_axis(tmp_path):
    state = _make_state(tmp_path)
    res = await ingest_source(
        state, format="audio", domain="library", subject="bibliography",
        data=b"ID3 fake mp3", filename="talk.mp3", locator_kind="timestamp",
    )
    assert res["canonical_present"] is False
    assert res["reason"] == "outside_pdf_axis"
    assert state.document_store.count() == 1


async def test_ingest_retry_attaches_canonical(tmp_path, monkeypatch):
    """P0-1 (critic): сбой конвертера → retry прикрепляет canonical в SSOT (persisted == ответ)."""
    from mcp_server.content.canonicalizer import CanonicalizationError

    monkeypatch.setattr(settings, "INGEST_POLICY", "normalize")
    state = _make_state(tmp_path)
    data = b"PK\x03\x04 docx bytes"

    async def _fail(data, format_value, converter_url=None):
        raise CanonicalizationError("converter_unavailable", "no sidecar")

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _fail)
    r1 = await ingest_source(state, format="docx", domain="library", subject="bibliography",
                             data=data, filename="doc.docx")
    assert r1["canonical_present"] is False and r1["reason"] == "converter_unavailable"

    async def _ok(data, format_value, converter_url=None):
        return {"pdf_bytes": b"%PDF converted", "tool": "libreoffice-headless",
                "tool_version": "7.5.0", "params_hash": "abc123"}

    monkeypatch.setattr("mcp_server.content.ingest.canonicalize", _ok)
    r2 = await ingest_source(state, format="docx", domain="library", subject="bibliography",
                             data=data, filename="doc.docx")
    assert r2["reused"] is True and r2["attached"] is True
    assert r2["canonical_present"] is True
    # persisted согласован с SSOT (не только в ответе)
    entry = state.store.entries[r2["source_id"]]
    assert entry.frontmatter.blobs["canonical"]["sha256"] == r2["canonical"]["sha256"]
    assert state.document_store.count() == 2  # original + canonical (attached)


async def test_bg_import_abort_on_put_failure(tmp_path, monkeypatch):
    """P0-2 (critic): отказ put(original) → abort импорта + temp-PDF сохранён."""
    from mcp_server.content.preprocessor import ValidationResult
    from mcp_server.tools import content as content_mod

    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 data that will fail the store")

    async def _fail_ingest(app_state, **kw):
        raise QuotaExceededError(1000, 0, 0)

    monkeypatch.setattr(content_mod, "ingest_source", _fail_ingest)

    class _FakePreprocessor:
        def validate(self, content, metadata):
            return ValidationResult(valid=True, content_size=0, estimated_sections=1)

    monkeypatch.setattr(content_mod, "get_preprocessor", lambda ct: _FakePreprocessor())

    app_state = MagicMock()
    app_state.heavy_ops_lock = asyncio.Lock()
    app_state.heavy_lock_owner = None
    app_state.import_progress = None
    app_state.import_cancel_event = None
    app_state.import_task = None

    import_id = "test-abort-1"
    rec = {"import_id": import_id, "status": "running", "log": [], "name": "t.pdf", "collection_id": ""}
    content_mod._import_queue.append(rec)

    params = {"_source_path": str(pdf), "content_type": "pdf", "domain": "library",
              "subject": "bibliography", "title": "t"}

    await content_mod._bg_import(
        import_id=import_id, params=params, app_state=app_state,
        cancel_event=asyncio.Event(), lock=app_state.heavy_ops_lock,
    )

    assert rec["status"] == "error"
    assert pdf.exists()  # temp НЕ удалён (оригинал сохранён для ручного retry)
    content_mod._import_queue.clear()


def test_get_store_hardens_against_mock_document_store(tmp_path, monkeypatch):
    """Харднинг: document_store = не-DocumentStore → настоящий DocumentStore (и кэш).

    getattr-default (None) не защищает от mock: MagicMock auto-attr отдаёт
    MagicMock, а не None — без isinstance-проверки _get_store вернул бы mock
    (regression Ф5-fix2).
    """
    monkeypatch.setattr(settings, "DOCUMENTS_DIR", str(tmp_path / "documents"))

    state = MagicMock()
    state.document_store = MagicMock()
    store = _get_store(state)
    assert isinstance(store, DocumentStore)
    assert state.document_store is store  # закэширован настоящий

    state2 = MagicMock()
    state2.document_store = "not-a-store"
    store2 = _get_store(state2)
    assert isinstance(store2, DocumentStore)
    assert state2.document_store is store2

