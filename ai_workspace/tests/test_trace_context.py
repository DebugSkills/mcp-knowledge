"""Тесты UI-звена trace_id (Ф6 TODO 3 / F3): trace-context на постановке job.

trace_id: arch-2026-10-05-ai-workspace. Склейка session↔job↔узел:

- вызывающий ``QuotaWiring.submit`` передаёт ``meta={session_id, turn_id}``
  (trace-context UI-запроса; сессия живёт в kb-console ``llm_stream.py``) →
- ``job.meta`` в HASH ``ws:job:{id}`` (JSON-поле, как ``board_versions``) →
- подписчик ``make_on_node_usage(client, store=...)`` best-effort доносит
  ``session_id``/``turn_id`` до node-события (``trace_id=job:epoch`` там уже
  есть из TODO 2) в единый стрим приёмки ``ws:quota:events``.

ПРОБА (P2-примечание №3 критика-2, зафиксирована честно): UI (kb-console)
job НЕ создаёт — чат ходит в шлюз напрямую (``core/llm_stream.py``:
httpx-стрим в LiteLLM, ``session_id`` только в debug-логе; grep по
``kb-console/src``: вызовов scheduler ``submit``/``create_job`` нет,
``pages/queue.py`` только ЧИТАЕТ ws-ключи). Полное UI-звено сейчас не
строится — реализован РЕЗЕРВНЫЙ путь: trace-context пробрасывается на
границе, где job РЕАЛЬНО создаётся (``scheduler/wiring.py::submit``);
когда оркестратор начнёт принимать чат-запросы, он передаст сюда тот же
``meta``. Фазу fallback не блокирует (решение плана REV.1, P2 №3).

Offline — фейки (FakeXAddRedis/FakeStore); integration — живой ws-redis:
``submit(meta)`` → ``job.meta`` → node-событие в стриме (склейка
admission→node по носителю; UI-конец — см. пробу выше).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ai_workspace.orchestrator.job import (
    JobRecord,
    JobState,
    JobStore,
    job_from_hash,
    job_to_hash,
)
from ai_workspace.scheduler.admission import QUOTA_EVENTS_KEY
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

EVENT = {
    "job": "j1", "trace_id": "j1:3", "node": "analyst", "kind": "llm-step",
    "role": "analyst", "model_class": "heavy", "shelf": "local",
    "cached": False, "prompt_chars": 100, "output_chars": 50,
    "tokens": 42, "wall_s": 0.5,
}
"""Пример события on_node_usage (контракт EVENT_KEYS из test_node_usage)."""

TRACE_META = {"session_id": "sess-abc-123", "turn_id": "turn-7"}


def _rec(**kw) -> JobRecord:
    base: dict[str, Any] = {
        "id": "j1", "user": "u1", "account_level": "member",
        "job_class": "interactive", "mode": "statya", "zone": "public",
    }
    base.update(kw)
    return JobRecord(**base)


class FakeXAddRedis:
    """Минимальный фейк ws-redis: захват xadd-вызовов (как test_node_events)."""

    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        self.xadd_calls.append((key, dict(fields), dict(kwargs)))
        return f"{key}-0-{len(self.xadd_calls)}"


class FakeStore:
    """Фейк JobStore: возвращает НАСТОЯЩИЙ JobRecord (контракт по типу),
    считает get-вызовы, умеет подниматься исключением."""

    def __init__(self, rec: JobRecord | None = None, *, fail: bool = False) -> None:
        self.rec = rec
        self.fail = fail
        self.gets = 0

    def get(self, job_id: str) -> JobRecord:
        self.gets += 1
        if self.fail:
            raise RuntimeError(f"store get failed for {job_id}")
        return self.rec if self.rec is not None else _rec(id=job_id)


# ── offline: кодек job.meta (HASH-контракт ws:job:{id}) ──────────────────


def test_record_meta_hash_roundtrip() -> None:
    """meta сериализуется в HASH-поле "meta" (JSON) и восстанавливается
    без потерь — тот же паттерн, что board_versions."""
    rec = _rec(state=JobState.QUEUED, meta=dict(TRACE_META))
    h = job_to_hash(rec)
    assert h["meta"] == json.dumps(TRACE_META, sort_keys=True, ensure_ascii=False)
    assert job_from_hash(h) == rec


def test_record_meta_empty_by_default_and_absent_decodes_empty() -> None:
    """Обратная совместимость: записи БЕЗ meta (старые ws:job:* и вызовы
    без параметра) кодируются/декодируются в пустой словарь, не ломая
    существующий HASH-контракт."""
    h = job_to_hash(_rec())
    assert json.loads(h["meta"]) == {}
    partial = {
        "id": "j1", "user": "u", "account_level": "med",
        "class": "batch", "mode": "m", "zone": "private",
    }
    assert job_from_hash(partial).meta == {}


# ── offline: подписчик доносит trace-context до node-события ─────────────


def test_subscriber_enriches_node_event_with_trace_context() -> None:
    """Склейка job.meta → node-событие: при store с meta={session_id,
    turn_id} эмит в ws:quota:events несёт ОБА поля рядом с trace_id."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    store = FakeStore(_rec(meta=dict(TRACE_META)))
    make_on_node_usage(client, store=store)(EVENT)

    assert len(client.xadd_calls) == 1
    key, fields, _kw = client.xadd_calls[0]
    assert key == QUOTA_EVENTS_KEY
    payload = json.loads(fields["event"])
    assert payload["type"] == "node_usage"
    assert payload["trace_id"] == "j1:3"  # из TODO 2 — не потерян
    assert payload["session_id"] == "sess-abc-123"
    assert payload["turn_id"] == "turn-7"
    for k, v in EVENT.items():
        assert payload[k] == v  # поля события движка без потерь


def test_subscriber_without_store_keeps_emit_unchanged() -> None:
    """Без store (прод-проводка TODO 2) эмит прежний: ни session-полей,
    ни обращений к job-store — обратная совместимость проводки."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    make_on_node_usage(client)(EVENT)

    assert len(client.xadd_calls) == 1
    payload = json.loads(client.xadd_calls[0][1]["event"])
    assert payload["trace_id"] == "j1:3"
    assert "session_id" not in payload and "turn_id" not in payload


def test_subscriber_whitelists_only_trace_keys() -> None:
    """В эмит идут ТОЛЬКО session_id/turn_id — прочие meta-ключи (карди-
    нальность/утечки) в стрим не попадают; ключи события доминируют."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    store = FakeStore(
        _rec(meta={"session_id": "s1", "turn_id": "t1", "secret": "x", "role": "y"})
    )
    make_on_node_usage(client, store=store)(EVENT)

    payload = json.loads(client.xadd_calls[0][1]["event"])
    assert payload["session_id"] == "s1" and payload["turn_id"] == "t1"
    assert "secret" not in payload
    assert payload["role"] == "analyst"  # роль СОБЫТИЯ движка, не meta


def test_subscriber_store_failure_is_best_effort() -> None:
    """Деградация store (job удалён/redis вниз) НЕ валит эмит: событие
    уходит без session-полей — наблюдение важнее полноты трейса."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    make_on_node_usage(client, store=FakeStore(fail=True))(EVENT)

    assert len(client.xadd_calls) == 1
    payload = json.loads(client.xadd_calls[0][1]["event"])
    assert payload["trace_id"] == "j1:3"
    assert "session_id" not in payload


def test_subscriber_caches_trace_meta_per_job() -> None:
    """meta читается из store ОДИН раз на job (кэш замыкания): N событий
    того же job → 1 get — без HGETALL на каждый узел."""
    from ai_workspace.scheduler.wiring import make_on_node_usage

    client = FakeXAddRedis()
    store = FakeStore(_rec(meta=dict(TRACE_META)))
    sub = make_on_node_usage(client, store=store)
    for i in range(3):
        sub({**EVENT, "trace_id": f"j1:{i}", "node": f"n{i}"})

    assert store.gets == 1
    assert len(client.xadd_calls) == 3


# ── integration: submit(meta) → job.meta → node-событие (живой ws-redis) ─

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


@pytest.fixture()
def ws():
    from ai_workspace.redis_client import make_ws_redis
    from ai_workspace.registry import Registry
    from ai_workspace.registry.quotas import QuotaRegistry

    client = make_ws_redis()
    user = f"{WS_TEST_ID_PREFIX}f6t3-{uuid4().hex[:8]}"
    yield client, QuotaRegistry(Registry(REGISTRY_DIR)), user
    keys = list(client.scan_iter(match=f"*{user}*"))
    if keys:
        client.delete(*keys)


@pytest.mark.integration
@requires_redis
def test_submit_meta_lands_in_job_hash(ws) -> None:
    """submit(meta={session_id, turn_id}) → значения в записи job И в
    HASH ws:job:{id} (поле meta, JSON) — носитель, не только объект."""
    from ai_workspace.scheduler.wiring import QuotaWiring

    client, book, user = ws
    jid = f"{user}-j1"
    rec = QuotaWiring(client, registry=book).submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=jid, meta=dict(TRACE_META),
    )

    assert rec.meta == TRACE_META
    assert json.loads(client.hget(f"ws:job:{jid}", "meta")) == TRACE_META
    assert JobStore(client).get(jid).meta == TRACE_META  # перечитано из HASH


@pytest.mark.integration
@requires_redis
def test_submit_without_meta_keeps_hash_contract(ws) -> None:
    """Регресс вызова без meta: job создаётся, meta в HASH = {} (старые
    вызовы не меняются — поля опциональны)."""
    from ai_workspace.scheduler.wiring import QuotaWiring

    client, book, user = ws
    jid = f"{user}-j2"
    rec = QuotaWiring(client, registry=book).submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=jid,
    )

    assert rec.meta == {}
    assert json.loads(client.hget(f"ws:job:{jid}", "meta")) == {}


@pytest.mark.integration
@requires_redis
def test_trace_glue_submit_to_node_event(ws) -> None:
    """Склейка по носителю: submit(meta) → подписчик(store) → XREVRANGE
    ws:quota:events видит node_usage с trace_id=job:epoch И session_id/
    turn_id из job.meta. UI-конец цепочки — резервный путь (см. пробу)."""
    from ai_workspace.scheduler.wiring import QuotaWiring, make_on_node_usage

    client, book, user = ws
    jid = f"{user}-j3"
    QuotaWiring(client, registry=book).submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=jid, meta=dict(TRACE_META),
    )
    sub = make_on_node_usage(client, store=JobStore(client))
    epoch = 5
    sub({
        "job": jid, "trace_id": f"{jid}:{epoch}", "node": "analyst",
        "kind": "llm-step", "role": "analyst", "model_class": "heavy",
        "shelf": "local", "cached": False, "prompt_chars": 10,
        "output_chars": 5, "tokens": 3, "wall_s": 0.1,
    })

    tail: list[dict[str, Any]] = []
    for _id, fields in client.xrevrange(QUOTA_EVENTS_KEY, count=200):
        if "event" in fields:
            tail.append(json.loads(fields["event"]))
    mine = [e for e in tail if e.get("type") == "node_usage" and e.get("job") == jid]
    assert mine, "node_usage job не найден в хвосте ws:quota:events"
    ev = mine[0]
    assert ev["trace_id"] == f"{jid}:{epoch}"
    assert ev["session_id"] == TRACE_META["session_id"]
    assert ev["turn_id"] == TRACE_META["turn_id"]
