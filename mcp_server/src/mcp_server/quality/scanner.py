"""Quality scanner — периодический обход базы знаний (4.5+13.15).

Сканер:
1. Обходит knowledge/**/*.md (паттерн reconcile #19)
2. Читает YAML frontmatter каждой записи
3. Вычисляет staleness_score (scoring.py)
4. Пишет score + quality_flags в Qdrant payload
5. Сканирует dup-пары внутри domain-бакетов
6. Создаёт issues для обнаруженных проблем
7. Наполняет review-очередь (top-N по staleness_score DESC)

Фаза 13.15: все sync Qdrant/FS вызовы — через run_in_executor.
Опциональный progress-трекер для live-отслеживания прогресса скана.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from mcp_server.config import Settings
from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.edit_war import detect_edit_war
from mcp_server.quality.issues import create_issue
from mcp_server.quality.scoring import (
    REVIEW_THRESHOLD,
    StalenessInput,
    staleness_score,
)

logger = logging.getLogger("mcp_knowledge.quality.scanner")

# ── Qdrant payload keys ──────────────────────────────────────
PAYLOAD_STALENESS_SCORE = "staleness_score"
PAYLOAD_QUALITY_FLAGS = "quality_flags"
PAYLOAD_STATUS = "status"

# ── Конфигурация dup-scan ────────────────────────────────────
DUP_SIMILARITY_THRESHOLD: float = 0.92  # cosine-порог для дублей
MAX_PAIRS_PER_BUCKET: int = 500  # макс пар для проверки в одном domain
# Лимит dup-issues на одну запись (13.14): книга на 15K секций даёт тысячи пар
# с одинаковым fm_i → 15K+ issues на один knowledge_id (засорение issues + CPU 120%).
MAX_ISSUES_PER_KNOWLEDGE: int = 10


async def run_scan(
    knowledge_dir: Path | None = None,
    *,
    qdrant_client=None,
    settings: Settings | None = None,
    progress=None,            # 13.15: ImportProgressTracker (опционально)
    progress_id: str | None = None,  # 13.15: id для трекера
    cancel_event: asyncio.Event | None = None,  # 13.18: отмена скана
) -> dict:
    """Запускает полный quality scan базы знаний.

    Args:
        knowledge_dir: путь к knowledge/ (по умолчанию из settings).
        qdrant_client: экземпляр QdrantClient (если None — только scoring, без записи).
        settings: настройки (если None — загружаются из env).
        progress: опциональный ImportProgressTracker для live-отслеживания.
        progress_id: id записи в трекере (если progress задан).
        cancel_event: asyncio.Event для отмены скана (13.18).

    Returns:
        dict с метриками сканирования (частичные при отмене):
            files_scanned, scores_updated, duplicates_detected, issues_created, review_queue_size
    """
    if settings is None:
        settings = Settings()

    if knowledge_dir is None:
        knowledge_dir = Path(settings.KNOWLEDGE_DIR)

    if not knowledge_dir.exists():
        logger.warning("Knowledge dir %s not found, scan skipped", knowledge_dir)
        return _empty_result()

    now = datetime.now(timezone.utc)
    metrics: dict = {
        "files_scanned": 0,
        "scores_updated": 0,
        "duplicates_detected": 0,
        "issues_created": 0,
        "review_queue_size": 0,
    }

    pid = progress_id

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # Шаг 1: обход knowledge/**/*.md
    if progress and pid:
        progress.set_phase(pid, "scanning_fs", "Обход файлов knowledge/...")
    entries = await _scan_filesystem(knowledge_dir)
    metrics["files_scanned"] = len(entries)

    if progress and pid:
        progress.start(pid, total=len(entries))  # actual total after walk

    # 13.18: проверка отмены после _scan_filesystem
    if _is_cancelled():
        logger.info("Quality scan %s cancelled after filesystem walk (%d files)", pid, len(entries))
        if progress and pid:
            progress.log(pid, "warning", "scan cancelled by user")
            progress.error(pid, "cancelled by user")
        return metrics

    if not entries:
        if progress and pid:
            progress.done(pid, {"metrics": metrics})
        return metrics

    # Шаг 2: вычисление staleness_score для каждой записи
    if progress and pid:
        progress.set_phase(pid, "scoring", f"Вычисление staleness_score для {len(entries)} записей...")

    def _compute_all_scores(
        entries: list[tuple[Path, KnowledgeFrontmatter]],
        now_dt: datetime,
        cancel: asyncio.Event | None = None,
    ) -> tuple[list[tuple[Path, KnowledgeFrontmatter, float]], int, bool]:
        """CPU + git-интенсивный scoring (detect_edit_war → git log на файл) в executor.

        13.15 fix 2: _compute_score вызывает detect_edit_war (синхронный git-вызов
        gitpython на КАЖДЫЙ файл) — цикл 15K записей в event loop блокировал
        однопоточный uvicorn (повтор инцидента 2026-08-07 при live-smoke).

        Task 2: разбит на батчи ~500 записей с progress-обновлением между батчами.
        13.18: проверка cancel_event между батчами (asyncio.Event.is_set() thread-safe).
        """
        BATCH_SIZE = 500
        scored: list[tuple[Path, KnowledgeFrontmatter, float]] = []
        review_queue_size = 0
        total_entries = len(entries)
        cancelled = False

        for batch_start in range(0, total_entries, BATCH_SIZE):
            # 13.18: проверка отмены между батчами
            if cancel is not None and cancel.is_set():
                cancelled = True
                break

            batch = entries[batch_start:batch_start + BATCH_SIZE]
            for filepath, frontmatter in batch:
                score = _compute_score(frontmatter, now_dt, filepath)
                scored.append((filepath, frontmatter, score))
                if score >= REVIEW_THRESHOLD:
                    review_queue_size += 1

            # Batch progress log (Task 2)
            if progress and pid:
                batch_end = min(batch_start + BATCH_SIZE, total_entries)
                progress.set_phase(
                    pid, "scoring",
                    f"Scoring {batch_end}/{total_entries} entries...",
                )

        return scored, review_queue_size, cancelled

    scoring_loop = asyncio.get_running_loop()
    scored, review_queue_size, scoring_cancelled = await scoring_loop.run_in_executor(
        None, _compute_all_scores, entries, now, cancel_event,
    )
    metrics["review_queue_size"] = review_queue_size

    # 13.18: проверка отмены после scoring executor
    if scoring_cancelled or _is_cancelled():
        logger.info("Quality scan %s cancelled during scoring phase", pid)
        if progress and pid:
            progress.log(pid, "warning", "scan cancelled by user (during scoring)")
            progress.error(pid, "cancelled by user")
        return metrics

    # Шаг 3: запись scores в Qdrant payload
    if qdrant_client is not None:
        if _is_cancelled():
            if progress and pid:
                progress.log(pid, "warning", "scan cancelled — skipping Qdrant update")
                progress.error(pid, "cancelled by user")
            return metrics
        if progress and pid:
            progress.set_phase(pid, "updating_qdrant", "Запись scores в Qdrant payload...")
        await _update_qdrant_payloads(qdrant_client, scored, progress=progress, progress_id=pid)
        metrics["scores_updated"] = len(scored)

    # 13.18: проверка после Qdrant update
    if _is_cancelled():
        logger.info("Quality scan %s cancelled after Qdrant update", pid)
        if progress and pid:
            progress.log(pid, "warning", "scan cancelled by user")
            progress.error(pid, "cancelled by user")
        return metrics

    # Шаг 4: dup-pair scan по domain-бакетам
    if progress and pid:
        progress.set_phase(pid, "dup_scan", "Сканирование дубликатов...")
    loop = asyncio.get_running_loop()
    dup_count, dup_map = await loop.run_in_executor(
        None, _scan_dup_pairs, scored, progress, pid, cancel_event,
    )
    metrics["duplicates_detected"] = dup_count

    # R2: пост-обработка — обновить scores с dup_count для affected entries
    if dup_map and qdrant_client is not None and not _is_cancelled():
        if progress and pid:
            progress.set_phase(pid, "scoring_dup", "Обновление scores с dup_count...")
        await _update_scores_with_dup(
            qdrant_client, scored, dup_map,
            progress=progress, progress_id=pid,
        )

    # 13.18: проверка после dup_scan
    if _is_cancelled():
        logger.info("Quality scan %s cancelled after dup_scan", pid)
        if progress and pid:
            progress.log(pid, "warning", "scan cancelled by user")
            progress.error(pid, "cancelled by user")
        return metrics

    # Шаг 5: создание issues для проблемных записей
    if progress and pid:
        progress.set_phase(pid, "issues", "Создание quality issues...")
    issue_count = await loop.run_in_executor(
        None, _create_issues_for_problems, scored, progress, pid, cancel_event,
    )
    metrics["issues_created"] = issue_count

    logger.info(
        "Quality scan complete: %d files, %d in review queue, %d dups, %d issues",
        metrics["files_scanned"],
        metrics["review_queue_size"],
        metrics["duplicates_detected"],
        metrics["issues_created"],
    )
    if progress and pid:
        progress.done(pid, {"metrics": metrics})
    return metrics


# ── Внутренние функции ────────────────────────────────────────

async def _scan_filesystem(
    knowledge_dir: Path,
) -> list[tuple[Path, KnowledgeFrontmatter]]:
    """Обходит knowledge/**/*.md и парсит frontmatter.

    Использует run_in_executor для filesystem-операций (блокирующий I/O
    в async-контексте, паттерн из crud.py).
    """
    import yaml

    loop = asyncio.get_running_loop()

    def _walk() -> list[tuple[Path, KnowledgeFrontmatter]]:
        results: list[tuple[Path, KnowledgeFrontmatter]] = []
        for md_file in knowledge_dir.rglob("*.md"):
            # 13.18: пропуск файлов из .trash/ (как markdown_store.py:191,204)
            if ".trash" in md_file.parts:
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content, yaml)
                if fm is not None:
                    results.append((md_file, fm))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse %s: %s", md_file, exc)
        return results

    return await loop.run_in_executor(None, _walk)


def _parse_frontmatter(
    content: str, yaml_module
) -> KnowledgeFrontmatter | None:
    """Парсит YAML frontmatter из markdown-строки."""
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm_dict = yaml_module.safe_load(parts[1])
        if not isinstance(fm_dict, dict):
            return None
        return KnowledgeFrontmatter(**fm_dict)
    except Exception:  # noqa: BLE001
        return None


def _compute_score(
    frontmatter: KnowledgeFrontmatter,
    now: datetime,
    filepath: Path,
) -> float:
    """Вычисляет staleness_score для одной записи."""
    # Подсчитываем recommended-поля
    recommended_missing = 0
    recommended_total = 3  # source, cross_subjects, evergreen
    fm_dict = frontmatter.model_dump()
    for field in ("source",):
        if field not in fm_dict or fm_dict[field] is None:
            recommended_missing += 1
    # evergreen — из модели напрямую
    if "evergreen" not in fm_dict:
        recommended_missing += 1
    is_evergreen = fm_dict.get("evergreen", False) is True

    # NF-3: интеграция edit_war в scoring
    is_edit_war = detect_edit_war(filepath)

    inp = StalenessInput(
        updated_at=frontmatter.updated_at,
        evergreen=is_evergreen,
        dup_count=0,
        recommended_missing=recommended_missing,
        recommended_total=recommended_total,
        edit_war=is_edit_war,
        broken_links=0,
        total_links=0,
    )
    return staleness_score(inp, now=now)


async def _update_qdrant_payloads(
    client,  # QdrantClient
    scored: list[tuple[Path, KnowledgeFrontmatter, float]],
    *,
    progress=None,
    progress_id: str | None = None,
) -> None:
    """Обновляет staleness_score + quality_flags в Qdrant payload через set_payload.

    Использует set_payload (не upsert) — обновляет существующие chunk-точки
    по фильтру knowledge_id, не создавая новых non-vector точек в коллекции.

    13.15: sync set_payload вынесен в run_in_executor батчами по ~200;
    между батчами — asyncio.sleep(0) (yield event loop).
    Опциональный progress.section_done() каждые ~200 записей.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    BATCH_SIZE = 200
    loop = asyncio.get_running_loop()
    pid = progress_id

    def _do_batch(batch: list[tuple[str, dict]]) -> list[str]:
        """Синхронный set_payload для батча (выполняется в executor)."""
        errors: list[str] = []
        for knowledge_id, payload_update in batch:
            try:
                client.set_payload(
                    payload=payload_update,
                    points_filter=Filter(
                        must=[FieldCondition(key="knowledge_id", match=MatchValue(value=knowledge_id))]
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"Failed to set_payload for {knowledge_id}: {exc}"
                logger.error(msg)
                errors.append(msg)
        return errors

    # Подготовка батчей
    batches: list[list[tuple[str, dict]]] = []
    current_batch: list[tuple[str, dict]] = []
    for _filepath, frontmatter, score in scored:
        knowledge_id = frontmatter.knowledge_id
        flags: list[str] = []
        if score >= REVIEW_THRESHOLD:
            flags.append("needs_review")
        payload_update = {
            PAYLOAD_STALENESS_SCORE: score,
            PAYLOAD_QUALITY_FLAGS: flags,
        }
        current_batch.append((knowledge_id, payload_update))
        if len(current_batch) >= BATCH_SIZE:
            batches.append(current_batch)
            current_batch = []
    if current_batch:
        batches.append(current_batch)

    processed = 0
    for batch in batches:
        # Выполняем sync-работу в executor (не блокирует event loop)
        batch_errors = await loop.run_in_executor(None, _do_batch, batch)
        if batch_errors:
            logger.warning("_update_qdrant_payloads: %d set_payload errors in batch", len(batch_errors))

        processed += len(batch)

        # Прогресс: section_done для каждой записи в батче
        if progress and pid:
            for _ in batch:
                try:
                    progress.section_done(pid, 0, "")
                except Exception:  # noqa: S110, BLE001
                    pass  # best-effort progress

        # Yield event loop между батчами
        if len(batches) > 1:
            await asyncio.sleep(0)

        if processed % 1000 == 0:
            logger.info("_update_qdrant_payloads: %d/%d scores written", processed, len(scored))

    logger.info("_update_qdrant_payloads: completed %d/%d scores", processed, len(scored))


async def _update_scores_with_dup(
    client,  # QdrantClient
    scored: list[tuple[Path, KnowledgeFrontmatter, float]],
    dup_map: dict[str, int],
    *,
    progress=None,
    progress_id: str | None = None,
) -> None:
    """R2: пересчёт staleness_score с реальным dup_count + обновление Qdrant.

    Для записей с dup_count > 0 пересчитывает score через staleness_score()
    и обновляет payload через set_payload. Использует run_in_executor для
    sync-операций.
    """
    from datetime import datetime, timezone

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()
    pid = progress_id

    updated = 0
    for _filepath, frontmatter, _old_score in scored:
        kid = frontmatter.knowledge_id
        dc = dup_map.get(kid, 0)
        if dc == 0:
            continue

        # Пересчитываем score с реальным dup_count
        new_score = _compute_score_with_dup(frontmatter, now, dc, _filepath)

        # Обновляем Qdrant payload
        try:
            await loop.run_in_executor(
                None,
                lambda k=kid, s=new_score: client.set_payload(
                    payload={
                        PAYLOAD_STALENESS_SCORE: s,
                    },
                    points_filter=Filter(
                        must=[FieldCondition(key="knowledge_id", match=MatchValue(value=k))]
                    ),
                ),
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_update_scores_with_dup: set_payload failed for %s: %s", kid, exc,
            )

    if updated:
        logger.info(
            "_update_scores_with_dup: %d entries updated with dup_count", updated,
        )
        if progress and pid:
            progress.log(pid, "info", f"R2: {updated} scores updated with dup_count")


def _compute_score_with_dup(
    frontmatter: KnowledgeFrontmatter,
    now: datetime,
    dup_count: int,
    filepath: Path,
) -> float:
    """R2: _compute_score с явным dup_count (переиспользует логику _compute_score)."""
    fm_dict = frontmatter.model_dump()

    recommended_missing = 0
    if "source" not in fm_dict or fm_dict["source"] is None:
        recommended_missing += 1
    if "evergreen" not in fm_dict:
        recommended_missing += 1
    is_evergreen = fm_dict.get("evergreen", False) is True

    is_edit_war = detect_edit_war(filepath)

    inp = StalenessInput(
        updated_at=frontmatter.updated_at,
        evergreen=is_evergreen,
        dup_count=dup_count,  # R2: реальный dup_count
        recommended_missing=recommended_missing,
        recommended_total=3,
        edit_war=is_edit_war,
        broken_links=0,
        total_links=0,
    )
    return staleness_score(inp, now=now)


def _scan_dup_pairs(
    scored: list[tuple[Path, KnowledgeFrontmatter, float]],
    progress=None,
    progress_id: str | None = None,
    cancel_event=None,  # 13.18: asyncio.Event для отмены (проверяется между domain-бакетами)
) -> tuple[int, dict[str, int]]:
    """Сканирует dup-пары внутри domain-бакетов.

    Без BGE-M3 эмбеддера использует упрощённую эвристику:
    одинаковый subject + пересечение tags ≥50% → кандидат в дубли.

    R2: возвращает не только dup_count, но и dup_map (knowledge_id → dup_count)
    для пост-обработки staleness_score с реальным dup_count.

    Task 2: логирует прогресс по domain-бакетам.
    13.18: проверка cancel_event между domain-бакетами.

    Args:
        scored: список (filepath, frontmatter, score).
        progress: опциональный ImportProgressTracker.
        progress_id: id записи в трекере.
        cancel_event: asyncio.Event для отмены (13.18).

    Returns:
        (dup_count, dup_map): количество обнаруженных dup-пар и
        словарь knowledge_id → dup_count для scoring.
    """
    # Группируем по domain
    by_domain: dict[str, list[tuple[Path, KnowledgeFrontmatter, float]]] = {}
    for filepath, fm, score in scored:
        by_domain.setdefault(fm.domain, []).append((filepath, fm, score))

    dup_count = 0
    dup_map: dict[str, int] = {}  # R2: knowledge_id → dup_count
    total_domains = len(by_domain)
    domain_idx = 0
    for entries in by_domain.values():
        domain_idx += 1
        # 13.18: проверка отмены между domain-бакетами
        if cancel_event is not None and cancel_event.is_set():
            break
        if len(entries) < 2:
            continue
        # Прогресс по domain-бакетам (Task 2)
        if progress and progress_id:
            progress.log(
                progress_id, "info",
                f"dup_scan: {domain_idx}/{total_domains} domain buckets",
            )
        # Ограничиваем число пар для производительности
        n = min(len(entries), MAX_PAIRS_PER_BUCKET)
        # Лимит dup-issues на одну запись: книга на 15K секций даёт тысячи пар
        # с одинаковым fm_i → тысячи issues на один knowledge_id (засорение + CPU).
        issue_counts: dict[str, int] = {}
        for i in range(n):
            for j in range(i + 1, n):
                _, fm_i, _ = entries[i]
                _, fm_j, _ = entries[j]
                if issue_counts.get(fm_i.knowledge_id, 0) >= MAX_ISSUES_PER_KNOWLEDGE:
                    continue  # запись уже имеет достаточно dup-issues
                # Структурные TOC-секции («Table of Content (part N)») почти идентичны
                # по subject+tags → массовые false-positive дубли. Пропускаем их.
                if _is_toc_section(fm_i) or _is_toc_section(fm_j):
                    continue
                if _are_dup_candidates(fm_i, fm_j):
                    dup_count += 1
                    issue_counts[fm_i.knowledge_id] = issue_counts.get(fm_i.knowledge_id, 0) + 1
                    # R2: dup_map для пост-обработки scoring
                    dup_map[fm_i.knowledge_id] = dup_map.get(fm_i.knowledge_id, 0) + 1
                    dup_map[fm_j.knowledge_id] = dup_map.get(fm_j.knowledge_id, 0) + 1
                    # Создаём issue для дубликата
                    create_issue(
                        issue_type="duplicate",
                        knowledge_id=fm_i.knowledge_id,
                        severity="warn",
                        detail=f"Possible duplicate of {fm_j.knowledge_id} (same subject={fm_i.subject}, tag overlap)",
                    )
    return dup_count, dup_map


def _is_toc_section(fm: KnowledgeFrontmatter) -> bool:
    """Структурная TOC-секция (оглавление книги) — не содержательный дубликат.

    Heuristic: knowledge_id содержит 'table-of-content' (паттерн импорта книг).
    """
    return "table-of-content" in (fm.knowledge_id or "")


def _are_dup_candidates(
    a: KnowledgeFrontmatter, b: KnowledgeFrontmatter
) -> bool:
    """Эвристика: одинаковый subject + tags пересекаются ≥50%."""
    if a.subject != b.subject:
        return False
    if not a.tags or not b.tags:
        return False
    a_set = set(a.tags)
    b_set = set(b.tags)
    if not a_set or not b_set:
        return False
    overlap = len(a_set & b_set)
    min_size = min(len(a_set), len(b_set))
    if min_size == 0:
        return False
    return (overlap / min_size) >= 0.5


def _create_issues_for_problems(
    scored: list[tuple[Path, KnowledgeFrontmatter, float]],
    progress=None,
    progress_id: str | None = None,
    cancel_event=None,  # 13.18: asyncio.Event для отмены (проверяется между батчами)
) -> int:
    """Создаёт issues для проблемных записей (высокий score, edit_war, etc.).

    Task 2: логирует прогресс по батчам ~500 записей.
    13.18: проверка cancel_event между батчами.
    """
    BATCH_SIZE = 500
    count = 0
    total = len(scored)
    for batch_start in range(0, total, BATCH_SIZE):
        # 13.18: проверка отмены между батчами
        if cancel_event is not None and cancel_event.is_set():
            break
        batch = scored[batch_start:batch_start + BATCH_SIZE]
        for _, frontmatter, score in batch:
            if score >= REVIEW_THRESHOLD:
                # Проверяем — не создан ли уже issue для этой записи
                create_issue(
                    issue_type="missing_field",
                    knowledge_id=frontmatter.knowledge_id,
                    severity="warn",
                    detail=f"Staleness score {score} >= {REVIEW_THRESHOLD} — needs review",
                )
                count += 1
        # Batch progress log
        if progress and progress_id:
            batch_end = min(batch_start + BATCH_SIZE, total)
            progress.log(
                progress_id, "info",
                f"issues: {batch_end}/{total} entries processed, {count} issues",
            )
    return count


def _empty_result() -> dict:
    return {
        "files_scanned": 0,
        "scores_updated": 0,
        "duplicates_detected": 0,
        "issues_created": 0,
        "review_queue_size": 0,
    }
