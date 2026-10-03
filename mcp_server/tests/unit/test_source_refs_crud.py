"""Ф3c2b (trace code-2026-10-02-bibliography): write_knowledge documents + update_entry source_refs.

Контракт (план §3.4, дизайн Ф3c2b):
- write_knowledge(documents=[{source_id, locator?}]) → fm.source_refs записан,
  в ответе documents_linked=N; алиас source_refs эквивалентен; оба заданы →
  merge_source_refs-дедуп (alias = existing, documents добавляются);
- fail-closed ДО записи: несуществующий source_id / не-Source / битый item →
  ошибка со ВСЕМИ собранными ошибками, запись НЕ создана (файлов в SSOT нет);
- update_entry(source_refs=[...]) → store.update(metadata={"source_refs": ...}),
  write→read round-trip + bump version; валидация так же fail-closed (запись
  не меняется); явный [] очищает refs (Л1: ключ не пишется);
- MCP-контракт: documents/source_refs в inputSchema write_knowledge/update_entry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import git
import pytest

from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools import TOOLS
from mcp_server.tools.crud import update_entry, write_knowledge

SID = "src-abc123def4567890"
SID2 = "src-fffffffffffffff0"
BOOK = "lib-bib-book0000000x"
MISSING = "src-missing0000000"
KID = "lib-bib-note-one"


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (паттерн Ф3c2a lifecycle:113-119)."""
    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


async def _mk_source(store: MarkdownStore, sid: str = SID) -> None:
    """Source-запись (content_type="source") — паттерн content/source.py:149-165."""
    fm = KnowledgeFrontmatter(
        knowledge_id=sid,
        domain="library",
        subject="bibliography",
        content_type="source",
        format="pdf",
        locator_kind="page",
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content=f"# Source {sid}"))


async def _mk_book(store: MarkdownStore) -> None:
    """Не-Source запись (content_type="book") — для negative-валидации."""
    fm = KnowledgeFrontmatter(
        knowledge_id=BOOK,
        domain="library",
        subject="bibliography",
        content_type="book",
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content="# Book"))


def _mk_app_state(store: MarkdownStore) -> MagicMock:
    """app_state с реальным store; embedder/qdrant = None → quality-gates skip."""
    st = MagicMock()
    st.store = store
    st.embedder = None
    st.qdrant = None
    st.qdrant_client = None
    st.pipeline = MagicMock()
    st.pipeline.enqueue = AsyncMock(return_value=None)
    st.knowledge_index = MagicMock()
    st.source_ref_index = None
    st.data_version = 0
    return st


def _md_paths(store: MarkdownStore) -> set[str]:
    """Снимок SSOT: относительные пути всех .md (детект «ничего не записано»)."""
    root = store._root  # noqa: SLF001 — тестовая верификация диска
    return {str(p.relative_to(root)) for p in root.rglob("*.md")}


_BASE = {
    "content": "# Заметка из книги",
    "domain": "library",
    "subject": "bibliography",
    "knowledge_id": KID,
}


# ── 1. write_knowledge documents: happy path ─────────────────────


async def test_write_documents_round_trip(store):
    await _mk_source(store)
    docs = [{"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}}]
    result = await write_knowledge(dict(_BASE, documents=docs), _mk_app_state(store))

    assert "error" not in result
    assert result["documents_linked"] == 1
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs == docs


async def test_write_source_refs_alias(store):
    """Алиас source_refs ≡ documents (один формат, один эффект)."""
    await _mk_source(store)
    refs = [{"source_id": SID}]
    result = await write_knowledge(dict(_BASE, source_refs=refs), _mk_app_state(store))

    assert "error" not in result
    assert result["documents_linked"] == 1
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs == refs


async def test_write_both_params_dedup(store):
    """Оба параметра → merge_source_refs: alias-first, дедуп по (source_id, kind)."""
    await _mk_source(store)
    await _mk_source(store, SID2)
    result = await write_knowledge(
        dict(
            _BASE,
            source_refs=[{"source_id": SID, "locator": {"kind": "page", "start": 1, "end": 2}}],
            documents=[
                {"source_id": SID, "locator": {"kind": "page", "start": 3, "end": 5}},  # дубликат ключа
                {"source_id": SID2},  # новый ключ
            ],
        ),
        _mk_app_state(store),
    )

    assert "error" not in result
    assert result["documents_linked"] == 2
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs == [
        {"source_id": SID, "locator": {"kind": "page", "start": 1, "end": 2}},
        {"source_id": SID2},
    ]


# ── 2. write_knowledge: fail-closed ДО записи ────────────────────


async def test_write_unknown_source_fail_closed_no_file(store):
    await _mk_source(store)
    before = _md_paths(store)
    result = await write_knowledge(
        dict(_BASE, documents=[{"source_id": SID}, {"source_id": MISSING}]),
        _mk_app_state(store),
    )

    assert "error" in result
    assert f"source '{MISSING}' not found" in result["error"]
    assert await store.read(KID) is None
    assert _md_paths(store) == before  # SSOT не тронут: НИ ОДНОГО нового файла


async def test_write_not_a_source_fail_closed(store):
    await _mk_book(store)
    before = _md_paths(store)
    result = await write_knowledge(
        dict(_BASE, documents=[{"source_id": BOOK}]),
        _mk_app_state(store),
    )

    assert "error" in result
    assert f"'{BOOK}' is not a Source" in result["error"]
    assert "book" in result["error"]
    assert await store.read(KID) is None
    assert _md_paths(store) == before


async def test_write_collects_all_errors(store):
    """Все ошибки перечислены, а не первая (пустой id + missing + не-Source)."""
    await _mk_book(store)
    result = await write_knowledge(
        dict(
            _BASE,
            documents=[
                {"locator": {"kind": "page", "start": 1, "end": 2}},  # нет source_id
                {"source_id": MISSING},
                {"source_id": BOOK},
            ],
        ),
        _mk_app_state(store),
    )

    assert "error" in result
    errors = result["errors"]
    assert isinstance(errors, list) and len(errors) == 3
    assert any("'source_id' must be a non-empty string" in e for e in errors)
    assert any(f"source '{MISSING}' not found" in e for e in errors)
    assert any(f"'{BOOK}' is not a Source" in e for e in errors)
    assert await store.read(KID) is None


async def test_write_documents_must_be_array(store):
    result = await write_knowledge(dict(_BASE, documents="oops"), _mk_app_state(store))

    assert "error" in result
    assert "documents" in result["error"] and "array" in result["error"]
    assert await store.read(KID) is None


# ── 3. update_entry source_refs ──────────────────────────────────


async def test_update_entry_source_refs_round_trip(store):
    await _mk_source(store)
    st = _mk_app_state(store)
    await write_knowledge(dict(_BASE), st)

    refs = [{"source_id": SID, "locator": {"kind": "page", "start": 10, "end": 20}}]
    result = await update_entry({"knowledge_id": KID, "source_refs": refs}, st)

    assert "error" not in result
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs == refs
    assert entry.frontmatter.version == 2  # metadata-only update тоже bump-ит версию


async def test_update_entry_fail_closed(store):
    await _mk_source(store)
    st = _mk_app_state(store)
    await write_knowledge(dict(_BASE), st)

    result = await update_entry(
        {"knowledge_id": KID, "source_refs": [{"source_id": MISSING}]}, st
    )

    assert "error" in result
    assert f"source '{MISSING}' not found" in result["error"]
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs is None
    assert entry.frontmatter.version == 1  # запись не менялась


async def test_update_entry_not_a_source(store):
    await _mk_book(store)
    st = _mk_app_state(store)
    await write_knowledge(dict(_BASE), st)

    result = await update_entry(
        {"knowledge_id": KID, "source_refs": [{"source_id": BOOK}]}, st
    )

    assert "error" in result
    assert f"'{BOOK}' is not a Source" in result["error"]


async def test_update_entry_empty_list_clears_refs(store):
    """Явный [] → source_refs очищен (Л1: ключ не пишется в YAML)."""
    await _mk_source(store)
    st = _mk_app_state(store)
    await write_knowledge(dict(_BASE, documents=[{"source_id": SID}]), st)

    result = await update_entry({"knowledge_id": KID, "source_refs": []}, st)

    assert "error" not in result
    entry = await store.read(KID)
    assert entry.frontmatter.source_refs is None


# ── 4. MCP-контракт: inputSchema ─────────────────────────────────


def _tool_schema(name: str) -> dict:
    return next(t for t in TOOLS if t["name"] == name)["inputSchema"]


def test_write_schema_exposes_documents_and_source_refs():
    props = _tool_schema("write_knowledge")["properties"]
    for key in ("documents", "source_refs"):
        assert key in props, f"write_knowledge.{key} отсутствует в MCP-схеме"
        assert props[key]["type"] == "array"
        assert props[key]["items"]["type"] == "object"
        assert "source_id" in props[key]["items"]["properties"]
        assert props[key]["items"]["required"] == ["source_id"]


def test_update_schema_exposes_source_refs():
    props = _tool_schema("update_entry")["properties"]
    assert "source_refs" in props
    assert props["source_refs"]["type"] == "array"
    assert props["source_refs"]["items"]["required"] == ["source_id"]
