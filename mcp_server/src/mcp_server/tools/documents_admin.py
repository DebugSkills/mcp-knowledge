# ruff: noqa: BLE001
"""Ф5b1 (bibliography): MCP-тулы documents-контура — source_get + documents_stats.

- `source_get(source_id)` — read-scope. Явный доступ к Source-записи
  (Source исключён из общей выдачи/поиска — Ф1 acceptance 8): метаданные +
  оба блоба (original/canonical: sha256/size/mime/present/available) +
  canonical_error (persisted). Гейт — переиспользует ref_available
  (зона/статус/license/public_allowed), отказ без oracle (как search/citation:
  не раскрываем существование).
- `documents_stats()` — admin-only (WRITE_TOOLS, исключён из EDITOR_TOOLS —
  паттерн errors_query). Агрегаты квоты/блобов/orphans/jobs/grace — БЕЗ
  абсолютных путей FS в выдаче.

Предикаты доступности НЕ дублируются: gate = ref_available + ref_from_entry
(availability.py / source_ref_index.py); orphan-детекция = physical_blobs +
orphan_list_from (documents_integrity.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from ..config import settings
from ..content.canonicalizer import CanonicalizationError, canonicalize, is_document_format
from ..content.source import _merge_blobs
from ..models import KnowledgeEntry
from ..storage.document_store import QuotaExceededError
from .availability import blob_available, ref_available
from .documents_integrity import (
    documents_check as _integrity_check,
    orphan_list_from,
    physical_blobs,
)
from .source_ref_index import ref_from_entry
from .source_ref_runtime import _scan_source_entries, index_add_entry

logger = logging.getLogger("mcp_knowledge.tools.documents_admin")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)

_GB = 1024 ** 3


def _source_title(entry) -> str:
    """Title Source-записи: первый markdown-заголовок тела → bibliography.title → id."""
    content = getattr(entry, "content", "") or ""
    m = _HEADING_RE.search(content)
    if m:
        return m.group(1).strip()
    fm = getattr(entry, "frontmatter", None)
    bib = getattr(fm, "bibliography", None) or {}
    title = bib.get("title") if isinstance(bib, dict) else None
    if title:
        return str(title)
    return getattr(fm, "knowledge_id", "") or ""


def _blob_view(blob_fm, fm_dict: dict, auth, document_store) -> dict | None:
    """Вид одного блоба: sha256/size/mime/present/available (sparse — не выдумываем)."""
    if not isinstance(blob_fm, dict):
        return None
    sha = blob_fm.get("sha256")
    if not isinstance(sha, str) or not sha:
        return None
    exists_fn = getattr(document_store, "exists", None) if document_store is not None else None
    present = bool(exists_fn(sha)) if callable(exists_fn) else False
    view: dict = {
        "sha256": sha,
        "present": present,
        "available": (
            bool(blob_available(sha, fm_dict, auth, exists_fn))
            if callable(exists_fn)
            else False
        ),
    }
    size = blob_fm.get("size")
    if size is None and present and document_store is not None:
        blob_size = getattr(document_store, "blob_size", None)
        if callable(blob_size):
            try:
                size = blob_size(sha)
            except Exception:  # noqa: BLE001 — физика сломалась, size=None
                size = None
    if size is not None:
        view["size"] = size
    mime = blob_fm.get("mime") or blob_fm.get("kind")
    if mime is None and present and document_store is not None:
        info = getattr(document_store, "info", None)
        if callable(info):
            try:
                bi = info(sha)
                if bi is not None:
                    mime = getattr(bi, "mime", None)
            except Exception:  # noqa: BLE001
                mime = None
    if mime is not None:
        view["mime"] = mime
    return view


async def source_get(params: dict, app_state) -> dict:
    """Получить Source-запись (метаданные + блобы) — read-scope, гейт по зоне/license."""
    source_id = params.get("source_id", "")
    if not source_id:
        return {"error": "Missing required parameter: 'source_id'"}

    store = getattr(app_state, "store", None)
    if store is None:
        return {"error": f"Source not found: '{source_id}'"}
    entry = await store.read(source_id)

    fm = getattr(entry, "frontmatter", None)
    if fm is None or getattr(fm, "content_type", None) != "source":
        # Не Source-запись (или нечитаемая) → отказ без oracle.
        return {"error": f"Source not found: '{source_id}'"}

    auth = params.get("_auth")
    # Гейт доступности: переиспользуем ref_available (НЕ дублируем предикаты).
    # Чужак-зона / license=unknown (public) / status=deprecated → отказ без oracle.
    ref = ref_from_entry(entry)
    if ref is None or not ref_available(ref, auth):
        return {"error": f"Source not found: '{source_id}'"}

    document_store = getattr(app_state, "document_store", None)
    fm_dict = {
        "source_id": source_id,
        "knowledge_id": source_id,
        "zone": getattr(fm, "zone", None) or "private",
        "status": getattr(fm, "status", None) or "published",
        "public_allowed": getattr(fm, "public_allowed", None),
        "license": getattr(fm, "license", None),
    }

    blobs_fm = getattr(fm, "blobs", None) or {}
    blobs_out: dict = {}
    for kind in ("original", "canonical"):
        view = _blob_view(blobs_fm.get(kind), fm_dict, auth, document_store)
        if view is not None:
            blobs_out[kind] = view

    resp: dict = {
        "source_id": source_id,
        "title": _source_title(entry),
        "domain": getattr(fm, "domain", None),
        "subject": getattr(fm, "subject", None),
        "format": getattr(fm, "format", None),
        "license": getattr(fm, "license", None),
        "zone": getattr(fm, "zone", None),
        "status": getattr(fm, "status", None),
        "blobs": blobs_out,
    }
    canonical_error = blobs_fm.get("canonical_error")
    if isinstance(canonical_error, dict):
        resp["canonical_error"] = dict(canonical_error)
    return resp


def _stats_core(document_store, index, max_bytes: int, grace_days: int) -> dict:
    """Синхронное ядро documents_stats (SQLite/FS — из executor)."""
    used = int(document_store.total_bytes())
    referenced = set(index.referenced_shas) if index is not None else set()
    physical = physical_blobs(document_store)
    orphans = len(orphan_list_from(physical, referenced)) if index is not None else None
    return {
        "quota": {
            "used_bytes": used,
            "max_bytes": max_bytes,
            "used_pct": round(used / max_bytes * 100.0, 2) if max_bytes else 0.0,
        },
        "blobs": {
            "total": len(physical),
            "orphans": orphans,
        },
        "jobs": dict(document_store.job_counts()),
        "grace_days": grace_days,
    }


async def documents_stats(params: dict, app_state) -> dict:
    """Агрегаты documents-контура (admin-only; без абсолютных путей FS).

    Доступ ограничен на auth-слое (WRITE_TOOLS, исключён из EDITOR_TOOLS —
    паттерн errors_query); здесь только сбор агрегатов. Блокирующий сбор
    (SQLite/FS-скан) — через run_in_executor (WORKERS=1 инвариант).
    """
    document_store = getattr(app_state, "document_store", None)
    if document_store is None:
        return {"error": "document_store is not initialized"}
    index = getattr(app_state, "source_ref_index", None)
    max_bytes = int(
        getattr(document_store, "max_bytes", None)
        or (settings.DOCUMENTS_STORE_MAX_GB * _GB)
    )
    grace_days = int(settings.DOCUMENTS_GC_GRACE_DAYS)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _stats_core, document_store, index, max_bytes, grace_days
    )

# ── Ф5b2: documents_check + documents_rebuild (admin-only) ────

# Краткий samples-лимит: отчёт не дампит все дефекты (открытие админ-страницы
# остаётся дешёвым), полные списки — в counts по категориям.
_INTEGRITY_SAMPLE_LIMIT = 20

# Категории дефектов ядра documents_integrity.documents_check (по факту выдачи).
_SAMPLE_CATEGORIES = (
    "missing_blob", "sha_mismatch", "canonical_missing",
    "provenance_incomplete", "dangling_source_refs", "orphans", "errors",
)

# Стабильные поля строки реестра (БЕЗ created_at) — база diff updated при
# rebuild: timestamp обновляется при каждой пересборке, но это не «изменение».
_REBUILD_SIG_FIELDS = (
    "role", "size", "mime", "original_filename",
    "derived_from", "tool", "tool_version",
)


def _blob_signature(info) -> tuple:
    """Стабильный снимок метаданных строки реестра (без timestamp)."""
    return tuple(getattr(info, field) for field in _REBUILD_SIG_FIELDS)


async def documents_check(params: dict, app_state) -> dict:
    """Integrity-проверка documents-контура (admin-only; read-only по умолчанию).

    Обёртка над ядром documents_integrity.documents_check (SSOT Source-refs ↔
    blob-стор). Дефолт create_issues=False: открытие админ-страницы НЕ плодит
    quality-issues; фиксация дефектов — только по явному create_issues=True.
    Возврат: агрегат (counts + счётчики issues по категориям) + краткий samples
    (limit, без неограниченного дампа дефектов).
    """
    store = getattr(app_state, "store", None)
    document_store = getattr(app_state, "document_store", None)
    if store is None or document_store is None:
        return {"error": "documents contour is not initialized"}

    create_issues = bool(params.get("create_issues", False))
    report = await _integrity_check(
        store, document_store, create_issues=create_issues
    )

    issues = {key: len(report.get(key) or []) for key in _SAMPLE_CATEGORIES}
    samples = {
        key: list(report.get(key) or [])[:_INTEGRITY_SAMPLE_LIMIT]
        for key in _SAMPLE_CATEGORIES
    }
    return {
        "ok": bool(report.get("ok", False)),
        "counts": dict(report.get("counts") or {}),
        "issues": issues,
        "samples": samples,
    }


async def documents_rebuild(params: dict, app_state) -> dict:
    """Перестроить реестр blob-стора из FS + Source SSOT (admin-only).

    Источники SSOT — скан Source-entries тем же путём, что startup-индекс
    (source_ref_runtime._scan_source_entries). Блокирующий rebuild (SQLite/FS) —
    run_in_executor. Возврат: added/updated/removed как дифф реестра до/после
    (по стабильным полям, created_at игнорируется) + сырые счётчики rebuild.
    Идемпотентность: повторный вызов при неизменном сторе → нулевые дельты.
    """
    store = getattr(app_state, "store", None)
    document_store = getattr(app_state, "document_store", None)
    if store is None or document_store is None:
        return {"error": "documents contour is not initialized"}

    entries = await _scan_source_entries(store)
    sources = [
        {"blobs": getattr(entry.frontmatter, "blobs", None)}
        for entry in entries
    ]

    loop = asyncio.get_running_loop()

    def _rebuild_and_diff() -> dict:
        before = {b.sha256: b for b in document_store.list_blobs()}
        result = document_store.rebuild(sources)
        after = {b.sha256: b for b in document_store.list_blobs()}
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        updated = [
            sha for sha in sorted(set(before) & set(after))
            if _blob_signature(before[sha]) != _blob_signature(after[sha])
        ]
        return {
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            **result,
        }

    return await loop.run_in_executor(None, _rebuild_and_diff)



# ── Ф5b3: documents_gc (mark-and-sweep) + documents_retry (реканонизация) ────

# Cap кандидатов в dry-run-отчёте (админ-страница дешёвая); ПОЛНЫЙ список
# остаётся во внутреннем sweep — delete идёт по ВСЕМ кандидатам.
_GC_CANDIDATE_LIMIT = 200

# Retry: whitelist-причины отказа канонизации (по факту CanonicalizationError).
_RETRY_REASON_WHITELIST = frozenset(
    {"conversion_failed", "converter_unavailable", "conversion_timeout"}
)


def _normalize_retry_reason(reason: str | None) -> str:
    """Retry: whitelist-маппинг причины отказа (непредставимые → conversion_failed)."""
    if reason in _RETRY_REASON_WHITELIST or reason == "quota_exceeded":
        return reason
    return "conversion_failed"


def _blob_age_days(document_store, sha: str, now_ts: float) -> float | None:
    """Возраст блоба в днях по FS mtime (None — файла нет).

    Фактический источник времени блоба — mtime (правдивее реестрового
    created_at: не перезаписывается rebuild'ом guard-строк).
    """
    mtime = document_store.blob_mtime(sha)
    if mtime is None:
        return None
    return (now_ts - mtime) / 86400.0


def _gc_sweep(document_store, index, grace_days: int, now_ts: float) -> dict:
    """Синхронное ядро mark-and-sweep (FS/SQLite — из executor).

    Mark: referenced = index.referenced_shas. Sweep-кандидаты = physical −
    referenced, старше grace (по mtime). Возвращает ПОЛНЫЙ список кандидатов
    (без cap) + счётчики kept_fresh/kept_referenced.
    """
    referenced = set(index.referenced_shas) if index is not None else set()
    physical = physical_blobs(document_store)
    # R2 (fail-closed): индекс недоступен (None) или пуст при наличии физических
    # блобов → деградация; GC не должен сметать живые документы без явного флага.
    index_unavailable = index is None or (not referenced and bool(physical))
    candidates: list[dict] = []
    kept_fresh = 0
    kept_referenced = 0
    reclaimable_bytes = 0
    for sha in sorted(physical):
        size = physical[sha]
        if sha in referenced:
            kept_referenced += 1
            continue
        age = _blob_age_days(document_store, sha, now_ts)
        if age is not None and age < grace_days:
            kept_fresh += 1
            continue
        candidates.append({
            "sha": sha,
            "size": size,
            "age_days": round(age, 2) if age is not None else None,
        })
        reclaimable_bytes += size
    return {
        "candidates": candidates,
        "reclaimable_bytes": reclaimable_bytes,
        "kept_fresh": kept_fresh,
        "kept_referenced": kept_referenced,
        "index_unavailable": index_unavailable,
    }


async def documents_gc(params: dict, app_state) -> dict:
    """Mark-and-sweep GC orphan-блобов (admin-only; dry-run по умолчанию).

    Инварианты: referenced НИКОГДА не удаляется (sweep = physical − referenced);
    свежий orphan сохраняется (grace = DOCUMENTS_GC_GRACE_DAYS по mtime);
    повторный sweep → 0 кандидатов (идемпотентность). Блокирующее ядро
    (FS/SQLite) — run_in_executor (WORKERS=1 инвариант).

    Fail-closed (R2): индекс None/пуст + есть физические блобы → отказ
    {"error": "index_unavailable"} (удаление в деградации только по явному
    allow_empty_index=True).
    """
    document_store = getattr(app_state, "document_store", None)
    if document_store is None:
        return {"error": "document_store is not initialized"}
    index = getattr(app_state, "source_ref_index", None)
    dry_run = bool(params.get("dry_run", True))
    allow_empty_index = bool(params.get("allow_empty_index", False))
    grace_days = int(settings.DOCUMENTS_GC_GRACE_DAYS)
    loop = asyncio.get_running_loop()

    sweep = await loop.run_in_executor(
        None, _gc_sweep, document_store, index, grace_days, time.time()
    )

    # R2 (fail-closed): деградация индекса (None/пуст + физические блобы) — НЕ
    # удалять без явного allow_empty_index (UI его не шлёт). Иначе GC сметёт
    # оригиналы живых документов (PDF original==canonical — безвозвратно).
    if sweep["index_unavailable"] and not allow_empty_index:
        if dry_run:
            return {
                "dry_run": True,
                "error": "index_unavailable",
                "candidates": [],
                "reclaimable_bytes": 0,
                "kept_fresh": sweep["kept_fresh"],
                "kept_referenced": sweep["kept_referenced"],
                "candidates_total": 0,
            }
        return {
            "dry_run": False,
            "error": "index_unavailable",
            "deleted": 0,
            "freed_bytes": 0,
            "errors": [],
            "kept_fresh": sweep["kept_fresh"],
            "kept_referenced": sweep["kept_referenced"],
            "candidates_total": 0,
        }

    if dry_run:
        return {
            "dry_run": True,
            "candidates": sweep["candidates"][:_GC_CANDIDATE_LIMIT],
            "reclaimable_bytes": sweep["reclaimable_bytes"],
            "kept_fresh": sweep["kept_fresh"],
            "kept_referenced": sweep["kept_referenced"],
            "candidates_total": len(sweep["candidates"]),
        }

    def _delete() -> tuple[int, int, list[dict]]:
        deleted = 0
        freed_bytes = 0
        errors: list[dict] = []
        for c in sweep["candidates"]:
            try:
                if document_store.delete_blob_direct(c["sha"]):
                    deleted += 1
                    freed_bytes += c["size"]
            except Exception as exc:  # noqa: BLE001 — частичные сбои не валят операцию
                errors.append({"sha": c["sha"], "error": str(exc)})
        return deleted, freed_bytes, errors

    deleted, freed_bytes, errors = await loop.run_in_executor(None, _delete)
    return {
        "dry_run": False,
        "deleted": deleted,
        "freed_bytes": freed_bytes,
        "errors": errors,
        "kept_fresh": sweep["kept_fresh"],
        "kept_referenced": sweep["kept_referenced"],
        "candidates_total": len(sweep["candidates"]),
    }


async def _put_canonical(document_store, data: bytes, **kw) -> dict:
    """put canonical-блоба в executor (паттерн ingest._put)."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: document_store.put(data, **kw))
    return result.to_dict()


async def documents_retry(params: dict, app_state) -> dict:
    """Реканонизация Source (admin-only): восстановить original → canonicalize.

    Тот же путь, что ingest (canonicalize → put → _merge_blobs). Конвертер
    инжектируем (app_state.canonicalizer, дефолт — content.canonicalizer.
    canonicalize). Идемпотентно: canonical уже есть → already_present.
    Ответ: {status: ok|already_present|failed, reason?, canonical_sha256?} —
    без путей FS.
    """
    source_id = params.get("source_id", "")
    if not source_id:
        return {"error": "Missing required parameter: 'source_id'"}

    store = getattr(app_state, "store", None)
    document_store = getattr(app_state, "document_store", None)
    if store is None or document_store is None:
        return {"error": "documents contour is not initialized"}

    entry = await store.read(source_id)
    fm = getattr(entry, "frontmatter", None)
    if fm is None or getattr(fm, "content_type", None) != "source":
        return {"error": f"Source not found: '{source_id}'"}

    loop = asyncio.get_running_loop()
    blobs = getattr(fm, "blobs", None) or {}
    original = blobs.get("original") or {}
    original_sha = original.get("sha256") if isinstance(original, dict) else None
    fmt = getattr(fm, "format", None)

    # (а) canonical уже есть (и blob жив) → idempotent no-op.
    canonical_blob = blobs.get("canonical")
    canonical_lost = False
    if isinstance(canonical_blob, dict) and canonical_blob.get("sha256"):
        exists = await loop.run_in_executor(
            None, document_store.exists, canonical_blob["sha256"]
        )
        if exists:
            return {
                "status": "already_present",
                "canonical_sha256": canonical_blob["sha256"],
            }
        # R3 (CR2): canonical в SSOT есть, но blob физически потерян — не
        # возвращать misleading ok; ниже переканонизируем и ЗАМЕНИМ sha.
        canonical_lost = True

    # Восстановить original по sha.
    if not original_sha:
        return {"status": "failed", "reason": "conversion_failed"}
    data = await loop.run_in_executor(None, document_store.get, original_sha)
    if data is None:
        return {"status": "failed", "reason": "conversion_failed"}

    # Канонизация — тот же путь, что ingest (шаг 2).
    converter = getattr(app_state, "canonicalizer", None) or canonicalize
    new_canonical: dict | None = None
    canonical_error: dict | None = None
    reason: str | None = None

    if fmt == "pdf":
        new_canonical = {
            "sha256": original_sha,
            "size": original.get("size"),
            "derived_from": None,
            "tool": "as-is",
            "tool_version": None,
        }
    elif not is_document_format(fmt):
        return {"status": "failed", "reason": "outside_pdf_axis"}
    elif settings.INGEST_POLICY != "normalize":
        return {"status": "failed", "reason": "policy_pdf_only"}
    else:
        try:
            conv = await converter(data, fmt)
            pdf_bytes = conv["pdf_bytes"]
            stem = original.get("original_filename") or original_sha[:12]
            canonical = await _put_canonical(
                document_store, pdf_bytes,
                mime="application/pdf",
                filename=f"{stem}.pdf",
                role="canonical",
                derived_from=original_sha,
                tool=conv.get("tool"),
                tool_version=conv.get("tool_version"),
                params_hash=conv.get("params_hash"),
            )
            new_canonical = {
                "sha256": canonical["sha256"],
                "size": canonical["size"],
                "derived_from": original_sha,
                "tool": conv.get("tool"),
                "tool_version": conv.get("tool_version"),
            }
            if conv.get("params_hash"):
                new_canonical["params_hash"] = conv["params_hash"]
        except QuotaExceededError:
            reason = "quota_exceeded"
            canonical_error = {
                "reason": "quota_exceeded",
                "message": "quota exceeded before canonical put",
                "at": datetime.now(timezone.utc).isoformat(),
            }
        except CanonicalizationError as exc:
            reason = _normalize_retry_reason(exc.reason)
            canonical_error = {
                "reason": reason,
                "message": (exc.message or "")[:200],
                "at": datetime.now(timezone.utc).isoformat(),
            }
            logger.warning(
                "documents_retry: canonicalization failed (%s) for %s",
                reason, source_id,
            )

    if new_canonical is not None:
        # SSOT-first: прикрепить canonical, снять canonical_error (stale).
        merge_base = blobs
        if canonical_lost:
            # R3 (CR2): мёртвый canonical (blob потерян) не frozen — убираем
            # stale-ссылку, чтобы _merge_blobs прикрепил новый валидный sha.
            merge_base = {k: v for k, v in blobs.items() if k != "canonical"}
        merged, attached = _merge_blobs(merge_base, {"canonical": new_canonical, "derived": []})
        if attached:
            merged_fm = fm.model_copy(
                update={"blobs": merged, "updated_at": datetime.now(timezone.utc)}
            )
            merged_entry = KnowledgeEntry(frontmatter=merged_fm, content=entry.content)
            await store.write_entry(merged_entry)
            index_add_entry(app_state, merged_entry)
        return {"status": "ok", "canonical_sha256": new_canonical["sha256"]}

    # Провал: persist/refresh canonical_error (SSOT-first, _merge_blobs).
    if canonical_error is not None:
        merged, attached = _merge_blobs(blobs, {"canonical_error": canonical_error})
        if attached:
            merged_fm = fm.model_copy(
                update={"blobs": merged, "updated_at": datetime.now(timezone.utc)}
            )
            merged_entry = KnowledgeEntry(frontmatter=merged_fm, content=entry.content)
            await store.write_entry(merged_entry)
    return {"status": "failed", "reason": reason}
