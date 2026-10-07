"""Ф6 TODO 4б (К4): GET /metrics ws-контура — Prometheus exposition + данные дашборда.

Читает ws-redis (``core.redis_client``; консоль НЕ импортирует
``ai_workspace`` — прецедент Ф2/Ф4.4b). Контракт ключей — дубль SSOT
``ai_workspace/scheduler/metrics.py`` (~6 строк, покрыто тестами формы):

- ``ws:metrics:node:{kind}:{model_class}:{shelf}:{role}`` — HASH
  ``{calls, cached, tokens}`` (лейблы уже санитизированы продюсером);
- ``ws:metrics:usage_fallback_total`` — HASH, поле ``count`` (fallback
  chars/4; ключа нет → честный 0, scrape-стабильность);
- Gauges: ``ZCARD ws:q:{shelf}``, ``SCARD ws:slots:{shelf}``, ключи
  ``ws:lease:{shelf}:*`` (активные lease, TTL-ключи сами исчезают).
  Полки local/ext — всегда; gpu — только при наличии ``ws:q:gpu``
  (конвенция «не выдумываем», как ``pages/queue.py``).

Носитель К4: счётчики живут в Redis ⇒ переживают рестарт консоли;
процесс консоли состояние в памяти НЕ дублирует.

/metrics — auth-exempt (метод-специфичный allowlist GET в
``auth.py``, как POST /api/access-request): Prometheus-scrape без
кредов. Сбой ws-redis НЕ роняет scrape: 200 + комментарий с КЛАССОМ
ошибки (str redis-исключений несёт host:port — гигиена
core/redis_client.py). Синхронные redis-вызовы — в executor
(паттерн mcp_server/metrics.py:metrics_endpoint).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from .core.redis_client import get_ws_redis

__all__ = [
    "BASE_SHELVES",
    "GPU_SHELF",
    "METRICS_PREFIX",
    "METRICS_ROUTE",
    "NODE_PREFIX",
    "USAGE_FALLBACK_KEY",
    "build_metrics_exposition",
    "fetch_ws_metrics_snapshot",
    "make_metrics_handler",
    "parse_node_key",
    "register_metrics_route",
]

METRICS_ROUTE = "/metrics"

# ── Контракт ключей ws-контура (SSOT — ai_workspace/scheduler) ──────────

METRICS_PREFIX = "ws:metrics:"
NODE_PREFIX = "ws:metrics:node:"
USAGE_FALLBACK_KEY = "ws:metrics:usage_fallback_total"

BASE_SHELVES: tuple[str, ...] = ("local", "ext")
GPU_SHELF = "gpu"

_NODE_LABELS: tuple[str, ...] = ("kind", "model_class", "shelf", "role")
"""Фиксированный порядок лейблов node-серий (cardinality bounded, план REV.2)."""


def q_key(shelf: str) -> str:
    return f"ws:q:{shelf}"


def slots_key(shelf: str) -> str:
    return f"ws:slots:{shelf}"


def parse_node_key(key: str) -> dict[str, str] | None:
    """``ws:metrics:node:{kind}:{model_class}:{shelf}:{role}`` → лейблы.

    Чужая арность (не 4 сегмента) → ``None`` — ключ пропускается, snapshot
    не падает на мусоре (read-хелпер не падает на чужих данных).
    """
    if not key.startswith(NODE_PREFIX):
        return None
    parts = key[len(NODE_PREFIX) :].split(":")
    if len(parts) != len(_NODE_LABELS):
        return None
    return dict(zip(_NODE_LABELS, parts))


def _to_int(raw: Any) -> int:
    """Коэрсия значения HASH → int; мусор/None → 0 (счётчик честно нулевой)."""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def _shelf_gauges(client: Any) -> dict[str, dict[str, int]]:
    """Gauges полок: queue_depth (ZCARD), slots_busy (SCARD), leases_active
    (число TTL-ключей ws:lease:{shelf}:*). local/ext всегда, gpu — только
    при наличии ws:q:gpu (не выдумываем полку)."""
    shelves = list(BASE_SHELVES)
    if client.exists(q_key(GPU_SHELF)):
        shelves.append(GPU_SHELF)

    leases: dict[str, int] = {}
    for key in client.scan_iter(match="ws:lease:*"):
        parts = str(key).split(":")
        if len(parts) >= 3 and parts[2]:
            leases[parts[2]] = leases.get(parts[2], 0) + 1

    gauges: dict[str, dict[str, int]] = {
        "queue_depth": {},
        "slots_busy": {},
        "leases_active": {},
    }
    for shelf in shelves:
        gauges["queue_depth"][shelf] = _to_int(client.zcard(q_key(shelf)))
        gauges["slots_busy"][shelf] = _to_int(client.scard(slots_key(shelf)))
        gauges["leases_active"][shelf] = leases.get(shelf, 0)
    return gauges


def fetch_ws_metrics_snapshot(client: Any) -> dict[str, Any]:
    """Собрать метрики ws-контура (чистое ядро, duck-typed клиент).

    Один SCAN ``ws:metrics:*``: node-HASH-и → список узлов с лейблами и
    счётчиками (неинкрементированные поля → 0), ``usage_fallback_total``
    → int (ключа нет → 0). Плюс Gauges полок. Сетевые сбои — НАРУЖУ
    исключением (хендлер/дашборд ловят и деградируют).
    """
    nodes: list[dict[str, Any]] = []
    usage_fallback = 0
    for key in sorted(str(k) for k in client.scan_iter(match=f"{METRICS_PREFIX}*")):
        if key == USAGE_FALLBACK_KEY:
            usage_fallback = _to_int(client.hget(key, "count"))
        elif key.startswith(NODE_PREFIX):
            labels = parse_node_key(key)
            if labels is None:
                continue
            fields = client.hgetall(key) or {}
            nodes.append(
                {
                    **labels,
                    "calls": _to_int(fields.get("calls")),
                    "cached": _to_int(fields.get("cached")),
                    "tokens": _to_int(fields.get("tokens")),
                }
            )
    return {
        "ok": True,
        "nodes": nodes,
        "usage_fallback": usage_fallback,
        "gauges": _shelf_gauges(client),
    }


# ── Prometheus exposition (text format 0.0.4) ───────────────────────────


def _escape_label(value: str) -> str:
    """Экранирование лейбл-значений (``\\``, ``"``, newline). Продюсер
    санитизирует ключи, но exposition строится из чужих данных — защита
    формата обязательна."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_str(labels: dict[str, Any]) -> str:
    inner = ",".join(
        f'{name}="{_escape_label(str(labels[name]))}"' for name in _NODE_LABELS
    )
    return "{" + inner + "}"


_NODE_SERIES: tuple[tuple[str, str, str], ...] = (
    ("ws_node_calls", "calls", "Вызовы узлов движка (node-события on_node_usage)"),
    ("ws_node_cached", "cached", "Попадания узлов в кэш ответов"),
    ("ws_node_tokens", "tokens", "Токены, потреблённые узлами"),
)

_GAUGE_SERIES: tuple[tuple[str, str, str], ...] = (
    (
        "ws_queue_depth",
        "queue_depth",
        "Ожидающих вызовов на полке (ZCARD ws:q)",
    ),
    ("ws_slots_busy", "slots_busy", "Занятых слотов полки (SCARD ws:slots)"),
    (
        "ws_leases_active",
        "leases_active",
        "Активных lease воркеров полки (ключи ws:lease)",
    ),
)


def build_metrics_exposition(client: Any) -> str:
    """Полный exposition-текст /metrics по одному snapshot-проходу."""
    snap = fetch_ws_metrics_snapshot(client)
    lines: list[str] = []
    for name, field, help_ in _NODE_SERIES:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} counter")
        for node in snap["nodes"]:
            lines.append(f"{name}{_labels_str(node)} {node[field]}")
    lines.append(
        "# HELP ws_usage_fallback_total "
        "Оценок токенов fallback-ом chars/4 (usage шлюза недоступен)"
    )
    lines.append("# TYPE ws_usage_fallback_total counter")
    lines.append(f"ws_usage_fallback_total {snap['usage_fallback']}")
    gauges = snap["gauges"]
    for name, key, help_ in _GAUGE_SERIES:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for shelf, value in sorted(gauges[key].items()):
            lines.append(f'{name}{{shelf="{_escape_label(shelf)}"}} {value}')
    return "\n".join(lines) + "\n"


# ── Endpoint + регистрация (паттерн documents_proxy.register_*) ─────────


def _plain(text: str) -> Response:
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def make_metrics_handler(
    client_factory: Callable[[], Any] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """Собрать хендлер ``GET /metrics`` (инжекция фабрики клиента — тесты).

    Прод: ``get_ws_redis`` (singleton, лениво; WS_REDIS_URL нет →
    RuntimeError). Синхронные redis-вызовы — в executor. Любой сбой →
    200 с комментарием-классом ошибки: scrape жив, секреты не утекают.
    """
    factory = client_factory if client_factory is not None else get_ws_redis

    async def metrics_endpoint(request: Request) -> Response:
        try:
            client = factory()
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, build_metrics_exposition, client)
        except Exception as exc:  # noqa: BLE001 — scrape деградирует комментарием
            return _plain(f"# ws-redis unavailable ({type(exc).__name__})\n")
        return _plain(text)

    return metrics_endpoint


def register_metrics_route(nicegui_app: Any, **handler_kwargs: Any) -> None:
    """Зарегистрировать ``GET /metrics`` на NiceGUI(FastAPI)-app.

    Вызывается из app.py на module-level (паттерн documents_proxy);
    анонимный доступ — метод-специфичный allowlist в ConsoleAuthMiddleware.
    """
    nicegui_app.add_api_route(
        METRICS_ROUTE,
        make_metrics_handler(**handler_kwargs),
        methods=["GET"],
    )
