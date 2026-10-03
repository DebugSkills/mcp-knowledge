"""Bibliography Ф3c1 (план §3.6:226): documents integrity — documents_check.

Контракт (канон фазы 3, acceptance: «integrity ловит source-без-blob /
canonical-потерю»):
- каждый ref каждого Source (original/canonical/derived) → blob физически
  существует ∧ sha256 ПЕРЕСЧИТАН потоково и совпал (authoritative-проверка:
  имени файла-оракула НЕ доверяем — CR2);
- canonical-chain: canonical жив (blob на месте ∧ sha сходится) ∧ provenance
  полон (derived: tool/tool_version/params_hash; PDF as-is: derived_from=null
  + tool="as-is");
- orphan-sweep: каждый физический blob → ∃ ≥1 ref из Source-записей, иначе
  documents_orphans (кандидат GC — здесь ТОЛЬКО фиксируется, не удаляется);
- entry→Source (Ф3c2c): каждый source_ref Knowledge-записи (не-Source)
  указывает на СУЩЕСТВУЮЩИЙ Source, иначе dangling_source_refs
  (broken_link); malformed ref (без source_id) — skip (прецедент: ref без sha);
- ретеншн canonicalization_jobs (Ф3c2c): done/failed старше
  DOCUMENTS_JOBS_RETENTION_DAYS от завершения удаляются (fail-safe).

Дефекты проводятся в СУЩЕСТВУЮЩИЙ механизм quality-issues:
- broken_link — source-без-blob / sha-mismatch / canonical-потеря /
  неполный provenance / ошибка чтения / dangling source_ref;
- orphaned — blob без ref.
Идемпотентность: detail детерминирован (без timestamp/counts) →
issue_id = SHA256(type, knowledge_id, detail) стабилен между прогонами
(create_issues_batch пропускает существующие).

Fail-safe: ошибка чтения ОДНОГО блоба → запись в errors + issue, проверка
продолжается; ok=false при любом найденном дефекте (или ошибке чтения).

Async-инвариант: блокирующее ядро (check_documents — потоковое хеширование,
FS-скан) вызывается через run_in_executor; обёртка documents_check делает
async-скан путей, парсит и проверяет одним executor-джобом (паттерн
source_ref_runtime._scan_source_entries).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger("mcp_knowledge.tools.documents_integrity")

# Потоковое хеширование чанками (1 МБ) — крупные PDF не грузятся в память.
_HASH_CHUNK = 1024 * 1024

# Cap orphan-issues: массовый delete_entry источников может оставить сотни
# блобов — отчёт перечисляет ВСЕ, issues только первые (детерминированно
# по sha), чтобы не заливать issues.jsonl (паттерн MAX_ORPHAN_ISSUES_PER_COLLECTION).
MAX_ORPHAN_ISSUES = 50

_HEX = set("0123456789abcdef")


def _hash_file(path: Path) -> str:
    """Потоковый sha256 файла (чанки 1 МБ) — authoritative re-hash."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_refs(blobs: dict | None):
    """Yield (ref_kind, ref_dict) из blobs {original, canonical, derived[]}.

    Порядок: original → canonical → derived (детерминирован).
    """
    if not isinstance(blobs, dict):
        return
    original = blobs.get("original")
    if isinstance(original, dict):
        yield "original", original
    canonical = blobs.get("canonical")
    if isinstance(canonical, dict):
        yield "canonical", canonical
    for derived in blobs.get("derived") or []:
        if isinstance(derived, dict):
            yield "derived", derived


def _provenance_missing(ref_kind: str, ref: dict) -> list[str]:
    """Отсутствующие поля provenance для ref (сортированные; [] = полный).

    Правила (§3.6:226):
    - original: provenance не требуется (корень цепочки);
    - as-is (derived_from=null): tool обязан быть "as-is" (PDF принят без
      преобразования) — tool_version/params не требуются;
    - derived (derived_from установлен): tool + tool_version + params_hash.
    """
    if ref_kind == "original":
        return []
    derived_from = ref.get("derived_from")
    if not derived_from:
        return [] if ref.get("tool") == "as-is" else ["tool"]
    return [
        field for field in ("tool", "tool_version", "params_hash") if not ref.get(field)
    ]


def physical_blobs(document_store) -> dict[str, int]:
    """Скан физических blob-файлов: {sha256: size} (валидные 64-hex имена, без re-hash).

    Переиспользуется check_documents (integrity, counts.blobs) и documents_stats
    (admin-статистика) — один источник «что физически лежит в сторе».
    """
    physical: dict[str, int] = {}
    for path in document_store._iter_blob_files():
        name = path.name
        if len(name) != 64 or not set(name) <= _HEX:
            continue  # не-blob файл в documents/ — вне дефект-класса
        try:
            physical[name] = path.stat().st_size
        except OSError:
            continue
    return physical


def orphan_list_from(physical: dict[str, int], referenced: set[str]) -> list[dict]:
    """Orphan-кандидаты GC = physical ∖ referenced (общая семантика, без FS).

    Переиспользуется check_documents (integrity) и documents_stats (admin) —
    один источник «orphan = физический blob без Source-ref».
    """
    return [
        {"sha256": sha, "size": physical[sha]}
        for sha in sorted(set(physical) - referenced)
    ]


def check_documents(entries, document_store, *, create_issues: bool = True) -> dict:
    """Синхронное ядро integrity-проверки (блокирующее — звать из executor).

    Args:
        entries: ВСЕ разобранные entries (Source — по blobs-контракту;
            не-Source — по source_refs: dangling-детект Ф3c2c).
        document_store: DocumentStore (blob-стор Ф1).
        create_issues: фиксировать дефекты в quality-issues (идемпотентно).

    Returns:
        {ok, checked, missing_blob, sha_mismatch, canonical_missing,
         provenance_incomplete, dangling_source_refs, orphans, errors, counts}
    """
    report: dict = {
        "ok": True,
        "checked": 0,
        "missing_blob": [],      # [{source_id, sha, ref}]
        "sha_mismatch": [],      # [{source_id, sha, actual, ref}]
        "canonical_missing": [], # [{source_id, sha}] — canonical-потеря (CR2)
        "provenance_incomplete": [],  # [{source_id, ref, sha, missing}]
        "orphans": [],           # [{sha256, size}] — кандидат GC
        "errors": [],            # [{source_id, sha, ref, error}] — read-fail
        "dangling_source_refs": [],  # [{knowledge_id, source_id}] — Ф3c2c
        "counts": {},
    }
    specs: list[dict] = []
    referenced: set[str] = set()
    refs_checked = 0
    entry_refs = 0

    # (0) Пре-ход (Ф3c2c): множество существующих Source-id — база dangling-детекта.
    # Два прохода обязательны: ВСЕ Source должны быть известны до проверки
    # source_refs любой Knowledge-записи (Source может идти ПОСЛЕ ссылок).
    source_ids_present: set[str] = set()
    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        if getattr(fm, "content_type", None) == "source":
            sid = getattr(fm, "knowledge_id", None) or ""
            if sid:
                source_ids_present.add(sid)

    def _issue(knowledge_id: str, detail: str, *, severity: str = "warn") -> None:
        specs.append({
            "issue_type": "broken_link",
            "knowledge_id": knowledge_id,
            "severity": severity,
            "detail": detail,
        })

    for entry in entries:
        fm = getattr(entry, "frontmatter", None)
        if getattr(fm, "content_type", None) != "source":
            # Ф3c2c: Knowledge-запись (не-Source) со source_refs → каждый
            # валидный ref обязан указывать на существующий Source. Malformed
            # ref (без source_id) — skip (прецедент: ref без sha в Ф3c1).
            kid = getattr(fm, "knowledge_id", None) or ""
            for ref in getattr(fm, "source_refs", None) or []:
                if not isinstance(ref, dict):
                    continue
                sid = ref.get("source_id")
                if not isinstance(sid, str) or not sid:
                    continue
                entry_refs += 1
                if sid not in source_ids_present:
                    report["dangling_source_refs"].append(
                        {"knowledge_id": kid, "source_id": sid}
                    )
                    _issue(kid, f"documents: source_ref → missing Source '{sid}'")
            continue
        source_id = getattr(fm, "knowledge_id", None) or ""
        report["checked"] += 1

        for ref_kind, ref in _iter_refs(getattr(fm, "blobs", None)):
            sha = ref.get("sha256")
            if not isinstance(sha, str) or not sha:
                # Malformed ref (нет sha) — вне дефект-класса Ф3c1
                # (ingest-гейты Ф3a); не падаем.
                continue
            referenced.add(sha)
            refs_checked += 1

            # (1) provenance-полнота цепочки
            missing_fields = _provenance_missing(ref_kind, ref)
            if missing_fields:
                report["provenance_incomplete"].append({
                    "source_id": source_id, "ref": ref_kind,
                    "sha": sha, "missing": missing_fields,
                })
                _issue(
                    source_id,
                    f"documents: provenance incomplete for ref '{ref_kind}' "
                    f"(sha256={sha}; missing: {', '.join(missing_fields)})",
                )

            # (2) физическое наличие + authoritative re-hash (fail-safe)
            try:
                if not document_store.exists(sha):
                    if ref_kind == "canonical":
                        # CR2: canonical-потеря = деградация доступности,
                        # не дублируется в missing_blob
                        report["canonical_missing"].append(
                            {"source_id": source_id, "sha": sha}
                        )
                        _issue(
                            source_id,
                            f"documents: canonical blob missing (sha256={sha}) — "
                            "availability degraded",
                            severity="critical",
                        )
                    else:
                        report["missing_blob"].append(
                            {"source_id": source_id, "sha": sha, "ref": ref_kind}
                        )
                        _issue(
                            source_id,
                            f"documents: blob missing for ref '{ref_kind}' "
                            f"(sha256={sha})",
                        )
                    continue
                actual = _hash_file(document_store._shard_path(sha))
                if actual != sha:
                    report["sha_mismatch"].append({
                        "source_id": source_id, "sha": sha,
                        "actual": actual, "ref": ref_kind,
                    })
                    _issue(
                        source_id,
                        f"documents: sha256 mismatch for ref '{ref_kind}' "
                        f"(expected={sha}, actual={actual})",
                        severity="critical",
                    )
                    if ref_kind == "canonical":
                        # Битый canonical = мёртвая цепочка (не тихая ложь)
                        report["canonical_missing"].append(
                            {"source_id": source_id, "sha": sha}
                        )
                        _issue(
                            source_id,
                            f"documents: canonical blob missing (sha256={sha}) — "
                            "availability degraded",
                            severity="critical",
                        )
            except Exception as exc:  # noqa: BLE001 — fail-safe per-blob
                report["errors"].append({
                    "source_id": source_id, "sha": sha,
                    "ref": ref_kind, "error": str(exc),
                })
                _issue(
                    source_id,
                    f"documents: blob check failed for ref '{ref_kind}' "
                    f"(sha256={sha}): {exc}",
                )
                logger.warning(
                    "[DOCUMENTS_CHECK] blob check failed for %s ref=%s: %s",
                    sha[:12], ref_kind, exc,
                )

    # (3) orphan-sweep: каждый физический blob → ∃ ≥1 ref
    physical = physical_blobs(document_store)
    orphan_list = orphan_list_from(physical, referenced)
    for item in orphan_list:
        report["orphans"].append(item)
    for item in orphan_list[:MAX_ORPHAN_ISSUES]:
        specs.append({
            "issue_type": "orphaned",
            "knowledge_id": f"blob-{item['sha256'][:16]}",
            "severity": "warn",
            "detail": (
                f"documents: blob without Source refs (sha256={item['sha256']}, "
                f"size={item['size']}) — GC candidate"
            ),
        })
    if len(orphan_list) > MAX_ORPHAN_ISSUES:
        logger.warning(
            "[DOCUMENTS_CHECK] %d orphan blobs — issues capped at %d "
            "(full list in report)",
            len(orphan_list), MAX_ORPHAN_ISSUES,
        )

    batch = {"created": 0, "refreshed": 0, "skipped": 0}
    if create_issues and specs:
        from ..quality.issues import create_issues_batch

        batch = create_issues_batch(specs)

    defect_keys = (
        "missing_blob", "sha_mismatch", "canonical_missing",
        "provenance_incomplete", "dangling_source_refs", "orphans", "errors",
    )
    report["ok"] = not any(report[key] for key in defect_keys)
    report["counts"] = {
        "sources": report["checked"],
        "refs": refs_checked,
        "entry_refs": entry_refs,
        "blobs": len(physical),
        "referenced": len(referenced),
        "issues_created": batch.get("created", 0),
        "issues_refreshed": batch.get("refreshed", 0),
        "issues_skipped": batch.get("skipped", 0),
    }
    return report


async def documents_check(store, document_store, *, create_issues: bool = True) -> dict:
    """Integrity-проверка documents (SSOT Source-refs ↔ blob-стор).

    Полный цикл: скан .md → ВСЕ entries (Source + Knowledge с source_refs —
    фильтрацию делает ядро) → check_documents (ядро в run_in_executor —
    потоковое хеширование/FS-скан не блокируют event loop) → ретеншн
    canonicalization_jobs (Ф3c2c, fail-safe).

    Args:
        store: MarkdownStore (SSOT).
        document_store: DocumentStore (blob-стор Ф1).
        create_issues: фиксировать дефекты в quality-issues (default True).

    Returns:
        Отчёт check_documents (см. docstring ядра) + counts["jobs_pruned"].
    """
    paths = await store.reindex_scan()
    loop = asyncio.get_running_loop()

    def _scan_and_check() -> dict:
        entries = []
        for path in paths:
            try:
                entry = store._parse_file(path)
            except Exception:  # noqa: BLE001 — битые файлы пропускаем
                logger.warning(
                    "[DOCUMENTS_CHECK] skip unparsable file %s", path,
                )
                continue
            entries.append(entry)
        return check_documents(entries, document_store, create_issues=create_issues)

    report = await loop.run_in_executor(None, _scan_and_check)

    # Ф3c2c: ретеншн завершённых canonicalization-jobs ПОСЛЕ integrity-ядра.
    # Fail-safe: сбой prune НЕ роняет проверку (jobs_pruned=0 + warning);
    # getattr — duck-typing фейков document_store без prune_jobs.
    report["counts"]["jobs_pruned"] = 0
    prune_jobs = getattr(document_store, "prune_jobs", None)
    if prune_jobs is not None:
        try:
            report["counts"]["jobs_pruned"] = await loop.run_in_executor(
                None, prune_jobs, settings.DOCUMENTS_JOBS_RETENTION_DAYS,
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe
            logger.warning(
                "[DOCUMENTS_CHECK] canonicalization jobs prune failed "
                "(non-fatal): %s",
                exc,
            )
    return report
