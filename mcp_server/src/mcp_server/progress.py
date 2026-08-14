# ruff: noqa: BLE001, S110
"""ImportProgressTracker — in-memory progress store for live import tracking.

Фаза 13.9 (Variant A): HTTP-polling progress bar + log panel в kb-console.
Безопасен при WORKERS=1 (сервер hard-pinned в main.py).
Все мутаторы best-effort: никогда не кидают исключений (импорт не должен падать).

Фаза 13.27: опциональная дисковая персистентность (persist_path) — снапшот
состояния пишется в JSON (throttled, atomic), используется для quality scan:
прогресс и статус скана переживают рестарт сервера (recovery в main.py).
"""

from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ImportProgressTracker:
    """In-memory хранилище прогресса импорта с TTL-очисткой.

    Args:
        max_messages: максимальное число лог-сообщений на import_id.
        ttl_seconds: время жизни записи после завершения (get удаляет просроченные).
        persist_path (13.27): путь к JSON-файлу для персистентного снапшота
            состояния (используется для quality scan — прогресс переживает
            рестарт сервера). None = без персистентности.
        persist_every: минимальный интервал между записями на диск (сек);
            done/error пишутся всегда (force).
    """

    def __init__(
        self,
        max_messages: int = 50,
        ttl_seconds: int = 600,
        persist_path: str | os.PathLike | None = None,
        persist_every: float = 2.0,
    ) -> None:
        self._max_messages = max_messages
        self._ttl_seconds = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}
        self._persist_path = Path(persist_path) if persist_path else None
        self._persist_every = max(0.1, persist_every)
        self._last_persist = 0.0

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

    # ── 13.27: Дисковая персистентность (переживает рестарт сервера) ──

    def persist(self, force: bool = False) -> None:
        """Записать снапшот _data на диск (throttled + atomic, best-effort).

        force=True пишет всегда (используется для событий создания/смены
        фазы и терминальных состояний done/error, а также prune).
        Никогда не кидает исключений — персистентность не должна ломать
        скан/импорт.
        """
        if self._persist_path is None:
            return
        now = _time.time()
        if not force and (now - self._last_persist) < self._persist_every:
            return
        try:
            self._last_persist = now
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._persist_path)  # atomic на Linux
        except Exception:
            pass

    def load(self) -> dict[str, dict[str, Any]]:
        """Прочитать персистентный снапшот и влить в _data (best-effort).

        Не перезаписывает живые записи с тем же id (приоритет — память).
        Возвращает копию загруженных записей (для recovery-логики на старте).
        """
        if self._persist_path is None:
            return {}
        try:
            if not self._persist_path.exists():
                return {}
            with open(self._persist_path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {}
            loaded: dict[str, dict[str, Any]] = {}
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                loaded[str(key)] = entry
                if key not in self._data:
                    self._data[key] = entry
            return loaded
        except Exception:
            return {}

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
        # 13.27: start — событие создания записи, пишем ВСЕГДА (force).
        # Не throttle: за start() может идти prune_finished (force-запись),
        # который ставит _last_persist=now → обычный persist() был бы
        # пропущен 2с и новая запись не попала бы на диск (S25 catch).
        self.persist(force=True)

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
            # Фазы меняются редко (≈7 на скан) — пишем сразу, чтобы
            # throttle-пропуск (2с) не терял смену фазы в персистентном файле.
            self.persist(force=True)
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
            self.persist()
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
            self.persist()
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
            self.persist()
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
            self.persist(force=True)
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
            self.persist(force=True)
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
            self.persist(force=True)
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
