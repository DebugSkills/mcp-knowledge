"""Health-проверки MCP Knowledge Server.

Без NiceGUI. Принимает base_url и api_key.
Health-эндпоинты (/health, /health/live, /metrics) — без auth (SKIP_PATHS).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("kb_console.health")


@dataclass
class HealthCheckResult:
    """Результат проверки одного компонента."""

    component: str
    ok: bool
    detail: str = ""


async def get_liveness(base_url: str = "http://localhost:8000") -> dict[str, Any]:
    """GET /health/live — проверка живости процесса.

    Returns:
        {"status": "alive"}
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        r = await client.get(f"{base_url.rstrip('/')}/health/live")
        r.raise_for_status()
        return r.json()


async def get_health(base_url: str = "http://localhost:8000") -> dict[str, Any]:
    """GET /health — глубокая проверка (readiness).

    Returns разобранные checks: список {component, ok, detail}.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        r = await client.get(f"{base_url.rstrip('/')}/health")
        data = r.json()

        checks_raw = data.get("checks", {})
        checks: list[dict[str, Any]] = []
        for component, check in checks_raw.items():
            if isinstance(check, dict):
                checks.append({
                    "component": component,
                    "ok": check.get("ok", False),
                    "detail": check.get("detail", check.get("error", "")),
                })
            else:
                checks.append({
                    "component": component,
                    "ok": bool(check),
                    "detail": str(check),
                })

        return {
            "status": data.get("status", "unknown"),
            "version": data.get("version", ""),
            "checks": checks,
        }


async def get_metrics(base_url: str = "http://localhost:8000") -> dict[str, float]:
    """GET /metrics — Prometheus-метрики.

    Парсит /metrics endpoint и извлекает 5 ключевых метрик:
    - mcp_requests_total / mcp_errors_total — через tool_requests
    - mcp_queue_size
    - mcp_search_latency_seconds (avg)
    - mcp_pipeline_processed_total / mcp_pipeline_failed_total
    - mcp_collection_size
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        r = await client.get(f"{base_url.rstrip('/')}/metrics")
        text = r.text

    result: dict[str, float] = {}

    # Парсим Prometheus text format (простой подход)
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Ищем ключевые метрики
        for key in [
            "mcp_queue_size",
            "mcp_collection_size",
            "mcp_pipeline_processed_total",
            "mcp_pipeline_failed_total",
            "mcp_process_uptime_seconds",
        ]:
            if line.startswith((key + " ", key + "{")):
                try:
                    # metric_name{labels} value
                    parts = line.rsplit(" ", 1)
                    if len(parts) == 2:
                        result[key] = float(parts[1])
                except (ValueError, IndexError):
                    pass

        # search_latency — берём sum и count для avg
        if line.startswith("mcp_search_latency_seconds_sum"):
            try:
                parts = line.rsplit(" ", 1)
                result["mcp_search_latency_seconds_sum"] = float(parts[1])
            except (ValueError, IndexError):
                pass
        if line.startswith("mcp_search_latency_seconds_count"):
            try:
                parts = line.rsplit(" ", 1)
                result["mcp_search_latency_seconds_count"] = float(parts[1])
            except (ValueError, IndexError):
                pass

    # Вычисляем среднюю latency
    total = result.get("mcp_search_latency_seconds_sum", 0)
    count = result.get("mcp_search_latency_seconds_count", 1)
    if count > 0:
        result["mcp_search_latency_seconds_avg"] = round(total / count, 4)

    return result
