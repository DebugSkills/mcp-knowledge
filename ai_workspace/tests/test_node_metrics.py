"""Тесты метрик-ядра ``ws:metrics:*`` (Ф6 TODO 4а / К4).

trace_id: arch-2026-10-05-ai-workspace. По node-событиям (``on_node_usage``,
уже эмитятся подписчиком в ``ws:quota:events``) подписчик дополнительно
инкрементирует Redis-хэши метрик — best-effort, рядом с XADD, без нового
стрима.

Схема ключей (cardinality bounded — 4 лейбла, ``node_id``/``job_id`` НЕ
лейблятся; решение зафиксировано в плане REV.2 ДО кода):

- ``ws:metrics:node:{kind}:{model_class}:{shelf}:{role}`` — HASH-счётчики:
  ``calls`` (+1 на событие), ``cached`` (+1 при cache-hit), ``tokens``
  (+``event.tokens``);
- ``ws:metrics:usage_fallback_total`` — HASH, поле ``count``: +1 когда узел
  РЕАЛЬНО оценил токены ``chars/4`` (событие несёт ``tokens_estimated=True``
  — признак ставит движок в ``_observe_node_usage``, агрегация — у подписчика;
  дизайн TODO 2: данные едут в событии, движок в Redis не пишет).

Offline — фейк redis (захват xadd + hincrby, реальные имена/арности методов
redis-py); integration — живой ws-redis (``make ws-up-test``, 6390).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

EVENT = {
    "job": "job-x1", "trace_id": "job-x1:3", "node": "secret-node-9",
    "kind": "llm-step", "role": "analyst", "model_class": "heavy",
    "shelf": "local", "cached": False, "prompt_chars": 100,
    "output_chars": 50, "tokens": 42, "wall_s": 0.5,
    "tokens_estimated": False,
}
"""Событие on_node_usage с признаком fallback-оценки (контракт EVENT_KEYS
test_node_usage; job/node заведомо уникальны — для проверки НЕ-попадания
в ключи метрик)."""

ANALYST_KEY = "ws:metrics:node:llm-step:heavy:local:analyst"
TOOL_KEY = "ws:metrics:node:tool-step:none:local:none"
FALLBACK_KEY = "ws:metrics:usage_fallback_total"


class FakeMetricsRedis:
    """Минимальный фейк ws-redis: захват xadd (стрим) + hincrby (метрики).

    Контракт по реальным методам redis-py (``xadd(key, fields, **kwargs)``,
    ``hincrby(key, field, amount) -> int``); ``fail_hincrby`` — деградация
    Redis на записи метрик (событие обязано уйти раньше).
    """

    def __init__(self, *, fail_hincrby: bool = False) -> None:
        self.xadd_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.hashes: dict[str, dict[str, int]] = {}
        self.fail_hincrby = fail_hincrby

    def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        self.xadd_calls.append((key, dict(fields), dict(kwargs)))
        return f"{key}-0-{len(self.xadd_calls)}"

    def hincrby(self, key: str, field: str, amount: int) -> int:
        if self.fail_hincrby:
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("connection refused")
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + int(amount)
        return bucket[field]


# ── offline: подписчик инкрементирует метрики по лейблам ─────────────────


def test_node_event_increments_metric_hash_by_labels() -> None:
    """Node-событие → HINCRBY в ключ 4-лейблов {kind,model_class,shelf,role}:
    calls +1, tokens +N; эмит в ws:quota:events остаётся прежним (рядом)."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeMetricsRedis()
    make_on_node_usage(client)(EVENT)

    assert client.hashes == {ANALYST_KEY: {"calls": 1, "tokens": 42}}
    assert len(client.xadd_calls) == 1  # стрим не пострадал
    payload = json.loads(client.xadd_calls[0][1]["event"])
    assert payload["type"] == "node_usage" and payload["tokens_estimated"] is False


def test_cached_event_increments_cached_counter_only() -> None:
    """Cache-hit: ``cached`` +1 (calls +1), tokens события = 0 → поле
    ``tokens`` не создаётся."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeMetricsRedis()
    make_on_node_usage(client)({**EVENT, "cached": True, "tokens": 0})

    assert client.hashes == {ANALYST_KEY: {"calls": 1, "cached": 1}}


def test_usage_fallback_total_increments_only_on_estimated() -> None:
    """К4/К2: ``usage_fallback_total`` +1 только при ``tokens_estimated=True``
    (узел реально оценил chars/4); с реальным usage — НЕ инкрементится."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    estimated = FakeMetricsRedis()
    make_on_node_usage(estimated)({**EVENT, "tokens_estimated": True})
    assert estimated.hashes[FALLBACK_KEY] == {"count": 1}

    real = FakeMetricsRedis()
    make_on_node_usage(real)({**EVENT, "tokens_estimated": False})
    assert FALLBACK_KEY not in real.hashes
    # повторный estimated-узел: счётчик растёт (HINCRBY, не SET)
    make_on_node_usage(estimated)({**EVENT, "tokens_estimated": True, "tokens": 7})
    assert estimated.hashes[FALLBACK_KEY] == {"count": 2}


def test_metric_keys_exclude_node_and_job_ids() -> None:
    """Bounded cardinality: ``node_id``/``job_id`` в ключах НЕ появляются
    (лейблы только {kind,model_class,shelf,role}); tool-step без роли/класса —
    плейсхолдер ``none``."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeMetricsRedis()
    sub = make_on_node_usage(client)
    sub(EVENT)
    sub({
        "job": "job-x1", "trace_id": "job-x1:3", "node": "citer",
        "kind": "tool-step", "role": None, "model_class": None,
        "shelf": "local", "cached": False, "prompt_chars": 30,
        "output_chars": 26, "tokens": 0, "wall_s": 0.1,
        "tokens_estimated": False,
    })

    keys = set(client.hashes)
    assert keys == {ANALYST_KEY, TOOL_KEY}
    for key in keys:
        assert "job-x1" not in key and "secret-node-9" not in key and "citer" not in key


def test_metrics_redis_failure_is_best_effort_after_emit() -> None:
    """Деградация Redis на метриках: эмит уже ушёл (порядок — событие
    важнее счётчиков), подписчик НЕ поднимает — узел/submit не валим."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeMetricsRedis(fail_hincrby=True)
    make_on_node_usage(client)({**EVENT, "tokens_estimated": True})  # не raise

    assert len(client.xadd_calls) == 1  # XADD был ПЕРЕД отказом метрик
    assert client.hashes == {}


def test_engine_run_through_subscriber_increments_metrics() -> None:
    """Сквозной offline: движок (FakeLLM без usage → все свежие LLM-узлы
    estimated) через подписчик → метрики всех 4 узлов + fallback ×3
    (analyst/critic/editor; citer — tool-step, не считается)."""
    from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
    from ai_workspace.scheduler.wiring import make_on_node_usage
    from ai_workspace.tests.test_engine import (
        EPOCH,
        VALID,
        FakeBoards,
        FakeJobs,
        FakeLLM,
        FakeMCP,
    )

    client = FakeMetricsRedis()
    engine = ModeEngine(
        jobs=FakeJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM({"analyst": ["черновик"], "critic": ["PASS"], "editor": ["документ"]}),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        on_node_usage=make_on_node_usage(client),
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: метрики"}, epoch=EPOCH)

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.node == "publish"
    assert client.hashes[ANALYST_KEY]["calls"] == 1
    assert client.hashes[ANALYST_KEY]["tokens"] > 0
    assert client.hashes["ws:metrics:node:critic-gate:heavy:local:critic"]["calls"] == 1
    assert client.hashes[TOOL_KEY] == {"calls": 1}  # tokens=0 → поля нет
    assert client.hashes[FALLBACK_KEY] == {"count": 3}  # 3 свежих LLM-вызова без usage


# ── integration: метрики в живом ws-redis ────────────────────────────────


@pytest.mark.integration
@requires_redis
def test_engine_metrics_reach_ws_redis() -> None:
    """К4 (живой носитель): прогон движка с подписчиком → ws-redis содержит
    HASH-метрики (HGETALL видит calls/tokens), fallback-счётчик растёт на
    оценённых узлах, в ключах нет job/node-идентификаторов."""
    from uuid import uuid4

    from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
    from ai_workspace.redis_client import make_ws_redis
    from ai_workspace.scheduler.wiring import make_on_node_usage
    from ai_workspace.tests.test_engine import (
        EPOCH,
        VALID,
        FakeBoards,
        FakeJobs,
        FakeLLM,
        FakeMCP,
    )

    client = make_ws_redis()
    job = f"{WS_TEST_ID_PREFIX}f6t4a-{uuid4().hex[:8]}"
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
    engine.seed(job, {"brief": "тема: метрики живьём"}, epoch=EPOCH)
    try:
        res = engine.run(job, epoch=EPOCH)

        assert res.status == "paused" and res.node == "publish"
        analyst = client.hgetall(ANALYST_KEY)
        assert int(analyst["calls"]) >= 1 and int(analyst["tokens"]) > 0
        fallback = client.hgetall(FALLBACK_KEY)
        assert int(fallback["count"]) >= 3  # все LLM-узлы прогона — без usage
        keys = list(client.scan_iter(match="ws:metrics:*"))
        assert keys, "ws:metrics:* не найдены"
        assert all(job not in k for k in keys)  # job_id не протек в лейблы
    finally:
        # тестовые метрики не должны протекать в чужие прогоны ws-redis:
        # убираем ТОЛЬКО ключи этого контура (test-only redis по conftest)
        keys = list(client.scan_iter(match="ws:metrics:*"))
        if keys:
            client.delete(*keys)
