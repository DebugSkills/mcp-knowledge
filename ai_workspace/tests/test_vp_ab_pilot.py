"""Offline-тесты A/B-пилота V-P (Ф6-a 6a.2b): варианты промпта роли на local 7B.

Живой прогон (ollama) в тесты НЕ входит — здесь проверяется offline-часть:
payload клиента полки (форма ответа — из реального ollama /v1/chat/completions,
зонд 2026-10-07), реестр-подделка вариантов, различие промптов, структурный
скор и механика прогона на скриптованном LLM (гейты авто-approve, метрики).
"""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
from ai_workspace.orchestrator.graph import Node
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import FakeBoards, FakeJobs, FakeLLM, FakeMCP
from ai_workspace.tools.vp_ab_pilot import (
    MAX_DOC_CHARS,
    MIN_DOC_CHARS,
    VARIANTS,
    OllamaClient,
    build_report,
    build_variant_registry,
    document_checks,
    load_seed_text,
    run_one,
    score_run,
)

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
BASE = Registry(REGISTRY_DIR)


# ── клиент полки: payload по контракту ollama /v1/chat/completions ────────

OLLAMA_RESPONSE = {
    "id": "chatcmpl-123", "object": "chat.completion", "created": 1788945678,
    "model": "qwen2.5:7b",
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "PASS — по рубрике"},
    }],
    "usage": {"prompt_tokens": 64, "completion_tokens": 57, "total_tokens": 121},
}
"""Форма ответа — из живого зонда ollama :11435 (не выдуманная)."""


def _fake_response(payload: dict) -> io.BytesIO:
    """Контекст-менеджер с телом ответа (как urllib-ответ)."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def test_ollama_client_builds_openai_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: float = -1) -> io.BytesIO:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["content_type"] = req.headers.get("Content-type")
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _fake_response(OLLAMA_RESPONSE)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OllamaClient()
    out = client.complete(
        role="critic", model_class="fast", prompt="промпт",
        inputs={"draft": "x"},
        params={"temperature": 0.0, "seed": 42, "thinking": False, "max_output_tokens": 2048},
    )

    assert out == "PASS — по рубрике"
    assert seen["url"] == "http://127.0.0.1:11435/v1/chat/completions"
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    assert isinstance(seen["timeout"], float) and seen["timeout"] > 0
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen2.5:7b"
    assert body["messages"] == [{"role": "user", "content": "промпт"}]
    assert body["temperature"] == 0.0 and body["seed"] == 42
    assert body["max_tokens"] == 2048
    assert "thinking" not in body, "ollama OpenAI-эндпоинт thinking не принимает"
    # журнал живого вызова: usage из ответа (доказательство реального вызова)
    assert client.calls[0]["usage"]["completion_tokens"] == 57
    assert client.calls[0]["fragment"]


def test_ollama_client_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def fake_urlopen(req: urllib.request.Request, timeout: float = -1) -> io.BytesIO:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("connection reset")
        return _fake_response(OLLAMA_RESPONSE)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OllamaClient()
    out = client.complete(role="analyst", model_class="heavy", prompt="p", inputs={})
    assert out == "PASS — по рубрике" and len(attempts) == 2

    def always_fail(req: urllib.request.Request, timeout: float = -1) -> io.BytesIO:
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", always_fail)
    with pytest.raises(RuntimeError, match="ollama"):
        client.complete(role="analyst", model_class="heavy", prompt="p", inputs={})


# ── реестр-подделка: варианты реально различаются ─────────────────────────

def _role_node(role: str) -> Node:
    spec = {"id": f"{role}-x", "kind": "llm-step", "role": role, "model_class": "fast"}
    return Node(id=spec["id"], kind="llm-step", spec=spec)


def _engine(registry: object) -> ModeEngine:
    return ModeEngine(
        jobs=FakeJobs(), boards=FakeBoards(), graph=load_mode(MODE),
        llm=FakeLLM({}), mcp=FakeMCP(), ledger=MemoryLedger(),
        registry=registry,  # type: ignore[arg-type]
    )


def test_variant_registry_overrides_roles_and_shelves() -> None:
    empty = build_variant_registry(BASE, "empty")
    full = build_variant_registry(BASE, "full")
    contract = build_variant_registry(BASE, "contract")

    roles_empty = empty.get("roles")
    assert roles_empty["critic"]["contract"] is None, "empty: контракт убран"
    assert roles_empty["critic"]["seed_skill"], "остальная мета роли сохранена"

    full_text = load_seed_text(BASE.get("roles")["critic"]["seed_skill"])
    assert full.get("roles")["critic"]["contract"] == full_text

    assert (contract.get("roles")["critic"]["contract"]
            == BASE.get("roles")["critic"]["contract"]), "contract: как в roles.yaml"

    # пилот local-only: все model_classes печатаются на local-полку (правда о маршруте)
    for name, spec in full.get("model_classes").items():
        assert spec["shelf"] == "local", name
    # прочие реестры — passthrough без изменений
    assert full.get("tools") == BASE.get("tools")


def test_prompt_variants_differ_for_critic() -> None:
    prompts = {}
    for variant in VARIANTS:
        engine = _engine(build_variant_registry(BASE, variant))
        prompts[variant] = engine._prompt(_role_node("critic"), {"draft": "текст"})

    assert prompts["empty"].startswith("# РОЛЬ: critic"), "empty: без префикса, как до 6a.2a"
    assert "НАЗНАЧЕНИЕ" not in prompts["empty"]
    assert prompts["contract"].startswith(
        BASE.get("roles")["critic"]["contract"].strip()
    ), "contract: контракт первой частью"
    full_prefix = load_seed_text(BASE.get("roles")["critic"]["seed_skill"]).strip()[:40]
    assert prompts["full"].startswith(full_prefix), "full: полный seed-скилл первой частью"
    assert len(prompts["empty"]) < len(prompts["contract"]) < len(prompts["full"])


# ── структурный скор ──────────────────────────────────────────────────────

MODE_OUTPUT = {"type": "article", "sections": ["document", "document+refs"]}


def test_document_checks_and_score_weights() -> None:
    good = {
        "document": "Документ. " + "текст " * 200,
        "document+refs": '{"tool": "mcp.citation_attach", "refs": ["src-0123"]}',
    }
    checks = document_checks(MODE_OUTPUT, good)
    assert checks.sections_ok and checks.citation_ok and checks.length_ok
    assert score_run(verdict_parse_ok=True, checks=checks) == 1.0

    empty_board: dict[str, str] = {}
    bad = document_checks(MODE_OUTPUT, empty_board)
    assert not (bad.sections_ok or bad.citation_ok or bad.length_ok)
    assert score_run(verdict_parse_ok=False, checks=bad) == 0.0

    short = {**good, "document": "коротко"}
    trimmed = document_checks(MODE_OUTPUT, short)
    assert trimmed.sections_ok and not trimmed.length_ok
    assert score_run(verdict_parse_ok=True, checks=trimmed) == pytest.approx(0.8)

    huge = {**good, "document": "x" * (MAX_DOC_CHARS + 1)}
    assert not document_checks(MODE_OUTPUT, huge).length_ok
    tiny = {**good, "document": "x" * (MIN_DOC_CHARS - 1)}
    assert not document_checks(MODE_OUTPUT, tiny).length_ok


# ── механика прогона на скриптованном LLM (гейты авто-approve) ────────────

TASK = {"id": "g01-structure", "zone": "public",
        "prompt": "Составь структуру статьи про MCP-RAG (разделы, порядок)."}


def test_run_one_offline_done_path() -> None:
    llm = FakeLLM({
        "analyst": ["План: 1) … 2) …\nВарианты и сравнение…"],
        "critic": ["PASS\nРУБРИКА: полнота 1.0"],
        "editor": ["# Статья\n\n" + "Раздел с содержанием. " * 60],
    })

    outcome = run_one(MODE, TASK, "contract", 1, llm)

    assert outcome.status == "done"
    assert outcome.verdict_parse_ok and outcome.verdict == "PASS"
    assert outcome.score == 1.0
    assert {e["node"] for e in outcome.node_events} >= {"analyst", "critic", "editor", "citer"}
    assert outcome.job_wall_s >= 0.0
    # метрики из on_node_usage посчитаны и отделены от tool-узла
    assert outcome.prompt_chars > 0 and outcome.tokens > 0 and outcome.llm_wall_s >= 0.0


def test_run_one_records_verdict_parse_failure() -> None:
    llm = FakeLLM({
        "analyst": ["План: …"],
        "critic": ["Не могу оценить текст: не хватает деталей для проверки."],
        "editor": ["# Документ"],
    })

    outcome = run_one(MODE, TASK, "empty", 1, llm)

    assert outcome.status == "failed"
    assert not outcome.verdict_parse_ok
    assert "вердикт не распознан" in outcome.detail
    assert outcome.score == 0.0
    assert outcome.document == ""


def test_build_report_renders_details_diagnostics_and_node_sums() -> None:
    llm_ok = FakeLLM({
        "analyst": ["План: …"],
        "critic": ["PASS\nРУБРИКА: полнота 1.0"],
        "editor": ["# Статья\n\n" + "Раздел. " * 120],
    })
    llm_bad = FakeLLM({
        "analyst": ["План: …"],
        "critic": ["Не могу оценить текст."],
        "editor": ["# Документ"],
    })
    done = run_one(MODE, TASK, "contract", 1, llm_ok)
    failed = run_one(MODE, TASK, "empty", 1, llm_bad)
    assert done.status == "done" and failed.status == "failed"

    report = build_report(
        [done, failed], modes=["statya"], tasks=[TASK], runs=1,
        provider="offline-прогон", live=None,
    )

    assert "## Детали прогонов" in report
    assert "вердикт не распознан" in report  # detail провала виден в отчёте
    assert "## Диагностика критика" in report
    assert "## Per-node usage" in report
    # суммы per-node: analyst встречается у обоих вариантов, calls агрегируются
    analyst_rows = [ln for ln in report.splitlines() if ln.startswith("| empty | analyst |")]
    assert analyst_rows and "| 1 |" in analyst_rows[0]
