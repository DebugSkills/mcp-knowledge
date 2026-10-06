"""Тесты artifact-store (Ф3.7): retention, дедуп, экспорт, promote→KB (I13).

Offline — MemoryBackend с инъектируемыми часами (retention проверяется по-настоящему);
integration — RedisBackend на ws-redis (реальный TTL/индекс) и хук движка e2e.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_workspace.artifacts import (
    RETENTION_SECONDS,
    ArtifactError,
    ArtifactNotFound,
    ArtifactStore,
    MemoryBackend,
    RedisBackend,
)
from ai_workspace.orchestrator.engine import load_mode
from ai_workspace.tests.conftest import requires_redis
from ai_workspace.tests.test_engine import EPOCH, FakeLLM, FakeMCP, make_engine
from ai_workspace.tests.test_mode_statya import MODE

DOC = "# Статья\n\nТекст с ссылкой на источник."


class Clock:
    """Управляемые часы для проверки retention."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class KbStub:
    """Стаб mcp.write_knowledge: фиксирует вызов."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, *, tool, args):
        self.calls.append((tool, dict(args)))
        return {"ok": True, "tool": tool}


def _store(clock: Clock | None = None, **kw) -> ArtifactStore:
    clock = clock or Clock()
    return ArtifactStore(MemoryBackend(clock=clock), clock=clock, **kw)


# ── offline: базовый контур ──────────────────────────────────────────────


def test_save_get_list_roundtrip() -> None:
    store = _store()
    rec = store.save(DOC, user="u1", job_id="j1", type="article", zone="public", title="Статья про MCP")

    assert rec.id and rec.size == len(DOC.encode()) and rec.zone == "public"
    assert store.get("u1", rec.id).title == "Статья про MCP"
    assert store.get_content("u1", rec.id) == DOC
    assert [r.id for r in store.list("u1")] == [rec.id]


def test_save_is_idempotent_by_content() -> None:
    store = _store()
    first = store.save(DOC, user="u1")
    second = store.save(DOC, user="u1")

    assert first.id == second.id
    assert len(store.list("u1")) == 1  # дедуп: тот же текст — та же запись


def test_artifacts_are_isolated_per_user() -> None:
    store = _store()
    rec = store.save(DOC, user="u1")

    with pytest.raises(ArtifactNotFound):
        store.get("u2", rec.id)
    assert store.list("u2") == []


def test_retention_expires_artifact() -> None:
    clock = Clock()
    store = _store(clock)
    rec = store.save(DOC, user="u1")

    clock.now += RETENTION_SECONDS - 1
    assert store.get("u1", rec.id).id == rec.id

    clock.now += 1  # ровно на границе retention
    with pytest.raises(ArtifactNotFound):
        store.get("u1", rec.id)
    assert store.list("u1") == []


def test_custom_ttl_and_prune_index() -> None:
    clock = Clock()
    store = _store(clock)
    rec = store.save(DOC, user="u1", ttl=60)

    clock.now += 61
    with pytest.raises(ArtifactNotFound):
        store.get("u1", rec.id)
    assert store.prune(user="u1") == 1
    assert store.prune(user="u1") == 0  # повторный prune идемпотентен


def test_delete_removes_from_index() -> None:
    store = _store()
    rec = store.save(DOC, user="u1")
    store.delete("u1", rec.id)

    with pytest.raises(ArtifactNotFound):
        store.get("u1", rec.id)
    assert store.list("u1") == []


def test_export_writes_markdown_file(tmp_path: Path) -> None:
    store = _store()
    rec = store.save(DOC, user="u1", title="Статья про MCP")
    path = store.export("u1", rec.id, tmp_path)

    assert path.exists() and path.read_text(encoding="utf-8") == DOC
    assert path.name.startswith(rec.id) and path.suffix == ".md"
    assert store.get("u1", rec.id).export_path == str(path)


def test_empty_content_rejected() -> None:
    with pytest.raises(ArtifactError):
        _store().save("", user="u1")


# ── offline: promote → KB (единственный путь, I13) ───────────────────────


def test_promote_calls_kb_with_explicit_scope() -> None:
    store = _store()
    rec = store.save(DOC, user="u1", type="article", zone="public")
    kb = KbStub()

    result = store.promote("u1", rec.id, kb=kb, domain="ai", subject="agents", zone="public",
                           tags=["mcp"])

    assert result == {"ok": True, "tool": "mcp.write_knowledge"}
    tool, args = kb.calls[0]
    assert tool == "mcp.write_knowledge"
    assert args["content"] == DOC and args["domain"] == "ai" and args["zone"] == "public"
    assert {"artifact", "article", "mcp"} <= set(args["tags"])


def test_promote_requires_explicit_scope() -> None:
    store = _store()
    rec = store.save(DOC, user="u1")
    kb = KbStub()

    with pytest.raises(ArtifactError):
        store.promote("u1", rec.id, kb=kb, domain="", subject="s", zone="public")
    with pytest.raises(ArtifactError):
        store.promote("u1", rec.id, kb=kb, domain="d", subject="s", zone="both")
    assert kb.calls == []  # fail-closed: в KB ничего не ушло


# ── offline: хук движка (job done → артефакт) ────────────────────────────


def test_engine_persists_artifact_on_done() -> None:
    clock = Clock()
    store = _store(clock)
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["итоговый текст"]},
                         graph=load_mode(MODE))
    engine.artifacts = store

    first = engine.run("j1", epoch=EPOCH)
    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token)
    done = engine.resume("j1", epoch=EPOCH, token=second.resume_token)

    assert done.status == "done" and done.artifact_id
    rec = store.get("u1", done.artifact_id)
    assert rec.type == "article" and rec.job_id == "j1" and rec.mode == "statya"
    # артефакт = композиция частей output.sections: документ + блок цитат citer'а
    content = store.get_content("u1", done.artifact_id)
    assert content.startswith("итоговый текст")
    assert "src-deadbeef" in content  # цитаты приложены к документу


def test_engine_without_artifacts_store_still_done() -> None:
    engine = make_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}, graph=load_mode(MODE))
    first = engine.run("j1", epoch=EPOCH)
    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token)
    done = engine.resume("j1", epoch=EPOCH, token=second.resume_token)

    assert done.status == "done" and done.artifact_id is None


# ── integration: RedisBackend (реальный TTL/индекс) ──────────────────────


@pytest.mark.integration
@requires_redis
def test_integration_artifact_store_on_redis(tmp_path: Path) -> None:
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    user = "test-artifacts-u1"
    store = ArtifactStore(RedisBackend(client))
    for key in list(client.scan_iter(match=f"ws:artifact:{user}:*")) + [f"ws:artifacts:{user}"]:
        client.delete(key)

    rec = store.save(DOC, user=user, job_id="j1", type="article", title="Тест")
    assert store.get_content(user, rec.id) == DOC
    assert store.get(user, rec.id).type == "article"
    assert [r.id for r in store.list(user)] == [rec.id]
    assert client.ttl(f"ws:artifact:{user}:{rec.id}") > 0  # retention реально выставлен

    path = store.export(user, rec.id, tmp_path)
    assert path.exists() and path.read_text(encoding="utf-8") == DOC

    kb = KbStub()
    store.promote(user, rec.id, kb=kb, domain="ai", subject="agents", zone="public")
    assert kb.calls and kb.calls[0][0] == "mcp.write_knowledge"

    store.delete(user, rec.id)
    assert store.list(user) == []


@pytest.mark.integration
@requires_redis
def test_integration_engine_artifact_on_redis() -> None:
    from ai_workspace.orchestrator.board import BoardStore
    from ai_workspace.orchestrator.job import JobStore
    from ai_workspace.orchestrator.ledger import RedisLedger
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    user, job_id = "test-artifacts-u2", "test-artifacts-j2"
    keys = [f"ws:job:{job_id}", f"ws:board:{job_id}", f"ws:board:{job_id}:owner",
            f"ws:fx:{job_id}", f"ws:resume:{job_id}", f"ws:artifacts:{user}"]
    for key in keys + list(client.scan_iter(match=f"ws:board:{job_id}:v:*")) + list(
        client.scan_iter(match=f"ws:artifact:{user}:*")
    ):
        client.delete(key)

    from ai_workspace.orchestrator.engine import ModeEngine

    jobs = JobStore(client)
    jobs.create(user=user, account_level="basic", job_class="interactive", mode="statya",
                zone="public", job_id=job_id)
    engine = ModeEngine(
        jobs=jobs,
        boards=BoardStore(client, job_id),
        graph=load_mode(MODE),
        llm=FakeLLM({"analyst": ["d"], "critic": ["PASS"], "editor": ["финальный doc"]}),
        mcp=FakeMCP(),
        ledger=RedisLedger(client),
        artifacts=ArtifactStore(RedisBackend(client)),
    )
    engine.seed(job_id, {"brief": "тема"}, epoch=EPOCH)

    first = engine.run(job_id, epoch=EPOCH)
    second = engine.resume(job_id, epoch=EPOCH, token=first.resume_token)
    done = engine.resume(job_id, epoch=EPOCH, token=second.resume_token)

    assert done.status == "done" and done.artifact_id
    assert engine.artifacts.get_content(user, done.artifact_id).startswith("финальный doc")

    for key in keys + list(client.scan_iter(match=f"ws:board:{job_id}:v:*")) + list(
        client.scan_iter(match=f"ws:artifact:{user}:*")
    ):
        client.delete(key)
