"""Ingest-пайплайн Source (bibliography Ф1, план §4.1 шаги 0–2).

ingest_source: put(original) → canonical (pdf ≡ original / иначе canonicalize) → Source-запись.
Fail-closed: конвертер недоступен/отказ → original сохранён, canonical=null (reason-код).
Шаги 3–4 (локаторная экстракция, секции/чанки) — контур Ф2.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..storage.document_store import DocumentStore, QuotaExceededError
from .canonicalizer import CanonicalizationError, canonicalize, is_document_format
from .source import register_source

logger = logging.getLogger("mcp_knowledge.content.ingest")

_MIME_BY_FORMAT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "txt": "text/plain",
    "html": "text/html",
    "epub": "application/epub+zip",
    "audio": "audio/mpeg",
    "video": "video/mp4",
    "image": "image/png",
    "sheet": "text/csv",
    "url": None,
}


def _mime_for(format_value: str | None, filename: str | None) -> str | None:
    if format_value and format_value in _MIME_BY_FORMAT:
        return _MIME_BY_FORMAT[format_value]
    if filename:
        import mimetypes
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return "application/octet-stream"


def _get_store(app_state) -> DocumentStore:
    """DocumentStore (blob-store). Source-запись пишется в MarkdownStore (app_state.store).

    Инвариант: возвращаем ТОЛЬКО настоящий DocumentStore. getattr-default `None`
    не защищает от mock/чужого объекта (MagicMock auto-attr отдаёт MagicMock, а не
    None) — поэтому явная isinstance-проверка: не-инстанс → создать/закэшировать
    настоящий store в app_state.document_store.
    """
    store = getattr(app_state, "document_store", None)
    if not isinstance(store, DocumentStore):
        store = DocumentStore(settings.DOCUMENTS_DIR, settings.DOCUMENTS_STORE_MAX_GB)
        app_state.document_store = store
    return store


async def _put(store: DocumentStore, data: bytes, **kw) -> dict:
    """put в executor (single-worker, не блокирует event loop)."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: store.put(data, **kw))
    return result.to_dict()


def _canonical_error_payload(reason: str, message: str) -> dict:
    """Ф4b3: persisted-причина отказа канонизации → blobs.canonical_error.

    Reader — citation.classify_reason (whitelist §3.4:192; субкоды вне whitelist
    мапятся в conversion_failed). at — ISO-8601 UTC; message урезан до 200 симв.
    """
    return {
        "reason": reason,
        "message": (message or "")[:200],
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def ingest_source(
    app_state,
    *,
    format: str,
    domain: str,
    subject: str,
    source_path: str | None = None,
    data: bytes | None = None,
    mime: str | None = None,
    filename: str | None = None,
    zone: str = "private",
    project: str | None = None,
    bibliography: dict | None = None,
    license: str | None = None,
    public_allowed: bool | None = None,
    locator_kind: str | None = None,
    title: str | None = None,
    ingest_policy_applied: str | None = None,
) -> dict:
    """Ingest Source: original первым, canonical вторым (fail-closed), Source-запись.

    Returns:
        {source_id, created, reused, original, canonical, canonical_present, reason, blobs}
    """
    store = _get_store(app_state)

    if data is None:
        if not source_path:
            raise ValueError("ingest_source: data or source_path required")
        data = Path(source_path).read_bytes()
    if filename is None:
        filename = (Path(source_path).name if source_path else None) or title or format
    mime = mime or _mime_for(format, filename)

    # 1. put(original) — ВЕРБАТИМ первым: отказ конвертера не теряет оригинал.
    original = await _put(store, data, mime=mime, filename=filename, role="original")

    # 2. canonical
    canonical_blob: dict | None = None
    reason: str | None = None
    canonical_error: dict | None = None  # Ф4b3: persisted-причина (если canonical нет)
    if format == "pdf":
        # canonical == original (as-is) — content-addressing дедупит, второго put нет.
        canonical_blob = {
            "sha256": original["sha256"],
            "size": original["size"],
            "derived_from": None,
            "tool": "as-is",
            "tool_version": None,
        }
    elif not is_document_format(format):
        reason = "outside_pdf_axis"  # media/код/таблицы/url — вне PDF-оси
    elif settings.INGEST_POLICY != "normalize":
        reason = "policy_pdf_only"    # аварийный fallback: non-PDF без canonical
    else:
        # Документный non-PDF → sidecar-канонизатор (fail-closed).
        try:
            conv = await canonicalize(data, format)
            pdf_bytes = conv["pdf_bytes"]
            stem = Path(filename).stem if filename else original["sha256"][:12]
            canonical = await _put(
                store, pdf_bytes,
                mime="application/pdf",
                filename=f"{stem}.pdf",
                role="canonical",
                derived_from=original["sha256"],
                tool=conv.get("tool"),
                tool_version=conv.get("tool_version"),
                params_hash=conv.get("params_hash"),
            )
            canonical_blob = {
                "sha256": canonical["sha256"],
                "size": canonical["size"],
                "derived_from": original["sha256"],
                "tool": conv.get("tool"),
                "tool_version": conv.get("tool_version"),
                "params_hash": conv.get("params_hash"),
            }
        except QuotaExceededError:
            reason = "quota_exceeded"   # canonical отброшен ДО put, original жив
            canonical_error = _canonical_error_payload("quota_exceeded", "quota exceeded before canonical put")
        except CanonicalizationError as exc:
            reason = exc.reason
            canonical_error = _canonical_error_payload(exc.reason, exc.message)
            logger.warning("ingest_source: canonicalization failed (%s) for %s", exc.reason, original["sha256"][:12])

    # 3. blobs (SSOT frontmatter)
    blobs: dict = {
        "original": {
            "sha256": original["sha256"],
            "mime": mime,
            "size": original["size"],
            "original_filename": filename,
        },
        "derived": [],
    }
    if canonical_blob is not None:
        blobs["canonical"] = {
            "sha256": canonical_blob["sha256"],
            "role": "canonical",
            "derived_from": canonical_blob.get("derived_from"),
            "tool": canonical_blob.get("tool"),
            "tool_version": canonical_blob.get("tool_version"),
        }
        if canonical_blob.get("params_hash"):
            blobs["canonical"]["params_hash"] = canonical_blob["params_hash"]
    elif canonical_error is not None:
        # Ф4b3: canonical нет — причина известна (persisted-диагностика).
        # outside_pdf_axis/policy_pdf_only сюда НЕ попадают: они выводимы из
        # format/ingest_policy_applied (classify_reason шаги 4-5, раньше canonical_error).
        blobs["canonical_error"] = canonical_error

    # 4. Source-запись (guard коллизий И2 внутри register_source) — в MarkdownStore.
    markdown_store = getattr(app_state, "store", None)
    if markdown_store is None:
        raise ValueError("ingest_source: app_state.store (MarkdownStore) is required")
    source = await register_source(
        markdown_store,
        original_sha256=original["sha256"],
        format=format,
        blobs=blobs,
        domain=domain,
        subject=subject,
        project=project,
        locator_kind=locator_kind,
        bibliography=bibliography,
        license=license,
        public_allowed=public_allowed,
        zone=zone,
        ingest_policy_applied=ingest_policy_applied or settings.INGEST_POLICY,
        title=title,
    )

    # Ф3b2: ведение availability-индекса — точечный add (O(1) upsert; запись
    # уже в SSOT, reuse+attach покрыт: add чистит shas, которых больше нет).
    # Lazy-import: tools/__init__ импортирует content.ingest (tools/content.py)
    # — модульный импорт дал бы цикл.
    from ..tools.source_ref_runtime import index_add_entry

    index_add_entry(app_state, source["entry"])

    # 5. Persisted-истина: canonical_present/blobs берём из ЗАПИСАННОГО Source
    # (после reuse-attach это согласовано с SSOT — P0-1 критики), а не из локальной переменной.
    persisted_entry = source["entry"]
    persisted_blobs = getattr(persisted_entry.frontmatter, "blobs", None) or {}
    persisted_canonical = persisted_blobs.get("canonical")
    canonical_present = False
    if isinstance(persisted_canonical, dict) and persisted_canonical.get("sha256"):
        loop = asyncio.get_running_loop()
        canonical_present = bool(
            await loop.run_in_executor(None, store.exists, persisted_canonical["sha256"])
        )

    return {
        "source_id": source["knowledge_id"],
        "created": source["created"],
        "reused": source["reused"],
        "attached": source.get("attached", False),
        "original": original,
        "canonical": canonical_blob,
        "canonical_present": canonical_present,
        "reason": reason,
        "blobs": persisted_blobs,
    }
