"""Ф5a1 (bibliography): провенанс-статус canonical в записи очереди + REST-снапшот /progress.

Покрытие:
  (а) успешный ingest → rec: source_id + canonical_present=True (+sha256)
  (б) canonical-провал → rec: canonical_present=False + canonical_error.reason (whitelist)
  (в) REST-снапшот /progress содержит поля и НЕ содержит _params
  (г) sparse: ключ отсутствует, если данных нет (никаких None-заглушек)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mcp_server.main import _import_provenance, import_progress
from mcp_server.tools import content as content_mod

# whitelist-причины (citation._CANONICAL_ERROR_REASONS, §3.4:192)
_WHITELIST_REASONS = {
    "queued",
    "conversion_failed",
    "conversion_timeout",
    "converter_unavailable",
    "quota_exceeded",
}


# ── (г) sparse: хелпер очереди ────────────────────────────────

def test_provenance_from_ingest_success():
    result = {
        "source_id": "src-abc123",
        "canonical_present": True,
        "canonical": {"sha256": "e3b0c44298fc1c149afbf4c8996fb924"},
        "blobs": {},
    }
    out = content_mod._import_provenance_from_ingest(result)
    assert out["source_id"] == "src-abc123"
    assert out["canonical_present"] is True
    assert out["canonical_sha256"] == "e3b0c44298fc1c149afbf4c8996fb924"
    assert "canonical_error" not in out


def test_provenance_from_ingest_canonical_failure():
    result = {
        "source_id": "src-def456",
        "canonical_present": False,
        "canonical": None,
        "blobs": {
            "canonical_error": {
                "reason": "converter_unavailable",
                "message": "no sidecar",
                "at": "2026-10-03T00:00:00+00:00",
            }
        },
    }
    out = content_mod._import_provenance_from_ingest(result)
    assert out["source_id"] == "src-def456"
    assert out["canonical_present"] is False
    assert "canonical_sha256" not in out
    assert out["canonical_error"]["reason"] in _WHITELIST_REASONS


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"source_id": None, "canonical_present": None, "canonical": None, "blobs": {}},
        {"canonical_present": None, "canonical": None, "blobs": {}},
        {"source_id": "src-x", "canonical_present": False, "canonical": None, "blobs": {}},
    ],
)
def test_provenance_from_ingest_sparse(result):
    """(г) нет данных → ключ отсутствует; никаких None-заглушек в значениях."""
    out = content_mod._import_provenance_from_ingest(result)
    # canonical_present=None (неизвестно) не пишется; canonical_sha256/error — только при данных.
    if result.get("canonical_present") is not None:
        assert out["canonical_present"] is result["canonical_present"]
    else:
        assert "canonical_present" not in out
    assert "canonical_sha256" not in out
    assert "canonical_error" not in out
    assert all(v is not None for v in out.values())


# ── (а)+(б) rec-уровень: _bg_import пишет провенанс ───────────

def _fake_state():
    app_state = MagicMock()
    app_state.heavy_ops_lock = asyncio.Lock()
    app_state.heavy_lock_owner = None
    app_state.import_progress = None
    app_state.import_cancel_event = None
    app_state.import_task = None
    return app_state


async def _run_bg_import(monkeypatch, ingest_result):
    from mcp_server.content.preprocessor import ValidationResult

    async def _fake_ingest(app_state, **kw):
        return ingest_result

    monkeypatch.setattr(content_mod, "ingest_source", _fake_ingest)

    class _FakePreprocessor:
        def validate(self, content, metadata):
            return ValidationResult(valid=True, content_size=0, estimated_sections=0)

        async def decompose(self, content, metadata, cancel_event=None):
            return []  # → "Decomposition produced 0 sections" (после ingest)

    monkeypatch.setattr(content_mod, "get_preprocessor", lambda ct: _FakePreprocessor())

    app_state = _fake_state()
    import_id = "test-prov-recv"
    rec = {"import_id": import_id, "status": "running", "log": [], "name": "t.pdf", "collection_id": ""}
    content_mod._import_queue.append(rec)

    params = {
        "_source_path": "", "content_type": "pdf", "domain": "library",
        "subject": "bibliography", "title": "t",
    }
    try:
        await content_mod._bg_import(
            import_id=import_id, params=params, app_state=app_state,
            cancel_event=asyncio.Event(), lock=app_state.heavy_ops_lock,
        )
    finally:
        content_mod._import_queue.clear()
    return rec


async def test_bg_import_writes_provenance_success(monkeypatch):
    """(а) успешный ingest → rec: source_id + canonical_present=True + sha256."""
    rec = await _run_bg_import(monkeypatch, {
        "source_id": "src-abc123",
        "canonical_present": True,
        "canonical": {"sha256": "e3b0c44298fc1c149afbf4c8996fb924"},
        "original": {"sha256": "e3b0c44298fc1c149afbf4c8996fb924"},
        "blobs": {},
        "reason": None,
        "created": True, "reused": False, "attached": False,
    })
    assert rec["source_id"] == "src-abc123"
    assert rec["canonical_present"] is True
    assert rec["canonical_sha256"] == "e3b0c44298fc1c149afbf4c8996fb924"
    assert "canonical_error" not in rec


async def test_bg_import_writes_canonical_error(monkeypatch):
    """(б) canonical-провал → rec: canonical_present=False + canonical_error.reason."""
    rec = await _run_bg_import(monkeypatch, {
        "source_id": "src-def456",
        "canonical_present": False,
        "canonical": None,
        "original": {"sha256": "aaa111222333444555666777888999000"},
        "blobs": {
            "canonical_error": {
                "reason": "converter_unavailable",
                "message": "no sidecar",
                "at": "2026-10-03T00:00:00+00:00",
            }
        },
        "reason": "converter_unavailable",
        "created": True, "reused": False, "attached": False,
    })
    assert rec["canonical_present"] is False
    assert "canonical_sha256" not in rec
    assert rec["canonical_error"]["reason"] in _WHITELIST_REASONS


# ── (в) REST-снапшот /progress: поля + нет _params ────────────

class _Auth:
    authenticated = True


def _progress_request(queue, tracker=None):
    req = MagicMock()
    req.state.auth = _Auth()
    req.app.state.import_queue = queue
    req.app.state.import_progress = tracker
    return req


async def test_import_progress_exposes_provenance_no_params():
    """(в) снапшот содержит поля провенанса и НЕ содержит _params."""
    rec = {
        "import_id": "imp-prov-1",
        "name": "x.pdf",
        "status": "done",
        "phase": "done",
        "imported": 5,
        "total": 5,
        "failed": 0,
        "error": None,
        "collection_id": "col-1",
        "finished_at": "2026-10-03T00:00:00Z",
        "result": {"collection_id": "col-1"},
        "operation_type": "import",
        "summary_text": "5 sections",
        "source_id": "src-abc123",
        "canonical_present": True,
        "canonical_sha256": "e3b0c44298fc1c149afbf4c8996fb924",
        "_params": {"secret": "must-not-leak"},
    }
    req = _progress_request([rec])
    snap = await import_progress("imp-prov-1", req)

    assert snap["source_id"] == "src-abc123"
    assert snap["canonical_present"] is True
    assert snap["canonical_sha256"] == "e3b0c44298fc1c149afbf4c8996fb924"
    assert "_params" not in snap


async def test_import_progress_canonical_error_sparse_no_params():
    """(в)+(г) провал: canonical_error в снапшоте; sparse-ключи отсутствуют; _params не утёк."""
    rec = {
        "import_id": "imp-prov-2",
        "name": "d.docx",
        "status": "error",
        "phase": "parsing",
        "imported": 0,
        "total": 0,
        "failed": 0,
        "error": "Decomposition produced 0 sections",
        "collection_id": "",
        "finished_at": None,
        "result": None,
        "operation_type": "import",
        "summary_text": None,
        "source_id": "src-def456",
        "canonical_present": False,
        "canonical_error": {
            "reason": "converter_unavailable",
            "message": "no sidecar",
            "at": "2026-10-03T00:00:00+00:00",
        },
        "_params": {"secret": "must-not-leak"},
    }
    req = _progress_request([rec])
    snap = await import_progress("imp-prov-2", req)

    assert snap["source_id"] == "src-def456"
    assert snap["canonical_present"] is False
    assert snap["canonical_error"]["reason"] in _WHITELIST_REASONS
    assert "canonical_sha256" not in snap
    assert "_params" not in snap


def test_import_provenance_main_sparse():
    """(г) хелпер снапшота: нет данных → пусто; _params никогда не пробрасывается."""
    assert _import_provenance({}) == {}
    assert _import_provenance({"_params": {"secret": "x"}, "source_id": "src-z"}) == {
        "source_id": "src-z"
    }
    assert all(v is not None for v in _import_provenance({"canonical_present": None}).values())
