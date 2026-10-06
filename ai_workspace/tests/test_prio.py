"""Тесты per-job priority override (Ф4.5a, D8 «разово»).

trace_id: arch-2026-10-05-ai-workspace. Offline-часть — чистая
``effective_priority`` + fail-closed валидация; integration — живой ws-redis
(``make ws-up-test``, 127.0.0.1:6390; авто-skip без WS_REDIS_URL).

Ключевое требование контракта (D8): override действует на ПОСЛЕДУЮЩИЕ
admit/enqueue; ОЧЕРЕДЬ НЕ ТРОГАЕТСЯ — состав/score ``ws:q``/``ws:starve`` и
материализованные ``ws:pos`` не меняются установкой override (явный ассерт).
События общего стрима ``ws:quota:events`` читаются ХВОСТОМ (``XREVRANGE``) —
стрим накапливается между тестами.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_workspace.scheduler.admission import QUOTA_EVENTS_KEY
from ai_workspace.scheduler.prio import (
    clear_job_priority,
    effective_priority,
    get_job_priority,
    prio_key,
    set_job_priority,
)
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "ws_prio.py"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"


@pytest.fixture()
def ws():
    """Живой ws-redis + квот-реестр + уникальный user; уборка своих ключей
    (паттерн test_quota_wiring.ws — фикс в conftest не поднимаем, контур
    приоритетов локален для этого файла)."""
    from uuid import uuid4

    from ai_workspace.redis_client import make_ws_redis
    from ai_workspace.registry import Registry
    from ai_workspace.registry.quotas import QuotaRegistry

    client = make_ws_redis()
    user = f"{WS_TEST_ID_PREFIX}f45a-{uuid4().hex[:8]}"
    yield client, QuotaRegistry(Registry(REGISTRY_DIR)), user
    keys = list(client.scan_iter(match=f"*{user}*"))
    if keys:
        client.delete(*keys)


# ── хелперы ─────────────────────────────────────────────────────────────


def _tail_events(client, *, count: int = 300) -> list[dict]:
    """Хвост общего стрима ws:quota:events (XREVRANGE — стрим растёт,
    читать только хвост; поле event — JSON)."""
    out: list[dict] = []
    for _id, fields in client.xrevrange(QUOTA_EVENTS_KEY, count=count):
        if "event" in fields:
            out.append(json.loads(fields["event"]))
    return out


def _events_for(events: list[dict], type_: str, job: str) -> list[dict]:
    """События типа для job, новые раньше (хвост уже перевёрнут XREVRANGE)."""
    return [e for e in events if e.get("type") == type_ and e.get("job") == job]


# ── offline: effective_priority (чистая функция) ────────────────────────


def test_effective_no_override_falls_back_to_account() -> None:
    assert effective_priority("med", None) == ("med", "account")


def test_effective_valid_override_wins() -> None:
    assert effective_priority("low", "high") == ("high", "job")


def test_effective_garbage_falls_back_with_warning(caplog) -> None:
    with caplog.at_level("WARNING", logger="ai_workspace.scheduler.prio"):
        prio, source = effective_priority("med", "ultra")
    assert (prio, source) == ("med", "account")
    assert "ultra" in caplog.text


def test_set_priority_fail_closed_validation_before_redis() -> None:
    """Валидация ДО любого обращения к redis: fail-closed по значению —
    невалидный prio отклоняется независимо от клиента (мусор в ключ
    попасть через API не может)."""
    with pytest.raises(ValueError, match="ultra"):
        set_job_priority(object(), "jx", "ultra")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ttl_s"):
        set_job_priority(object(), "jx", "high", ttl_s=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="job"):
        set_job_priority(object(), "", "high")  # type: ignore[arg-type]


# ── integration: ключ / TTL / события ───────────────────────────────────


@pytest.mark.integration
@requires_redis
def test_set_get_clear_ttl_lifecycle(ws) -> None:
    client, _book, user = ws
    job = f"{user}-pj1"

    assert get_job_priority(client, job) is None  # без override → account

    set_job_priority(
        client, job, "high", ttl_s=60, actor="op", reason="горящий дедлайн"
    )
    assert get_job_priority(client, job) == "high"
    pttl1 = client.pttl(prio_key(job))
    assert 0 < pttl1 <= 60_000  # SET EX: TTL установлен

    set_job_priority(client, job, "med", ttl_s=3600)  # повторный set продлевает
    pttl2 = client.pttl(prio_key(job))
    assert pttl2 > pttl1  # окно TTL перезаписано: 60 c → 3600 c
    assert get_job_priority(client, job) == "med"

    removed = clear_job_priority(client, job, actor="op")
    assert removed is True
    assert get_job_priority(client, job) is None
    assert not client.exists(prio_key(job))

    # события — хвостом общего стрима: обе установки + clear
    sets = _events_for(_tail_events(client), "job_priority_set", job)
    assert [e["prio"] for e in sets] == ["med", "high"]  # новые раньше
    assert sets[0]["ttl_s"] == 3600 and sets[1]["ttl_s"] == 60
    assert sets[1]["actor"] == "op" and sets[1]["reason"] == "горящий дедлайн"
    cleared = _events_for(_tail_events(client), "job_priority_cleared", job)
    assert cleared and cleared[0]["removed"] is True
    assert cleared[0]["actor"] == "op"


@pytest.mark.integration
@requires_redis
def test_garbage_value_falls_back_and_admit_still_works(ws, caplog) -> None:
    """Мусор в ключе (ручная правка мимо API) → account + warning + событие
    job_priority_invalid; admission продолжается (availability > strictness)."""
    from ai_workspace.scheduler.wiring import QuotaWiring

    client, book, user = ws
    job = f"{user}-pj2"
    client.set(prio_key(job), "ultra")

    with caplog.at_level("WARNING", logger="ai_workspace.scheduler.prio"):
        prio, source = effective_priority(
            "med", get_job_priority(client, job), redis=client, job=job
        )
    assert (prio, source) == ("med", "account")
    assert "ultra" in caplog.text
    invalid = _events_for(_tail_events(client), "job_priority_invalid", job)
    assert invalid and invalid[0]["value"] == "ultra"
    assert invalid[0]["fallback"] == "med"

    # admit пострадавшего job'а НЕ блокируется покоцанным ключом
    wiring = QuotaWiring(client, registry=book)
    rec = wiring.submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=job,
    )
    assert rec.id == job
    assert not _events_for(_tail_events(client), "job_priority_applied", job)


# ── integration: wiring.submit — override на ПОСЛЕДУЮЩИЙ admit ─────────


@pytest.mark.integration
@requires_redis
def test_submit_override_applies_to_subsequent_admit(ws) -> None:
    from ai_workspace.scheduler.wiring import QuotaWiring

    client, book, user = ws
    base, job = f"{user}-pj3-base", f"{user}-pj3"
    wiring = QuotaWiring(client, registry=book)

    # без override: source=account — события applied нет
    wiring.submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=base,
    )
    assert not _events_for(_tail_events(client), "job_priority_applied", base)

    # override ДО admit job'а: последующий submit видит source=job
    set_job_priority(client, job, "high", actor="op", reason="D8 разово")
    wiring.submit(
        user=user, account_level="member", job_class="interactive",
        mode="statya", zone="public", job_id=job,
    )
    applied = _events_for(_tail_events(client), "job_priority_applied", job)
    assert applied and applied[0]["prio"] == "high"
    assert applied[0]["source"] == "job"
    assert applied[0]["account_prio"] == "med"  # member → med (quotas.yaml D2)
    assert applied[0]["user"] == user


# ── integration: ОЧЕРЕДЬ НЕ ТРОНУТА (ключевое требование D8) ───────────


@pytest.mark.integration
@requires_redis
def test_set_priority_does_not_touch_queue(ws) -> None:
    """Установка/снятие override НЕ реордерит очередь: состав и score
    ws:q/ws:starve, ws:vt, vftlast и материализованные ws:pos идентичны
    до/после (никакого requeue(vft_override))."""
    from ai_workspace.scheduler.position import PositionStore
    from ai_workspace.scheduler.queue import Queue

    client, _book, user = ws
    shelf = f"{user}-sh"  # уникальная полка: ключи самоочищаются по user
    q = Queue(client, shelf=shelf, clock=lambda: 1_000.0)
    job = f"{user}-pq"
    for i, (prio, cls, cost) in enumerate(
        [("low", "background", 10.0), ("med", "batch", 5.0),
         ("high", "interactive", 2.0)]
    ):
        q.enqueue(
            q.make_call(f"{job}{i}", 0), prio=prio, call_class=cls,
            cost_est=cost, now=1_000.0 + i,
        )
    PositionStore(client).update_pos(shelf)  # материализовать ws:pos

    def snapshot() -> dict:
        return {
            "q": client.zrange(f"ws:q:{shelf}", 0, -1, withscores=True),
            "starve": client.zrange(f"ws:starve:{shelf}", 0, -1, withscores=True),
            "vt": client.get(f"ws:vt:{shelf}"),
            "vftlast": {
                k: client.get(k)
                for k in client.scan_iter(match=f"ws:vftlast:{shelf}:*")
            },
            "pos": {
                k: client.get(k)
                for k in client.scan_iter(match="ws:pos:*") if user in k
            },
            "posq": client.get(f"ws:posq:{shelf}"),
        }

    before = snapshot()
    assert len(before["q"]) == 3 and len(before["pos"]) == 3  # не вакуумно

    set_job_priority(client, job, "high", actor="op", reason="D8")
    clear_job_priority(client, job)
    set_job_priority(client, job, "low")

    assert snapshot() == before  # ОЧЕРЕДЬ НЕ ТРОНУТА (D8)


# ── integration: CLI (scripts/ws_prio.py) ───────────────────────────────


@pytest.mark.integration
@requires_redis
def test_cli_show_set_clear_and_exit_codes(ws) -> None:
    client, _book, user = ws
    job = f"{user}-cli"
    env = {**os.environ}  # WS_REDIS_URL задан (requires_redis)

    def run(*args: str, url: str | None = None) -> subprocess.CompletedProcess:
        e = dict(env)
        if url:
            e["WS_REDIS_URL"] = url
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True, text=True, cwd=REPO_ROOT, env=e, timeout=60, check=False,
        )

    r = run("show", "--job", job)  # без override
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"job": job, "prio": None}

    r = run("set", "--job", job, "--prio", "high", "--actor", "op", "--ttl", "600")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["ok"] is True
    assert get_job_priority(client, job) == "high"
    assert 0 < client.pttl(prio_key(job)) <= 600_000  # --ttl уважен

    r = run("show", "--job", job)
    assert json.loads(r.stdout) == {"job": job, "prio": "high"}

    r = run("set", "--job", job, "--prio", "ultra")  # валидация → exit 3
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "ultra" in json.loads(r.stdout)["detail"]

    r = run("clear", "--job", job, "--actor", "op")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["removed"] is True
    assert get_job_priority(client, job) is None

    # redis недоступен → exit 2 (fail-closed)
    r = run("show", "--job", job, url="redis://127.0.0.1:6399/0")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "error" in json.loads(r.stdout)
