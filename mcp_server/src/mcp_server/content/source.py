"""Source SSOT helpers (bibliography Ф1, план §3.1).

Source = отдельный SSOT-тип (content_type="source") в knowledge-репо:
- id `src-<sha256_16>` от ПОЛНОГО sha256 оригинала (И2 стабильный id).
- frontmatter: format/locator_kind/ingest_policy_applied/bibliography/license/
  public_allowed/blobs{original, canonical, derived[]}.
- Guard коллизий: id занят ∧ полный sha256 совпадает → идемпотентный reuse;
  не совпадает → отказ (без авто-суффикса).
- Source НЕ индексируется (INDEX_EXCLUDED_CONTENT_TYPES, см. models.is_indexable).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import KnowledgeEntry, KnowledgeFrontmatter

logger = logging.getLogger("mcp_knowledge.content.source")

SRC_PREFIX = "src-"
SRC_DOMAIN = "library"
SRC_SUBJECT = "bibliography"

# Лицензии, разрешающие публикацию (И1/§3.4: own | cc-* | licensed).
_PUBLIC_LICENSES = {"own", "licensed"}


class SourceCollisionError(Exception):
    """Коллизия id src-<sha256_16>: id занят, но полный sha256 НЕ совпадает (И2)."""

    def __init__(self, source_id: str, expected: str, actual: str | None):
        self.source_id = source_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Source id collision: {source_id} exists with original sha256 "
            f"{actual}, but {expected} was requested"
        )


def is_public_license(license_value: str | None) -> bool:
    """Машиночитаемая политика публикации из license (own | cc-* | licensed)."""
    if not license_value:
        return False
    return license_value == "own" or license_value == "licensed" or license_value.startswith("cc-")


def make_source_id(sha256_hex: str) -> str:
    """src-<sha256_16> — стабильный id от неизменяемого контента (И2)."""
    if not isinstance(sha256_hex, str) or len(sha256_hex) != 64:
        raise ValueError(f"make_source_id: invalid sha256 length {len(sha256_hex) if isinstance(sha256_hex, str) else 'n/a'}")
    if not all(c in "0123456789abcdef" for c in sha256_hex):
        raise ValueError("make_source_id: sha256 contains non-hex characters")
    return f"{SRC_PREFIX}{sha256_hex[:16]}"


def build_source_content(title: str | None, format: str | None) -> str:
    """Тело Source-записи (минимальный markdown; метаданные — во frontmatter)."""
    heading = title or f"Source ({format or 'unknown'})"
    return f"# {heading}\n\nИсточник документа. Метаданные — во frontmatter.\n"


def _merge_blobs(existing: dict | None, incoming: dict | None) -> tuple[dict, bool]:
    """Идемпотентный merge производных (canonical/derived) в существующие blobs.

    Правила (attach ≠ recreate, C2 frozen):
    - canonical: прикрепить, ТОЛЬКО если у existing его нет (иначе frozen — не трогаем).
    - derived: дозаписать отсутствующие (дедуп по sha256).
    - canonical_error (Ф4b3): производное диагностическое поле, НЕ frozen:
      canonical есть/появился → снять (не оставлять stale); canonical нет →
      обновить свежей причиной (последний инцидент важнее).
    Возвращает (merged, attached) — attached=True если хоть что-то изменилось.
    """
    existing = existing or {}
    incoming = incoming or {}
    merged: dict = dict(existing)
    attached = False

    if not merged.get("canonical"):
        canonical = incoming.get("canonical")
        if isinstance(canonical, dict) and canonical.get("sha256"):
            merged["canonical"] = canonical
            attached = True

    merged_derived = list(merged.get("derived") or [])
    seen_shas = {d.get("sha256") for d in merged_derived if isinstance(d, dict)}
    for d in (incoming.get("derived") or []):
        if isinstance(d, dict) and d.get("sha256") and d.get("sha256") not in seen_shas:
            merged_derived.append(d)
            seen_shas.add(d.get("sha256"))
            attached = True
    merged["derived"] = merged_derived

    # Ф4b3: canonical_error — снимается при появлении canonical, обновляется при отказе.
    if merged.get("canonical"):
        if merged.pop("canonical_error", None) is not None:
            attached = True  # stale-причина снята — это тоже delta
    else:
        incoming_error = incoming.get("canonical_error")
        if isinstance(incoming_error, dict) and incoming_error.get("reason"):
            if merged.get("canonical_error") != incoming_error:
                merged["canonical_error"] = dict(incoming_error)
                attached = True

    return merged, attached


async def register_source(
    store,
    *,
    original_sha256: str,
    format: str,
    blobs: dict,
    domain: str = SRC_DOMAIN,
    subject: str = SRC_SUBJECT,
    project: str | None = None,
    locator_kind: str | None = None,
    bibliography: dict | None = None,
    license: str | None = None,
    public_allowed: bool | None = None,
    zone: str = "private",
    ingest_policy_applied: str | None = None,
    title: str | None = None,
) -> dict:
    """Создать (или переиспользовать) Source-запись.

    Guard коллизий (И2): существующая запись с тем же id и тем же original.sha256 →
    идемпотентный reuse; с другим sha256 → SourceCollisionError.

    Returns:
        {knowledge_id, created, reused, entry}
    """
    source_id = make_source_id(original_sha256)

    existing = await store.read(source_id)
    if existing is not None:
        existing_blobs = getattr(existing.frontmatter, "blobs", None) or {}
        existing_original = existing_blobs.get("original") or {}
        existing_sha = existing_original.get("sha256") if isinstance(existing_original, dict) else None
        if existing_sha == original_sha256:
            # Идемпотентный reuse ≠ no-op: прикрепить вновь доступные производные/provenance
            # (retry после сбоя конвертера / recovery-sweep / fallback-догон). Attach ≠
            # recreate — существующий canonical не перезаписывается (C2 frozen цел).
            merged, attached = _merge_blobs(existing_blobs, blobs)
            if attached:
                merged_fm = existing.frontmatter.model_copy(
                    update={"blobs": merged, "updated_at": datetime.now(timezone.utc)}
                )
                merged_entry = KnowledgeEntry(frontmatter=merged_fm, content=existing.content)
                await store.write_entry(merged_entry)
                logger.info("Source %s reuse + attach (canonical/provenance merged)", source_id)
                return {"knowledge_id": source_id, "created": False, "reused": True,
                        "attached": True, "entry": merged_entry}
            logger.info("Source %s already exists (idempotent reuse, no delta)", source_id)
            return {"knowledge_id": source_id, "created": False, "reused": True,
                    "attached": False, "entry": existing}
        raise SourceCollisionError(source_id, original_sha256, existing_sha)

    if public_allowed is None:
        public_allowed = is_public_license(license)

    now = datetime.now(timezone.utc)
    fm = KnowledgeFrontmatter(
        knowledge_id=source_id,
        domain=domain,
        subject=subject,
        project=project,
        content_type="source",
        format=format,
        locator_kind=locator_kind,
        ingest_policy_applied=ingest_policy_applied,
        bibliography=bibliography,
        license=license,
        public_allowed=public_allowed,
        blobs=blobs,
        zone=zone,
        created_at=now,
        updated_at=now,
    )
    entry = KnowledgeEntry(frontmatter=fm, content=build_source_content(title, format))
    await store.write_entry(entry)
    logger.info("Source record created: %s (format=%s)", source_id, format)
    return {"knowledge_id": source_id, "created": True, "reused": False, "entry": entry}
