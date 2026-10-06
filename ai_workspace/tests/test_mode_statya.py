"""Тесты первого drop-in-режима «статья» (Ф3.6): валидность + e2e через engine.

Проверяет: файл режима проходит схему S1–S8 и линт L1–L10; движок исполняет его
целиком с двумя human-gate; ``on_approve``/``on_edit`` управляют переходами
(structure: edit → analyst; publish: edit → editor, approve → финал).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_workspace.orchestrator.engine import ModeEngine, load_mode
from ai_workspace.orchestrator.mode_lint import validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry
from ai_workspace.tests.conftest import requires_redis
from ai_workspace.tests.test_engine import (
    EPOCH,
    FakeJobs,
    FakeLLM,
    FakeMCP,
    make_engine,
)
from ai_workspace.tools.modes_validate import main as modes_validate_main

AI_WORKSPACE = Path(__file__).resolve().parents[1]
MODE = AI_WORKSPACE / "modes" / "statya.yaml"
REGISTRY_DIR = AI_WORKSPACE / "registry"
REPO_ROOT = AI_WORKSPACE.parent


def test_statya_mode_is_schema_and_lint_clean() -> None:
    import yaml

    doc = yaml.safe_load(MODE.read_text(encoding="utf-8"))
    registry = Registry(REGISTRY_DIR)
    findings = validate_schema(doc, registry) + validate_lint(doc, registry, base_dir=REPO_ROOT)
    assert findings == []


def test_statya_mode_passes_cli_validator() -> None:
    assert modes_validate_main(["--file", str(MODE)]) == 0


def test_statya_declares_two_human_gates_and_citer() -> None:
    graph = load_mode(MODE)
    gates = [n.id for n in graph.nodes.values() if n.kind == "human-gate"]
    assert gates == ["structure", "publish"]
    citer = graph.node("citer")
    assert citer.get("tool") == "mcp.citation_attach" and citer.get("policy") == "strict"
    assert graph.node("structure").get("on_edit") == "analyst"
    assert graph.node("publish").get("on_edit") == "editor"


def _statya_engine(script=None):
    default = {"analyst": ["draft v1"], "critic": ["PASS — годно"], "editor": ["итоговый документ"]}
    return make_engine(script or default, graph=load_mode(MODE))


def test_statya_e2e_two_gates_and_citation() -> None:
    engine = _statya_engine()
    first = engine.run("j1", epoch=EPOCH)

    assert first.status == "paused" and first.node == "structure"
    assert first.detail == "Утвердить структуру статьи?"
    _, sections = engine.boards.read()
    assert sections["draft"] == "draft v1"
    assert "verdict" not in sections  # critic ещё не вызывался

    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token, decision="approve")
    assert second.status == "paused" and second.node == "publish"
    _, sections = engine.boards.read()
    assert sections["verdict"].startswith("PASS")
    assert sections["document"] == "итоговый документ"
    assert "document+refs" in sections  # citer отработал до publish-гейта

    third = engine.resume("j1", epoch=EPOCH, token=second.resume_token, decision="approve")
    assert third.status == "done"


def test_statya_structure_edit_returns_to_analyst() -> None:
    engine = _statya_engine({"analyst": ["draft v1", "draft v2"], "critic": ["PASS"], "editor": ["doc"]})
    paused = engine.run("j1", epoch=EPOCH)

    again = engine.resume("j1", epoch=EPOCH, token=paused.resume_token, decision="edit",
                          edit="структура: добавить раздел про egress")

    assert again.status == "paused" and again.node == "structure"
    _, llm, _, _, boards = engine._test  # type: ignore[attr-defined]
    assert llm.calls.count("analyst") == 2  # правка структуры вернула на переработку
    assert boards.sections["structure"] == "структура: добавить раздел про egress"
    assert boards.sections["draft"] == "draft v2"


def test_statya_publish_edit_returns_to_editor() -> None:
    engine = _statya_engine({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc v1", "doc v2"]})
    first = engine.run("j1", epoch=EPOCH)
    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token, decision="approve")

    third = engine.resume("j1", epoch=EPOCH, token=second.resume_token, decision="edit",
                          edit="убрать последний абзац")

    assert third.status == "paused" and third.node == "publish"
    _, llm, _, _, boards = engine._test  # type: ignore[attr-defined]
    assert llm.calls.count("editor") == 2
    assert boards.sections["document"] == "doc v2"
    assert boards.sections["publish"] == "убрать последний абзац"


def test_statya_publish_reject_fails() -> None:
    engine = _statya_engine()
    first = engine.run("j1", epoch=EPOCH)
    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token, decision="approve")

    result = engine.resume("j1", epoch=EPOCH, token=second.resume_token, decision="reject")

    assert result.status == "failed"
    assert result.detail == "человек отклонил"


def test_statya_critic_revise_loop_then_pass() -> None:
    engine = _statya_engine(
        {"analyst": ["v1", "v2"], "critic": ["REVISE — нет данных", "PASS"], "editor": ["doc"]}
    )
    first = engine.run("j1", epoch=EPOCH)

    assert first.status == "paused" and first.node == "structure"
    second = engine.resume("j1", epoch=EPOCH, token=first.resume_token, decision="approve")

    assert second.status == "paused" and second.node == "publish"
    _, llm, _, _, boards = engine._test  # type: ignore[attr-defined]
    assert llm.calls.count("critic") == 2
    assert boards.sections["draft"] == "v2"


@pytest.mark.integration
@requires_redis
def test_statya_e2e_on_redis() -> None:
    from ai_workspace.orchestrator.board import BoardStore
    from ai_workspace.orchestrator.job import JobStore
    from ai_workspace.orchestrator.ledger import RedisLedger
    from ai_workspace.redis_client import make_ws_redis

    client = make_ws_redis()
    job_id = "test-statya-j1"
    keys = [f"ws:job:{job_id}", f"ws:board:{job_id}", f"ws:board:{job_id}:owner",
            f"ws:fx:{job_id}", f"ws:resume:{job_id}"]
    for key in keys + list(client.scan_iter(match=f"ws:board:{job_id}:v:*")):
        client.delete(key)

    jobs = JobStore(client)
    jobs.create(user="u1", account_level="basic", job_class="interactive", mode="statya",
                zone="public", job_id=job_id)
    engine = ModeEngine(
        jobs=jobs,
        boards=BoardStore(client, job_id),
        graph=load_mode(MODE),
        llm=FakeLLM({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}),
        mcp=FakeMCP(),
        ledger=RedisLedger(client),
    )
    engine.seed(job_id, {"brief": "тема"}, epoch=EPOCH)

    first = engine.run(job_id, epoch=EPOCH)
    assert first.node == "structure"
    second = engine.resume(job_id, epoch=EPOCH, token=first.resume_token)
    assert second.node == "publish"
    third = engine.resume(job_id, epoch=EPOCH, token=second.resume_token)
    assert third.status == "done"

    for key in keys + list(client.scan_iter(match=f"ws:board:{job_id}:v:*")):
        client.delete(key)
    assert FakeJobs  # фейки не используются — движок на реальных store'ах
