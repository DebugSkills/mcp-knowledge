"""Ф3c2a (trace code-2026-10-02-bibliography): source_refs helper + import-path wiring.

Контракт (план §3.4, дизайн Ф3c2a):
- refs_from_section_meta: Section.meta (source_id + locator_spans) → list[ref];
  группировка по (source_id, kind) → покрывающий диапазон {start:min, end:max};
  display НЕ копируется; source_id без спанов → ref БЕЗ ключа locator (Л1);
  нет source_id → None.
- merge_source_refs: дедуп по (source_id, kind|None), existing-wins,
  чужие Source-ссылки сохраняются, None-безопасно.
- _batch_write_sections: write→read round-trip — fm.source_refs верные,
  fm.locator_spans (Ф2) не тронуты; переимпорт → merge без потери foreign.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import git
import pytest

from mcp_server.content.preprocessor import Section
from mcp_server.content.source_refs import merge_source_refs, refs_from_section_meta
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.content import _batch_write_sections

SID = "src-abc123def4567890"
SID2 = "src-fffffffffffffff0"
FOREIGN = "src-foreign00000000"


def _span(kind: str, start, end, off_s=0, off_e=100) -> dict:
    """Спан в формате content.locator.spans_to_meta (locator.py:437-439)."""
    return {
        "locator": {"kind": kind, "start": start, "end": end,
                    "display": f"с. {start}–{end}"},
        "offset_start": off_s,
        "offset_end": off_e,
    }


# ── 1. refs_from_section_meta ────────────────────────────────────


def test_refs_covering_range_same_kind():
    """Спаны page 3,4,5 одного source → ОДИН ref page 3–5, без display."""
    meta = {
        "source_id": SID,
        "locator_spans": [_span("page", 3, 3), _span("page", 4, 4), _span("page", 5, 5)],
    }
    assert refs_from_section_meta(meta) == [
        {"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}}
    ]


def test_refs_two_kinds_two_refs():
    meta = {
        "source_id": SID,
        "locator_spans": [_span("page", 3, 4), _span("timestamp", 10.0, 20.0)],
    }
    refs = refs_from_section_meta(meta)
    assert {(r["source_id"], r["locator"]["kind"]) for r in refs} == {
        (SID, "page"), (SID, "timestamp"),
    }
    page = next(r for r in refs if r["locator"]["kind"] == "page")
    assert page["locator"]["start"] == 3 and page["locator"]["end"] == 4


def test_refs_source_without_spans_no_locator_key():
    """source_id без спанов → ref без ключа locator (Л1: нет данных → нет ключа)."""
    assert refs_from_section_meta({"source_id": SID}) == [{"source_id": SID}]
    assert refs_from_section_meta({"source_id": SID, "locator_spans": []}) == [
        {"source_id": SID}
    ]


def test_refs_no_source_id_none():
    assert refs_from_section_meta({"locator_spans": [_span("page", 1, 2)]}) is None
    assert refs_from_section_meta({}) is None
    assert refs_from_section_meta(None) is None


# ── 2. merge_source_refs ─────────────────────────────────────────


def test_merge_dedup_existing_wins():
    existing = [{"source_id": SID, "locator": {"kind": "page", "start": 1, "end": 9}}]
    incoming = [{"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}}]
    assert merge_source_refs(existing, incoming) == existing


def test_merge_preserves_foreign_refs():
    foreign = [{"source_id": FOREIGN, "locator": {"kind": "page", "start": 1, "end": 2}}]
    mine = [{"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}}]
    assert merge_source_refs(foreign, mine) == foreign + mine


def test_merge_kind_none_is_distinct_key():
    existing = [{"source_id": SID}]  # без locator → kind=None
    incoming = [{"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}}]
    assert merge_source_refs(existing, incoming) == existing + incoming


def test_merge_none_safe():
    assert merge_source_refs(None, None) is None
    assert merge_source_refs(None, [{"source_id": SID}]) == [{"source_id": SID}]
    assert merge_source_refs([{"source_id": SID}], None) == [{"source_id": SID}]


# ── 3. _batch_write_sections round-trip (реальный MarkdownStore) ─


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (паттерн test_documents_integrity:48-56)."""
    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


def _mk_section(seq: int, kid: str, sid: str | None, spans: list[dict] | None) -> Section:
    meta: dict = {
        "knowledge_id": kid,
        "domain": "library",
        "subject": "bibliography",
        "content_type": "pdf",
    }
    if sid is not None:
        meta["source_id"] = sid
        if spans:
            meta["locator_spans"] = spans
    return Section(
        title=f"Секция {seq}",
        body=f"Тело секции {seq}",
        sequence_number=seq,
        tags=[],
        meta=meta,
    )


def _mk_app_state(store: MarkdownStore) -> MagicMock:
    """app_state с реальным store (прецедент test_import_blob_integration:182)."""
    app_state = MagicMock()
    app_state.store = store
    pipeline = MagicMock()
    pipeline.enqueue = AsyncMock(return_value=None)
    app_state.pipeline = pipeline
    app_state.import_progress = None
    return app_state


_PARAMS = {
    "domain": "library",
    "subject": "bibliography",
    "title": "Книга тестов",
    "tags": [],
    "cross_subjects": [],
}


async def test_batch_write_round_trip_source_refs_and_spans_untouched(store):
    spans1 = [_span("page", 3, 3, 0, 50), _span("page", 4, 4, 50, 100)]
    sections = [
        _mk_section(1, "lib-bib-sec-one", SID, spans1),
        _mk_section(2, "lib-bib-sec-two", SID2, None),  # source без спанов
    ]
    result = await _batch_write_sections(
        sections, dict(_PARAMS), _mk_app_state(store), "", asyncio.Event()
    )
    assert result["imported"] == 2 and result["failed"] == 0

    e1 = await store.read("lib-bib-sec-one")
    assert e1.frontmatter.source_refs == [
        {"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 4}}
    ]
    # Ф2 не тронута: спаны переживают write→read как есть
    assert e1.frontmatter.locator_spans == spans1
    assert e1.frontmatter.source_id == SID

    e2 = await store.read("lib-bib-sec-two")
    assert e2.frontmatter.source_refs == [{"source_id": SID2}]
    assert e2.frontmatter.locator_spans is None


# ── 4. Переимпорт → merge без потери foreign refs ────────────────


async def test_reimport_merges_without_foreign_loss(store):
    sections = [_mk_section(1, "lib-bib-sec-one", SID, [_span("page", 3, 4)])]
    r1 = await _batch_write_sections(
        sections, dict(_PARAMS), _mk_app_state(store), "", asyncio.Event()
    )
    assert r1["imported"] == 1

    # Между прогонами запись получила foreign-ссылку (напр. ручной update / Ф3c2b)
    await store.update(
        "lib-bib-sec-one",
        metadata={"source_refs": [
            {"source_id": FOREIGN, "locator": {"kind": "page", "start": 1, "end": 2}},
        ]},
    )

    # Переимпорт того же PDF: make_knowledge_id детерминирован → те же ids
    r2 = await _batch_write_sections(
        sections, dict(_PARAMS), _mk_app_state(store), "", asyncio.Event()
    )
    assert r2["imported"] == 1

    e = await store.read("lib-bib-sec-one")
    assert e.frontmatter.source_refs == [
        {"source_id": FOREIGN, "locator": {"kind": "page", "start": 1, "end": 2}},
        {"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 4}},
    ]


async def test_reimport_existing_wins_on_same_key(store):
    sections = [_mk_section(1, "lib-bib-sec-one", SID, [_span("page", 3, 4)])]
    await _batch_write_sections(
        sections, dict(_PARAMS), _mk_app_state(store), "", asyncio.Event()
    )
    await store.update(
        "lib-bib-sec-one",
        metadata={"source_refs": [
            {"source_id": SID, "locator": {"kind": "page", "start": 1, "end": 9}},
        ]},
    )
    await _batch_write_sections(
        sections, dict(_PARAMS), _mk_app_state(store), "", asyncio.Event()
    )
    e = await store.read("lib-bib-sec-one")
    assert e.frontmatter.source_refs == [
        {"source_id": SID, "locator": {"kind": "page", "start": 1, "end": 9}}
    ]
