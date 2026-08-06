# ruff: noqa: BLE001, S110
"""import_content MCP Tool (#16) — импорт крупных текстов в SSOT.

Фаза 5 §5: отдельный tool для декомпозиции + best-effort batch записи.

Flow:
  1. Валидация параметров (content, content_type, domain, subject)
  2. Registry lookup → ContentPreprocessor
  3. preprocessor.validate() → проверка контента
  4. preprocessor.decompose() → list[Section]
  5. Создание root-коллекции (linking.build_collection)
  6. Batch write: per-section store.write + pipeline.enqueue
  7. Partial_success контракт: failed_sections[], cleanup_orphans опция

Tasks: 5.1, 5.4, 5.5
"""

from __future__ import annotations

import asyncio
import logging

from ..content.linking import build_collection
from ..content.preprocessor import ImportMeta
from ..content.registry import get as get_preprocessor
from ..models import KnowledgeEntry, KnowledgeFrontmatter

logger = logging.getLogger("mcp_knowledge.tools.content")

# ── Конфигурация ──────────────────────────────────────────

IMPORT_BATCH_COMMIT = 10  # 1 git-коммит на N секций


async def _collect_quality_report(
    section_body: str,
    fm: KnowledgeFrontmatter,
    domain: str,
    embedder,
    qdrant_client,
) -> dict:
    """Прогнать quality gates на секции (advisory, не блокирует импорт).

    Returns:
        {"issues": [...], "warnings": [...], "duplicates": [...]}
    """
    issues: list[dict] = []
    warnings: list[str] = []
    duplicates: list[dict] = []

    # 1. Frontmatter validation (evaluate_frontmatter)
    try:
        from mcp_server.quality.gates import evaluate_frontmatter

        fm_text = f"---\n{fm.model_dump_json(indent=2)}\n---\n{section_body}"
        gate_result = evaluate_frontmatter(fm_text, strict=False)
        for issue in gate_result.issues:
            if issue.severity == "critical":
                issues.append({
                    "field": issue.field,
                    "severity": issue.severity,
                    "message": issue.message,
                })
            else:
                warnings.append(issue.message)
        warnings.extend(gate_result.warnings)
    except Exception as exc:
        warnings.append(f"Frontmatter gate skipped: {exc}")

    # 2. Semantic duplicate check (check_duplicates)
    try:
        if embedder is not None and qdrant_client is not None:
            from mcp_server.quality.dup_gate import check_duplicates

            dup_result = await check_duplicates(
                section_body, domain, fm.knowledge_id,
                embedder=embedder,
                qdrant_client=qdrant_client,
            )
            if dup_result:
                duplicates.extend(dup_result)
                warnings.append(f"Semantic duplicates detected: {len(dup_result)}")
    except Exception as exc:
        warnings.append(f"Dup-gate check skipped: {exc}")

    return {"issues": issues, "warnings": warnings, "duplicates": duplicates}


async def import_content(params: dict, app_state) -> dict:
    """MCP Tool #16: import_content — декомпозиция + batch запись в SSOT.

    Args:
        params: {
            content (str): исходный текст (Markdown/plain)
            content_type (str): тип контента → выбор препроцессора ("book")
            domain (str): первичная классификация
            subject (str): вторичная классификация
            project? (str): опциональный проект
            title? (str): заголовок коллекции (авто если не указан)
            tags? (list[str]): унаследованные теги
            cross_subjects? (list[str]): кросс-теги
            max_chunk_tokens? (int): лимит токенов (default 512)
            wait_for_index? (bool): ждать индексации (default false)
            cleanup_orphans? (bool): удалить orphan-детей при failure (default false)
            quality_checks? (bool): включить quality gates (default true, отключить для массового импорта)
        }
        app_state: Application state (store, pipeline, embedder, qdrant, ...)

    Returns:
        {
            collection_id: str,
            imported: int,
            failed: int,
            failed_sections: [{sequence_number, title, error}],
            partial_success: bool,
            indexed: bool,
            pending: bool,
            quality_report?: dict,
        }
    """
    # ── Параметры ──────────────────────────────────────────
    content = params.get("content", "")
    content_type = params.get("content_type", "book")
    domain = params.get("domain", "")
    subject = params.get("subject", "")
    project = params.get("project")
    title = params.get("title", "")
    tags = params.get("tags", [])
    cross_subjects = params.get("cross_subjects", [])
    
    wait_for_index = params.get("wait_for_index", False)
    cleanup_orphans = params.get("cleanup_orphans", False)
    quality_checks = params.get("quality_checks", True)  # 6.4: опциональное отключение для mass-import

    # ── Валидация обязательных параметров ──────────────────
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    if not content:
        return {"error": "Missing required parameter: 'content'"}
    if not domain:
        return {"error": "Missing required parameter: 'domain'"}
    if not subject:
        return {"error": "Missing required parameter: 'subject'"}

    # Registry lookup
    try:
        preprocessor = get_preprocessor(content_type)
    except ValueError as e:
        return {"error": str(e)}

    # ── Валидация контента препроцессором ──────────────────
    metadata = ImportMeta(
        domain=domain,
        subject=subject,
        project=project,
        title=title or content_type.capitalize(),
        tags=tags,
        cross_subjects=cross_subjects,
    )

    validation = preprocessor.validate(content, metadata)
    if not validation.valid:
        return {
            "error": f"Content validation failed: {validation.error}",
            "content_size": validation.content_size,
        }

    # ── Декомпозиция ────────────────────────────────────────
    try:
        sections = await preprocessor.decompose(content, metadata)
    except Exception as e:
        logger.exception("Decomposition failed")
        return {"error": f"Decomposition failed: {e}"}

    if not sections:
        return {"error": "Decomposition produced 0 sections"}

    # [IMPORT] start — размер и число секций для диагностики тяжёлой операции
    # (инцидент 2026-08-06: крупный импорт без общего timing невидим).
    logger.info(
        "[IMPORT] start type=%s domain=%s subject=%s size=%.1f KB sections=%d",
        content_type, domain, subject, len(content.encode("utf-8")) / 1024, len(sections),
    )

    # ── Создание коллекции (linking) ────────────────────────
    section_titles = [s.title for s in sections]
    section_ids = [s.meta["knowledge_id"] for s in sections]

    if not title:
        title = f"{domain}/{subject} {content_type}"

    collection = build_collection(
        domain=domain,
        subject=subject,
        project=project,
        title=title,
        section_titles=section_titles,
        section_ids=section_ids,
        tags=tags,
        cross_subjects=cross_subjects,
    )

    # ── Batch write: по секциям ─────────────────────────────
    store = app_state.store
    pipeline = app_state.pipeline
    knowledge_index = getattr(app_state, "knowledge_index", None)

    imported = 0
    failed = 0
    failed_sections: list[dict] = []
    indexed = True
    pending = not wait_for_index

    # Сначала пишем root-коллекцию
    try:
        root_fm = collection.to_frontmatter()
        root_entry = KnowledgeEntry(
            frontmatter=root_fm,
            content=f"# {title}\n\nКоллекция импортированных секций. Оглавление — в frontmatter.children.",
        )
        await store.write_entry(root_entry)
        await pipeline.enqueue(root_entry, wait_for_index=False)
        logger.info("import_content: root collection %s created", collection.knowledge_id)
    except Exception as e:
        logger.error("Failed to write collection root: %s", e)
        return {"error": f"Failed to create collection root: {e}"}

    # Batch write детей
    for i, section in enumerate(sections):
        try:
            meta = section.meta
            fm = KnowledgeFrontmatter(
                knowledge_id=meta["knowledge_id"],
                domain=meta["domain"],
                subject=meta["subject"],
                project=meta.get("project"),
                content_type=meta.get("content_type", "book"),
                parent_knowledge_id=collection.knowledge_id,
                sequence_number=section.sequence_number,
                tags=section.tags,
                cross_subjects=meta.get("cross_subjects", []),
            )
            entry = KnowledgeEntry(frontmatter=fm, content=section.body)

            # SSOT запись (без git-коммита — батчим ниже)
            await store.write_entry(entry)

            # Индексация (best-effort)
            try:
                await pipeline.enqueue(entry, wait_for_index=False)
            except Exception as idx_err:
                logger.warning(
                    "Pipeline enqueue failed for %s (non-fatal): %s",
                    fm.knowledge_id, idx_err,
                )

            # Quality gate check (advisory — не блокирует; 6.4: опционально)
            if quality_checks:
                try:
                    qdrant = getattr(app_state, "qdrant", None)
                    embedder = getattr(app_state, "embedder", None)
                    qr = await _collect_quality_report(
                        section_body=section.body,
                        fm=fm,
                        domain=domain,
                        embedder=embedder,
                        qdrant_client=qdrant,
                    )
                    quality_issues.extend(qr["issues"])
                    quality_warnings.extend(qr["warnings"])
                    quality_duplicates.extend(qr["duplicates"])
                except Exception as qe:
                    logger.debug("Quality report collection skipped: %s", qe)

            imported += 1

            # Batch git-commit каждые IMPORT_BATCH_COMMIT секций
            if imported % IMPORT_BATCH_COMMIT == 0:
                await store.flush(
                    f"import_content: batch #{imported // IMPORT_BATCH_COMMIT}"
                )
                logger.info(
                    "import_content: %d/%d sections written, git commit",
                    imported, len(sections),
                )

        except Exception as e:
            failed += 1
            failed_sections.append({
                "sequence_number": section.sequence_number,
                "title": section.title,
                "error": str(e),
            })
            logger.error(
                "import_content: section %d '%s' failed: %s",
                section.sequence_number, section.title, e,
            )
            # Продолжаем best-effort

    # Финальный git-коммит для оставшихся
    if imported % IMPORT_BATCH_COMMIT != 0:
        try:
            await store.flush(
                f"import_content: final batch (total {imported} sections)"
            )
        except Exception:
            pass  # non-fatal

    # ── INDEX.gen.yaml update (best-effort) ─────────────────
    if knowledge_index:
        try:
            knowledge_index.update_section(domain)
        except Exception as exc:
            logger.warning("INDEX update failed for domain=%s: %s", domain, exc)

    # ── Ожидание индексации (опционально) ──────────────────
    if wait_for_index:
        try:
            result = await pipeline.wait_for_index(  # N1+N3+N5: публичный метод
                collection.knowledge_id, timeout=30.0
            )
            indexed = result.indexed
            pending = result.pending
        except Exception as e:
            logger.warning(
                "import_content: indexing wait failed for %s: %s — pending=true",
                collection.knowledge_id, e,
            )
            pending = True

    # ── Cleanup orphans при partial_success ─────────────────
    partial_success = failed > 0
    orphan_cleanup_count = 0
    if partial_success and cleanup_orphans:
        logger.info(
            "import_content: cleanup_orphans enabled — %d failed sections, "
            "removing orphaned children via soft-delete",
            failed,
        )
        qdrant = getattr(app_state, "qdrant", None)
        # Map failed sequence_numbers to their knowledge_ids
        failed_seqs = {fs["sequence_number"] for fs in failed_sections}
        for i, sid in enumerate(section_ids):
            seq = i + 1  # sequence_number = index + 1
            if seq in failed_seqs:
                try:
                    # Soft-delete: Markdown → .trash/
                    await store.delete(sid)
                    # Удаление из Qdrant
                    if qdrant:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, qdrant.delete_by_knowledge_id, sid
                        )
                    orphan_cleanup_count += 1
                    logger.info(
                        "import_content: soft-deleted orphan child %s (seq=%d)",
                        sid, seq,
                    )
                except Exception as e:
                    logger.warning(
                        "import_content: failed to cleanup orphan %s: %s", sid, e
                    )
        logger.info("import_content: cleanup_orphans removed %d children", orphan_cleanup_count)

    # ── Сборка результата ───────────────────────────────────
    result = {
        "collection_id": collection.knowledge_id,
        "imported": imported,
        "failed": failed,
        "failed_sections": failed_sections,
        "partial_success": partial_success,
        "indexed": indexed,
        "pending": pending,
        "orphan_cleanup_count": orphan_cleanup_count,
        "quality_checks_applied": quality_checks,
        "quality_report": {
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }

    logger.info(
        "[IMPORT] done collection=%s imported=%d failed=%d partial=%s indexed=%s",
        collection.knowledge_id, imported, failed, partial_success, indexed,
    )
    return result
