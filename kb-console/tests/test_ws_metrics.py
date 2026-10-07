"""Ф6 TODO 4б (К4): GET /metrics ws-метрик + дашборд очереди — тесты kb-console.

Покрывает:
  (а) unit: формирование exposition из подставленного набора ключей
      (FakeRedis без живого Redis): node-счётчики ws_node_* по лейблам
      {kind,model_class,shelf,role}, счётчик ws_usage_fallback_total,
      Gauges ws_queue_depth/ws_slots_busy/ws_leases_active (полки
      local/ext всегда, gpu — только при наличии ключей), пропуск
      чужих/битых ключей, нули для неинкрементированных полей HASH;
  (б) endpoint: make_metrics_handler отдаёт 200 + text/plain + строки
      ws_node_*; недоступный ws-redis → 200 с комментарием-классом
      ошибки (без host:port — гигиена core/redis_client.py);
  (в) auth-exempt: GET /metrics — метод-специфичный анонимный allowlist
      (как /api/access-request POST); POST и /metrics/ — НЕ allowlist;
  (г) дашборд queue.py: блок «Метрики узлов» (таблица calls/cached/
      tokens по лейблам + usage_fallback_total), отдельный fail-soft —
      сбой метрик НЕ роняет панель очередей.
"""

from __future__ import annotations

import asyncio
import fnmatch
from unittest.mock import patch

from starlette.requests import Request

# Хелперы middleware-прогона — общий стиль с test_auth.py (fail-модель
# pure-ASGI: scope/receive/send без поднятия приложения).
from test_auth import _make_mw, _make_scope, _run

from kb_console.pages import queue
from kb_console.ws_metrics import (
    METRICS_ROUTE,
    USAGE_FALLBACK_KEY,
    build_metrics_exposition,
    fetch_ws_metrics_snapshot,
    make_metrics_handler,
    parse_node_key,
)

# ── Фейковый ws-redis (duck-typed: scan_iter/hgetall/hget/zcard/scard) ──


class FakeMetricsRedis:
    """Dict-обёртка над подмножеством API redis-py, нужным метрикам.

    SCAN эмулируется fnmatch по glob-паттерну (семантика MATCH в Redis).
    """

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.sets: dict[str, set[str]] = {}

    def _all_keys(self) -> list[str]:
        return list(self.kv) + list(self.hashes) + list(self.zsets) + list(self.sets)

    def scan_iter(self, match: str | None = None):
        keys = self._all_keys()
        if match is not None:
            keys = [k for k in keys if fnmatch.fnmatchcase(k, match)]
        return iter(sorted(keys))

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self._all_keys())


def _seed_metrics(c: FakeMetricsRedis) -> None:
    """Два узла + fallback=63 + очередь/слоты/lease на local/ext."""
    c.hashes["ws:metrics:node:llm:flash:local:methodist"] = {
        "calls": "5",
        "cached": "2",
        "tokens": "1234",
    }
    c.hashes["ws:metrics:node:tool:none:ext:none"] = {"calls": "7"}
    c.hashes[USAGE_FALLBACK_KEY] = {"count": "63"}
    c.zsets["ws:q:local"] = {"a": 1.0, "b": 2.0}
    c.sets["ws:slots:local"] = {"w1"}
    c.kv["ws:lease:local:jobA:0:0"] = "1"
    c.kv["ws:lease:ext:jobB:0:0"] = "1"


# ── (а) unit: parse_node_key / snapshot / exposition ────────────────────


def test_parse_node_key_labels_and_arity():
    """Валидный ключ → 4 лейбла в фиксированном порядке; битая арность → None."""
    labels = parse_node_key("ws:metrics:node:llm:flash:local:methodist")
    assert labels == {
        "kind": "llm",
        "model_class": "flash",
        "shelf": "local",
        "role": "methodist",
    }
    assert parse_node_key("ws:metrics:node:a:b:c") is None  # 3 сегмента
    assert parse_node_key("ws:metrics:node:a:b:c:d:e") is None  # 5 сегментов
    assert parse_node_key("ws:metrics:usage_fallback_total") is None


def test_snapshot_nodes_usage_fallback_and_gauges():
    """snapshot: узлы из HASH-ей (0 для неинкрементированных полей),
    usage_fallback=63, gauges по полкам (gpu НЕ выдуман)."""
    c = FakeMetricsRedis()
    _seed_metrics(c)

    snap = fetch_ws_metrics_snapshot(c)

    assert snap["ok"] is True
    nodes = {
        (n["kind"], n["model_class"], n["shelf"], n["role"]): n for n in snap["nodes"]
    }
    llm = nodes[("llm", "flash", "local", "methodist")]
    assert llm["calls"] == 5
    assert llm["cached"] == 2
    assert llm["tokens"] == 1234
    tool = nodes[("tool", "none", "ext", "none")]
    assert tool["calls"] == 7
    assert tool["cached"] == 0  # поле не инкрементировалось → 0, не выдумка
    assert tool["tokens"] == 0
    assert snap["usage_fallback"] == 63
    g = snap["gauges"]
    assert g["queue_depth"] == {"local": 2, "ext": 0}
    assert g["slots_busy"] == {"local": 1, "ext": 0}
    assert g["leases_active"] == {"local": 1, "ext": 1}


def test_snapshot_empty_redis():
    """Пустой ws-redis: узлов нет, fallback 0, полки local/ext с нулями."""
    snap = fetch_ws_metrics_snapshot(FakeMetricsRedis())
    assert snap["nodes"] == []
    assert snap["usage_fallback"] == 0
    assert snap["gauges"]["queue_depth"] == {"local": 0, "ext": 0}
    assert "gpu" not in snap["gauges"]["queue_depth"]


def test_snapshot_skips_foreign_keys_without_crash():
    """Чужие/битые ключи под ws:metrics:* пропускаются, snapshot жив."""
    c = FakeMetricsRedis()
    c.kv["ws:metrics:unknown:thing"] = "x"
    c.hashes["ws:metrics:node:only:three"] = {"calls": "1"}
    snap = fetch_ws_metrics_snapshot(c)
    assert snap["nodes"] == []


def test_exposition_node_counters_lines():
    """exposition: ws_node_calls/cached/tokens с 4 лейблами, HELP/TYPE,
    данные обоих узлов присутствуют в тексте."""
    c = FakeMetricsRedis()
    _seed_metrics(c)

    text = build_metrics_exposition(c)

    assert (
        'ws_node_calls{kind="llm",model_class="flash",shelf="local",role="methodist"} 5'
        in text
    )
    assert (
        'ws_node_cached{kind="llm",model_class="flash",shelf="local",role="methodist"}'
        " 2" in text
    )
    assert (
        'ws_node_tokens{kind="llm",model_class="flash",shelf="local",role="methodist"}'
        " 1234" in text
    )
    assert 'ws_node_calls{kind="tool",model_class="none",shelf="ext",role="none"} 7' in text
    assert "# TYPE ws_node_calls counter" in text
    assert "# HELP ws_node_calls" in text


def test_exposition_usage_fallback_and_gauges():
    """exposition: ws_usage_fallback_total + Gauges полок (shelf-лейбл)."""
    c = FakeMetricsRedis()
    _seed_metrics(c)

    text = build_metrics_exposition(c)

    assert "ws_usage_fallback_total 63" in text
    assert 'ws_queue_depth{shelf="local"} 2' in text
    assert 'ws_slots_busy{shelf="local"} 1' in text
    assert 'ws_leases_active{shelf="local"} 1' in text
    assert 'ws_leases_active{shelf="ext"} 1' in text
    assert "# TYPE ws_queue_depth gauge" in text
    assert "gpu" not in text  # полка gpu не выдумана


def test_exposition_gpu_gauge_only_when_keys_exist():
    """gpu-полка появляется в Gauges только при наличии ws:q:gpu."""
    c = FakeMetricsRedis()
    c.zsets["ws:q:gpu"] = {"g1": 1.0}
    text = build_metrics_exposition(c)
    assert 'ws_queue_depth{shelf="gpu"} 1' in text


def test_exposition_fallback_zero_when_never_incremented():
    """Нет ключа usage_fallback → счётчик честно 0; node-серий нет вовсе."""
    text = build_metrics_exposition(FakeMetricsRedis())
    assert "ws_usage_fallback_total 0" in text
    assert "ws_node_calls{" not in text


def test_exposition_escapes_label_values_defensively():
    """Экранирование \" и \\ в лейблах — защита exposition-формата."""
    c = FakeMetricsRedis()
    c.hashes['ws:metrics:node:ll"m:flash:local:non\\e'] = {"calls": "1"}
    text = build_metrics_exposition(c)
    assert 'ws_node_calls{kind="ll\\"m",model_class="flash",shelf="local",role="non\\\\e"} 1' in text


# ── (б) endpoint: 200 + ws_node_ строки; fail-soft без secrets ─────────


def _request(path: str = METRICS_ROUTE, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def test_endpoint_200_text_plain_with_ws_node_lines():
    """Хендлер с фейковым клиентом: 200, text/plain, тело содержит
    ws_node_-строки и Gauges (носитель К4)."""
    c = FakeMetricsRedis()
    _seed_metrics(c)
    handler = make_metrics_handler(client_factory=lambda: c)

    response = asyncio.run(handler(_request()))

    assert response.status_code == 200
    assert response.media_type.startswith("text/plain")
    body = response.body.decode()
    assert "ws_node_calls{" in body
    assert "ws_usage_fallback_total 63" in body
    assert 'ws_queue_depth{shelf="local"}' in body


def test_endpoint_redis_down_200_comment_class_only():
    """ws-redis недоступен (нет WS_REDIS_URL/сеть): 200 + комментарий с
    КЛАССОМ ошибки; URL/host:port из str исключения НЕ утекают."""

    def _boom():
        raise RuntimeError("WS_REDIS_URL не задан (redis://secret-host:6379)")

    handler = make_metrics_handler(client_factory=_boom)

    response = asyncio.run(handler(_request()))

    assert response.status_code == 200
    body = response.body.decode()
    assert "ws-redis unavailable" in body
    assert "RuntimeError" in body
    assert "secret-host" not in body
    assert "6379" not in body


# ── (в) auth-exempt: метод-специфичный allowlist GET /metrics ───────────


class TestMetricsAllowlist:
    """GET /metrics анонимно (Prometheus-scrape без кредов, К4); строгий
    матчер: POST и /metrics/ — НЕ allowlist (минимальная поверхность)."""

    def test_metrics_get_without_auth_passes_to_app(self):
        mw, stub = _make_mw()
        _run(mw, _make_scope("http", METRICS_ROUTE))
        assert len(stub.calls) == 1

    def test_metrics_post_not_allowlisted_302(self):
        mw, stub = _make_mw()
        scope = _make_scope("http", METRICS_ROUTE)
        scope["method"] = "POST"
        sent = _run(mw, scope)
        assert stub.calls == []
        assert sent[0]["status"] == 302

    def test_metrics_trailing_slash_strict_302(self):
        """/metrics/ — строгий матчер (как /healthz), НЕ allowlist."""
        mw, stub = _make_mw()
        sent = _run(mw, _make_scope("http", "/metrics/"))
        assert stub.calls == []
        assert sent[0]["status"] == 302


def test_middleware_allows_metrics_in_off_mode_transit():
    """mode=off — транзит и так полный; GET /metrics доходит до app."""
    mw, stub = _make_mw(mode="off")
    _run(mw, _make_scope("http", METRICS_ROUTE))
    assert len(stub.calls) == 1


# ── (г) дашборд queue.py: блок метрик + отдельный fail-soft ────────────


def _metrics_state() -> dict:
    return {
        "client": None,
        "snapshot": {"ok": True, "now": 1.0, "shelves": []},
        "error": None,
        "metrics": {
            "ok": True,
            "nodes": [
                {
                    "kind": "llm",
                    "model_class": "flash",
                    "shelf": "local",
                    "role": "methodist",
                    "calls": 5,
                    "cached": 2,
                    "tokens": 1234,
                }
            ],
            "usage_fallback": 63,
            "gauges": {"queue_depth": {"local": 0, "ext": 0}},
        },
        "metrics_error": None,
    }


def test_render_metrics_block_table_and_fallback():
    """_render: таблица узлов (лейблы + calls/cached/tokens) и строка
    usage_fallback_total присутствуют при живых метриках."""
    state = _metrics_state()
    with patch.object(queue, "ui") as mock_ui:
        queue._render(state)

    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Метрики узлов" in t for t in labels)
    assert any("usage_fallback_total" in t and "63" in t for t in labels)
    assert mock_ui.table.called, "таблица метрик отрисована"
    rows = mock_ui.table.call_args.kwargs["rows"]
    assert rows[0]["calls"] == 5
    assert rows[0]["tokens"] == 1234


def test_render_metrics_unavailable_shows_class_only():
    """Сбой метрик → предупреждение с КЛАССОМ ошибки (без host:port);
    панель очередей при этом отрисована (отдельный fail-soft)."""
    state = _metrics_state()
    state["metrics"] = None
    state["metrics_error"] = "ConnectionError"
    with patch.object(queue, "ui") as mock_ui:
        queue._render(state)

    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Метрики недоступны" in t and "ConnectionError" in t for t in labels)
    assert any("usage_fallback_total" in t and "—" in t for t in labels)


def test_render_metrics_empty_nodes_hint():
    """Метрик узлов нет (движок не писал) — подсказка, не пустая таблица."""
    state = _metrics_state()
    state["metrics"] = {
        "ok": True,
        "nodes": [],
        "usage_fallback": 0,
        "gauges": {"queue_depth": {"local": 0, "ext": 0}},
    }
    with patch.object(queue, "ui") as mock_ui:
        queue._render(state)

    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Метрик узлов ещё нет" in t for t in labels)


def test_build_queue_metrics_failure_does_not_break_queue():
    """build_queue: fetch_ws_metrics упал → очередь жива (баннера ws-redis
    нет), метрики — предупреждение с классом ошибки; страница НЕ падает."""
    with (
        patch.object(queue, "make_ws_redis", return_value=object()),
        patch.object(
            queue,
            "fetch_queue_snapshot",
            return_value={"ok": True, "now": 1.0, "shelves": []},
        ),
        patch.object(queue, "fetch_ws_metrics_snapshot", side_effect=ConnectionError),
        patch.object(queue, "ui") as mock_ui,
    ):
        mock_ui.refreshable.side_effect = lambda fn: fn
        queue.build_queue()

    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Метрики недоступны" in t and "ConnectionError" in t for t in labels)
    assert not any("ws-redis недоступен" in t for t in labels)  # очередь цела


def test_build_queue_fetches_metrics_into_state():
    """build_queue: метрики запрошены и попадают в отрисовку (таблица)."""
    snapshot = _metrics_state()["metrics"]
    with (
        patch.object(queue, "make_ws_redis", return_value=object()),
        patch.object(
            queue,
            "fetch_queue_snapshot",
            return_value={"ok": True, "now": 1.0, "shelves": []},
        ),
        patch.object(queue, "fetch_ws_metrics_snapshot", return_value=snapshot),
        patch.object(queue, "ui") as mock_ui,
    ):
        mock_ui.refreshable.side_effect = lambda fn: fn
        queue.build_queue()

    assert mock_ui.table.called
