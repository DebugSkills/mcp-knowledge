"""Тесты Source SSOT helpers (bibliography Ф1): id, guard коллизий, reuse, license."""

from __future__ import annotations

import pytest

from mcp_server.content.source import (
    SourceCollisionError,
    is_public_license,
    make_source_id,
    register_source,
)
from mcp_server.models import KnowledgeEntry


class _FakeStore:
    """Async MarkdownStore-подобный фейк (read/write_entry в памяти)."""

    def __init__(self):
        self.entries: dict[str, KnowledgeEntry] = {}

    async def read(self, knowledge_id):
        return self.entries.get(knowledge_id)

    async def write_entry(self, entry: KnowledgeEntry):
        self.entries[entry.frontmatter.knowledge_id] = entry
        return entry


def _sha(n: int = 0) -> str:
    return f"{n:064x}"


def test_make_source_id_format():
    sid = make_source_id(_sha(0xABCDEF))
    assert sid == f"src-{_sha(0xABCDEF)[:16]}"
    assert sid.startswith("src-")
    assert len(sid) == 4 + 16


def test_make_source_id_invalid():
    with pytest.raises(ValueError):
        make_source_id("abc")
    with pytest.raises(ValueError):
        make_source_id("g" * 64)  # не-hex


def test_is_public_license():
    assert is_public_license("own") is True
    assert is_public_license("licensed") is True
    assert is_public_license("cc-by-4.0") is True
    assert is_public_license("restricted") is False
    assert is_public_license("unknown") is False
    assert is_public_license(None) is False


async def test_register_source_creates():
    store = _FakeStore()
    src = await register_source(
        store, original_sha256=_sha(1), format="pdf",
        blobs={"original": {"sha256": _sha(1)}, "derived": []},
        license="cc-by-4.0",
    )
    assert src["created"] is True and src["reused"] is False
    assert src["knowledge_id"] == make_source_id(_sha(1))
    entry = store.entries[src["knowledge_id"]]
    assert entry.frontmatter.content_type == "source"
    assert entry.frontmatter.license == "cc-by-4.0"
    assert entry.frontmatter.public_allowed is True  # выведено из license


async def test_register_source_idempotent_reuse():
    store = _FakeStore()
    await register_source(
        store, original_sha256=_sha(2), format="pdf",
        blobs={"original": {"sha256": _sha(2)}, "derived": []},
    )
    # повторный вызов с тем же sha → reuse (не дубликат)
    src2 = await register_source(
        store, original_sha256=_sha(2), format="pdf",
        blobs={"original": {"sha256": _sha(2)}, "derived": []},
    )
    assert src2["reused"] is True and src2["created"] is False
    assert len(store.entries) == 1


async def test_register_source_collision():
    store = _FakeStore()
    await register_source(
        store, original_sha256=_sha(3), format="pdf",
        blobs={"original": {"sha256": _sha(3)}, "derived": []},
    )
    # тот же id (первые 16 hex совпадают), но другой ПОЛНЫЙ sha → коллизия
    colliding = "0" * 16 + "f" * 48  # первые 16 те же, хвост отличается
    with pytest.raises(SourceCollisionError):
        await register_source(
            store, original_sha256=colliding, format="pdf",
            blobs={"original": {"sha256": colliding}, "derived": []},
        )


async def test_register_source_reuse_attaches_canonical():
    """P0-1: reuse идемпотентно merge'ит canonical (attach ≠ recreate)."""
    store = _FakeStore()
    await register_source(
        store, original_sha256=_sha(5), format="docx",
        blobs={"original": {"sha256": _sha(5)}, "derived": []},
    )
    canonical = {"sha256": "c" * 64, "role": "canonical",
                 "derived_from": _sha(5), "tool": "libreoffice-headless"}
    src2 = await register_source(
        store, original_sha256=_sha(5), format="docx",
        blobs={"original": {"sha256": _sha(5)}, "canonical": canonical, "derived": []},
    )
    assert src2["reused"] is True and src2["attached"] is True
    entry = store.entries[src2["knowledge_id"]]
    assert entry.frontmatter.blobs["canonical"]["sha256"] == "c" * 64


async def test_register_source_reuse_does_not_overwrite_canonical():
    """C2 frozen: существующий canonical НЕ перезаписывается при reuse."""
    store = _FakeStore()
    first = {"sha256": "a" * 64, "role": "canonical", "derived_from": _sha(6), "tool": "v1"}
    await register_source(
        store, original_sha256=_sha(6), format="docx",
        blobs={"original": {"sha256": _sha(6)}, "canonical": first, "derived": []},
    )
    other = {"sha256": "b" * 64, "role": "canonical", "derived_from": _sha(6), "tool": "v2"}
    src2 = await register_source(
        store, original_sha256=_sha(6), format="docx",
        blobs={"original": {"sha256": _sha(6)}, "canonical": other, "derived": []},
    )
    assert src2["attached"] is False  # delta нет — canonical уже есть (frozen)
    entry = store.entries[src2["knowledge_id"]]
    assert entry.frontmatter.blobs["canonical"]["sha256"] == "a" * 64
