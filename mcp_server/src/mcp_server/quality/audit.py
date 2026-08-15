"""Audit-журнал действий по качеству (Фаза 1 dedup, 2026-08-13).

Append-only JSONL журнал: кто и что сделал (deprecate/restore/bulk/merge/auto).
Отдельно от issues.jsonl — другой lifecycle (issues — проблемы, audit — действия).

Паттерн: копия issues.py (threading.Lock + атомарный append через tmp+os.replace),
но журнал append-only: каждое действие — новая строка, никаких мутаций прошлых.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("mcp_knowledge.quality.audit")

_DEFAULT_STORE_DIR: str | None = None


def _get_default_store_dir() -> str:
    global _DEFAULT_STORE_DIR
    if _DEFAULT_STORE_DIR is None:
        try:
            from ..config import settings

            _DEFAULT_STORE_DIR = settings.QUALITY_DIR
        except Exception:  # noqa: BLE001
            _DEFAULT_STORE_DIR = "/app/data/quality"
    return _DEFAULT_STORE_DIR


_store_dir_override: str | None = None
_audit_lock = threading.Lock()

# ── Реестр действий ───────────────────────────────────────────
AUDIT_ACTIONS: frozenset[str] = frozenset({
    "deprecate", "restore", "bulk_deprecate", "merge",
    "auto_deprecate", "resolve_issue",
    # Фаза 3 (1a): история полных сканов + фиксация FP-решений оператора
    "scan_completed", "fp_rejection",
})


def _get_audit_path() -> Path:
    base = _store_dir_override or _get_default_store_dir()
    return Path(base) / "audit.jsonl"


def set_store_dir(path: str) -> None:
    """Переопределить директорию хранилища (для тестов)."""
    global _store_dir_override
    _store_dir_override = path


def get_audit_store_path() -> Path:
    """Получить путь к файлу audit.jsonl."""
    return _get_audit_path()


def write_audit(
    action: str,
    knowledge_id: str,
    actor: str,
    reason: str = "",
    metadata: dict | None = None,
    strict: bool = False,
) -> bool:
    """Записать одно действие в audit.jsonl (append-only, атомарно).

    Args:
        action: Одно из AUDIT_ACTIONS.
        knowledge_id: ID записи, над которой выполнено действие.
        actor: Кто выполнил (operator | operator-batch | auto | system).
        reason: Человекочитаемая причина.
        metadata: Дополнительные структурированные данные (issue_id, count, ...).
        strict: Зарезервирован для API-совместимости (авто-путь передаёт strict=True).
            В обоих режимах ошибка записи возвращает False, НЕ raise.

    Returns:
        True при успешной записи; False при ошибке (никогда не бросает).
    """
    if action not in AUDIT_ACTIONS:
        logger.warning("Unknown audit action '%s' (recorded anyway)", action)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "knowledge_id": knowledge_id,
        "actor": actor,
        "reason": reason,
        "metadata": metadata or {},
    }

    with _audit_lock:
        store_path = _get_audit_path()
        try:
            store_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            # Append-only: прямая дозапись с flush (атомарно для строки < PIPE_BUF).
            # НЕ tmp+replace: replace при append-модели теряет предыдущие записи
            # (tmp пересоздаётся каждый раз).
            with open(store_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write audit record: %s", exc)
            return False
    logger.debug("Audit: %s %s by %s", action, knowledge_id, actor)
    return True


def list_audit(
    actor: str | None = None,
    action: str | None = None,
    knowledge_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Прочитать записи audit.jsonl с фильтрами (последние — первыми).

    Args:
        actor: Фильтр по actor (operator | auto | ...).
        action: Фильтр по действию (deprecate | restore | ...).
        knowledge_id: Фильтр по записи.
        limit: Максимум записей (default 100).

    Returns:
        list[dict]: Записи в порядке «новые сверху».
    """
    store_path = _get_audit_path()
    if not store_path.exists():
        return []

    records: list[dict] = []
    try:
        for line in store_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if actor is not None and rec.get("actor") != actor:
                continue
            if action is not None and rec.get("action") != action:
                continue
            if knowledge_id is not None and rec.get("knowledge_id") != knowledge_id:
                continue
            records.append(rec)
    except OSError as exc:
        logger.error("Failed to read audit log: %s", exc)
        return []

    records.reverse()  # новые сверху
    return records[:limit]


def count_actions(actor: str | None = None, action: str | None = None) -> int:
    """Число записей audit по фильтру (для FP-мониторинга Фазы 3)."""
    return len(list_audit(actor=actor, action=action, limit=10**6))


def get_fp_rate(window_scans: int = 2) -> dict:
    """FP-rate за последние N полных сканов (Фаза 3, 1d).

    Читает audit.jsonl и считает:
    - scans_in_window: число записей scan_completed (полные сканы, не отменённые);
    - rejections: fp_rejection события с ts >= ts(scan_completed[-window_scans]);
    - approved: авто-скрытия (bulk_deprecate + actor="auto") в том же окне.
    fp_free = scans_in_window >= window_scans AND rejections == 0.

    Returns:
        {"scans_in_window", "rejections", "approved", "fp_rate", "fp_free"}
    """
    records = list_audit(limit=10**6)
    records.reverse()  # хронологический порядок (старые → новые)

    scans = [r for r in records if r.get("action") == "scan_completed"]
    if len(scans) < window_scans:
        return {
            "scans_in_window": len(scans),
            "rejections": 0,
            "approved": 0,
            "fp_rate": 0.0,
            "fp_free": False,
        }

    # Окно: от ts предпоследнего из window_scans последних полных сканов до сейчас
    window_start_ts = scans[-window_scans].get("ts", "")

    rejections = 0
    approved = 0
    for rec in records:
        if rec.get("ts", "") < window_start_ts:
            continue
        action = rec.get("action")
        if action == "fp_rejection":
            rejections += 1
        elif action == "bulk_deprecate" and rec.get("actor") == "auto":
            approved += 1

    fp_rate = rejections / (rejections + approved) if (rejections + approved) else 0.0
    return {
        "scans_in_window": len(scans),
        "rejections": rejections,
        "approved": approved,
        "fp_rate": round(fp_rate, 4),
        "fp_free": rejections == 0,
    }
