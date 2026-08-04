# ruff: noqa: BLE001, S110
"""D1: Prometheus-метрики — /metrics эндпоинт.

Задача 2.13 плана Фазы 2.

Метрики:
- queue_size, dlq_size — состояние системы
- search_latency (p50/p95/p99) — производительность поиска
- embed_backend — GPU/CPU backend
- collection_size — размер Qdrant
- reconcile_* — результаты reconciliation
- index_gen_latency, knowledge_map_latency, tag_search_latency
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger("mcp_knowledge.metrics")

# ── Metric definitions ────────────────────────────────────

# Gauges (текущее состояние)
queue_size = Gauge(
    "mcp_queue_size",
    "Текущий размер очереди индексации",
)
dlq_size = Gauge(
    "mcp_dlq_size",
    "Количество записей в Dead Letter Queue",
)
collection_size = Gauge(
    "mcp_collection_size",
    "Количество точек в Qdrant",
)
embed_backend_info = Gauge(
    "mcp_embed_backend",
    "Тип embedding backend (1=GPU, 0=CPU)",
)

# Counters (накопительные)
reconcile_checked = Counter(
    "mcp_reconcile_checked_total",
    "Проверено записей при reconciliation",
)
reconcile_reindexed = Counter(
    "mcp_reconcile_reindexed_total",
    "Переиндексировано записей при reconciliation",
)
reconcile_skipped = Counter(
    "mcp_reconcile_skipped_total",
    "Пропущено записей при reconciliation (OK)",
)
reconcile_orphans = Counter(
    "mcp_reconcile_deleted_orphans_total",
    "Удалено сирот при reconciliation",
)

# Histograms (latency distribution)
search_latency = Histogram(
    "mcp_search_latency_seconds",
    "Время семантического поиска (сек)",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
tag_search_latency = Histogram(
    "mcp_tag_search_latency_seconds",
    "Время поиска по тегам (сек)",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5],
)
index_gen_latency = Histogram(
    "mcp_index_gen_latency_seconds",
    "Время генерации INDEX.gen.yaml (сек)",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0],
)
map_latency = Histogram(
    "mcp_knowledge_map_latency_seconds",
    "Время get_knowledge_map (сек)",
    buckets=[0.0001, 0.001, 0.005, 0.01, 0.05, 0.1],
)
write_latency = Histogram(
    "mcp_write_latency_seconds",
    "Время write_knowledge (сек)",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0],
)
pipeline_processed = Counter(
    "mcp_pipeline_processed_total",
    "Всего обработано записей пайплайном",
)
pipeline_failed = Counter(
    "mcp_pipeline_failed_total",
    "Всего неудачных попыток индексации",
)
quality_gate_skipped = Counter(
    "mcp_quality_gate_skipped_total",
    "Сколько раз quality-gate (collision check / dup-gate) был пропущен (non-fatal)",
    ["gate", "reason"],
)

# ── Фаза 12: Observability metrics (V2) ─────────────────────

health_check_status = Gauge(
    "mcp_health_check_status",
    "Состояние компонента (1=ok, 0=degraded)",
    ["component"],
)

rate_limit_rejected = Counter(
    "mcp_rate_limit_rejected_total",
    "Отказано из-за rate limit",
    ["key_level"],
)

optimistic_lock_conflicts = Counter(
    "mcp_optimistic_lock_conflicts_total",
    "Конфликтов optimistic locking (VersionConflictError)",
)

process_uptime_seconds = Gauge(
    "mcp_process_uptime_seconds",
    "Время работы процесса с последнего health refresh (сек)",
)

tool_requests = Counter(
    "mcp_tool_requests_total",
    "Всего вызовов MCP tools",
    ["tool", "status"],
)


# ── Helpers ────────────────────────────────────────────────

_process_start_time = time.monotonic()


def update_health_metrics(checks: dict) -> None:
    """Обновить health-метрики из результатов deep checks.

    Args:
        checks: {component: {ok: bool, ...}} — результат _run_deep_checks()
    """
    for component, check in checks.items():
        ok = check.get("ok", False)
        health_check_status.labels(component=component).set(1 if ok else 0)
    process_uptime_seconds.set(time.monotonic() - _process_start_time)


def update_queue_metrics(pipeline) -> None:
    """Обновить метрики очереди из pipeline."""
    try:
        queue_size.set(pipeline._queue.qsize())
    except Exception:
        pass


def update_dlq_metrics(dlq_instance) -> None:
    """Обновить метрики DLQ."""
    try:
        dlq_size.set(dlq_instance.size)
    except Exception:
        pass


def update_collection_metrics(qdrant) -> None:
    """Обновить метрики коллекции Qdrant."""
    try:
        info = qdrant.collection_info()
        collection_size.set(info.get("points_count", 0))
    except Exception:
        pass


def set_embed_backend(backend_name: str) -> None:
    """Установить тип embedding backend."""
    embed_backend_info.set(1 if backend_name == "gpu" else 0)


def record_reconcile_result(result: dict) -> None:
    """Записать результаты reconciliation."""
    reconcile_checked.inc(result.get("checked", 0))
    reconcile_reindexed.inc(result.get("reindexed", 0))
    reconcile_skipped.inc(result.get("skipped", 0))
    reconcile_orphans.inc(result.get("deleted_orphans", 0))


@asynccontextmanager
async def track_search_latency():
    """Контекстный менеджер для трекинга latency поиска."""
    start = time.monotonic()
    try:
        yield
    finally:
        search_latency.observe(time.monotonic() - start)


@asynccontextmanager
async def track_tag_search_latency():
    """Контекстный менеджер для трекинга latency поиска по тегам."""
    start = time.monotonic()
    try:
        yield
    finally:
        tag_search_latency.observe(time.monotonic() - start)


def record_map_latency(elapsed_seconds: float) -> None:
    """Записать latency get_knowledge_map."""
    map_latency.observe(elapsed_seconds)


def record_write_latency(elapsed_seconds: float) -> None:
    """Записать latency write_knowledge."""
    write_latency.observe(elapsed_seconds)


def record_index_gen_latency(elapsed_seconds: float) -> None:
    """Записать latency генерации INDEX."""
    index_gen_latency.observe(elapsed_seconds)


# ── /metrics endpoint handler ──────────────────────────────


async def metrics_endpoint(request: Request) -> Response:
    """GET /metrics — Prometheus exposition endpoint.

    Перед отдачей обновляет динамические метрики из app.state.
    """
    app_state = request.app.state

    # Обновляем динамические метрики
    try:
        update_queue_metrics(app_state.pipeline)
    except Exception:
        pass

    try:
        update_dlq_metrics(app_state.pipeline._dlq)
    except Exception:
        pass

    try:
        update_collection_metrics(app_state.qdrant)
    except Exception:
        pass

    return Response(
        content=generate_latest(),
        media_type="text/plain; charset=utf-8",
    )
