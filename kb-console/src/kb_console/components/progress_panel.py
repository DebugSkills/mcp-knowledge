"""Переиспользуемый компонент прогресса quality scan (поллинг + progress bar + логи).

Используется на страницах «Качество» (quality.py) и «Поиск» (search.py).
Один активный poll на страницу; скрытие при отсутствии скана — через container.visible = False.

Паттерн:
- Таймер: ui.timer + cleanup on_disconnect (status.py / import_page.py pattern)
- Уровни логов → CSS-классы (переиспользовано из quality.py)
- on_done колбэк — вызывается однократно при done/error (для обновления очереди)

Фаза 13.16: DRY-компонент, извлечённый из quality.py _run_scan / _poll_progress.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from ..core.mcp_client import MCPClient

# Интервал опроса прогресса скана (сек)
SCAN_POLL_INTERVAL = 1.0

# Уровни логов → CSS-классы
_LEVEL_COLORS: dict[str, str] = {
    "info": "text-grey",
    "warning": "text-orange",
    "error": "text-negative",
}


def build_scan_progress(
    client: MCPClient,
    *,
    on_done: Callable[[], Awaitable[None]] | None = None,
    poll_interval: float = SCAN_POLL_INTERVAL,
) -> ui.column:
    """Создать панель прогресса quality scan с авто-поллингом.

    Монтирует в возвращаемый контейнер: заголовок, linear_progress bar,
    контейнер логов. Автоматически скрывается если скан не активен
    (get_scan_progress → None / 404).

    Использование:
        scan_client = MCPClient(...)
        panel = build_scan_progress(client=scan_client, on_done=my_refresh)
        # panel монтируется в текущий NiceGUI-контекст;
        # scan_client закрывается через ui.context.client.on_disconnect

    Args:
        client: MCPClient для опроса GET /quality/scan/progress.
        on_done: Колбэк при завершении скана (done/error). Вызывается
                 однократно. None = без действия.
        poll_interval: Интервал опроса в секундах (default 1.0).

    Returns:
        ui.column с прогресс-панелью (изначально скрыт, container.visible=False).
    """
    container = ui.column().classes("w-full q-mb-md")
    container.visible = False

    _poll_timer: ui.timer | None = None
    _done_called: bool = False

    def _stop_poll() -> None:
        nonlocal _poll_timer
        if _poll_timer is not None:
            _poll_timer.cancel()
            _poll_timer = None

    async def _poll() -> None:
        nonlocal _done_called

        snapshot = await client.get_scan_progress()
        if snapshot is None:
            # Нет активного скана (not_found / 404 / endpoint absent).
            # Скрываем панель, НО не останавливаем poll — скан может
            # начаться позже (например, с вкладки «Качество»), и панель
            # должна появиться автоматически (13.16 UX).
            container.visible = False
            return

        container.visible = True
        container.clear()

        with container:
            status: str = snapshot.get("status", "running")
            phase: str = snapshot.get("phase", "?")
            done_val: int = snapshot.get("imported", 0)
            total_val: int = snapshot.get("total", 0)
            percent: float = (done_val / total_val * 100) if total_val else 0
            is_done: bool = status in ("done", "error")

            # Заголовок: фаза + счётчик + процент
            ui.label(
                f"📊 Скан: {phase}  ·  {done_val}/{total_val} ({percent:.0f}%)"
                + (f"  ·  {status}" if is_done else "")
            ).classes("text-body2")

            # Прогресс-бар
            ui.linear_progress(
                value=(done_val / total_val) if total_val else 0,
            ).props("rounded").classes("w-full")

            # Последние ~5 лог-сообщений (моноширинный, цвет по level)
            msgs: list[dict[str, Any]] = snapshot.get("messages", [])
            if msgs:
                with ui.column().classes("w-full q-mt-xs gap-0"):
                    for m in msgs[-5:]:
                        level: str = m.get("level", "info")
                        color: str = _LEVEL_COLORS.get(level, "text-grey")
                        ui.label(
                            f"[{m.get('t', '')}] {m.get('text', '')}"
                        ).classes(f"text-caption font-mono {color}")

            # Финальные метрики при done/error
            if is_done:
                summary = snapshot.get("summary", {})
                metrics: dict[str, Any] = (
                    summary.get("metrics", {}) if isinstance(summary, dict) else {}
                )
                if metrics:
                    with ui.row().classes("gap-4 q-mt-sm"):
                        ui.label(
                            f"Файлов: {metrics.get('files_scanned', 0)}"
                        ).classes("text-caption")
                        ui.label(
                            f"В очереди: {metrics.get('review_queue_size', 0)}"
                        ).classes("text-caption")
                        ui.label(
                            f"Дублей: {metrics.get('duplicates_detected', 0)}"
                        ).classes("text-caption")
                        ui.label(
                            f"Issues: {metrics.get('issues_created', 0)}"
                        ).classes("text-caption")

                _stop_poll()

                if on_done is not None and not _done_called:
                    _done_called = True
                    await on_done()

    # Запуск таймера
    _poll_timer = ui.timer(poll_interval, _poll)

    # Cleanup при дисконнекте (таймер + MCPClient закрываются вызывающей стороной)
    def _cleanup() -> None:
        _stop_poll()
    ui.context.client.on_disconnect(_cleanup)

    # Первый poll — немедленно (проверить, идёт ли скан прямо сейчас)
    ui.timer(0.0, _poll, once=True)

    return container
