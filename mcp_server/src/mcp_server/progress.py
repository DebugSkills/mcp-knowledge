# ruff: noqa: BLE001, S110
"""ImportProgressTracker — in-memory progress store for live import tracking.

Фаза 13.9 (Variant A): HTTP-polling progress bar + log panel в kb-console.
Безопасен при WORKERS=1 (сервер hard-pinned в main.py).
Все мутаторы best-effort: никогда не кидают исключений (импорт не должен падать).
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any


class ImportProgressTracker:
    """In-memory хранилище прогресса импорта с TTL-очисткой.

    Args:
        max_messages: максимальное число лог-сообщений на import_id.
        ttl_seconds: время жизни записи после завершения (get удаляет просроченные).
    """

    def __init__(self, max_messages: int = 50, ttl_seconds: int = 600) -> None:
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}

    # ── Helpers ─────────────────────────────────────────────

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _now_time(self) -> str:
        # tz-aware локальное время (DTZ005), отображается как "HH:MM:SS"
        return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")

    def _ensure(self, import_id: str) -> dict[str, Any] | None:
        """Получить запись или None (best-effort: не падает)."""
        if not isinstance(import_id, str) or import_id not in self._data:
            return None
        return self._data[import_id]

    def _touch(self, entry: dict[str, Any]) -> None:
        entry["updated_at"] = self._now_iso()

    # ── Public API ──────────────────────────────────────────

    def start(self, import_id: Any, total: int, meta: dict[str, Any] | None = None) -> None:
        """Создать запись прогресса для нового импорта.

        Args:
            import_id: уникальный идентификатор (str или None для best-effort skip).
            total: общее число секций.
            meta: опциональные метаданные (file, size, ...).
        """
        if not isinstance(import_id, str):
            return
        now = self._now_iso()
        self._data[import_id] = {
            "import_id": import_id,
            "status": "running",
            "phase": "decomposing",
            "imported": 0,
            "total": total,
            "failed": 0,
            "messages": [],
            "started_at": now,
            "updated_at": now,
            "meta": meta or {},
        }

    def set_phase(self, import_id: str, phase: str, text: str | None = None) -> None:
        """Обновить фазу импорта и опционально залогировать."""
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["phase"] = phase
            self._touch(entry)
            if text:
                self.log(import_id, "info", text)
        except Exception:
            pass

    def log(self, import_id: str, level: str, text: str) -> None:
        """Добавить лог-сообщение (реальные строки из logger).

        Messages ограничены max_messages — newest last.
        """
        if not isinstance(import_id, str):
            return
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["messages"].append({
                "t": self._now_time(),
                "level": level,
                "text": text,
            })
            # Обрезаем до max_messages (сохраняем newest)
            if len(entry["messages"]) > self._max_messages:
                entry["messages"] = entry["messages"][-self._max_messages:]
            self._touch(entry)
        except Exception:
            pass

    def section_done(self, import_id: str, sequence: int, title: str) -> None:
        """Зафиксировать успешную запись секции."""
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["imported"] += 1
            self._touch(entry)
        except Exception:
            pass

    def section_failed(self, import_id: str, sequence: int, title: str, error: str) -> None:
        """Зафиксировать ошибку записи секции."""
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["failed"] += 1
            self.log(import_id, "warning", f"Section #{sequence} «{title}» failed: {error}")
            self._touch(entry)
        except Exception:
            pass

    def done(self, import_id: str, summary: dict[str, Any] | None = None) -> None:
        """Пометить импорт как завершённый."""
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["status"] = "done"
            if summary:
                entry["summary"] = summary
            self._touch(entry)
        except Exception:
            pass

    # ── 13.19: Prune finished entries ──────────────────────

    def prune_finished(self, keep_id: str | None = None) -> int:
        """Удалить все записи со статусом done/error, кроме keep_id.

        Best-effort: никогда не кидает исключений.
        Возвращает число удалённых записей.

        Args:
            keep_id: если передан, запись с этим import_id не удаляется.
        """
        try:
            to_remove = [
                import_id
                for import_id, entry in self._data.items()
                if isinstance(entry, dict)
                and entry.get("status") in ("done", "error")
                and import_id != keep_id
            ]
            for import_id in to_remove:
                del self._data[import_id]
            return len(to_remove)
        except Exception:
            return 0

    def error(self, import_id: str, error: str) -> None:
        """Пометить импорт как ошибочный."""
        entry = self._ensure(import_id)
        if entry is None:
            return
        try:
            entry["status"] = "error"
            self.log(import_id, "error", error)
            self._touch(entry)
        except Exception:
            pass

    def get(self, import_id: Any) -> dict[str, Any] | None:
        """Получить снапшот прогресса (копия, с TTL-очисткой).

        Returns:
            Копия словаря или None если import_id не найден или просрочен.
        """
        if not isinstance(import_id, str) or import_id not in self._data:
            return None

        try:
            entry = self._data[import_id]
            # TTL prune: удаляем ТОЛЬКО записи со status in ("done", "error").
            # running-записи НЕ удаляем по TTL — даже если фаза застряла
            # (останутся, пока не перезапишутся новым сканом).
            # Активные импорты постоянно трогают updated_at (на каждой секции/батче),
            # поэтому не старше ttl — никогда не удаляются живьём. Удаляются только
            # зависшие или завершённые (после ttl после последнего обновления).
            status = entry.get("status", "running")
            if status in ("done", "error"):
                now_ts = _time.time()
                updated_at = entry.get("updated_at", "")
                if updated_at:
                    try:
                        updated_dt = datetime.fromisoformat(updated_at)
                        age = now_ts - updated_dt.timestamp()
                        if age > self._ttl_seconds:
                            del self._data[import_id]
                            return None
                    except (ValueError, OSError):
                        pass  # невалидный timestamp — не удаляем

            # Возвращаем копию (snapshot, не ссылку)
            return dict(entry)
        except Exception:
            return None
