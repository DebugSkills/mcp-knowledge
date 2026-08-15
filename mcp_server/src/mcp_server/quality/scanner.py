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
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp_server.config import Settings
from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.dup_gate import compute_cosine
from mcp_server.quality.dup_ranking import (
    NEGATION_TOKENS,  # Фаза 3 (0d): единый источник
)
from mcp_server.quality.edit_war import detect_edit_war
from mcp_server.quality.issues import (
    bulk_update_status,
    create_issue,
    list_issue_ids,
)
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
    embedder=None,            # P0: EmbeddingManager для embedding-dup (fallback при None)
) -> dict:
    """Запускает полный quality scan базы знаний.

    Args:
        knowledge_dir: путь к knowledge/ (по умолчанию из settings).
        qdrant_client: экземпляр QdrantClient (если None — только scoring, без записи).
        settings: настройки (если None — загружаются из env).
        progress: опциональный ImportProgressTracker для live-отслеживания.
        progress_id: id записи в трекере (если progress задан).
        cancel_event: asyncio.Event для отмены скана (13.18).
        embedder: EmbeddingManager для embedding-based dup (P0). Если None —
            _scan_dup_pairs делает graceful fallback на теговую эвристику.

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
            for filepath, frontmatter, _meta in batch:
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
    # Фаза 1 (1b/1c): side-channel сигналы + skip deprecated (re-detection loop)
    content_meta: dict[str, dict] = {
        fm.knowledge_id: meta for _f, fm, meta in entries
    }
    deprecated_kids = await _load_deprecated_kids(qdrant_client) if qdrant_client is not None else set()
    dup_count, dup_map = await loop.run_in_executor(
        None, _scan_dup_pairs, scored, progress, pid, cancel_event, embedder,
        deprecated_kids, content_meta,
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

    # Шаг 5.5 (P0): auto-clear — закрыть open missing_field issues записей,
    # чьё условие исчезло (score упал ниже REVIEW_THRESHOLD). Накопленные
    # issues никогда не чистились (Д3) — этот шаг разрывает цикл.
    cleared = await loop.run_in_executor(None, _auto_clear_stale_issues, scored)
    if cleared:
        logger.info("Auto-clear: %d stale missing_field issues resolved", cleared)

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
) -> list[tuple[Path, KnowledgeFrontmatter, dict]]:
    """Обходит knowledge/**/*.md и парсит frontmatter.

    Использует run_in_executor для filesystem-операций (блокирующий I/O
    в async-контексте, паттерн из crud.py).

    Фаза 1 dedup: для каждой записи возвращает content_meta
    (content_hash — SHA256 нормализованного тела, content_length) —
    единственный безопасный сигнал для будущего авто-режима (Critic P0-1).
    """
    import yaml

    loop = asyncio.get_running_loop()

    def _walk() -> list[tuple[Path, KnowledgeFrontmatter, dict]]:
        results: list[tuple[Path, KnowledgeFrontmatter, dict]] = []
        for md_file in knowledge_dir.rglob("*.md"):
            # 13.18: пропуск файлов из .trash/ (как markdown_store.py:191,204)
            if ".trash" in md_file.parts:
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content, yaml)
                if fm is not None:
                    body = _extract_body(content)
                    results.append((
                        md_file, fm,
                        {"content_hash": _content_body_hash(body), "content_length": len(body)},
                    ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse %s: %s", md_file, exc)
        return results

    return await loop.run_in_executor(None, _walk)


def _extract_body(content: str) -> str:
    """Тело записи (после YAML frontmatter) для content_hash.

    Если frontmatter не закрыт/отсутствует — возвращаем весь контент.
    """
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return content


def _normalize_body(body: str) -> str:
    """Нормализация тела для content_hash: strip + схлопывание пустых строк."""
    normalized = re.sub(r"\n{3,}", "\n\n", body.strip())
    return normalized


def _content_body_hash(body: str) -> str:
    """SHA256 нормализованного тела (полные 64 hex, Фаза 3 P2-1).

    16-hex (64-bit) рисковал коллизиями; полный sha256 безопаснее.
    Старые metadata (16 hex) при сравнении с новыми дадут hash_match=False
    → консервативно 🟡 до следующего скана (metadata-refresh, 0b).
    """
    return hashlib.sha256(_normalize_body(body).encode("utf-8")).hexdigest()


def has_negation_pattern(slug_a: str, slug_b: str) -> bool:
    """Антоним-guard (Фаза 1 dedup): разница токенов slug содержит отрицание.

    Контрпример Critic: chto-lyubit-ai ≈ chto-ne-lyubit-ai при cosine 0.995 —
    «что ИИ любит» / «что ИИ НЕ любит». Такие пары НЕ должны попадать в 🟢.
    """
    if not slug_a or not slug_b:
        return False
    diff = set(slug_a.split("-")) ^ set(slug_b.split("-"))
    return bool(diff & NEGATION_TOKENS)


async def _load_deprecated_kids(qdrant_client) -> set[str]:
    """Загружает knowledge_id записей со status=deprecated из Qdrant (Фаза 1).

    Deprecate живёт в Qdrant payload (SSOT .md не тронут) → без этого скан
    пересоздаёт dup-issues для скрытых записей на каждом проходе
    (re-detection loop). Возвращает set knowledge_id.
    """
    if qdrant_client is None or not hasattr(qdrant_client, "scroll"):
        return set()
    loop = asyncio.get_running_loop()
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        scroll_filter = Filter(
            must=[FieldCondition(key="status", match=MatchValue(value="deprecated"))]
        )
        deprecated: set[str] = set()
        offset = None
        while True:
            _filt, _off = scroll_filter, offset
            points, next_offset = await loop.run_in_executor(
                None,
                lambda f=_filt, o=_off: qdrant_client.scroll(
                    scroll_filter=f,
                    limit=1000,
                    offset=o,
                    with_payload=True,
                    with_vectors=False,
                ),
            )
            for p in points:
                kid = (p.payload or {}).get("knowledge_id")
                if kid:
                    deprecated.add(kid)
            if next_offset is None or len(points) == 0:
                break
            offset = next_offset
        logger.info("Loaded %d deprecated knowledge_ids for dup-scan skip", len(deprecated))
        return deprecated
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load deprecated kids (%s), dup-scan without skip", exc)
        return set()


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
    embedder=None,      # P0: EmbeddingManager для embedding-dup (fallback на теги при None)
    deprecated_kids: set[str] | None = None,  # Фаза 1: skip скрытых записей (re-detection loop)
    content_meta: dict[str, dict] | None = None,  # Фаза 1: kid → {content_hash, content_length}
) -> tuple[int, dict[str, int]]:
    """Сканирует dup-пары внутри domain-бакетов.

    P0 (A2a): если embedder доступен — семантическая детекция через
    compute_cosine (порог DUP_SIMILARITY_THRESHOLD=0.92) вместо грубой
    теговой эвристики (снижает ложные дубли книг с общими тегами).
    Если embedder недоступен — graceful fallback на _are_dup_candidates.

    R2: возвращает не только dup_count, но и dup_map (knowledge_id → dup_count)
    для пост-обработки staleness_score с реальным dup_count.

    Task 2: логирует прогресс по domain-бакетам.
    13.18: проверка cancel_event между domain-бакетами.

    Args:
        scored: список (filepath, frontmatter, score).
        progress: опциональный ImportProgressTracker.
        progress_id: id записи в трекере.
        cancel_event: asyncio.Event для отмены (13.18).
        embedder: EmbeddingManager (embed_sync). Если None — теговая эвристика.

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
        # P0 (A2a): pre-embed репрезентативных текстов батчем, если embedder есть
        use_embedding = embedder is not None and hasattr(embedder, "embed_sync")
        emb_vectors: dict[int, list[float]] = {}
        if use_embedding:
            try:
                texts = [_representative_text(fm) for _f, fm, _s in entries[:n]]
                vecs = embedder.embed_sync(texts)
                emb_vectors = {
                    i: (vec.tolist() if hasattr(vec, "tolist") else list(vec))
                    for i, vec in enumerate(vecs)
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("Embedding dup-scan failed (%s), fallback to tag heuristic", exc)
                use_embedding = False
                emb_vectors = {}

        # Лимит dup-issues на одну запись: книга на 15K секций даёт тысячи пар
        # с одинаковым fm_i → тысячи issues на один knowledge_id (засорение + CPU).
        issue_counts: dict[str, int] = {}
        for i in range(n):
            for j in range(i + 1, n):
                _, fm_i, _ = entries[i]
                _, fm_j, _ = entries[j]
                # Фаза 1 (1c): deprecated-записи пропускаем — иначе re-detection loop
                # (deprecate живёт в Qdrant payload, SSOT .md не тронут → скан видит запись снова).
                if deprecated_kids and (
                    fm_i.knowledge_id in deprecated_kids or fm_j.knowledge_id in deprecated_kids
                ):
                    continue
                if issue_counts.get(fm_i.knowledge_id, 0) >= MAX_ISSUES_PER_KNOWLEDGE:
                    continue  # запись уже имеет достаточно dup-issues
                # Структурные TOC-секции («Table of Content (part N)») почти идентичны
                # по subject+tags → массовые false-positive дубли. Пропускаем их.
                if _is_toc_section(fm_i) or _is_toc_section(fm_j):
                    continue
                # P0 (A2a): cosine-similarity или теговая эвристика
                is_dup = False
                similarity: float | None = None
                if use_embedding and i in emb_vectors and j in emb_vectors:
                    similarity = compute_cosine(emb_vectors[i], emb_vectors[j])
                    is_dup = similarity >= DUP_SIMILARITY_THRESHOLD
                else:
                    is_dup = _are_dup_candidates(fm_i, fm_j)
                if not is_dup:
                    continue
                dup_count += 1
                issue_counts[fm_i.knowledge_id] = issue_counts.get(fm_i.knowledge_id, 0) + 1
                # R2: dup_map для пост-обработки scoring
                dup_map[fm_i.knowledge_id] = dup_map.get(fm_i.knowledge_id, 0) + 1
                dup_map[fm_j.knowledge_id] = dup_map.get(fm_j.knowledge_id, 0) + 1
                # Создаём issue для дубликата (cosine в detail — сигнал уверенности, P0)
                detail = (
                    f"Possible duplicate of {fm_j.knowledge_id} "
                    f"(same subject={fm_i.subject}, cosine={similarity:.3f})"
                    if similarity is not None
                    else f"Possible duplicate of {fm_j.knowledge_id} "
                         f"(same subject={fm_i.subject}, tag overlap)"
                )
                # Фаза 1 (1b): структурированные сигналы для Review Queue (Фаза 2).
                # metadata НЕ входит в issue_id — идемпотентность сохранена.
                meta_i = (content_meta or {}).get(fm_i.knowledge_id, {})
                meta_j = (content_meta or {}).get(fm_j.knowledge_id, {})
                create_issue(
                    issue_type="duplicate",
                    knowledge_id=fm_i.knowledge_id,
                    severity="warn",
                    detail=detail,
                    metadata={
                        "cosine": round(similarity, 4) if similarity is not None else None,
                        "content_hash": meta_i.get("content_hash"),
                        "content_length": meta_i.get("content_length"),
                        "target_content_hash": meta_j.get("content_hash"),
                        "target_content_length": meta_j.get("content_length"),
                        "slug_negation": has_negation_pattern(
                            fm_i.knowledge_id, fm_j.knowledge_id
                        ),
                        "standalone": fm_i.parent_knowledge_id is None,
                        "target_standalone": fm_j.parent_knowledge_id is None,
                        "subject": fm_i.subject,
                        # Фаза 3 (0a, P1-1): реальное сравнение subject + target kid
                        "target_subject": fm_j.subject,
                        "target_kid": fm_j.knowledge_id,
                    },
                )
    return dup_count, dup_map


def _representative_text(fm: KnowledgeFrontmatter) -> str:
    """Репрезентативный текст записи для embedding-dup (kid + subject + tags).

    KnowledgeFrontmatter не имеет поля title — используем knowledge_id
    (читаемый slug) + subject + tags.
    """
    parts = [fm.knowledge_id or "", fm.subject or ""]
    parts.extend(fm.tags or [])
    return " ".join(p for p in parts if p)


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


def _auto_clear_stale_issues(
    scored: list[tuple[Path, KnowledgeFrontmatter, float]],
) -> int:
    """P0 (A3a): закрыть open missing_field issues записей, чьё условие исчезло.

    Накопленные issues никогда не очищались (Д3): даже после исправления
    записи старый open issue оставался навсегда. Этот шаг переоценивает:
    если запись теперь имеет score < REVIEW_THRESHOLD — закрываем её
    open missing_field issues как resolved (self-healing стор).

    Args:
        scored: список (filepath, frontmatter, score) после scoring.

    Returns:
        int: число закрытых issues.
    """
    # Собираем запись → текущий score
    score_by_kid: dict[str, float] = {}
    for _f, fm, score in scored:
        score_by_kid[fm.knowledge_id] = score

    # Закрываем open missing_field issues записей, больше не проблемных
    to_close: list[str] = []
    for kid, score in score_by_kid.items():
        if score < REVIEW_THRESHOLD:
            open_ids = list_issue_ids(
                types=["missing_field"], status="open", knowledge_id=kid,
            )
            to_close.extend(open_ids)

    if not to_close:
        return 0
    return bulk_update_status(to_close, "resolved", "Auto-cleared: condition no longer holds (quality scan)")


def _empty_result() -> dict:
    return {
        "files_scanned": 0,
        "scores_updated": 0,
        "duplicates_detected": 0,
        "issues_created": 0,
        "review_queue_size": 0,
    }
