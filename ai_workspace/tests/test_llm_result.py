"""Тесты Ф6 TODO 1 (критерий К2): ``LLMClient.complete → LLMResult``.

Контракт шлюза подтверждён живой пробой mcp-knowledge-litellm (план Ф6,
решение оператора №2): заголовок ответа ``x-litellm-call-id`` →
``request_id``; ``usage: {prompt_tokens, completion_tokens, total_tokens}``
из тела ответа → реальные токены узла. Заглушка chars/4 остаётся fallback'ом
на случай ``usage is None`` (шлюз не отдал usage — не падаем, оцениваем).

Скоуп: возврат ``{request_id, usage}`` + реальные токены в точках применения
оценки к узлу (``on_node_usage``-событие, агрегат ``usage:{node}``,
квот-списание). Accounting/quota-механика не переносится (F8/P2).
"""

from __future__ import annotations

from typing import Any

from ai_workspace.orchestrator.engine import (
    LLMResult,
    MemoryLedger,
    ModeEngine,
    _chars4_usage,
    _tokens_from_usage,
    load_mode,
)
from ai_workspace.tests.test_engine import EPOCH, VALID, FakeBoards, FakeJobs, FakeMCP
from ai_workspace.tests.test_quota_wiring import FakeQuota

GW_REQUEST_ID = "abc"
GW_USAGE = {"prompt_tokens": 64, "completion_tokens": 57, "total_tokens": 121}
"""Форма фактов — как в живой пробе шлюза (не выдуманная)."""

_UNSET = object()
"""Сентинел «usage не задан» (отличать от явного usage=None; B006)."""


class GatewayLLM:
    """Мок шлюза: LLMResult так, как его собирает клиент из ответа LiteLLM.

    ``request_id`` — из заголовка ``x-litellm-call-id``, ``usage`` — из тела;
    фиксирует ``job_id`` каждого вызова (движок обязан передавать его клиенту
    для metadata запроса).
    """

    def __init__(
        self,
        script: dict[str, list[str]],
        *,
        request_id: str | None = GW_REQUEST_ID,
        usage: Any = _UNSET,
    ) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.request_id = request_id
        # _UNSET → копия GW_USAGE; явный None → шлюз usage не отдал (fallback)
        if usage is _UNSET:
            self.usage: dict[str, Any] | None = dict(GW_USAGE)
        elif usage is None:
            self.usage = None
        else:
            self.usage = dict(usage)
        self.prompts: dict[str, str] = {}
        self.job_ids: list[str | None] = []

    def complete(
        self, *, role: str, model_class: str, prompt: str, inputs,
        params=None, job_id: str | None = None,
    ) -> LLMResult:
        self.prompts[role] = prompt
        self.job_ids.append(job_id)
        queue = self.script.get(role)
        if not queue:
            raise AssertionError(f"нет скриптованного ответа для роли {role!r}")
        output = queue.pop(0)
        return LLMResult(
            output=output,
            request_id=self.request_id,
            usage=dict(self.usage) if self.usage is not None else None,
        )


def make_gateway_engine(llm: GatewayLLM, *, quota: FakeQuota | None = None) -> ModeEngine:
    jobs = FakeJobs()
    engine = ModeEngine(
        jobs=jobs,
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=llm,
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        quota=quota,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=EPOCH)
    return engine


SCRIPT = {"analyst": ["черновик"], "critic": ["PASS — годно"], "editor": ["документ"]}


# ── LLMResult: request_id + реальные токены на узле ──────────────────────


def test_llm_step_reports_real_tokens_and_request_id() -> None:
    """Узел с usage из шлюза: tokens == total_tokens (НЕ chars/4), request_id — в агрегате."""
    events: list[dict] = []
    llm = GatewayLLM(dict(SCRIPT))
    engine = make_gateway_engine(llm)
    engine.on_node_usage = events.append

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused" and res.node == "publish"
    by_node = {e["node"]: e for e in events}
    a = by_node["analyst"]
    assert a["kind"] == "llm-step" and a["cached"] is False
    assert a["tokens"] == 121, "реальные total_tokens из usage, а не оценка chars/4"
    assert a["tokens"] != _chars4_usage(llm.prompts["analyst"], "черновик")

    c = by_node["critic"]
    assert c["kind"] == "critic-gate" and c["tokens"] == 121

    # персистентный агрегат узла: те же реальные токены + request_id шлюза
    agg = engine.ledger.get("j1", "usage:analyst")
    assert agg["tokens"] == 121
    assert agg["request_id_last"] == GW_REQUEST_ID


def test_usage_none_falls_back_to_estimate_without_crash() -> None:
    """usage=None (шлюз не отдал): fallback на оценку chars/4, прогон не падает."""
    events: list[dict] = []
    llm = GatewayLLM(dict(SCRIPT), request_id=None, usage=None)
    engine = make_gateway_engine(llm)
    engine.on_node_usage = events.append

    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused"  # прогон дошёл до human-gate — не упал
    a = next(e for e in events if e["node"] == "analyst")
    assert a["tokens"] == _chars4_usage(llm.prompts["analyst"], "черновик") > 0
    agg = engine.ledger.get("j1", "usage:analyst")
    assert agg["tokens"] == a["tokens"]
    assert "request_id_last" not in agg  # id не было — не выдумываем


def test_job_id_reaches_llm_client() -> None:
    """Движок передаёт job_id в каждый LLM-вызов (metadata запроса шлюза)."""
    llm = GatewayLLM(dict(SCRIPT))
    engine = make_gateway_engine(llm)

    engine.run("j1", epoch=EPOCH)

    assert llm.job_ids and all(j == "j1" for j in llm.job_ids)


def test_quota_charge_uses_real_usage_tokens() -> None:
    """Квот-списание (_bump_usage) — по реальным total_tokens, не по оценке."""
    quota = FakeQuota()
    llm = GatewayLLM(dict(SCRIPT))
    engine = make_gateway_engine(llm, quota=quota)

    paused = engine.run("j1", epoch=EPOCH)
    assert paused.status == "paused"
    res = engine.resume("j1", epoch=EPOCH, token=paused.resume_token)

    assert res.status == "done"
    used = engine.ledger.get("j1", "usage")
    assert used and used["tokens"] == 121 * 3  # analyst + critic + editor, citer — tool-step
    assert quota.charges == [("u1", 121 * 3)]


# ── извлечение токенов из usage-объекта (fail-soft на битых данных) ──────


def test_tokens_from_usage_contract() -> None:
    assert _tokens_from_usage(GW_USAGE) == 121
    assert _tokens_from_usage({"prompt_tokens": 64, "completion_tokens": 57}) == 121
    assert _tokens_from_usage({"total_tokens": 0}) == 0
    # недоступен/битый → None (вызывающий падает обратно на оценку)
    assert _tokens_from_usage(None) is None
    assert _tokens_from_usage({}) is None
    assert _tokens_from_usage({"total_tokens": "мусор"}) is None
