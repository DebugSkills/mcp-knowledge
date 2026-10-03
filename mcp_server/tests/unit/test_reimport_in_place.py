"""Ф6a (trace code-2026-10-02-bibliography): reimport-in-place (§3.6).

Контракт (план §3.6:228):
- guard от случайности сохранён: self-replace (replace_collection_id ==
  детерминированный collection_id) без reimport_in_place → отказ;
- осознанный пере-импорт — reimport_in_place=True ТОЛЬКО вместе с
  replace_collection_id == ожидаемому id (иначе отказ с причиной);
- поток: heavy_ops_lock → pre-checks (запись есть; integrity зелёный при
  document_store) → delete cascade старой (crud.delete_entry) → импорт под
  тем же id → flush → reindex → финальный documents_check;
- source_id (src-<sha256_16>) стабилен, citations (source_refs) выживают;
- ×2 → 4 среза (SSOT / Qdrant points / registry.db / citation JSON) идентичны.

Golden-срезы на tmp-фикстурах: SSOT — реальный MarkdownStore (нормализован:
strip created_at/updated_at), Qdrant points — FakeQdrant (in-memory), registry.db —
реальный DocumentStore, citation — source_refs секций. Детерминированный
FakePreprocessor задаёт фиксированные knowledge_id секций + source_id.
"""

from __future__ import annotations

import asyncio
import json
import re
import types
from datetime import datetime, timezone

import pytest

from mcp_server.config import settings
from mcp_server.content.preprocessor import Section, ValidationResult
from mcp_server.content.source import register_source
from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.document_store import DocumentStore
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools import content as content_mod
from mcp_server.tools.content import import_content

pytestmark = pytest.mark.asyncio

COLLECTION_ID = "library-bibliography-reimport-book-collection"
SECTION_IDS = ["lib-bib-chapter-1", "lib-bib-chapter-2"]


# ── Fakes ────────────────────────────────────────────────────


class FakeQdrant:
    """In-memory Qdrant-фейк: трекает точки по knowledge_id (payload-id срез)."""

    def __init__(self):
        self._points: dict[str, dict] = {}

    def upsert_point(self, knowledge_id, parent_knowledge_id, content_type, sequence_number):
        self._points[knowledge_id] = {
            "knowledge_id": knowledge_id,
            "parent_knowledge_id": parent_knowledge_id,
            "content_type": content_type,
            "sequence_number": sequence_number,
        }

    def scroll(self, limit=1000, offset=None, scroll_filter=None, with_payload=None,
               with_vectors=False, collection_name=None):
        parent_val = None
        if scroll_filter is not None:
            for cond in getattr(scroll_filter, "must", []) or []:
                if getattr(cond, "key", "") == "parent_knowledge_id":
                    parent_val = getattr(getattr(cond, "match", None), "value", None)
        pts = []
        for kid, payload in self._points.items():
            if parent_val is None or payload.get("parent_knowledge_id") == parent_val:
                pts.append(types.SimpleNamespace(payload=dict(payload)))
        return (pts, None)

    def delete_by_knowledge_id(self, knowledge_id, collection_name=None):
        self._points.pop(knowledge_id, None)

    def get_all_knowledge_ids(self, collection_name=None):
        return set(self._points.keys())


class _FakePreprocessor:
    """Детерминированный препроцессор: 2 секции с фикс. ids (+опц. source_id)."""

    def __init__(self, source_id=None):
        self._source_id = source_id

    def validate(self, content, metadata):
        return ValidationResult(valid=True, content_size=len(content), estimated_sections=2)

    async def decompose(self, content, metadata, cancel_event=None):
        sections = []
        for seq in (1, 2):
            meta = {
                "knowledge_id": SECTION_IDS[seq - 1],
                "domain": metadata.domain,
                "subject": metadata.subject,
                "content_type": "book",
            }
            if self._source_id:
                meta["source_id"] = self._source_id
            sections.append(Section(
                title=f"Chapter {seq}",
                body=f"Body {seq}.",
                sequence_number=seq,
                tags=[],
                meta=meta,
            ))
        return sections


# ── Helpers ──────────────────────────────────────────────────


def _params(**overrides):
    params = {
        "content": "dummy",
        "content_type": "book",
        "domain": "library",
        "subject": "bibliography",
        "title": "Reimport Book",
        "quality_checks": False,
    }
    params.update(overrides)
    return params


def _reimport_params(**overrides):
    params = _params(reimport_in_place=True, replace_collection_id=COLLECTION_ID)
    params.update(overrides)
    return params


def _mk_app_state(store, ds=None, qdrant=None):
    state = types.SimpleNamespace()
    state.store = store

    async def _enqueue(entry, wait_for_index=False):
        if qdrant is not None:
            fm = entry.frontmatter
            qdrant.upsert_point(
                fm.knowledge_id,
                getattr(fm, "parent_knowledge_id", None),
                getattr(fm, "content_type", None),
                getattr(fm, "sequence_number", None),
            )

    state.pipeline = types.SimpleNamespace(enqueue=_enqueue)
    state.knowledge_index = types.SimpleNamespace(update_section=lambda *a, **k: None)
    state.qdrant = qdrant
    state.qdrant_client = None
    state.embedder = None
    state.document_store = ds
    state.heavy_ops_lock = asyncio.Lock()
    state.heavy_lock_owner = None
    state.import_progress = None
    state.import_cancel_event = None
    state.import_task = None
    state.data_version = 0
    return state


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    return MarkdownStore(knowledge_root=root)


@pytest.fixture
def ds(tmp_path):
    return DocumentStore(tmp_path / "documents", max_gb=1)


def _install_preprocessor(monkeypatch, source_id=None):
    monkeypatch.setattr(content_mod, "get_preprocessor",
                        lambda ct: _FakePreprocessor(source_id))


async def _make_collection(store, kid):
    fm = KnowledgeFrontmatter(
        knowledge_id=kid,
        domain="library",
        subject="bibliography",
        content_type="collection",
        zone="private",
        tags=["test"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content="# Other\n\nBody."))


async def _seed_source(store, ds) -> str:
    data = b"reimport-test-pdf-content" * 8
    put = ds.put(data, mime="application/pdf", filename="src.pdf")
    sha = put.sha256
    res = await register_source(
        store,
        original_sha256=sha,
        format="pdf",
        blobs={
            "original": {"sha256": sha, "mime": "application/pdf",
                         "size": put.size, "original_filename": "src.pdf"},
            "canonical": {"sha256": sha, "role": "canonical", "derived_from": None,
                          "tool": "as-is", "tool_version": None},
            "derived": [],
        },
        domain="library",
        subject="bibliography",
        zone="private",
    )
    return res["knowledge_id"]


# ── Golden slices ────────────────────────────────────────────


def _ssot_snapshot(store) -> str:
    parts = []
    for p in sorted(store._root.rglob("*.md")):
        if ".trash" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^(created_at|updated_at):.*\n", "", text)
        parts.append(f"==={p.relative_to(store._root)}===\n{text}")
    return "\n".join(parts)


def _points_snapshot(qdrant) -> str:
    lines = []
    for kid in sorted(qdrant._points):
        p = qdrant._points[kid]
        lines.append(
            f"{kid}|{p.get('parent_knowledge_id')}|{p.get('content_type')}|{p.get('sequence_number')}"
        )
    return "\n".join(lines)


def _registry_snapshot(ds) -> str:
    return "\n".join(sorted(b.sha256 for b in ds.list_blobs()))


async def _citation_snapshot(store, section_ids) -> str:
    lines = []
    for kid in sorted(section_ids):
        e = await store.read(kid)
        refs = e.frontmatter.source_refs if e is not None else None
        lines.append(f"{kid}:{json.dumps(refs, sort_keys=True)}")
    return "\n".join(lines)


async def _golden(store, ds, qdrant):
    return (
        _ssot_snapshot(store),
        _points_snapshot(qdrant),
        _registry_snapshot(ds),
        await _citation_snapshot(store, SECTION_IDS),
    )


# ── (а) без reimport_in_place → guard как раньше ─────────────


async def test_self_replace_guard_without_reimport(store, ds, monkeypatch):
    _install_preprocessor(monkeypatch)
    app_state = _mk_app_state(store, ds, FakeQdrant())
    await import_content(_params(), app_state)  # первичный импорт → COLLECTION_ID
    result = await import_content(_params(replace_collection_id=COLLECTION_ID), app_state)
    assert "error" in result
    assert "Self-replace detected" in result["error"]


# ── (б) reimport_in_place без/с неверным replace_collection_id ─


async def test_reimport_requires_replace_collection_id(store, ds, monkeypatch):
    _install_preprocessor(monkeypatch)
    app_state = _mk_app_state(store, ds, FakeQdrant())
    result = await import_content(
        _params(reimport_in_place=True, replace_collection_id=""), app_state,
    )
    assert "error" in result
    assert "requires 'replace_collection_id'" in result["error"]


async def test_reimport_rejects_wrong_replace_collection_id(store, ds, monkeypatch):
    _install_preprocessor(monkeypatch)
    await _make_collection(store, "library-bibliography-other-collection")
    app_state = _mk_app_state(store, ds, FakeQdrant())
    result = await import_content(
        _params(
            reimport_in_place=True,
            replace_collection_id="library-bibliography-other-collection",
        ),
        app_state,
    )
    assert "error" in result
    assert "does not match expected" in result["error"]


# ── (в) корректный reimport → успех, тот же source_id, citations живы ─


async def test_reimport_success_same_source_id_citations_survive(store, ds, monkeypatch):
    sid = await _seed_source(store, ds)
    _install_preprocessor(monkeypatch, source_id=sid)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)

    r1 = await import_content(_params(), app_state)
    assert "error" not in r1, r1.get("error")
    assert r1["collection_id"] == COLLECTION_ID

    r2 = await import_content(_reimport_params(), app_state)
    assert "error" not in r2, r2.get("error")
    assert r2["collection_id"] == COLLECTION_ID
    assert r2["reimport_in_place"] is True
    assert r2["replaced"] is True
    assert r2["cascade_deleted"] == 2

    # тот же source_id (детерминированный) + citation жив
    e1 = await store.read("lib-bib-chapter-1")
    assert e1.frontmatter.source_refs == [{"source_id": sid}]
    # Source-запись жива (cascade задевает только коллекцию)
    assert await store.read(sid) is not None
    # финальный integrity зелёный
    assert r2["integrity_ok"] is True


# ── (г) ×2 → 4 среза идентичны ───────────────────────────────


async def test_reimport_idempotent_golden_slices(store, ds, monkeypatch):
    sid = await _seed_source(store, ds)
    _install_preprocessor(monkeypatch, source_id=sid)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)

    await import_content(_params(), app_state)  # первичный импорт

    r1 = await import_content(_reimport_params(), app_state)  # reimport #1
    assert "error" not in r1, r1.get("error")  # F-6: явная причина вместо KeyError
    g1 = await _golden(store, ds, qdrant)
    assert r1["cascade_deleted"] == 2

    r2 = await import_content(_reimport_params(), app_state)  # reimport #2
    assert "error" not in r2, r2.get("error")  # F-6: явная причина вместо KeyError
    g2 = await _golden(store, ds, qdrant)
    assert r2["cascade_deleted"] == 2

    assert g1 == g2


# ── (д) ошибка на шаге cascade / финальный integrity ─────────


async def test_reimport_cascade_failure_explicit(store, ds, monkeypatch):
    sid = await _seed_source(store, ds)
    _install_preprocessor(monkeypatch, source_id=sid)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)
    await import_content(_params(), app_state)

    import mcp_server.tools.crud as crud_mod

    async def _fail_delete(params, app_state):
        return {"error": "cascade boom"}

    monkeypatch.setattr(crud_mod, "delete_entry", _fail_delete)

    result = await import_content(_reimport_params(), app_state)
    assert "error" in result
    assert "cascade delete failed" in result["error"]
    assert "cascade boom" in result["error"]


async def test_reimport_runs_final_integrity(store, ds, monkeypatch):
    sid = await _seed_source(store, ds)
    _install_preprocessor(monkeypatch, source_id=sid)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)
    await import_content(_params(), app_state)

    result = await import_content(_reimport_params(), app_state)
    assert "error" not in result, result.get("error")
    assert "integrity_ok" in result
    assert result["integrity_ok"] is True
    assert result["integrity"] is not None
    assert result["integrity"]["counts"]["sources"] == 1


# ── (е) lock соблюдён (один импорт за раз) ───────────────────


async def test_reimport_releases_lock(store, ds, monkeypatch):
    sid = await _seed_source(store, ds)
    _install_preprocessor(monkeypatch, source_id=sid)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)
    await import_content(_params(), app_state)
    await import_content(_reimport_params(), app_state)
    assert not app_state.heavy_ops_lock.locked()


async def test_reimport_lock_busy_returns_error(store, ds, monkeypatch):
    _install_preprocessor(monkeypatch)
    qdrant = FakeQdrant()
    app_state = _mk_app_state(store, ds, qdrant)
    await import_content(_params(), app_state)  # создаёт COLLECTION_ID (early-гейт прошёл)

    held = asyncio.Lock()
    await held.acquire()  # другая heavy-операция держит lock
    app_state.heavy_ops_lock = held
    monkeypatch.setattr(settings, "RECONCILE_LOCK_WAIT_SECONDS", 0.05)

    result = await import_content(_reimport_params(), app_state)
    assert "error" in result
    assert "busy" in result["error"]
    held.release()
