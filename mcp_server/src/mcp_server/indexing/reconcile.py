# ruff: noqa: BLE001, S110
"""C1: Reconciliation при старте — сверка Markdown SSOT ↔ Qdrant.

Задача 2.9 плана Фазы 2.

Flow:
1. Обход knowledge/**/*.md → сравнение updated_at с Qdrant payload
2. Расхождения → доиндексация (через pipeline.reindex_all)
3. Обратная сверка: Qdrant-точки без .md → удаление сирот
4. Фаза 5: parent-child orphan detection (child без parent, collection incomplete)
5. Лог: {checked, reindexed, skipped, deleted_orphans, orphaned_detected}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import settings
from ..storage.markdown_store import MarkdownStore
from ..storage.qdrant_client import QdrantClient
from ..storage.schema import ZONE_PRIVATE, ZONE_PUBLIC, collection_for_zone
from .knowledge_index import KnowledgeIndex
from .pipeline import IndexingPipeline

logger = logging.getLogger("mcp_knowledge.reconcile")

# ── Фаза P0 (cleanup): лимит orphan-issues на коллекцию ─────────
# Книга на 6K+ секций даёт тысячи ложных "missing child" (все children —
# секции ВНУТРИ одного .md, а не отдельные файлы). Cap не ухудшает
# выявление реальных битых коллекций: настоящий потерянный child имеет
# НЕ-общий slug-префикс с parent, поэтому проходит prefix-skip и попадает
# в лимит первым.
MAX_ORPHAN_ISSUES_PER_COLLECTION: int = 5


def _has_common_prefix(parent_id: str, child_id: str) -> bool:
    """True если parent и child делят slug-префикс книги (секция, не потеря).

    Книги импортируются как collection с children[] — секции ВНУТРИ одного
    parent .md. Их knowledge_id разделяют длинный общий slug-префикс
    (например `universal-fpf-specification-...`), а расходятся только в
    хвосте (`-<seq>` / `-<hash>`). Такие «missing child» — 100% false-positive.

    Реализация: общий префикс до первого расхождения; отбрасываем хвостовой
    `-<hash>`/`-<part-N>` сегмент (после последнего `-`); если оставшееся
    ядро ≥ _MIN_COMMON — считаем одной книгой. Реальный потерянный child
    (чужой slug) общего префикса не имеет → НЕ пропускается.
    """
    _MIN_COMMON = 6
    if not parent_id or not child_id:
        return False
    common = 0
    for a, b in zip(parent_id, child_id):
        if a != b:
            break
        common += 1
    prefix = parent_id[:common]
    # Отбрасываем хвостовой seq/hash-сегмент (после последнего '-')
    if "-" in prefix:
        prefix = prefix.rsplit("-", 1)[0]
    return len(prefix) >= _MIN_COMMON


class ReconcileResult:
    """Результат reconciliation."""

    def __init__(self):
        self.checked: int = 0
        self.reindexed: int = 0
        self.skipped: int = 0
        self.deleted_orphans: int = 0
        self.orphaned_detected: int = 0  # Фаза 5: parent-child orphans
        self.errors: list[str] = []
        # 023: режим доиндексации — none|incremental|full|skipped
        self.mode: str = "none"
        # 023-B: число записей с updated_at-дрейфом (fm новее payload Qdrant)
        self.drifted: int = 0

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "reindexed": self.reindexed,
            "skipped": self.skipped,
            "deleted_orphans": self.deleted_orphans,
            "orphaned_detected": self.orphaned_detected,
            "errors": self.errors,
            "mode": self.mode,
            "drifted": self.drifted,
        }


async def reconcile(
    store: MarkdownStore,
    qdrant: QdrantClient,
    pipeline: IndexingPipeline,
    knowledge_index: KnowledgeIndex,
    skip_reindex: bool = False,
    skip_orphan_detection: bool = False,
    # 023 Block C (§7.9(а)): heavy_ops_lock для full-reindex + owner-ручки.
    # Дефолт None ⇒ харнессы T/D (без лока) остаются зелёными без правок.
    get_heavy_lock: Callable[[], asyncio.Lock] | None = None,
    set_lock_owner: Callable[[str | None], None] | None = None,
    get_lock_owner: Callable[[], str | None] | None = None,
) -> dict:
    """Выполнить полную сверку Markdown SSOT ↔ Qdrant при старте.

    Args:
        skip_reindex: True при недоступном embed (degraded-режим) — пропустить
            доиндексацию missing-записей (иначе reindex_all падает на каждом
            файле и блокирует старт сервера / уводит в рестарт-цикл).
        get_heavy_lock: callable, возвращающий текущий ``heavy_ops_lock`` из
            ``app.state`` (ре-рид объекта на момент acquire — recovery 022
            атомно заменяет lock новым объектом). None ⇒ лок не берётся
            (поведение = до Block C; харнессы T/D).
        set_lock_owner / get_lock_owner: рушки маркера владельца лока
            (``heavy_lock_owner``). Протокол compare-and-clear в ``finally``.

    Returns:
        dict с результатами: {checked, reindexed, skipped, deleted_orphans, errors}
    """
    result = ReconcileResult()
    logger.info("🔍 [RECONCILE] starting Markdown↔Qdrant consistency check")

    # ── Шаг 1: Прямая сверка — Markdown → Qdrant ──────────────────
    md_paths = await store.reindex_scan()
    # W2: union knowledge_id по ОБЕИМ зонам (public + private)
    # 023-B: вместо set[str] собираем dict[kid → updated_at_str|None],
    # чтобы в цикле presence детектировать updated_at-дрейф (§7.8).
    qdrant_meta: dict[str, str | None] = {}
    # 2026-09-26: kid → набор зон, где он найден (нужно orphan-удалению:
    # прод-клиент требует явную коллекцию, qdrant_client._require_collection).
    qdrant_zones: dict[str, set[str]] = {}
    for zone in (ZONE_PUBLIC, ZONE_PRIVATE):
        for kid, upd in qdrant.get_knowledge_updated_at(
            collection_name=collection_for_zone(zone)
        ).items():
            qdrant_zones.setdefault(kid, set()).add(zone)
            if kid in qdrant_meta:
                # коллизия ключей между зонами (kid глобально уникален, но
                # легаси-точки возможны) — max по нормализованному datetime
                from ..storage.qdrant_client import _max_updated_at
                qdrant_meta[kid] = _max_updated_at(qdrant_meta[kid], upd)
            else:
                qdrant_meta[kid] = upd

    missing_in_qdrant: list[Path] = []
    drifted_paths: list[Path] = []

    for path in md_paths:
        result.checked += 1
        try:
            entry = store._parse_file(path)
            kid = entry.frontmatter.knowledge_id

            if kid not in qdrant_meta:
                # Запись есть в Markdown, но отсутствует в Qdrant
                missing_in_qdrant.append(path)
                logger.info("[RECONCILE] %s not in Qdrant — will reindex", kid)
            else:
                # 023-B: запись присутствует — проверяем updated_at-дрейф
                # (комментарий «Проверяем updated_at» наконец становится правдой).
                fm_updated = entry.frontmatter.updated_at
                payload_updated = qdrant_meta[kid]
                if _is_drifted(
                    fm_updated, payload_updated,
                    getattr(entry.frontmatter, "updated_at_explicit", True),
                ):
                    drifted_paths.append(path)
                    result.drifted += 1
                    logger.info(
                        "[RECONCILE] %s updated_at drift (fm=%s > payload=%s) — will reindex",
                        kid,
                        fm_updated.isoformat() if fm_updated else None,
                        payload_updated,
                    )
                else:
                    result.skipped += 1
        except Exception as e:
            msg = f"Failed to parse {path}: {e}"
            result.errors.append(msg)
            logger.warning("[RECONCILE] %s", msg)

    # 023-B: общий бюджет missing + drifted (cost-driver один — число
    # переэмбедденных записей; раздельные пороги удваивают конфигурацию).
    combined_reindex_paths = missing_in_qdrant + drifted_paths

    # Доиндексация отсутствующих + дрейфнувших
    if combined_reindex_paths:
        if skip_reindex:
            # Degraded (Ollama недоступна): не блокируем старт reindex-циклом —
            # файлы доиндексируются после запуска Ollama (ленивый retry embed
            # или следующий старт сервера). drifted тоже пропускается.
            logger.warning(
                "[RECONCILE] %d entries (missing=%d, drifted=%d) — reindex SKIPPED "
                "(embedding недоступна, degraded-режим)",
                len(combined_reindex_paths),
                len(missing_in_qdrant),
                len(drifted_paths),
            )
            result.skipped += len(combined_reindex_paths)
            result.mode = "skipped"
        else:
            # 023: гибрид с порогом — len(combined) <= K → incremental upsert
            # через alias (без blue-green); иначе полный reindex_all.
            # K <= 0 → kill-switch: всегда полный (поведение = до 023).
            k = settings.RECONCILE_INCREMENTAL_MAX_ENTRIES
            if k > 0 and len(combined_reindex_paths) <= k:
                logger.info(
                    "[RECONCILE] %d entries (missing=%d, drifted=%d) — incremental reindex (<=K=%d)",
                    len(combined_reindex_paths),
                    len(missing_in_qdrant),
                    len(drifted_paths),
                    k,
                )
                try:
                    inc = await pipeline.index_missing(combined_reindex_paths)
                    result.reindexed = inc.get("total_docs", 0)
                    result.mode = "incremental"
                    result.errors.extend(inc.get("errors", []))
                except Exception as e:
                    msg = f"Incremental reindex failed: {e}"
                    result.errors.append(msg)
                    logger.error("[RECONCILE] %s", msg)
            else:
                logger.info(
                    "[RECONCILE] %d entries (missing=%d, drifted=%d) — full reindex (K=%d)",
                    len(combined_reindex_paths),
                    len(missing_in_qdrant),
                    len(drifted_paths),
                    k,
                )
                # 023 Block C (§7.9(а)): full-reindex берёт heavy_ops_lock —
                # bounded wait + identity-check (P1-3) + compare-and-clear owner.
                # Без get_heavy_lock (харнессы T/D, дефолт) — прежнее поведение
                # без лока (гонка R5 остаётся known-gap, закрывается только при
                # передаче лока из main.py).
                proceed = True
                lock_obj: asyncio.Lock | None = None
                lock_acquired = False
                if get_heavy_lock is not None:
                    lock_obj = get_heavy_lock()
                    try:
                        await asyncio.wait_for(
                            lock_obj.acquire(),
                            timeout=settings.RECONCILE_LOCK_WAIT_SECONDS,
                        )
                        lock_acquired = True
                    except asyncio.TimeoutError:
                        # (б) Лок занят дольше таймаута → defer на следующий
                        # старт (missing остаются → естественный retry).
                        proceed = False
                        result.mode = "deferred"
                        logger.warning(
                            "[RECONCILE] heavy_ops_lock busy >%ds — "
                            "full reindex deferred to next start",
                            settings.RECONCILE_LOCK_WAIT_SECONDS,
                        )
                    if lock_acquired:
                        # P1-3: identity-check — лок могли заменить recovery-путём
                        # 022, пока мы ждали; acquire резолвится на СТАРОМ объекте.
                        if lock_obj is not get_heavy_lock():
                            lock_obj.release()
                            lock_acquired = False
                            proceed = False
                            result.mode = "deferred"
                            logger.warning(
                                "[RECONCILE] heavy lock replaced during wait — "
                                "full reindex deferred"
                            )
                        else:
                            # (г) set owner ПОСЛЕ identity-check (не до — иначе
                            # маркер фантомного лока искажает диагностику guard'а).
                            if set_lock_owner is not None:
                                set_lock_owner("reconcile")
                if proceed:
                    try:
                        reindex_result = await pipeline.reindex_all()
                        result.reindexed = reindex_result.get(
                            "total_docs", len(combined_reindex_paths)
                        )
                        result.mode = "full"
                    except Exception as e:
                        msg = f"Reindex failed: {e}"
                        result.errors.append(msg)
                        logger.error("[RECONCILE] %s", msg)
                    finally:
                        # (г) compare-and-clear — не затереть чужой маркер.
                        if lock_acquired:
                            if (
                                get_lock_owner is not None
                                and get_lock_owner() == "reconcile"
                            ):
                                if set_lock_owner is not None:
                                    set_lock_owner(None)
                            lock_obj.release()

    # ── Шаг 2: Обратная сверка — Qdrant → Markdown ──────────────────
    md_ids = set()
    for path in md_paths:
        try:
            entry = store._parse_file(path)
            md_ids.add(entry.frontmatter.knowledge_id)
        except Exception:
            pass

    # 023-B (P2-5): orphan-сверка по qdrant_meta.keys() — dict в set-вычитании
    # дал бы TypeError/пустой orphan-список (регресс D10).
    orphan_ids = set(qdrant_meta.keys()) - md_ids
    if orphan_ids:
        logger.info("[RECONCILE] %d orphan points in Qdrant — deleting", len(orphan_ids))
        loop = asyncio.get_running_loop()
        for kid in orphan_ids:
            try:
                # Зональный контракт (live-дефект 2026-09-26): коллекция
                # обязательна; зона известна из scroll-обхода (legacy-коллизии —
                # удаляем во всех зонах, где видели kid; no-op безопасен).
                for zone in sorted(qdrant_zones.get(kid) or {ZONE_PRIVATE}):
                    await loop.run_in_executor(
                        None, qdrant.delete_by_knowledge_id, kid,
                        collection_for_zone(zone),
                    )
                result.deleted_orphans += 1
            except Exception as e:
                msg = f"Failed to delete orphan {kid}: {e}"
                result.errors.append(msg)
                logger.warning("[RECONCILE] %s", msg)

    # ── Шаг 3: Перестройка INDEX.gen.yaml ───────────────────────────
    try:
        knowledge_index.rebuild_all()
        logger.info("[RECONCILE] INDEX.gen.yaml rebuilt")
    except Exception as e:
        msg = f"INDEX rebuild failed: {e}"
        result.errors.append(msg)
        logger.warning("[RECONCILE] %s", msg)

    # ── Шаг 4: Parent-child orphan detection (Фаза 5) ────────────────
    if skip_reindex or skip_orphan_detection:
        # Degraded: children не в Qdrant (embed недоступен) → тысячи ложных
        # "orphaned" issues + минуты старта. Диагностика имеет смысл только
        # при полной индексации.
        # Ложные срабатывания: children коллекции-книги — секции ВНУТРИ
        # одного .md (не отдельные файлы) → проверка по MD-файлам даёт
        # тысячи ложных missing child (инцидент 2026-08-06, P1: проверка
        # по Qdrant scroll).
        logger.warning(
            "[RECONCILE] parent-child orphan detection SKIPPED "
            "(skip_reindex=%s, skip_orphan_detection=%s)",
            skip_reindex, skip_orphan_detection,
        )
    else:
        await _detect_parent_child_orphans(store, md_paths, result)

    summary = result.to_dict()
    logger.info(
        "✅ RECONCILE complete: checked=%d, reindexed=%d, skipped=%d, "
        "drifted=%d, orphans=%d, orphaned_detected=%d, errors=%d, mode=%s",
        result.checked, result.reindexed, result.skipped,
        result.drifted,
        result.deleted_orphans, result.orphaned_detected, len(result.errors),
        result.mode,
    )
    # 023 (P1-1): запись метрики внутри reconcile() — call-site тестируем
    # прямым вызовом reconcile() (main-обёртка _run_reconcile вложена в lifespan
    # и нетестируема). Чинит мёртвую метрику mcp_reconcile_reindexed_total.
    try:
        from ..metrics import record_reconcile_result
        record_reconcile_result(summary)
    except Exception:  # noqa: BLE001, S110
        # Метрика — best-effort: сбой логирования не должен валить reconcile.
        pass
    return summary


async def _detect_parent_child_orphans(
    store: MarkdownStore,
    md_paths: list[Path],
    result: ReconcileResult,
) -> None:
    """Фаза 5 §6.5: обнаружение parent-child orphan-записей.

    - Child с parent_knowledge_id, где parent отсутствует → issue "orphaned"
    - Collection с incomplete children (child из children[] удалён) → issue "orphaned"
    """
    try:
        from mcp_server.quality.issues import create_issue_async
    except ImportError:
        logger.warning("[RECONCILE] create_issue_async not available — skip orphan detection")
        return

    # Парсим все .md и строим карту knowledge_id → frontmatter
    entries: dict[str, dict] = {}
    for path in md_paths:
        try:
            entry = store._parse_file(path)
            fm = entry.frontmatter
            kid = fm.knowledge_id
            entries[kid] = {
                "parent_knowledge_id": getattr(fm, "parent_knowledge_id", None),
                "content_type": getattr(fm, "content_type", None),
                "children": getattr(fm, "children", None),
            }
        except Exception:
            pass

    # Проверка 1: child без parent
    for kid, info in entries.items():
        parent_id = info.get("parent_knowledge_id")
        if parent_id and parent_id not in entries:
            logger.warning("[RECONCILE] orphan child %s (parent %s not found)", kid, parent_id)
            try:
                await create_issue_async(
                    "orphaned",
                    kid,
                    "warn",
                    f"Parent '{parent_id}' not found — child is orphaned",
                )
            except Exception as e:
                logger.debug("[RECONCILE] failed to create orphan issue for %s: %s", kid, e)
            result.orphaned_detected += 1

    # Проверка 2: collection с incomplete children
    for kid, info in entries.items():
        if info.get("content_type") != "collection":
            continue
        children = info.get("children")
        if not children:
            continue
        orphan_issues_created = 0
        for child_ref in children:
            if isinstance(child_ref, dict):
                child_id = child_ref.get("knowledge_id")
                if child_id and child_id not in entries:
                    # Prefix-skip: секция той же книги (общий slug-префикс) —
                    # не потерянная запись (инцидент orphan-flood 2026-08-12).
                    if _has_common_prefix(kid, child_id):
                        logger.debug(
                            "[RECONCILE] skip missing child %s of %s (same-book section)",
                            child_id, kid,
                        )
                        continue
                    # Cap: не более MAX_ORPHAN_ISSUES_PER_COLLECTION на коллекцию
                    if orphan_issues_created >= MAX_ORPHAN_ISSUES_PER_COLLECTION:
                        logger.warning(
                            "[RECONCILE] collection %s has >%d missing children — "
                            "orphan issues capped",
                            kid, MAX_ORPHAN_ISSUES_PER_COLLECTION,
                        )
                        break
                    logger.warning(
                        "[RECONCILE] collection %s has missing child %s", kid, child_id
                    )
                    try:
                        await create_issue_async(
                            "orphaned",
                            kid,
                            "warn",
                            f"Collection has missing child: {child_id}",
                        )
                    except Exception as e:
                        logger.debug(
                            "[RECONCILE] failed to create orphan issue for %s: %s", kid, e
                        )
                    orphan_issues_created += 1
                    result.orphaned_detected += 1

    if result.orphaned_detected > 0:
        logger.info("[RECONCILE] %d parent-child orphan issues detected", result.orphaned_detected)


# ── 023-B: drift-detection helper ───────────────────────────


def _is_drifted(
    fm_updated: datetime,
    payload_updated: str | None,
    fm_explicit: bool = True,
) -> bool:
    """023-B + 024: обнаружить updated_at-дрейф между frontmatter и Qdrant payload.

    Вердикт (порядок проверок существенен — P2-1 Critic 024):
    - payload None/невалидный ⇒ drifted (**порча точки** — проверяется ДО
      явности, иначе fieldless+порча маскируется как «нет сигнала»).
    - `fm_explicit=False` (024: поля `updated_at` не было в исходном YAML ⇒
      парсер подставил default_factory=now) ⇒ **не drifted**: сигнала о
      свежести нет, а `now` — не свежесть (иначе ложный дрейф на каждом
      проходе). Граница документирована в каноне §13.16/§13.17.
    - fm > payload (по нормализованному datetime) ⇒ drifted.
    - fm == payload или payload новее (clock skew) ⇒ не drifted
      (fail-closed к «не трогать»).

    tz-нормализация (P2-1): naive → replace(timezone.utc) для ОБЕИХ сторон —
    `fromisoformat` парсит naive-строку, а сравнение naive↔aware даёт
    TypeError (шумный fail-open). Live-корпус сегодня весь `+00:00`, но
    контракт не должен на этом полагаться (D8).

    Helper вынесен из `reconcile()` чтобы держать CC ≤10 (P3-e, Critic iter2);
    тестируется напрямую (D2/D6/D8/D9).
    """
    from ..storage.qdrant_client import _normalize_dt

    payload_dt = _normalize_dt(payload_updated)
    if payload_dt is None:
        # payload без/с невалидным updated_at у присутствующего kid ⇒ порча
        # (проверяется ДО явности — P2-1 Critic 024: порча не маскируется).
        return True

    if not fm_explicit:
        # 024: поля updated_at не было в исходном YAML ⇒ парсер подставил now
        # (default_factory). Это не сигнал свежести ⇒ дрейф не выводим.
        return False

    fm_dt = fm_updated
    if fm_dt.tzinfo is None:
        fm_dt = fm_dt.replace(tzinfo=timezone.utc)

    return fm_dt > payload_dt
