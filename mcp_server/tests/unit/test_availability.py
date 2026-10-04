"""Тесты availability: Ф1 (canonical_present + blob_available) + Ф3b1 (SourceRefIndex + least-strict)."""

from __future__ import annotations

from mcp_server.models import KnowledgeEntry, KnowledgeFrontmatter
from mcp_server.tools.availability import (
    auth_zones,
    blob_available,
    blob_available_indexed,
    canonical_present,
    ref_available,
    source_accessible,
)
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex, ref_from_entry


def _exists(sha256: str) -> bool:
    return True


def _missing(sha256: str) -> bool:
    return False


SUBSCRIBER = {"level": "subscriber"}
READ = {"level": "read"}
WRITE = {"level": "write"}


def _sha(char: str) -> str:
    return char * 64


def _src_entry(
    source_id: str,
    *,
    zone: str = "private",
    status: str = "published",
    license_value: str | None = None,
    public_allowed: bool | None = None,
    shas: tuple[str, ...] = (),
) -> KnowledgeEntry:
    """Минимальная SSOT Source-запись: original=shas[0], canonical=shas[1], derived=shas[2:]."""
    original = {"sha256": shas[0]} if len(shas) > 0 else None
    canonical = {"sha256": shas[1]} if len(shas) > 1 else None
    derived = [{"sha256": s} for s in shas[2:]]
    fm = KnowledgeFrontmatter(
        knowledge_id=source_id,
        domain="library",
        subject="bibliography",
        content_type="source",
        zone=zone,
        status=status,
        license=license_value,
        public_allowed=public_allowed,
        blobs={"original": original, "canonical": canonical, "derived": derived},
    )
    return KnowledgeEntry(frontmatter=fm, content="# Source\n")


# ── canonical_present (auth-free) ──────────────────────────

def test_canonical_present_true():
    blobs = {"canonical": {"sha256": "a" * 64}}
    assert canonical_present(blobs, _exists) is True


def test_canonical_present_missing_blob():
    blobs = {"canonical": {"sha256": "a" * 64}}
    assert canonical_present(blobs, _missing) is False


def test_canonical_present_no_canonical():
    assert canonical_present({"original": {"sha256": "a" * 64}}, _exists) is False
    assert canonical_present(None, _exists) is False
    assert canonical_present({"canonical": None}, _exists) is False


# ── auth_zones / source_accessible ─────────────────────────

def test_auth_zones():
    assert auth_zones({"level": "subscriber"}) == {"public"}
    assert auth_zones({"level": "read"}) == {"public"}  # P2-1: ниже admin → public
    assert auth_zones({"level": "editor"}) == {"public"}
    assert auth_zones({"level": "import"}) == {"public"}
    assert auth_zones({"level": "write"}) == {"public", "private"}
    assert auth_zones({}) == {"public"}  # без ключа → public only


def test_source_accessible_zone_and_status():
    pub = {"zone": "public", "status": "published"}
    priv = {"zone": "private", "status": "published"}
    assert source_accessible(pub, {"level": "subscriber"}) is True
    assert source_accessible(priv, {"level": "subscriber"}) is False
    assert source_accessible(priv, {"level": "read"}) is False  # P2-1: ниже admin
    assert source_accessible(priv, {"level": "write"}) is True
    assert source_accessible({"zone": "public", "status": "deprecated"}, {"level": "read"}) is False


# ── blob_available (level_b, fail-closed license) ──────────

def test_blob_available_public_cc_subscriber():
    src = {"zone": "public", "status": "published", "license": "cc-by-4.0", "public_allowed": True}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _exists) is True


def test_blob_available_license_unknown_fail_closed():
    src = {"zone": "public", "status": "published", "license": "unknown", "public_allowed": True}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _exists) is False


def test_blob_available_license_restricted_fail_closed():
    src = {"zone": "public", "status": "published", "license": "restricted", "public_allowed": False}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _exists) is False


def test_blob_available_private_subscriber_blocked():
    src = {"zone": "private", "status": "published", "license": "own", "public_allowed": True}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _exists) is False


def test_blob_available_private_full_key_allowed():
    src = {"zone": "private", "status": "published", "license": "restricted", "public_allowed": False}
    assert blob_available("a" * 64, src, {"level": "write"}, _exists) is True


def test_blob_available_blob_missing():
    src = {"zone": "public", "status": "published", "license": "cc-by-4.0", "public_allowed": True}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _missing) is False


def test_blob_available_deprecated():
    src = {"zone": "public", "status": "deprecated", "license": "cc-by-4.0", "public_allowed": True}
    assert blob_available("a" * 64, src, {"level": "subscriber"}, _exists) is False


# ── SourceRefIndex (Ф3b1): build/get/add/remove/rescan ──────

def test_index_build_and_get():
    pub = _src_entry("src-" + "a" * 16, zone="public", license_value="cc-by-4.0",
                     public_allowed=True, shas=(_sha("a"), _sha("b")))
    priv = _src_entry("src-" + "c" * 16, shas=(_sha("c"),))
    note = KnowledgeEntry(  # content_type ≠ source → не индексируется
        frontmatter=KnowledgeFrontmatter(knowledge_id="note-0001", domain="library",
                                         subject="bibliography"),
        content="# Note\n",
    )
    index = SourceRefIndex.build([pub, priv, note])
    assert [r.source_id for r in index.get(_sha("a"))] == ["src-" + "a" * 16]
    assert [r.source_id for r in index.get(_sha("b"))] == ["src-" + "a" * 16]
    assert [r.source_id for r in index.get(_sha("c"))] == ["src-" + "c" * 16]
    assert index.get(_sha("d")) == []
    assert len(index) == 3  # distinct shas: a, b, c
    ref = index.get(_sha("a"))[0]  # поля ref — из SSOT frontmatter
    assert (ref.zone, ref.status, ref.license, ref.public_allowed) == (
        "public", "published", "cc-by-4.0", True)


def test_index_build_dedup_same_sha():
    # original == canonical (pdf as-is, И2 дедуп) → один ключ, один ref
    e = _src_entry("src-" + "e" * 16, zone="public", license_value="own",
                   public_allowed=True, shas=(_sha("f"), _sha("f")))
    index = SourceRefIndex.build([e])
    assert len(index) == 1
    assert len(index.get(_sha("f"))) == 1


def test_index_size_telemetry():
    e1 = _src_entry("src-" + "1" * 16, shas=(_sha("a"), _sha("b")))
    e2 = _src_entry("src-" + "2" * 16, shas=(_sha("a"),))  # второй ref на тот же blob
    index = SourceRefIndex.build([e1, e2])
    assert len(index) == 2
    assert index.size() == {"blobs": 2, "refs": 3}


def test_index_add_upsert_and_stale_cleanup():
    index = SourceRefIndex.build([_src_entry("src-" + "9" * 16, shas=(_sha("a"), _sha("b")))])
    index.add(SourceRef(source_id="src-" + "8" * 16, zone="public", license="cc-by-4.0",
                        public_allowed=True, shas=(_sha("a"),)))
    assert len(index.get(_sha("a"))) == 2
    # upsert: тот же source_id теперь ссылается на другой sha — старые ключи вычищаются
    index.add(SourceRef(source_id="src-" + "8" * 16, zone="private", shas=(_sha("c"),)))
    assert [r.source_id for r in index.get(_sha("a"))] == ["src-" + "9" * 16]
    assert len(index.get(_sha("c"))) == 1


def test_index_remove_returns_count():
    e = _src_entry("src-" + "7" * 16, shas=(_sha("a"), _sha("b"), _sha("d")))  # +derived
    index = SourceRefIndex.build([e])
    assert index.remove("src-" + "7" * 16) == 3  # снято 3 (sha, ref)-пары
    assert index.get(_sha("a")) == [] and index.get(_sha("b")) == [] and index.get(_sha("d")) == []
    assert len(index) == 0
    assert index.remove("src-unknown") == 0


def test_index_rescan_atomic_replace():
    index = SourceRefIndex.build([_src_entry("src-" + "6" * 16, shas=(_sha("a"),))])
    index.rescan([_src_entry("src-" + "5" * 16, shas=(_sha("z"),))])
    assert index.get(_sha("a")) == []  # старые refs исчезли
    assert [r.source_id for r in index.get(_sha("z"))] == ["src-" + "5" * 16]
    assert len(index) == 1


def test_ref_from_entry_ignores_non_source():
    note = KnowledgeEntry(frontmatter=KnowledgeFrontmatter(
        knowledge_id="note-0002", domain="d", subject="s"), content="")
    assert ref_from_entry(note) is None


def test_ref_available_direct():
    assert ref_available(SourceRef(source_id="s-1", zone="public", license="cc-by-4.0",
                                    public_allowed=True), SUBSCRIBER) is True
    assert ref_available(SourceRef(source_id="s-2", zone="private"), SUBSCRIBER) is False
    assert ref_available(SourceRef(source_id="s-3", zone="private"), WRITE) is True


# ── blob_available_indexed (least-strict агрегация) ─────────

def test_indexed_least_strict_public_and_private_refs():
    pub = _src_entry("src-" + "a" * 16, zone="public", license_value="cc-by-4.0",
                     public_allowed=True, shas=(_sha("a"),))
    priv = _src_entry("src-" + "c" * 16, zone="private", license_value="restricted",
                      public_allowed=False, shas=(_sha("a"),))
    index = SourceRefIndex.build([pub, priv])
    # public-ref с разрешающим license открывает blob для subscriber (least-strict)
    assert blob_available_indexed(_sha("a"), SUBSCRIBER, exists_fn=_exists, index=index) is True
    assert blob_available_indexed(_sha("a"), WRITE, exists_fn=_exists, index=index) is True


def test_indexed_private_only_blob_denied_for_subscriber():
    priv = _src_entry("src-" + "c" * 16, zone="private", license_value="own", shas=(_sha("c"),))
    index = SourceRefIndex.build([priv])
    assert blob_available_indexed(_sha("c"), SUBSCRIBER, exists_fn=_exists, index=index) is False
    assert blob_available_indexed(_sha("c"), WRITE, exists_fn=_exists, index=index) is True


def test_indexed_license_unknown_fail_closed():
    pub = _src_entry("src-" + "b" * 16, zone="public", license_value="unknown",
                     public_allowed=True, shas=(_sha("u"),))
    index = SourceRefIndex.build([pub])
    assert blob_available_indexed(_sha("u"), SUBSCRIBER, exists_fn=_exists, index=index) is False


def test_indexed_license_restricted_fail_closed():
    pub = _src_entry("src-" + "b" * 16, zone="public", license_value="restricted",
                     public_allowed=True, shas=(_sha("r"),))
    index = SourceRefIndex.build([pub])
    assert blob_available_indexed(_sha("r"), SUBSCRIBER, exists_fn=_exists, index=index) is False


def test_indexed_public_allowed_false_denied():
    pub = _src_entry("src-" + "b" * 16, zone="public", license_value="cc-by-4.0",
                     public_allowed=False, shas=(_sha("p"),))
    index = SourceRefIndex.build([pub])
    assert blob_available_indexed(_sha("p"), SUBSCRIBER, exists_fn=_exists, index=index) is False


def test_indexed_deprecated_ref_excluded():
    dep = _src_entry("src-" + "d" * 16, zone="public", status="deprecated",
                     license_value="cc-by-4.0", public_allowed=True, shas=(_sha("a"),))
    live = _src_entry("src-" + "e" * 16, zone="private", shas=(_sha("a"),))
    # единственный public-ref deprecated → subscriber отказ; full-key — через private live-ref
    index = SourceRefIndex.build([dep, live])
    assert blob_available_indexed(_sha("a"), SUBSCRIBER, exists_fn=_exists, index=index) is False
    assert blob_available_indexed(_sha("a"), WRITE, exists_fn=_exists, index=index) is True
    # deprecated — единственный ref → отказ даже для full-key (status ≠ deprecated безусловно)
    only_dep = SourceRefIndex.build([dep])
    assert blob_available_indexed(_sha("a"), WRITE, exists_fn=_exists, index=only_dep) is False


def test_indexed_remove_public_ref_immediate_deny():
    pub = _src_entry("src-" + "a" * 16, zone="public", license_value="own",
                     public_allowed=True, shas=(_sha("a"),))
    priv = _src_entry("src-" + "c" * 16, zone="private", license_value="restricted",
                      shas=(_sha("a"),))
    index = SourceRefIndex.build([pub, priv])
    assert blob_available_indexed(_sha("a"), SUBSCRIBER, exists_fn=_exists, index=index) is True
    index.remove("src-" + "a" * 16)  # удаление единственного public-ref → немедленный отказ
    assert blob_available_indexed(_sha("a"), SUBSCRIBER, exists_fn=_exists, index=index) is False
    assert blob_available_indexed(_sha("a"), WRITE, exists_fn=_exists, index=index) is True


def test_indexed_blob_missing_integrity_first():
    pub = _src_entry("src-" + "a" * 16, zone="public", license_value="cc-by-4.0",
                     public_allowed=True, shas=(_sha("a"),))
    index = SourceRefIndex.build([pub])
    # integrity: нет blob → недоступно независимо от refs (даже full-key)
    assert blob_available_indexed(_sha("a"), WRITE, exists_fn=_missing, index=index) is False


def test_indexed_no_refs_denied():
    index = SourceRefIndex()
    assert blob_available_indexed(_sha("x"), READ, exists_fn=_exists, index=index) is False


# ── canonical_present: auth-free вне зависимости от availability ──

def test_canonical_present_auth_free_despite_license_and_zone():
    # физическое наличие blob не зависит от auth/лицензии: license=unknown →
    # availability отказывает, canonical_present остаётся True (состояние стора)
    blobs = {"canonical": {"sha256": _sha("a")}}
    assert canonical_present(blobs, _exists) is True
    e = _src_entry("src-" + "b" * 16, zone="public", license_value="unknown",
                   public_allowed=True, shas=(_sha("a"), _sha("a")))
    index = SourceRefIndex.build([e])
    assert blob_available_indexed(_sha("a"), SUBSCRIBER, exists_fn=_exists, index=index) is False
