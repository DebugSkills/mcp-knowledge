"""Страница «Статус» — liveness, health-компоненты, метрики, таблица инструментов."""

from __future__ import annotations

import asyncio

from nicegui import ui

from ..components.auth_banner import AuthBanner
from ..config import MCP_API_KEY, MCP_SERVER_URL, REFRESH_SECONDS
from ..core.auth_polling import Backoff, poll_step
from ..core.auth_state import AuthError, TransportError, record_auth_error
from ..core.health import get_health, get_liveness, get_metrics
from ..core.mcp_client import MCPClient


async def _fetch_status_data(client: MCPClient):
    """Собрать все данные для страницы статуса.

    007: AuthError/TransportError НЕ глушатся (пробрасываются странице/
    примитиву); гасятся только прочие ошибки рендер-уровня.
    """
    data = {
        "liveness": None,
        "health": None,
        "metrics": {},
        "tools": [],
    }

    try:
        data["liveness"] = await get_liveness(MCP_SERVER_URL)
    except (AuthError, TransportError):
        raise  # 007: отказ ключа/транспорт — наружу (баннер/бэкофф)
    except Exception as exc:
        data["liveness"] = {"error": str(exc)}

    try:
        data["health"] = await get_health(MCP_SERVER_URL)
    except (AuthError, TransportError):
        raise
    except Exception as exc:
        data["health"] = {"error": str(exc)}

    try:
        data["metrics"] = await get_metrics(MCP_SERVER_URL)
    except (AuthError, TransportError):
        raise
    except Exception:
        pass

    try:
        data["tools"] = await client.tools_list()
    except (AuthError, TransportError):
        raise
    except Exception:
        pass

    return data


def build_status() -> None:
    """Построить страницу «Статус».

    Автообновление через @ui.refreshable: перерисовываются ТОЛЬКО элементы
    внутри refreshable-блока (diff), страница не перезагружается, позиция
    скролла сохраняется — обновление незаметно.
    """

    latest: dict = {}

    # 007: auth-aware refresh — баннер + бэкофф (key_ref="global")
    _refresh_backoff = Backoff(base=REFRESH_SECONDS)
    _refresh_timer: ui.timer | None = None

    @ui.refreshable
    def render_status() -> None:
        _render_status(latest.get("data", {}))

    async def _fetch_step():
        client = MCPClient(base_url=MCP_SERVER_URL, api_key=MCP_API_KEY)
        try:
            return await _fetch_status_data(client)
        finally:
            await client.close()

    async def refresh() -> None:
        try:
            outcome = await poll_step(
                _fetch_step,
                key_ref="global",
                backoff=_refresh_backoff,
                on_auth_blocked=_auth_banner.show,
            )
            if _refresh_timer is not None and _refresh_timer.interval != outcome.interval:
                _refresh_timer.interval = outcome.interval
            if not outcome.skipped:
                latest["data"] = outcome.value
                render_status.refresh()
        except AuthError as exc:
            # 007 P4/R4: перехват СТРОГО выше RuntimeError-ветки; стоп таймера
            # + баннер. TransportError страницей НЕ потребляется (P1-B) —
            # его ест poll_step (бэкофф).
            record_auth_error(exc)
            if _refresh_timer is not None:
                _refresh_timer.cancel()
            _auth_banner.show(exc)
            return
        except RuntimeError as exc:
            if "parent slot" in str(exc):
                # Вкладка скрыта — подавляем, таймер сам остановится.
                return
            raise

    def _restart_refresh_timer() -> None:
        """Ручное возобновление (баннер): перезапустить таймер + обновить."""
        nonlocal _refresh_timer
        _refresh_backoff.reset()
        if _refresh_timer is None:
            _refresh_timer = ui.timer(REFRESH_SECONDS, refresh)
        asyncio.ensure_future(refresh())

    _auth_banner = AuthBanner(key_ref="global", on_resume=_restart_refresh_timer)

    # ── Layout ─────────────────────────────────────────────
    ui.label("Статус MCP Knowledge Server").classes("text-h4 q-mb-md")

    _auth_banner.mount()

    with ui.row().classes("gap-4 items-center"):
        ui.button("🔄 Обновить", on_click=refresh).props("flat")
        ui.label(f"Автообновление: каждые {REFRESH_SECONDS} сек. (элементы, скролл сохраняется)").classes("text-grey")

    render_status()

    # Автообновление (007: через _refresh_timer для стопа/рестарта при 401)
    _refresh_timer = ui.timer(REFRESH_SECONDS, refresh)

    # Первичная загрузка
    ui.timer(0.1, refresh, once=True)

    # Отмена таймера при закрытии/перезагрузке вкладки — иначе
    # RuntimeError: The parent slot of Timer has been deleted (в логах).
    def _cleanup_timers() -> None:
        if _refresh_timer is not None:
            _refresh_timer.cancel()
    ui.context.client.on_disconnect(_cleanup_timers)


def _render_status(data: dict) -> None:
    """Отрисовать собранные данные статуса."""

    # ── Liveness ──────────────────────────────────────────
    liveness = data.get("liveness", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("🔍 Liveness").classes("text-h6")
        if "error" in liveness:
            ui.label(f"❌ Ошибка: {liveness['error']}").classes("text-negative")
        else:
            status_text = liveness.get("status", "?")
            color = "positive" if status_text == "alive" else "negative"
            ui.label(f"Состояние: {status_text}").classes(f"text-{color}")

    # ── Health Checks ─────────────────────────────────────
    health = data.get("health", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("🩺 Health Checks").classes("text-h6")

        if "error" in health:
            ui.label(f"❌ Ошибка: {health['error']}").classes("text-negative")
        else:
            ui.label(
                f"Статус: {health.get('status', '?')} | "
                f"Версия: {health.get('version', '?')}"
            ).classes("text-subtitle1 q-mb-sm")

            checks = health.get("checks", [])
            if checks:
                with ui.row().classes("gap-4"):
                    for check in checks:
                        comp = check.get("component", "?")
                        ok = check.get("ok", False)
                        detail = check.get("detail", "")

                        icon_text = "✅" if ok else "❌"

                        with ui.card().classes("q-pa-sm"):
                            ui.label(f"{icon_text} {comp}").classes("text-subtitle2")
                            if detail:
                                ui.label(str(detail)).classes("text-caption text-grey")
            else:
                ui.label("Нет данных о компонентах").classes("text-grey")

    # ── Ключевые метрики ──────────────────────────────────
    metrics = data.get("metrics", {})
    with ui.card().classes("w-full q-mb-md"):
        ui.label("📊 Ключевые метрики").classes("text-h6")

        if not metrics:
            ui.label("Нет данных").classes("text-grey")
        else:
            metric_labels = {
                "mcp_queue_size": "Размер очереди",
                "mcp_collection_size": "Точек в Qdrant",
                "mcp_pipeline_processed_total": "Обработано пайплайном",
                "mcp_pipeline_failed_total": "Ошибок пайплайна",
                "mcp_search_latency_seconds_avg": "Средняя latency поиска (сек)",
            }
            for key, label in metric_labels.items():
                val = metrics.get(key)
                if val is not None:
                    ui.label(f"{label}: {val}").classes("text-body2")

    # ── Инструменты ───────────────────────────────────────
    tools = data.get("tools", [])
    with ui.card().classes("w-full q-mb-md"):
        ui.label(f"🔧 Инструменты ({len(tools)})").classes("text-h6")

        if not tools:
            ui.label("Нет данных об инструментах").classes("text-grey")
        else:
            columns = [
                {"name": "name", "label": "Имя", "field": "name", "sortable": True, "align": "left"},
                {"name": "description", "label": "Описание", "field": "description", "sortable": False, "align": "left"},
            ]
            rows = [{"name": t["name"], "description": t.get("description", "")} for t in tools]
            ui.table(columns=columns, rows=rows, row_key="name").classes("w-full")
