"""T2a (trace code-2026-10-02-bibliography): update_fragment — привязка Source к секции.

Контракт (план Ф2b2/T2a):
- source_id (непустая строка) → fail-closed валидация (запись существует,
  content_type == "source") → fm.source_id записан, ответ ok; повторная
  привязка того же sid → ok (идемпотентность на уровне контракта);
- несуществующий / не-Source source_id → {"error": ...}, frontmatter НЕ изменён;
- source_id="" → ключ снят (fm.source_id is None, Л1: ключ не пишется в YAML);
- без параметра → прежнее поведение (ключ не появляется, в ответе его нет);
- source_id + zone в одном вызове → metadata мержится (zone-гейт W1.5 не сломан);
- read-time: get_entry секции после привязки отдаёт citation (citations_for_refs
  замокан — реальный контракт enrichment: read.py:220-245).

Real-store паттерн (Ф3c2a lifecycle:113-119): настоящий MarkdownStore в git-репо.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import git
import pytest

from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.storage.markdown_store import MarkdownStore
from mcp_server.tools.fragments import update_fragment
from mcp_server.tools.read import get_entry

SID = "src-abc123def4567890"
MISSING = "src-missing0000000"
NOT_SOURCE = "lib-bib-book0000000x"
ROOT = "lib-bib-root000000000"
SEC = "lib-bib-sec-one0000000"


@pytest.fixture
def store(tmp_path):
    """Реальный MarkdownStore в git-репозитории (паттерн Ф3c2a lifecycle:113-119)."""
    root = tmp_path / "knowledge"
    root.mkdir()
    git.Repo.init(str(root))
    return MarkdownStore(knowledge_root=root)


async def _mk_source(store: MarkdownStore, sid: str = SID) -> None:
    """Source-запись (content_type="source") — паттерн test_source_refs_crud."""
    fm = KnowledgeFrontmatter(
        knowledge_id=sid,
        domain="library",
        subject="bibliography",
        content_type="source",
        format="pdf",
        locator_kind="page",
    )
    await store.write_entry(KnowledgeEntry(frontmatter=fm, content=f"# Source {sid}"))


async def _mk_section(store: MarkdownStore, root_zone: str = "private") -> None:
    """Книга-коллекция + секция (parent_knowledge_id задан — guard P1-5 пройден)."""
    root_fm = KnowledgeFrontmatter(
        knowledge_id=ROOT,
        domain="library",
        subject="bibliography",
        content_type="collection",
        zone=root_zone,
    )
    await store.write_entry(KnowledgeEntry(frontmatter=root_fm, content="# Root"))
    sec_fm = KnowledgeFrontmatter(
        knowledge_id=SEC,
        domain="library",
        subject="bibliography",
        content_type="book",
        parent_knowledge_id=ROOT,
        sequence_number=1,
    )
    await store.write_entry(KnowledgeEntry(frontmatter=sec_fm, content="# Section title\n\nbody"))


async def _mk_not_source(store: MarkdownStore) -> None:
    """Не-Source запись (content_type="book") — для negative-валидации."""
    fm = KnowledgeFrontmatter(
        knowledge_id=NOT_SOURCE,
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


class TestUpdateFragmentSourceId:
    async def test_bind_source_roundtrip_and_rebind(self, store):
        """Привязка валидного Source → fm.source_id записан; повторная привязка → ok."""
        await _mk_source(store)
        await _mk_section(store)
        app = _mk_app_state(store)

        resp = await update_fragment({"fragment_id": SEC, "source_id": SID}, app)
        assert "error" not in resp, resp
        assert resp.get("source_id") == SID
        entry = await store.read(SEC)
        assert entry.frontmatter.source_id == SID

        # Идемпотентность: повторная привязка того же sid → ok, значение то же
        resp2 = await update_fragment({"fragment_id": SEC, "source_id": SID}, app)
        assert "error" not in resp2, resp2
        entry2 = await store.read(SEC)
        assert entry2.frontmatter.source_id == SID

    async def test_bind_missing_source_fail_closed(self, store):
        """Несуществующий source_id → error; frontmatter и версия НЕ изменены."""
        await _mk_section(store)
        app = _mk_app_state(store)
        before = await store.read(SEC)
        v_before = before.frontmatter.version

        resp = await update_fragment({"fragment_id": SEC, "source_id": MISSING}, app)
        assert "error" in resp
        assert MISSING in resp["error"]
        after = await store.read(SEC)
        assert after.frontmatter.source_id is None
        assert after.frontmatter.version == v_before

    async def test_bind_not_a_source_fail_closed(self, store):
        """source_id указывает на не-Source запись → error, frontmatter НЕ изменён."""
        await _mk_source(store)
        await _mk_section(store)
        await _mk_not_source(store)
        app = _mk_app_state(store)
        v_before = (await store.read(SEC)).frontmatter.version

        resp = await update_fragment({"fragment_id": SEC, "source_id": NOT_SOURCE}, app)
        assert "error" in resp
        assert NOT_SOURCE in resp["error"]
        after = await store.read(SEC)
        assert after.frontmatter.source_id is None
        assert after.frontmatter.version == v_before

    async def test_unlink_empty_string(self, store):
        """source_id="" → ключ снят (fm.source_id is None)."""
        await _mk_source(store)
        await _mk_section(store)
        app = _mk_app_state(store)

        await update_fragment({"fragment_id": SEC, "source_id": SID}, app)
        assert (await store.read(SEC)).frontmatter.source_id == SID

        resp = await update_fragment({"fragment_id": SEC, "source_id": ""}, app)
        assert "error" not in resp, resp
        entry = await store.read(SEC)
        assert entry.frontmatter.source_id is None

    async def test_no_param_regression(self, store):
        """Без source_id → прежнее поведение: ключ не появляется, в ответе его нет."""
        await _mk_section(store)
        app = _mk_app_state(store)

        resp = await update_fragment(
            {"fragment_id": SEC, "title": "Renamed section"}, app,
        )
        assert "error" not in resp, resp
        assert "source_id" not in resp
        entry = await store.read(SEC)
        assert entry.frontmatter.source_id is None
        assert entry.content.startswith("# Renamed section")

    async def test_zone_and_source_combined(self, store):
        """source_id + zone в одном вызове → metadata мержится, оба применены (W1.5 не сломан).

        Книга public (W1.5: секция public в private-книге принудительно
        опускается вниз — принуждение покрыто существующими zone-тестами).
        """
        await _mk_source(store)
        await _mk_section(store, root_zone="public")
        app = _mk_app_state(store)

        resp = await update_fragment(
            {"fragment_id": SEC, "source_id": SID, "zone": "public"}, app,
        )
        assert "error" not in resp, resp
        assert resp.get("source_id") == SID
        assert resp.get("zone") == "public"
        entry = await store.read(SEC)
        assert entry.frontmatter.source_id == SID
        assert entry.frontmatter.zone == "public"

    async def test_get_entry_citation_after_bind(self, store):
        """read-time: get_entry секции после привязки отдаёт citation (fm.source_id → refs)."""
        await _mk_source(store)
        await _mk_section(store)
        app = _mk_app_state(store)
        await update_fragment({"fragment_id": SEC, "source_id": SID}, app)

        fake_citations = [{
            "citation": "Author, A. (2020). Test Book.",
            "citation_reason": "frontmatter.source_id",
        }]
        with patch(
            "mcp_server.tools.read.citations_for_refs",
            new=AsyncMock(return_value=fake_citations),
        ):
            resp = await get_entry({"knowledge_id": SEC}, app)
        assert "error" not in resp, resp
        assert resp.get("citation") == "Author, A. (2020). Test Book."
        assert resp.get("citation_reason") == "frontmatter.source_id"
