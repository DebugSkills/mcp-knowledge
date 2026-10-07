"""Тесты подписчика node-событий → ``ws:quota:events`` (Ф6 TODO 2 / К1).

trace_id: arch-2026-10-05-ai-workspace. События ``on_node_usage`` с
``trace_id = job:epoch`` уходят в ЕДИНЫЙ стрим приёмки ``ws:quota:events``
через существующий ``prio.emit_event`` — второй стрим НЕ вводится
(P2-примечание 1 критика-2: ``emit_event`` пишет только в ws:quota:events;
``ws:events:{shelf}`` — стрим слотов/очереди, туда трейс узлов не идёт).

Подписчик — ``wiring.make_on_node_usage``: ставится в wiring для
golden-run/интеграций (прод-воркера в репо НЕТ — R1, grep-фиксация;
прод-сбор per-node метрик — arq-воркер, P2-хвост Ф6).

Offline — фейковый redis (захват xadd); integration — живой ws-redis
(``make ws-up-test``, 127.0.0.1:6390; авто-skip без WS_REDIS_URL).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai_workspace.scheduler.admission import QUOTA_EVENTS_KEY
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

EVENT = {
    "job": "j1", "trace_id": "j1:3", "node": "analyst", "kind": "llm-step",
    "role": "analyst", "model_class": "heavy", "shelf": "local",
    "cached": False, "prompt_chars": 100, "output_chars": 50,
    "tokens": 42, "wall_s": 0.5,
}
"""Пример события on_node_usage (контракт EVENT_KEYS из test_node_usage)."""


class FakeXAddRedis:
    """Минимальный фейк ws-redis: захват xadd-вызовов (ключ + поля + опции)."""

    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        self.xadd_calls.append((key, dict(fields), dict(kwargs)))
        return f"{key}-0-{len(self.xadd_calls)}"


# ── offline: подписчик → единый стрим ────────────────────────────────────


def test_subscriber_emits_node_usage_to_quota_events_stream() -> None:
    """Событие движка уходит в ws:quota:events (type=node_usage, trace_id
    без потерь, формат _quota_event с ts, обрезка хвоста как у контура)."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    make_on_node_usage(client)(EVENT)

    assert len(client.xadd_calls) == 1
    key, fields, kwargs = client.xadd_calls[0]
    assert key == QUOTA_EVENTS_KEY  # единый стрим приёмки
    payload = json.loads(fields["event"])
    assert payload["type"] == "node_usage"
    assert payload["trace_id"] == "j1:3"  # К1: trace_id = job:epoch
    for k, v in EVENT.items():
        assert payload[k] == v  # поля события движка проходят без потерь
    assert "ts" in payload  # формат _quota_event (SSOT admission)
    assert kwargs.get("maxlen") and kwargs.get("approximate") is True  # I12


def test_subscriber_does_not_create_second_stream() -> None:
    """P2-примечание 1: emit_event-путь НЕ заводит второй стрим — все
    xadd-ы идут только в ws:quota:events (никаких ws:events:{shelf})."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    sub = make_on_node_usage(client)
    for i in range(3):
        sub({**EVENT, "trace_id": f"j1:{i}", "node": f"n{i}"})

    assert len(client.xadd_calls) == 3
    streams = {key for key, _fields, _kw in client.xadd_calls}
    assert streams == {QUOTA_EVENTS_KEY}


def test_subscriber_best_effort_redis_down_does_not_raise() -> None:
    """Деградация ws-redis глотается emit_event — подписчик не поднимает
    (наблюдение не валит узел; движок тоже оборачивает, тут — свой слой)."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    from ai_workspace.scheduler.wiring import make_on_node_usage

    class DeadRedis:
        def xadd(self, *a: Any, **k: Any) -> str:
            raise RedisConnectionError("connection refused")

    make_on_node_usage(DeadRedis())(EVENT)  # не поднимает


# ── integration: движок + подписчик на живом ws-redis ────────────────────


@pytest.mark.integration
@requires_redis
def test_engine_node_events_reach_quota_stream_with_trace_id() -> None:
    """К1 (живой носитель): прогон движка с wiring-подписчиком →
    XREVRANGE ws:quota:events видит node_usage-события job с
    trace_id == f"{job}:{epoch}" на каждом узле."""
    from uuid import uuid4

    from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
    from ai_workspace.redis_client import make_ws_redis
    from ai_workspace.scheduler.wiring import make_on_node_usage
    from ai_workspace.tests.test_engine import (
        VALID,
        FakeBoards,
        FakeJobs,
        FakeLLM,
        FakeMCP,
    )

    client = make_ws_redis()
    job = f"{WS_TEST_ID_PREFIX}f6t2-{uuid4().hex[:8]}"
    epoch = 7  # ≠ 1: доказываем, что epoch реально попадает в trace_id
    engine = ModeEngine(
        jobs=FakeJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM({"analyst": ["черновик"], "critic": ["PASS"], "editor": ["документ"]}),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        on_node_usage=make_on_node_usage(client),
    )
    engine.jobs.create(job)
    engine.seed(job, {"brief": "тема: трейс узлов"}, epoch=epoch)

    res = engine.run(job, epoch=epoch)

    assert res.status == "paused" and res.node == "publish"  # human-gate
    tail: list[dict[str, Any]] = []
    for _id, fields in client.xrevrange(QUOTA_EVENTS_KEY, count=200):
        if "event" in fields:
            tail.append(json.loads(fields["event"]))
    mine = [e for e in tail if e.get("type") == "node_usage" and e.get("job") == job]
    assert mine, "node_usage-события job не найдены в хвосте ws:quota:events"
    assert {e["trace_id"] for e in mine} == {f"{job}:{epoch}"}
    assert {e["node"] for e in mine} == {"analyst", "critic", "editor", "citer"}
