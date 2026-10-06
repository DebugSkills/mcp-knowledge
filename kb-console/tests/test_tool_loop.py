"""Hermetic-тесты tool-loop (Ф2 шаг #2a, arch-2026-10-05-ai-workspace).

Несущая проверка I5/I6: зона ``search_knowledge`` берётся из серверного
параметра (роль), а НЕ из аргументов модели. Тест (a) — red-first
мутационный детектор: реализация, читающая ``arguments["zone"]`` (например
``params = dict(arguments); params.setdefault("zone", zone)``), краснеет.

Моки проходят проверку реальным типом (правило 10): FakeMCP — подкласс
настоящего MCPClient; llm_stream — скриптованный async-gen по контракту
модуля (str-дельты | {"tool_calls": [...]}).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from kb_console.core.auth_state import TransportError
from kb_console.core.llm_stream import LLMStreamError
from kb_console.core.mcp_client import MCPClient
from kb_console.core.tool_loop import (
    SEARCH_TOOL_NAME,
    SEARCH_TOOL_SCHEMA,
    ToolLoopError,
    run_turn,
)


def _tool_call_event(call_id: str, arguments: Any, name: str = SEARCH_TOOL_NAME) -> dict[str, Any]:
    """Событие tool_calls (OpenAI-вид); arguments — JSON-строка или dict."""
    return {
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ]
    }


class FakeMCP(MCPClient):
    """Fake MCPClient: пишет все tools_call-вызовы; isinstance — настоящий."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result if result is not None else {"results": []}
        self._error = error

    async def tools_call(  # type: ignore[override]
        self, name: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        self.calls.append((name, dict(params or {})))
        if self._error is not None:
            raise self._error
        return self._result


class FakeLLM:
    """Скриптованный LLM: сценарий на каждый вызов (последний — повторяется).

    Сценарий — list событий (str | dict | Exception) либо callable(messages)
    → list событий. Пишет снимки messages и полученные tools.
    """

    def __init__(self, scripts: list[Any]) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[Any] = []

    async def __call__(
        self, messages: list[dict[str, Any]], *, tools: Any = None
    ) -> AsyncIterator[Any]:
        idx = len(self.calls)
        snapshot = [dict(m) for m in messages]
        self.calls.append(snapshot)
        if tools is not None:
            self.tools_seen.append(tools)
        script = self._scripts[min(idx, len(self._scripts) - 1)]
        if callable(script):
            script = script(snapshot)
        for event in script:
            if isinstance(event, Exception):
                raise event
            yield event


class _Tracker:
    def __init__(self) -> None:
        self.current = 0
        self.max_seen = 0


class TrackingLLM:
    """LLM с подсчётом одновременных вызовов (детектор single-flight)."""

    def __init__(self, tracker: _Tracker, sleep_s: float = 0.05) -> None:
        self._tracker = tracker
        self._sleep_s = sleep_s

    async def __call__(
        self, messages: list[dict[str, Any]], *, tools: Any = None
    ) -> AsyncIterator[Any]:
        self._tracker.current += 1
        self._tracker.max_seen = max(self._tracker.max_seen, self._tracker.current)
        try:
            await asyncio.sleep(self._sleep_s)
            yield "ok"
        finally:
            self._tracker.current -= 1


# ── (a) зона из роли, не из аргументов модели (red-first мутационный детектор) ──


async def test_zone_injected_from_server_not_from_model_args() -> None:
    """Модель подсовывает zone=private (JSON-строка И dict) — фактический
    вызов MCP идёт с zone=public (роль contributor). Детектор I5/I6."""
    fake_mcp = FakeMCP(result={"results": [{"knowledge_id": "k1"}]})
    assert isinstance(fake_mcp, MCPClient)  # мок — реальный тип (правило 10)
    llm = FakeLLM(
        [
            [_tool_call_event("c1", json.dumps({"query": "fpf", "top_k": 5, "zone": "private"}))],
            ["Готово"],
        ]
    )
    text = await run_turn(
        [{"role": "user", "content": "найди fpf"}],
        session_id="t-a-json",
        zone="public",
        mcp_client=fake_mcp,
        llm_stream=llm,
    )
    assert text == "Готово"
    assert len(fake_mcp.calls) == 1
    name, params = fake_mcp.calls[0]
    assert name == SEARCH_TOOL_NAME
    assert params["zone"] == "public"  # зона из серверного параметра, НЕ из args
    assert params["query"] == "fpf"
    assert params["top_k"] == 5

    # тот же детектор для dict-аргументов
    fake_mcp2 = FakeMCP(result={"results": []})
    llm2 = FakeLLM(
        [
            [_tool_call_event("c1", {"query": "q", "zone": "private"})],
            ["Готово 2"],
        ]
    )
    text2 = await run_turn(
        [{"role": "user", "content": "q"}],
        session_id="t-a-dict",
        zone="public",
        mcp_client=fake_mcp2,
        llm_stream=llm2,
    )
    assert text2 == "Готово 2"
    assert fake_mcp2.calls[0][1]["zone"] == "public"

    # гигиена схемы (I5): зона не раскрывается модели ни в schema, ни в tools
    assert "zone" not in json.dumps(SEARCH_TOOL_SCHEMA, ensure_ascii=False)
    assert llm.tools_seen, "схема инструмента не дошла до LLM"
    assert "zone" not in json.dumps(llm.tools_seen, ensure_ascii=False)


# ── (b) tool-результат попадает в контекст, финальный текст собирается ──


async def test_tool_result_reaches_context_and_final_text() -> None:
    fake_mcp = FakeMCP(
        result={"results": [{"knowledge_id": "kb-1", "title": "FPF Basics", "score": 0.9}]}
    )

    def final_answer(messages: list[dict[str, Any]]) -> list[Any]:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs and "FPF Basics" in str(tool_msgs[0].get("content")):
            return ["Итог: FPF Basics"]
        return ["Итог: ничего не найдено"]

    llm = FakeLLM(
        [
            [_tool_call_event("c1", {"query": "first principles"})],
            final_answer,
        ]
    )
    text = await run_turn(
        [{"role": "user", "content": "что такое FPF?"}],
        session_id="t-b",
        zone="public",
        mcp_client=fake_mcp,
        llm_stream=llm,
    )
    assert text == "Итог: FPF Basics"
    # tool-результат реально в контексте второго LLM-вызова
    second = llm.calls[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and "FPF Basics" in tool_msgs[0]["content"]
    # assistant-сообщение с tool_calls тоже в контексте
    assert [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]


# ── (c) лимит итераций на «зацикленной» модели ──


async def test_max_iters_stops_looping_model() -> None:
    fake_mcp = FakeMCP(result={"results": []})
    llm = FakeLLM([[_tool_call_event("c-loop", {"query": "again"})]])  # всегда tool
    with pytest.raises(ToolLoopError) as ei:
        await run_turn(
            [{"role": "user", "content": "цикл"}],
            session_id="t-c",
            zone="public",
            mcp_client=fake_mcp,
            llm_stream=llm,
            max_iters=2,
        )
    assert ei.value.kind == "max_iterations"
    assert len(llm.calls) == 2  # LLM вызван ровно max_iters раз — не бесконечно
    assert len(fake_mcp.calls) == 2


# ── (d) single-flight: один session_id — нет двух одновременных LLM-вызовов ──


async def test_single_flight_same_session_serializes_llm_calls() -> None:
    tracker = _Tracker()
    llm = TrackingLLM(tracker, sleep_s=0.05)
    msgs = [{"role": "user", "content": "hi"}]
    results = await asyncio.gather(
        run_turn(msgs, session_id="t-d", zone="public", llm_stream=llm),
        run_turn(msgs, session_id="t-d", zone="public", llm_stream=llm),
    )
    assert results == ["ok", "ok"]
    assert tracker.max_seen == 1  # два конкурентных хода не дали 2 одновременных

    # валидация детектора: разные сесси не сериализуются (конкуренция видна)
    tracker2 = _Tracker()
    llm2 = TrackingLLM(tracker2, sleep_s=0.05)
    await asyncio.gather(
        run_turn(msgs, session_id="t-d-x", zone="public", llm_stream=llm2),
        run_turn(msgs, session_id="t-d-y", zone="public", llm_stream=llm2),
    )
    assert tracker2.max_seen == 2


# ── (e) 429 → доменное исключение, без утечки ключей ──


async def test_llm_429_maps_to_domain_error_without_secret_leak() -> None:
    fake_mcp = FakeMCP()
    llm = FakeLLM([[LLMStreamError("LiteLLM HTTP 429 Too Many Requests: quota exceeded")]])
    with pytest.raises(ToolLoopError) as ei:
        await run_turn(
            [{"role": "user", "content": "q"}],
            session_id="t-e-llm",
            zone="public",
            mcp_client=fake_mcp,
            llm_stream=llm,
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.status == 429
    leaked = str(ei.value)
    assert "Bearer" not in leaked
    assert "LITELLM" not in leaked
    assert "sk-" not in leaked


async def test_mcp_429_maps_to_domain_error() -> None:
    fake_mcp = FakeMCP(error=TransportError("Слишком много запросов: превышен rate limit (429)"))
    llm = FakeLLM([[_tool_call_event("c1", {"query": "q"})]])
    with pytest.raises(ToolLoopError) as ei:
        await run_turn(
            [{"role": "user", "content": "q"}],
            session_id="t-e-mcp",
            zone="public",
            mcp_client=fake_mcp,
            llm_stream=llm,
        )
    assert ei.value.kind == "rate_limit"
    assert ei.value.status == 429
