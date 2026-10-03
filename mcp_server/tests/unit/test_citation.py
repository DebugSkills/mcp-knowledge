"""Тесты citation-ядра (bibliography Ф4b1, план §3.4:179-192).

Контракты:
- citation либо полон по уровню, либо ОТСУТСТВУЕТ ЦЕЛИКОМ (None, не {}/null-поле);
- level_b требует canonical_present ∧ blob_available_indexed (least-strict);
- render_gost не фабрикует при недостаточном CSL;
- classify_reason: ровно одна из 9 причин §3.4:192 при canonical_present=false.
"""

from __future__ import annotations

from mcp_server.content.citation import (
    DEFAULT_VIEWER_URL_TPL,
    CitationDecision,
    build_citation,
    classify_reason,
    decide_citation,
    render_gost,
)
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex

SHA_ORIG = "a" * 64
SHA_CANON = "b" * 64

SUBSCRIBER = {"level": "subscriber"}
READ = {"level": "read"}

CSL = {
    "type": "book",
    "title": "Введение в информационный поиск",
    "author": [
        {"family": "Маннинг", "given": "Кристофер Д."},
        {"family": "Рагхаван", "given": "Прабхакар"},
        {"family": "Шютце", "given": "Хайнрих"},
    ],
    "issued": {"date-parts": [[2008]]},
    "publisher": "Вильямс",
}

CSL_MANY_AUTHORS = {
    "title": "Структуры данных",
    "author": [
        {"family": "Ахо", "given": "Альфред"},
        {"family": "Хопкрофт", "given": "Джон"},
        {"family": "Ульман", "given": "Джеффри"},
        {"family": "Кнут", "given": "Дональд"},
    ],
}


def _exists_all(sha256: str) -> bool:
    return True


def _exists_none(sha256: str) -> bool:
    return False


class _Exists:
    """exists_fn с явным множеством живых blob."""

    def __init__(self, *shas: str):
        self.shas = set(shas)

    def __call__(self, sha256: str) -> bool:
        return sha256 in self.shas


def _blobs(canonical: str | None = None, original: str | None = SHA_ORIG, error: str | None = None) -> dict:
    blobs: dict = {
        "original": {"sha256": original, "mime": "application/pdf", "size": 10, "original_filename": "doc.pdf"},
        "derived": [],
    }
    if canonical:
        blobs["canonical"] = {"sha256": canonical, "role": "canonical", "derived_from": None, "tool": "as-is"}
    if error:
        blobs["canonical_error"] = {"reason": error}
    return blobs


def _fm(**over) -> dict:
    fm = {
        "knowledge_id": "src-aaaa1111bbbb2222",
        "content_type": "source",
        "zone": "public",
        "status": "published",
        "format": "pdf",
        "ingest_policy_applied": "normalize",
        "license": "cc-by-4.0",
        "public_allowed": True,
        "bibliography": dict(CSL),
        "blobs": _blobs(canonical=SHA_CANON),
    }
    fm.update(over)
    return fm


def _ref(
    sha: str,
    *,
    source_id: str = "src-aaaa1111bbbb2222",
    zone: str = "public",
    status: str = "published",
    license_value: str | None = "cc-by-4.0",
    public_allowed: bool | None = True,
) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        zone=zone,
        status=status,
        public_allowed=public_allowed,
        license=license_value,
        shas=(sha,),
    )


def _index(*refs: SourceRef) -> SourceRefIndex:
    index = SourceRefIndex()
    for ref in refs:
        index.add(ref)
    return index


# ── build_citation: уровни ────────────────────────────────


def test_level_a_public_licensed_without_canonical():
    """public+licensed без canonical → метаданные, БЕЗ viewer_url/sha256."""
    fm = _fm(format="docx", blobs=_blobs(canonical=None))
    citation = build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index())
    assert citation is not None
    assert "viewer_url" not in citation
    assert "sha256" not in citation
    assert citation["source_id"] == "src-aaaa1111bbbb2222"
    assert citation["title"] == "Введение в информационный поиск"
    assert "locator" not in citation


def test_level_b_public_licensed_with_canonical():
    """canonical есть ∧ blob доступен (public licensed ref) → +viewer_url/sha256."""
    fm = _fm()
    citation = build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index(_ref(SHA_CANON)))
    assert citation is not None
    assert citation["sha256"] == SHA_CANON
    assert citation["viewer_url"] == DEFAULT_VIEWER_URL_TPL.format(sha256=SHA_CANON)
    assert citation["viewer_url"] == f"/documents/{SHA_CANON}"


def test_level_b_private_read_key():
    """private + read-ключ → зона доступна → citation с viewer."""
    fm = _fm(zone="private", license=None, public_allowed=None)
    citation = build_citation(fm, None, READ, exists_fn=_exists_all, index=_index(_ref(SHA_CANON, zone="private")))
    assert citation is not None
    assert citation["viewer_url"] == f"/documents/{SHA_CANON}"
    assert citation["zone"] == "private"


def test_subscriber_private_omitted():
    """subscriber + private → citation ОТСУТСТВУЕТ целиком (не {})."""
    fm = _fm(zone="private")
    assert build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index()) is None


def test_subscriber_public_unknown_license_omitted():
    """subscriber + public + license=unknown → fail-closed: citation отсутствует.

    public_allowed=True изолирует license-гейт (M3-детектор: снятие
    is_public_license-чека в ref_available роняет этот тест).
    """
    fm = _fm(license="unknown", public_allowed=True)
    assert build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index()) is None


def test_subscriber_public_not_allowed_omitted():
    """public_allowed=False → независимо от license citation отсутствует."""
    fm = _fm(license="cc-by-4.0", public_allowed=False)
    assert build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index()) is None


def test_url_only_level_a_without_viewer():
    """URL-only Source (blobs нет) → level_a без viewer_url/sha256."""
    fm = _fm(format="url", blobs=None)
    citation = build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index())
    assert citation is not None
    assert "viewer_url" not in citation
    assert "sha256" not in citation


def test_deprecated_source_omitted():
    fm = _fm(status="deprecated")
    assert build_citation(fm, None, READ, exists_fn=_exists_all, index=_index(_ref(SHA_CANON, zone="private"))) is None


def test_insufficient_csl_omitted():
    """Нет title / нет authors / нет bibliography → citation отсутствует."""
    for bib in ({}, {"title": "Т"}, {"author": [{"family": "Иванов"}]}, None):
        fm = _fm(bibliography=bib)
        assert build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index()) is None, bib


def test_no_source_id_omitted():
    fm = _fm(knowledge_id=None)
    assert build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index()) is None


def test_locator_passthrough():
    """locator (page/url/segment) прокидывается в citation как есть."""
    locators = [
        {"kind": "page", "start": 120, "end": 145, "display": "с. 120–145"},
        {"kind": "url", "url": "https://example.com/doc#p=3"},
        {"kind": "segment", "start": 12.5, "end": 48.0},
    ]
    for locator in locators:
        fm = _fm()
        citation = build_citation(fm, locator, SUBSCRIBER, exists_fn=_exists_all, index=_index(_ref(SHA_CANON)))
        assert citation is not None
        assert citation["locator"] == locator


def test_level_b_requires_index_availability_no_leak():
    """M1-детектор: canonical в сторе, но refs только private/restricted →
    subscriber'у viewer НЕ даётся (level_a). Снятие availability-чека = утечка."""
    fm = _fm()  # public licensed сам по себе
    private_ref = _ref(
        SHA_CANON, source_id="src-other0000000000", zone="private", license_value="restricted", public_allowed=None
    )
    citation = build_citation(fm, None, SUBSCRIBER, exists_fn=_exists_all, index=_index(private_ref))
    assert citation is not None
    assert "viewer_url" not in citation
    assert "sha256" not in citation


def test_level_b_canonical_blob_missing_from_store():
    """canonical в FM, blob физически потерян → level_a (деградация, не тихая ложь)."""
    fm = _fm()
    citation = build_citation(fm, None, SUBSCRIBER, exists_fn=_Exists(SHA_ORIG), index=_index(_ref(SHA_CANON)))
    assert citation is not None
    assert "viewer_url" not in citation


def test_viewer_url_template_parameter():
    fm = _fm()
    citation = build_citation(
        fm,
        None,
        SUBSCRIBER,
        exists_fn=_exists_all,
        index=_index(_ref(SHA_CANON)),
        viewer_url_tpl="/docs/{sha256}",
    )
    assert citation is not None
    assert citation["viewer_url"] == f"/docs/{SHA_CANON}"


def test_decide_citation_levels():
    """CitationDecision.level: 'a' | 'b' | None + omitted-флаг."""
    d_b = decide_citation(_fm(), None, SUBSCRIBER, exists_fn=_exists_all, index=_index(_ref(SHA_CANON)))
    assert isinstance(d_b, CitationDecision)
    assert d_b.level == "b"
    assert d_b.citation is not None and not d_b.omitted

    d_a = decide_citation(
        _fm(format="docx", blobs=_blobs(canonical=None)), None, SUBSCRIBER, exists_fn=_exists_all, index=_index()
    )
    assert d_a.level == "a"

    d_none = decide_citation(_fm(zone="private"), None, SUBSCRIBER, exists_fn=_exists_all, index=_index())
    assert d_none.level is None
    assert d_none.citation is None
    assert d_none.omitted


def test_citation_shape_contract():
    """Форма level_b: {source_id, title, authors, locator, formatted, viewer_url, sha256}."""
    locator = {"kind": "page", "start": 1, "end": 2, "display": "с. 1–2"}
    citation = build_citation(_fm(), locator, SUBSCRIBER, exists_fn=_exists_all, index=_index(_ref(SHA_CANON)))
    assert citation is not None
    assert set(citation) == {
        "source_id",
        "zone",
        "title",
        "authors",
        "locator",
        "formatted",
        "viewer_url",
        "sha256",
    }
    assert citation["zone"] == "public"  # P1-1: read-time зона Source в citation
    assert citation["authors"] == ["Маннинг К. Д.", "Рагхаван П.", "Шютце Х."]


# ── P1-1/P2-1 (Ф4-fix1) ────────────────────────────────────


def test_zone_read_time_field_both_levels():
    """P1-1: zone (read-time, НЕ в Qdrant payload) в citation уровней a и b.

    Консольный viewer-гейт различает private-источники по этому полю.
    """
    cite_b = build_citation(_fm(), None, SUBSCRIBER, exists_fn=_exists_all, index=_index(_ref(SHA_CANON)))
    assert cite_b is not None and cite_b["zone"] == "public"
    cite_a = build_citation(
        _fm(format="docx", blobs=_blobs(canonical=None)), None, SUBSCRIBER, exists_fn=_exists_all, index=_index()
    )
    assert cite_a is not None and cite_a["zone"] == "public"
    fm_priv = _fm(zone="private", license=None, public_allowed=None)
    cite_priv = build_citation(
        fm_priv, None, READ, exists_fn=_exists_all, index=_index(_ref(SHA_CANON, zone="private"))
    )
    assert cite_priv is not None and cite_priv["zone"] == "private"


def test_module_importable_standalone():
    """P2-1: прямой `import mcp_server.content.citation` без прайминга tools.

    Детектор возврата top-level импорта из `..tools.*`: цикл
    `tools/__init__` → read/search → `citation_enrich` → `content.citation`
    (partially initialized) роняет импорт в чистом интерпретаторе и
    standalone-прогон `pytest tests/unit/test_citation.py` на collection.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import mcp_server.content.citation"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"standalone import failed:\n{proc.stderr}"
    assert "ImportError" not in proc.stderr


# ── render_gost ────────────────────────────────────────────


def test_render_gost_full():
    assert render_gost(CSL) == (
        "Маннинг К. Д., Рагхаван П., Шютце Х. Введение в информационный поиск. — 2008. — Вильямс"
    )


def test_render_gost_frontmatter_shape():
    assert render_gost({"bibliography": dict(CSL)}) == render_gost(dict(CSL))


def test_render_gost_partial_without_year_and_publisher():
    csl = {"title": "Черновик", "author": [{"family": "Иванов", "given": "И"}]}
    assert render_gost(csl) == "Иванов И. Черновик"


def test_render_gost_partial_year_and_url():
    csl = {"title": "Черновик", "author": [{"family": "Иванов"}], "issued": 2020, "URL": "https://x/y.pdf"}
    assert render_gost(csl) == "Иванов. Черновик. — 2020. — URL: https://x/y.pdf"


def test_render_gost_partial_publisher_only():
    csl = {"title": "Черновик", "author": [{"literal": "Иванов И. И. (ред.)"}], "publisher": "Наука"}
    assert render_gost(csl) == "Иванов И. И. (ред.). Черновик. — Наука"


def test_render_gost_many_authors_et_al():
    assert render_gost(CSL_MANY_AUTHORS) == "Ахо А. [и др.]. Структуры данных"


def test_render_gost_never_fabricates():
    """Пустой/недостаточный CSL → None (не фабриковать)."""
    assert render_gost(None) is None
    assert render_gost({}) is None
    assert render_gost({"title": "Т"}) is None
    assert render_gost({"author": [{"family": "Иванов"}]}) is None
    assert render_gost({"title": "Т", "author": []}) is None
    assert render_gost({"title": "Т", "author": [{}]}) is None


def test_render_gost_year_variants():
    base = {"title": "Т", "author": [{"family": "Иванов"}]}
    assert render_gost({**base, "issued": "2008-05-01"}) == "Иванов. Т. — 2008"
    assert render_gost({**base, "issued": {"literal": "2021"}}) == "Иванов. Т. — 2021"


# ── classify_reason: 9 причин §3.4:192 ─────────────────────


def test_classify_none_when_canonical_present():
    assert classify_reason(_fm(), exists_fn=_exists_all, index=_index()) is None


def test_classify_policy_pdf_only():
    fm = _fm(format="docx", ingest_policy_applied="pdf_only", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "policy_pdf_only"


def test_classify_queued():
    """SSOT-derived pending (§4.6:322): документный ∧ normalize ∧ original жив ∧ canonical нет."""
    fm = _fm(format="md", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "queued"


def test_classify_conversion_failed():
    fm = _fm(format="md", blobs=_blobs(canonical=None, error="conversion_failed"))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "conversion_failed"


def test_classify_conversion_timeout():
    fm = _fm(format="epub", blobs=_blobs(canonical=None, error="conversion_timeout"))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "conversion_timeout"


def test_classify_converter_unavailable():
    fm = _fm(format="docx", blobs=_blobs(canonical=None, error="converter_unavailable"))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "converter_unavailable"


def test_classify_quota_exceeded():
    fm = _fm(format="docx", blobs=_blobs(canonical=None, error="quota_exceeded"))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "quota_exceeded"


def test_classify_unwhitelisted_error_maps_to_conversion_failed():
    fm = _fm(format="epub", blobs=_blobs(canonical=None, error="unpacked_size_limit"))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "conversion_failed"


def test_classify_outside_pdf_axis():
    fm = _fm(format="audio", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "outside_pdf_axis"


def test_classify_url_no_blobs():
    fm = _fm(format="url", blobs=None)
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "url_no_blobs"


def test_classify_blob_missing_canonical_lost():
    """Integrity-инцидент: FM обещает canonical, которого нет в сторе."""
    fm = _fm()
    assert classify_reason(fm, exists_fn=_exists_none, index=_index()) == "blob_missing"


def test_classify_blob_missing_original_lost():
    """Original потерян (canonical тоже нет) → integrity, НЕ queued."""
    fm = _fm(format="md", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_none, index=_index()) == "blob_missing"


def test_classify_integrity_outranks_policy():
    fm = _fm(format="docx", ingest_policy_applied="pdf_only", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_none, index=_index()) == "blob_missing"


def test_classify_pdf_without_canonical_is_anomaly():
    """pdf без canonical (ingest всегда ставит as-is) → integrity-инцидент."""
    fm = _fm(format="pdf", blobs=_blobs(canonical=None))
    assert classify_reason(fm, exists_fn=_exists_all, index=_index()) == "blob_missing"
