"""Issue-store — append-only JSONL с атомарной записью (4.1).

Хранилище quality-issues: data/quality/issues.jsonl.
Формат: одна JSON-запись на строку (JSONL).
Гарантии:
- Атомарность: write → *.tmp → os.replace() (atomic на Linux)
- Идемпотентность: issue_id = hash(type, knowledge_id, detail) → дубликаты пропускаются
- Потокобезопасность: threading.Lock сериализует все операции

Зависимости: только stdlib + Pydantic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("mcp_knowledge.quality.issues")

# ── Конфигурация пути хранилища ────────────────────────────

_DEFAULT_STORE_DIR: str | None = None  # Ленивая инициализация из config


def _get_default_store_dir() -> str:
    global _DEFAULT_STORE_DIR
    if _DEFAULT_STORE_DIR is None:
        try:
            from ..config import settings

            _DEFAULT_STORE_DIR = settings.QUALITY_DIR
        except Exception:  # noqa: BLE001
            _DEFAULT_STORE_DIR = "/app/data/quality"
    return _DEFAULT_STORE_DIR


# Глобальное переопределение для тестов
_store_dir_override: str | None = None

# Блокировка для атомарных read-modify-write
_store_lock = threading.Lock()


def _get_store_path() -> Path:
    """Путь к issues.jsonl (с учётом оверрайда для тестов)."""
    base = _store_dir_override or _get_default_store_dir()
    return Path(base) / "issues.jsonl"


def set_store_dir(path: str) -> None:
    """Переопределить директорию хранилища (для тестов)."""
    global _store_dir_override
    _store_dir_override = path


def get_issues_store_path() -> Path:
    """Получить путь к файлу issues.jsonl."""
    return _get_store_path()


# ── Типы ────────────────────────────────────────────────────

IssueType = Literal["duplicate", "missing_field", "edit_war", "broken_link", "conflicting", "orphaned"]
IssueSeverity = Literal["info", "warn", "critical"]
IssueStatus = Literal["open", "resolved", "ignored"]


# ── Pydantic модель ─────────────────────────────────────────


class Issue(BaseModel):
    """Запись о проблеме качества в БЗ."""

    issue_id: str = Field(..., description="Уникальный ID (детерминированный хеш)")
    type: IssueType = Field(..., description="Тип проблемы")
    knowledge_id: str = Field(..., description="ID записи знаний")
    severity: IssueSeverity = Field(..., description="Серьёзность: info | warn | critical")
    detail: str = Field(..., description="Детальное описание проблемы")
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Время обнаружения",
    )
    status: IssueStatus = Field(default="open", description="Статус: open | resolved | ignored")
    resolved_at: datetime | None = Field(default=None, description="Время разрешения")
    resolution: str | None = Field(default=None, description="Описание решения")
    metadata: dict | None = Field(
        default=None,
        description="Структурированные сигналы (Фаза 1 dedup: cosine, content_hash, slug_negation, standalone)",
    )


# ── Helpers ─────────────────────────────────────────────────


def _make_issue_id(issue_type: str, knowledge_id: str, detail: str) -> str:
    """Детерминированный issue_id: SHA256 от (type, knowledge_id, detail)."""
    seed = f"{issue_type}|{knowledge_id}|{detail}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"iss_{digest[:16]}"


def _read_all_issues(store_path: Path) -> list[dict]:
    """Прочитать все записи из JSONL файла."""
    if not store_path.exists():
        return []
    issues = []
    with open(store_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                issues.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Пропущена битая строка в %s: %s", store_path, line[:80])
    return issues


def _write_all_issues(store_path: Path, issues: list[dict]) -> None:
    """Атомарно записать все записи: tmp → os.replace()."""
    store_dir = store_path.parent
    store_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = store_path.with_suffix(".jsonl.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for issue in issues:
                # Сериализуем datetime в ISO строку
                serialized = _serialize_issue(issue)
                f.write(json.dumps(serialized, ensure_ascii=False) + "\n")
        # Атомарная замена на Linux
        os.replace(tmp_path, store_path)
    except Exception:
        # Подчищаем .tmp при ошибке
        if tmp_path.exists():
            os.unlink(tmp_path)
        raise


def _serialize_issue(issue: dict) -> dict:
    """Сериализовать issue-словарь: datetime → ISO-строка."""
    result = dict(issue)
    for key in ("detected_at", "resolved_at"):
        value = result.get(key)
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _issue_from_dict(data: dict) -> Issue:
    """Создать Issue из словаря (с десериализацией ISO-дат)."""
    return Issue(**data)


# ── Public API ──────────────────────────────────────────────


def create_issue(
    issue_type: IssueType,
    knowledge_id: str,
    severity: IssueSeverity,
    detail: str,
    metadata: dict | None = None,
) -> Issue:
    """Создать issue (идемпотентно — дубликаты пропускаются).

    Идемпотентность: issue_id = SHA256(type, knowledge_id, detail).
    Если issue с таким ID уже существует, возвращается существующая запись.
    ВАЖНО (Фаза 1 dedup): metadata НЕ участвует в issue_id — пересчёт
    сигналов (cosine/content_hash) не ломает идемпотентность.

    Атомарность: read → append → write(tmp) → os.replace() под threading.Lock.

    Args:
        issue_type: Тип проблемы (duplicate, missing_field, edit_war, broken_link, conflicting)
        knowledge_id: ID записи знаний
        severity: Серьёзность (info, warn, critical)
        detail: Детальное описание
        metadata: Структурированные сигналы (опционально, Фаза 1 dedup)

    Returns:
        Issue: Созданная (или существующая) запись
    """
    issue_id = _make_issue_id(issue_type, knowledge_id, detail)
    now = datetime.now(timezone.utc)

    with _store_lock:
        store_path = _get_store_path()
        existing = _read_all_issues(store_path)

        # Проверка на дубликат
        for entry in existing:
            if entry.get("issue_id") == issue_id:
                # Фаза 3 (0b): metadata-refresh. metadata НЕ в issue_id — обновление
                # сигналов при пересканe не ломает идемпотентность ID (Фаза 1).
                # Без refresh старые open dup-issues никогда не получают
                # target_subject → застревают в 🟡 навсегда (ловушка Н1).
                if metadata is not None and entry.get("metadata") != metadata:
                    entry["metadata"] = metadata
                    _write_all_issues(store_path, existing)
                    logger.debug("Idempotent skip + metadata refresh: issue %s", issue_id)
                else:
                    logger.debug("Idempotent skip: issue %s already exists", issue_id)
                return _issue_from_dict(entry)

        # Новый issue
        new_issue = Issue(
            issue_id=issue_id,
            type=issue_type,
            knowledge_id=knowledge_id,
            severity=severity,
            detail=detail,
            detected_at=now,
            status="open",
            metadata=metadata,
        )
        existing.append(new_issue.model_dump(mode="json"))
        _write_all_issues(store_path, existing)

        logger.info(
            "Created issue %s: type=%s knowledge_id=%s severity=%s",
            issue_id, issue_type, knowledge_id, severity,
        )
        return new_issue


def list_issues(
    types: list[IssueType] | None = None,
    status: str = "open",
    limit: int = 50,
) -> list[Issue]:
    """Получить список issues с фильтрацией.

    Args:
        types: Фильтр по типам (None = все типы)
        status: Фильтр по статусу (open | resolved | ignored)
        limit: Максимальное количество возвращаемых записей

    Returns:
        list[Issue]: Отфильтрованный список (новые первыми)
    """
    store_path = _get_store_path()
    if not store_path.exists():
        return []

    with _store_lock:
        all_issues = _read_all_issues(store_path)

    result = []
    for entry in reversed(all_issues):  # Новые первыми
        if types is not None and entry.get("type") not in types:
            continue
        if entry.get("status") != status:
            continue
        result.append(_issue_from_dict(entry))
        if limit is not None and len(result) >= limit:
            break

    return result


def count_issues(
    types: list[IssueType] | None = None,
    status: str = "open",
) -> int:
    """Подсчитать число issues с фильтрацией БЕЗ лимита.

    Используется для точного total в list_quality_issues (иначе при
    обрезке лимитом total всегда равен limit для переполненного стора).

    Args:
        types: Фильтр по типам (None = все типы)
        status: Фильтр по статусу (open | resolved | ignored)

    Returns:
        int: Число соответствующих записей.
    """
    store_path = _get_store_path()
    if not store_path.exists():
        return 0

    with _store_lock:
        all_issues = _read_all_issues(store_path)

    count = 0
    for entry in all_issues:
        if types is not None and entry.get("type") not in types:
            continue
        if entry.get("status") != status:
            continue
        count += 1
    return count


def update_issue_status(
    issue_id: str,
    status: IssueStatus,
    resolution: str | None = None,
) -> Issue | None:
    """Обновить статус issue (open → resolved | ignored).

    При статусе resolved или ignored автоматически устанавливается resolved_at.

    Args:
        issue_id: ID проблемы
        status: Новый статус (resolved | ignored)
        resolution: Описание решения (опционально)

    Returns:
        Issue или None если issue_id не найден
    """
    if status not in ("resolved", "ignored"):
        raise ValueError(f"Invalid target status: {status}. Expected 'resolved' or 'ignored'.")

    now = datetime.now(timezone.utc)

    with _store_lock:
        store_path = _get_store_path()
        all_issues = _read_all_issues(store_path)

        updated = None
        for entry in all_issues:
            if entry.get("issue_id") == issue_id:
                entry["status"] = status
                entry["resolved_at"] = now.isoformat()
                if resolution is not None:
                    entry["resolution"] = resolution
                updated = _issue_from_dict(entry)
                break

        if updated is None:
            return None

        _write_all_issues(store_path, all_issues)

    logger.info("Updated issue %s: status=%s", issue_id, status)
    return updated


def bulk_update_status(
    issue_ids: list[str],
    status: IssueStatus,
    resolution: str | None = None,
) -> int:
    """Пакетно обновить статус нескольких issues (Фаза P0 bulk-resolve).

    Итерирует issue_ids и ставит status (reuse логики update_issue_status,
    но пакетно, под одним _store_lock — одно атомарное read-modify-write
    вместо N). Пропускает отсутствующие ID.

    Args:
        issue_ids: список issue_id для обновления.
        status: Новый статус (resolved | ignored).
        resolution: Описание решения (опционально, применяется ко всем).

    Returns:
        int: Число реально обновлённых issues.
    """
    if status not in ("resolved", "ignored"):
        raise ValueError(f"Invalid target status: {status}. Expected 'resolved' or 'ignored'.")

    if not issue_ids:
        return 0

    target: set[str] = set(issue_ids)
    now = datetime.now(timezone.utc)
    updated_count = 0

    with _store_lock:
        store_path = _get_store_path()
        all_issues = _read_all_issues(store_path)

        changed = False
        for entry in all_issues:
            if entry.get("issue_id") in target:
                entry["status"] = status
                entry["resolved_at"] = now.isoformat()
                if resolution is not None:
                    entry["resolution"] = resolution
                updated_count += 1
                changed = True

        if changed:
            _write_all_issues(store_path, all_issues)

    logger.info(
        "Bulk-updated %d issues: status=%s", updated_count, status,
    )
    return updated_count


def list_issue_ids(
    types: list[IssueType] | None = None,
    status: str = "open",
    knowledge_id: str | None = None,
) -> list[str]:
    """Вернуть ВСЕ issue_id по фильтру БЕЗ лимита (Фаза P0 bulk-resolve).

    Переиспользует чтение стора. В отличие от list_issues (limit-пагинация),
    возвращает полный список ID для bulk-операций.

    Args:
        types: Фильтр по типам (None = все типы).
        status: Фильтр по статусу (open | resolved | ignored).
        knowledge_id: Опциональный фильтр по knowledge_id.

    Returns:
        list[str]: Список issue_id (в порядке чтения стора).
    """
    store_path = _get_store_path()
    if not store_path.exists():
        return []

    with _store_lock:
        all_issues = _read_all_issues(store_path)

    result: list[str] = []
    for entry in all_issues:
        if types is not None and entry.get("type") not in types:
            continue
        if entry.get("status") != status:
            continue
        if knowledge_id is not None and entry.get("knowledge_id") != knowledge_id:
            continue
        result.append(entry.get("issue_id", ""))
    return result


def close_all_dup_issues(knowledge_id: str, resolution: str | None = None) -> int:
    """Закрыть ВСЕ open duplicate-issues записи (Фаза 1 dedup).

    После deprecate/merge у записи могут оставаться НЕСКОЛЬКО duplicate-issues
    (от разных dup-пар). Этот хелпер закрывает их все разом — не одну.

    Args:
        knowledge_id: ID записи.
        resolution: описание решения (опционально).

    Returns:
        int: число закрытых issues.
    """
    issue_ids = list_issue_ids(
        types=["duplicate"], status="open", knowledge_id=knowledge_id,
    )
    if not issue_ids:
        return 0
    return bulk_update_status(issue_ids, "resolved", resolution)


def reopen_dup_issues(knowledge_id: str) -> int:
    """Переоткрыть ЗАКРЫТЫЕ duplicate-issues записи (Фаза 3).

    Вызывается при restore: запись снова опубликована → dup-пары снова
    актуальны и должны вернуться в Review Queue (HITL), а cooldown-щит
    (restored_by_operator) защищает её от авто-re-deprecate.

    Без переоткрытия идемпотентный skip create_issue (issue_id =
    SHA256(type, kid, detail)) возвращал бы закрытый issue → скан не
    создавал бы open-issue для восстановленной пары → дубль невидим.

    Args:
        knowledge_id: ID записи.

    Returns:
        int: число переоткрытых issues.
    """
    with _store_lock:
        store_path = _get_store_path()
        all_issues = _read_all_issues(store_path)

        reopened = 0
        for entry in all_issues:
            if (
                entry.get("type") == "duplicate"
                and entry.get("knowledge_id") == knowledge_id
                and entry.get("status") in ("resolved", "ignored")
            ):
                entry["status"] = "open"
                entry.pop("resolved_at", None)
                entry.pop("resolution", None)
                reopened += 1

        if reopened:
            _write_all_issues(store_path, all_issues)

    if reopened:
        logger.info(
            "Reopened %d duplicate issue(s) for restored record %s", reopened, knowledge_id,
        )
    return reopened


# ── Async-safe wrappers (NF-5 fix) ─────────────────────────

async def create_issue_async(
    issue_type: IssueType,
    knowledge_id: str,
    severity: IssueSeverity,
    detail: str,
) -> Issue:
    """Async-safe обёртка create_issue: run_in_executor + threading.Lock."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, create_issue, issue_type, knowledge_id, severity, detail
    )


async def list_issues_async(
    types: list[IssueType] | None = None,
    status: str = "open",
    limit: int = 50,
) -> list[Issue]:
    """Async-safe обёртка list_issues."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, list_issues, types, status, limit)


async def update_issue_status_async(
    issue_id: str,
    status: IssueStatus,
    resolution: str | None = None,
) -> Issue | None:
    """Async-safe обёртка update_issue_status."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, update_issue_status, issue_id, status, resolution
    )
