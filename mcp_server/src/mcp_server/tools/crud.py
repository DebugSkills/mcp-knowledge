# ruff: noqa: BLE001
"""A5-A6: write_knowledge + update_entry + delete_entry.

Three-way write flow (P1-2):
1. store.write(entry)           → Markdown SSOT (.md + git commit)
2. pipeline.enqueue(entry, ...) → chunk → embed → Qdrant upsert
3. knowledge_index.update_section(domain) → инкрементальный INDEX.gen.yaml

update_entry: store.update(expected_version) + pipeline.enqueue (переиндексация)
  G3-fix: optimistic locking через expected_version параметр
delete_entry: store.delete() (soft-delete в .trash/) + qdrant.delete_by_knowledge_id()
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..content.source_refs import merge_source_refs
from ..metrics import quality_gate_skipped, record_write_latency
from ..models import VersionConflictError, WriteRequest
from ..storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone
from .zone_utils import resolve_zone

logger = logging.getLogger("mcp_knowledge.tools.crud")


def _get_qdrant(app_state):
    """Получить Qdrant-клиент: app_state.qdrant_client (raw) или app_state.qdrant (wrapper)."""
    return getattr(app_state, "qdrant_client", None) or getattr(app_state, "qdrant", None)


def _recommended_field_warnings(params: dict, strict: bool) -> tuple[list[dict], list[str], bool]:
    """Проверка recommended-полей (source/evergreen/cross_subjects) — advisory (E5).

    Возвращает (issues, warnings, blocked). При strict=True warn повышается до block.
    """
    issues: list[dict] = []
    warnings: list[str] = []
    blocked = False
    for rec_field in ("source", "evergreen", "cross_subjects"):
        val = params.get(rec_field)
        missing = val is None or val == "" or val is False or val == []
        if missing:
            msg = f"Recommended field '{rec_field}' is missing"
            if strict:
                issues.append({"field": rec_field, "severity": "critical", "message": msg})
                blocked = True
            else:
                warnings.append(msg)
    return issues, warnings, blocked


async def _validate_source_refs(refs: list, app_state, *, param_name: str) -> list[str] | None:
    """Ф3c2b (bibliography): fail-closed валидация source_refs ДО записи.

    Каждый item: object с непустой строкой ``source_id``; запись существует
    (``store.read``) и её ``content_type == "source"``. Собирает ВСЕ ошибки
    (не только первую); ``None`` = валидно.

    Формат сообщений: ``{param_name}: 'source_id' must be a non-empty string`` /
    ``{param_name}: source 'X' not found`` / ``{param_name}: 'X' is not a
    Source (content_type='book')``.
    """
    store = app_state.store
    errors: list[str] = []
    for item in refs:
        if not isinstance(item, dict):
            errors.append(f"{param_name}: item must be an object, got {type(item).__name__}")
            continue
        sid = item.get("source_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"{param_name}: 'source_id' must be a non-empty string")
            continue
        try:
            src_entry = await store.read(sid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{param_name}: source '{sid}' read failed: {exc}")
            continue
        if src_entry is None:
            errors.append(f"{param_name}: source '{sid}' not found")
            continue
        ct = getattr(src_entry.frontmatter, "content_type", None)
        if ct != "source":
            errors.append(f"{param_name}: '{sid}' is not a Source (content_type={ct!r})")
    return errors or None


async def write_knowledge(params: dict, app_state) -> dict:
    """Записать новое знание: SSOT → chunk → embed → Qdrant → INDEX.

    Three-way write flow (P1-2):
    1. store.write → Markdown SSOT
    2. pipeline.enqueue → async indexing
    3. knowledge_index.update_section → INDEX.gen.yaml
    """
    # Валидация обязательных параметров
    content = params.get("content", "")
    domain = params.get("domain", "")
    subject = params.get("subject", "")

    if not content:
        return {"error": "Missing required parameter: 'content'"}
    if not domain:
        return {"error": "Missing required parameter: 'domain'"}
    if not subject:
        return {"error": "Missing required parameter: 'subject'"}

    # ── Phase 4: Pre-write quality checks (GAP-1 fix) ──
    strict = params.get("strict", False)
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    blocked = False

    # W1.4: zone — валидация значения (ValueError → ошибка тула).
    # W2: зона резолвится ДО collision-check — get_all_knowledge_ids
    # фильтрует по зональной коллекции (collection_for_zone).
    try:
        zone, _ = resolve_zone(params.get("zone"), None)
    except ValueError as exc:
        return {"error": str(exc)}

    # 1. knowledge_id collision check (если клиент указал ID)
    provided_kid = params.get("knowledge_id")
    if provided_kid:
        try:
            qdrant = _get_qdrant(app_state)
            if qdrant and hasattr(qdrant, "get_all_knowledge_ids"):
                # P2-1 W2: union по ОБЕИМ зонам — SSOT-уникальность knowledge_id
                # глобальна (планы W2.13:151 «dup-проверка по всей базе»); иначе
                # публичная запись могла продублировать ID приватной.
                existing_ids: set[str] = set()
                for _zone in (ZONE_PUBLIC, ZONE_PRIVATE):
                    existing_ids.update(
                        qdrant.get_all_knowledge_ids(collection_name=collection_for_zone(_zone))
                    )
                if provided_kid in existing_ids:
                    msg = f"knowledge_id '{provided_kid}' already exists (duplicate)"
                    quality_issues.append({"field": "knowledge_id", "severity": "critical", "message": msg})
                    blocked = True
        except Exception as exc:
            logger.warning("Collision check skipped (non-fatal): %s", exc)
            quality_gate_skipped.labels(gate="collision", reason="exception").inc()

    # 2. Recommended-поля (advisory, E5)
    rec_issues, rec_warnings, rec_blocked = _recommended_field_warnings(params, strict)
    quality_issues.extend(rec_issues)
    quality_warnings.extend(rec_warnings)
    blocked = blocked or rec_blocked

    # 3. Semantic dup-gate (advisory, §4.3)
    try:
        embedder = getattr(app_state, "embedder", None)
        qdrant = _get_qdrant(app_state)
        if embedder is not None and qdrant is not None and hasattr(qdrant, "search"):
            from mcp_server.quality.dup_gate import check_duplicates

            quality_duplicates = await check_duplicates(
                content, domain, provided_kid,
                embedder=embedder,
                qdrant_client=qdrant,
                collection_name=collection_for_zone(zone),
            )
            if quality_duplicates and strict:
                msg = f"Semantic duplicates detected: {quality_duplicates}"
                quality_issues.append({"field": "*content", "severity": "critical", "message": msg})
                blocked = True
    except Exception as exc:
        logger.warning("Dup-gate check skipped (non-fatal): %s", exc)
        quality_gate_skipped.labels(gate="dup_gate", reason="exception").inc()

    if blocked:
        logger.warning(
            "write_knowledge blocked by quality gate: %d critical issues, %d duplicates",
            len(quality_issues), len(quality_duplicates),
        )
        return {
            "error": "Quality gate blocked the write",
            "quality_report": {
                "blocked": True,
                "issues": quality_issues,
                "warnings": quality_warnings,
                "duplicates": quality_duplicates,
            },
        }
    # ── End quality gate ──

    # Ф3c2b (bibliography): documents / алиас source_refs → fm.source_refs.
    # Fail-closed ДО записи: битый item / несуществующий / не-Source → отказ
    # (ВСЕ ошибки собраны), SSOT не трогаем. Оба параметра → merge с дедупом
    # по (source_id, locator.kind): alias-first (существующие ключи выигрывают).
    documents_in = params.get("documents")
    refs_in = params.get("source_refs")
    for pname, pval in (("documents", documents_in), ("source_refs", refs_in)):
        if pval is not None and not isinstance(pval, list):
            return {"error": f"{pname}: must be an array of {{source_id, locator?}} objects"}
    source_refs = merge_source_refs(refs_in, documents_in)
    if source_refs is not None:
        refs_errors = await _validate_source_refs(source_refs, app_state, param_name="documents")
        if refs_errors:
            logger.warning(
                "write_knowledge: documents validation failed (%d errors) — write refused",
                len(refs_errors),
            )
            return {"error": "; ".join(refs_errors), "errors": refs_errors}

    wait_for_index = params.get("wait_for_index", False)

    # Шаг 1: SSOT запись
    store = app_state.store
    req = WriteRequest(
        content=content,
        domain=domain,
        subject=subject,
        project=params.get("project"),
        cross_subjects=params.get("cross_subjects", []),
        tags=params.get("tags", []),
        knowledge_id=params.get("knowledge_id"),
        wait_for_index=wait_for_index,
        zone=zone,
        source_refs=source_refs,  # Ф3c2b: documents/alias после fail-closed валидации
    )
    entry = await store.write(req)
    knowledge_id = entry.frontmatter.knowledge_id

    # Шаг 2: Enqueue в indexing pipeline (с трекингом latency)
    t0 = time.monotonic()
    pipeline = app_state.pipeline
    indexed = False
    try:
        enqueue_result = await pipeline.enqueue(entry, wait_for_index=wait_for_index)
        if wait_for_index:
            indexed = enqueue_result.indexed
            if not indexed:
                logger.warning(
                    "write_knowledge: indexing NOT confirmed for %s (pending=%s) — "
                    "worker may be dead/slow or event loop mismatch",
                    knowledge_id, enqueue_result.pending,
                )
    except Exception as exc:
        logger.error(
            "Pipeline enqueue failed for %s (wait=%s): %s",
            knowledge_id, wait_for_index, exc,
        )
        # Не фатально — SSOT уже записан, Qdrant отстаёт
    finally:
        record_write_latency(time.monotonic() - t0)

    # Шаг 3: INDEX.gen.yaml update (best-effort)
    try:
        knowledge_index = app_state.knowledge_index
        knowledge_index.update_section(domain)
    except Exception as exc:
        logger.warning(
            "INDEX update failed for domain=%s (non-fatal): %s", domain, exc
        )

    pending = not indexed

    logger.info(
        "write_knowledge: %s (domain=%s, subject=%s, wait=%s, indexed=%s)",
        knowledge_id, domain, subject, wait_for_index, indexed,
    )
    return {
        "knowledge_id": knowledge_id,
        "domain": domain,
        "subject": subject,
        "indexed": indexed,
        "pending": pending,
        "documents_linked": len(source_refs or []),  # Ф3c2b
        "quality_report": {
            "blocked": False,
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }


async def update_entry(params: dict, app_state) -> dict:
    """Обновить существующую запись: контент + переиндексация.
    
    G3-fix: поддерживает expected_version для optimistic locking.
    """
    knowledge_id = params.get("knowledge_id", "")
    content = params.get("content")
    expected_version = params.get("version")  # G3-fix: optimistic locking

    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}
    if content is not None and not isinstance(content, str):
        return {"error": "Parameter 'content' must be a string"}
    if content is not None and not content.strip():
        return {"error": "Parameter 'content' must not be empty"}

    # ── Phase 4: Pre-write quality checks (GAP-1 fix) ──
    # Для update контент — только тело markdown (frontmatter сохраняется от
    # существующей записи). Проверяем semantic dup-gate (advisory) + recommended-поля.
    strict = params.get("strict", False)
    quality_issues: list[dict] = []
    quality_warnings: list[str] = []
    quality_duplicates: list[dict] = []
    blocked = False

    if content is not None:
        # Semantic dup-gate (advisory, §4.3) — только при обновлении контента
        try:
            embedder = getattr(app_state, "embedder", None)
            qdrant = _get_qdrant(app_state)
            if embedder is not None and qdrant is not None and hasattr(qdrant, "search"):
                from mcp_server.quality.dup_gate import check_duplicates

                quality_duplicates = await check_duplicates(
                    content, "", knowledge_id,  # domain неизвестен — без фильтра
                    embedder=embedder,
                    qdrant_client=qdrant,
                )
                if quality_duplicates and strict:
                    msg = f"Semantic duplicates detected: {quality_duplicates}"
                    quality_issues.append({"field": "*content", "severity": "critical", "message": msg})
                    blocked = True
        except Exception as exc:
            logger.warning("Dup-gate check skipped (non-fatal): %s", exc)
            quality_gate_skipped.labels(gate="dup_gate", reason="exception").inc()

    # Recommended-поля (advisory) — из параметров обновления
    rec_issues, rec_warnings, rec_blocked = _recommended_field_warnings(params, strict)
    quality_issues.extend(rec_issues)
    quality_warnings.extend(rec_warnings)
    blocked = blocked or rec_blocked

    if blocked:
        logger.warning(
            "update_entry blocked by quality gate for %s: %d critical issues",
            knowledge_id, len(quality_issues),
        )
        return {
            "error": "Quality gate blocked the update",
            "quality_report": {
                "blocked": True,
                "issues": quality_issues,
                "warnings": quality_warnings,
                "duplicates": quality_duplicates,
            },
        }
    # ── End quality gate ──

    # Ф3c2b (bibliography): source_refs — та же fail-closed валидация ДО
    # store.update; применение через metadata (setattr в frontmatter — поле
    # fm.source_refs, models.py). Явный [] очищает refs (→ None, Л1: ключ
    # source_refs не пишется в YAML).
    refs_upd = params.get("source_refs")
    metadata_update: dict = {}
    if refs_upd is not None:
        if not isinstance(refs_upd, list):
            return {"error": "source_refs: must be an array of {source_id, locator?} objects"}
        refs_errors = await _validate_source_refs(refs_upd, app_state, param_name="source_refs")
        if refs_errors:
            logger.warning(
                "update_entry: source_refs validation failed for %s (%d errors) — update refused",
                knowledge_id, len(refs_errors),
            )
            return {"error": "; ".join(refs_errors), "errors": refs_errors}
        metadata_update["source_refs"] = refs_upd or None

    store = app_state.store
    try:
        entry = await store.update(
            knowledge_id,
            content=content,
            metadata=metadata_update or None,
            expected_version=expected_version,
        )
    except VersionConflictError as e:
        # Фаза 12: инкремент метрики optimistic_lock_conflicts
        from ..metrics import optimistic_lock_conflicts
        optimistic_lock_conflicts.inc()

        # F2: Optimistic locking conflict — клиент должен перечитать и повторить
        logger.warning(
            "update_entry: version conflict for %s (expected=%s, actual=%s): %s",
            knowledge_id, expected_version, e.actual, e,
        )
        return {
            "message": f"Version conflict: {e}",
            "knowledge_id": knowledge_id,
            "expected_version": expected_version,
            "current_version": e.actual,
            "conflict": True,
        }

    if entry is None:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    # Переиндексация (wait_for_index опционален — контракт: для синхронных
    # сценариев (тесты, blue-green) ждём завершения, иначе drain очереди
    # при stop может записать точку ПОСЛЕ delete_by_knowledge_id)
    wait_for_index = params.get("wait_for_index", False)
    pipeline = app_state.pipeline
    await pipeline.enqueue(entry, wait_for_index=wait_for_index)

    # INDEX update (best-effort)
    try:
        knowledge_index = app_state.knowledge_index
        knowledge_index.update_section(entry.frontmatter.domain)
    except Exception as exc:
        logger.warning("INDEX update failed for %s: %s", knowledge_id, exc)

    logger.info("update_entry: %s v%d", knowledge_id, entry.frontmatter.version)
    # Ф3b2: точечное обновление Source-ref (license/zone/public_allowed могли
    # измениться — upsert по source_id; не-Source записи игнорируются).
    from .source_ref_runtime import index_add_entry

    index_add_entry(app_state, entry)
    # Task 1: инкремент data_version после мутации
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort
    return {
        "knowledge_id": knowledge_id,
        "version": entry.frontmatter.version,
        "updated_at": entry.frontmatter.updated_at.isoformat(),
        "pending": True,
        "quality_report": {
            "blocked": False,
            "issues": quality_issues,
            "warnings": quality_warnings,
            "duplicates": quality_duplicates,
        },
    }


async def delete_entry(params: dict, app_state) -> dict:
    """Удалить запись: soft-delete (→ .trash/) + удаление из Qdrant.

    Фаза 13.14: +cascade param — при cascade=True удалить также все
    дочерние секции по parent_knowledge_id (рекурсивно через scroll).
    """
    knowledge_id = params.get("knowledge_id", "")
    cascade = params.get("cascade", False)

    if not knowledge_id:
        return {"error": "Missing required parameter: 'knowledge_id'"}

    cascade_deleted = 0

    # Получаем зависимости (до cascade — нужны для удаления детей)
    store = app_state.store
    qdrant = app_state.qdrant
    loop = asyncio.get_running_loop()

    # W2: зона записи определяется ДО cascade — scroll и delete_by_knowledge_id
    # идут в зональную коллекцию (collection_for_zone).
    entry = await store.read(knowledge_id)
    zone = getattr(entry.frontmatter, "zone", None) or ZONE_PRIVATE if entry else ZONE_PRIVATE
    domain = entry.frontmatter.domain if entry else None

    # Шаг 0 (cascade): найти и удалить дочерние секции
    if cascade:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            qdrant_raw = _get_qdrant(app_state)
            # Scroll все точки где parent_knowledge_id = knowledge_id
            child_ids: list[str] = []
            offset = None
            while True:
                points, next_offset = qdrant_raw.scroll(
                    scroll_filter=Filter(
                        must=[FieldCondition(key="parent_knowledge_id", match=MatchValue(value=knowledge_id))]
                    ),
                    limit=1000,
                    offset=offset,
                    with_payload=["knowledge_id"],
                    with_vectors=False,
                    collection_name=collection_for_zone(zone),
                )
                for point in points:
                    kid = point.payload.get("knowledge_id") if point.payload else None
                    if kid:
                        child_ids.append(kid)
                if next_offset is None or len(points) == 0:
                    break
                offset = next_offset

            # Удаляем секции пачкой: store.delete_many (→ .trash/) с ОДНИМ git-коммитом
            # (Фаза 13.22 P1: раньше N+1 git-коммитов блокировали event loop).
            if child_ids:
                try:
                    cascade_deleted = await store.delete_many(
                        child_ids,
                        commit_message=f"cascade delete: {knowledge_id} ({len(child_ids)} sections)",
                    )
                except Exception as exc:
                    logger.warning("[DELETE] cascade: store.delete_many failed: %s", exc)
                # Qdrant-точки удаляем по одной (не git-операция, не блокирует)
                for child_id in child_ids:
                    try:
                        # run_in_executor НЕ принимает kwargs → ПОЗИЦИОННО
                        await loop.run_in_executor(
                            None, qdrant.delete_by_knowledge_id, child_id,
                            collection_for_zone(zone),
                        )
                    except Exception as exc:
                        logger.warning("[DELETE] cascade: failed to delete qdrant point %s: %s", child_id, exc)

            logger.info("[DELETE] cascade: %d child sections deleted for book %s", cascade_deleted, knowledge_id)
            # Ф3b2: снять refs удалённых секций (идемпотентно; Source-секций не
            # бывает — но инвариант «нет stale-refs» поддержан глобально).
            from .source_ref_runtime import index_remove_id

            for child_id in child_ids:
                index_remove_id(app_state, child_id)
        except Exception as exc:
            logger.error("[DELETE] cascade scroll failed for %s: %s", knowledge_id, exc)

    # Шаг 1: Soft-delete Markdown SSOT
    deleted = await store.delete(knowledge_id)
    if not deleted:
        return {"error": f"Knowledge entry not found: '{knowledge_id}'"}

    # Шаг 1b (Ф3b2): снять Source-ref удалённой записи из availability-индекса.
    # Least-strict: пока на blob ссылается ДРУГОЙ ref — он остаётся доступен.
    from .source_ref_runtime import index_remove_id

    removed_refs = index_remove_id(app_state, knowledge_id)

    # Шаг 2: Удаление из Qdrant (позиционно — run_in_executor не принимает kwargs)
    await loop.run_in_executor(
        None, qdrant.delete_by_knowledge_id, knowledge_id, collection_for_zone(zone)
    )

    # Шаг 3: INDEX update (best-effort)
    if domain:
        try:
            knowledge_index = app_state.knowledge_index
            knowledge_index.update_section(domain)
        except Exception as exc:
            logger.warning("INDEX update failed for domain=%s: %s", domain, exc)

    logger.info(
        "[DELETE] delete_entry: %s → .trash/ + Qdrant removed (cascade_deleted=%d, source_refs_removed=%d)",
        knowledge_id, cascade_deleted, removed_refs,
    )
    # Task 1: инкремент data_version после мутации
    try:
        app_state.data_version += 1
    except Exception:  # noqa: S110
        pass  # best-effort
    return {
        "knowledge_id": knowledge_id,
        "deleted": True,
        "cascade_deleted": cascade_deleted,
        "message": f"Entry '{knowledge_id}' moved to .trash/ and removed from Qdrant"
                   + (f" (+{cascade_deleted} child sections)" if cascade_deleted else ""),
    }
