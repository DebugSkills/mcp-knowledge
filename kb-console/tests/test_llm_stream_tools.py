"""Ф2 (ai-workspace) шаг #2b-1: tool-aware SSE-стриминг stream_chat_with_tools.

Hermetic-транспорт — реальный httpx (AsyncClient + MockTransport), как в
test_chat_stream.py; форма SSE-чанков — реальный контракт LiteLLM/OpenAI:
``data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":…,
"function":{"name":…,"arguments":"<фрагмент>"}}]}}]}``, событие
``tool_calls`` приходит один раз при ``finish_reason == "tool_calls"``,
терминатор ``data: [DONE]``.

Контракт tool_loop._drain_llm (#2a): str-дельты текста + dict с ключом
``tool_calls``; адаптер run_turn_stream конвертирует типизированные события
в этот формат (проверяется отдельным тестом).
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb_console.core.llm_stream import (
    LLMStreamError,
    accumulate_tool_calls,
    run_turn_stream,
    stream_chat_with_tools,
)

# ── SSE-фикстуры (реальная раскладка потока LiteLLM) ──────────


def _tool_delta(
    index: int, *, id: str | None = None, name: str | None = None, args: str | None = None
) -> dict:
    """Дельта tool_call: id/name — в первой дельте вызова, arguments — фрагмент."""
    tc: dict = {"index": index, "type": "function"}
    if id is not None:
        tc["id"] = id
    fn: dict = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        tc["function"] = fn
    return tc


def _chunk(
    content: str | None = None,
    *,
    role: str | None = None,
    finish: str | None = None,
    tool_calls: list[dict] | None = None,
) -> dict:
    """Чанк chat.completion.chunk: role → content/tool_calls → finish."""
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choice: dict = {"index": 0, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    return {"id": "c0", "object": "chat.completion.chunk", "choices": [choice]}


def _sse_bytes(
    chunks: list[dict], *, done: bool = True, trailing: dict | None = None
) -> bytes:
    """SSE-поток: keep-alive + data-чанки + терминатор [DONE] (+ мусор после)."""
    out = [b": keep-alive\n\n"]
    for c in chunks:
        out.append(b"data: " + json.dumps(c).encode() + b"\n\n")
    if done:
        out.append(b"data: [DONE]\n\n")
    if trailing is not None:
        out.append(b"data: " + json.dumps(trailing).encode() + b"\n\n")
    return b"".join(out)


def _client(payload: bytes, status_code: int = 200):
    """httpx.AsyncClient на MockTransport + захват тела запроса."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            status_code,
            content=payload,
            headers={"Content-Type": "text/event-stream"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


async def _collect(messages, *, tools=None, client=None, **kw) -> list[dict]:
    """Собрать все события stream_chat_with_tools в список."""
    kwargs: dict = {"client": client}
    if tools is not None:
        kwargs["tools"] = tools
    kwargs.update(kw)
    return [e async for e in stream_chat_with_tools(messages, **kwargs)]


# ── accumulate_tool_calls: чистый helper без сети ─────────────


class TestAccumulateToolCalls:
    def test_arguments_glued_from_fragments_in_order(self):
        """3+ фрагмента arguments по одному index → корректная JSON-строка."""
        chunks = [
            [_tool_delta(0, id="call_1", name="search_knowledge", args='{"qu')],
            [_tool_delta(0, args='ery": "m')],
            [_tool_delta(0, args='cp knowledge"}')],
        ]
        calls = accumulate_tool_calls(chunks)
        assert len(calls) == 1
        assert calls[0]["id"] == "call_1"
        assert calls[0]["type"] == "function"
        assert calls[0]["function"]["name"] == "search_knowledge"
        args = calls[0]["function"]["arguments"]
        assert args == '{"query": "mcp knowledge"}'
        assert json.loads(args) == {"query": "mcp knowledge"}  # валидный JSON

    def test_multiple_indexes_order_and_fields_kept(self):
        """Разные index (приходят вперемешку) → порядок по index, id/name целы."""
        chunks = [
            [_tool_delta(1, id="call_b", name="second", args="[1")],
            [_tool_delta(0, id="call_a", name="first", args='{"x":')],
            [_tool_delta(1, args=", 2]")],
            [_tool_delta(0, args="1}")],
        ]
        calls = accumulate_tool_calls(chunks)
        assert [c["id"] for c in calls] == ["call_a", "call_b"]
        assert [c["function"]["name"] for c in calls] == ["first", "second"]
        assert calls[0]["function"]["arguments"] == '{"x":1}'
        assert calls[1]["function"]["arguments"] == "[1, 2]"

    def test_missing_id_gets_fallback(self):
        """id не пришёл → предсказуемый fallback call_<index>."""
        calls = accumulate_tool_calls([[_tool_delta(2, name="fn", args="{}")]])
        assert calls[0]["id"] == "call_2"

    def test_empty_input(self):
        assert accumulate_tool_calls([]) == []
        assert accumulate_tool_calls([[], []]) == []


# ── stream_chat_with_tools: типизированные события ────────────


class TestStreamChatWithTools:
    async def test_text_only_stream(self):
        """Только текст → text-события + done, БЕЗ tool_calls."""
        payload = _sse_bytes(
            [
                _chunk(role="assistant"),
                _chunk("Раз"),
                _chunk("два"),
                _chunk(finish="stop"),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert events == [
            {"type": "text", "delta": "Раз"},
            {"type": "text", "delta": "два"},
            {"type": "done"},
        ]

    async def test_tool_calls_event_on_finish_reason(self):
        """Дельты tool_calls молча копятся; событие — ОДИН раз на finish_reason."""
        payload = _sse_bytes(
            [
                _chunk(role="assistant", tool_calls=[_tool_delta(0, id="c1", name="search_knowledge", args='{"qu')]),
                _chunk(tool_calls=[_tool_delta(0, args='ery": "x"}')]),
                _chunk(finish="tool_calls"),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert events == [
            {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "search_knowledge", "arguments": '{"query": "x"}'},
                    }
                ],
            },
            {"type": "done"},
        ]

    async def test_mixed_text_then_tool_calls_order(self):
        """Смешанный поток: text-дельты до tool_calls; порядок событий верный."""
        payload = _sse_bytes(
            [
                _chunk(role="assistant", content="Ищу…"),
                _chunk(tool_calls=[_tool_delta(0, id="c1", name="f", args='{"a')]),
                _chunk(tool_calls=[_tool_delta(0, args='": 1}')]),
                _chunk(finish="tool_calls"),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert [e["type"] for e in events] == ["text", "tool_calls", "done"]
        assert events[0]["delta"] == "Ищу…"
        assert events[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'

    async def test_multiple_tool_calls_in_one_stream(self):
        """Параллельные вызовы (index 0 и 1) → оба в одном событии по порядку."""
        payload = _sse_bytes(
            [
                _chunk(tool_calls=[_tool_delta(0, id="c0", name="f", args='{"i":')]),
                _chunk(tool_calls=[_tool_delta(1, id="c1", name="g", args='{"j":')]),
                _chunk(tool_calls=[_tool_delta(0, args="0}")]),
                _chunk(tool_calls=[_tool_delta(1, args="1}")]),
                _chunk(finish="tool_calls"),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert events[0]["type"] == "tool_calls"
        assert [c["id"] for c in events[0]["tool_calls"]] == ["c0", "c1"]
        assert [c["function"]["arguments"] for c in events[0]["tool_calls"]] == [
            '{"i":0}',
            '{"j":1}',
        ]

    async def test_done_terminates_generator_ignores_trailing(self):
        """[DONE] завершает генератор; чанки после терминатора игнорируются."""
        payload = _sse_bytes(
            [_chunk("a")], trailing=_chunk("после-терминатора")
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert events == [{"type": "text", "delta": "a"}, {"type": "done"}]

    async def test_no_finish_reason_emits_on_done(self):
        """Прокси без finish_reason: накопленные вызовы выдаются на [DONE]."""
        payload = _sse_bytes(
            [
                _chunk(tool_calls=[_tool_delta(0, id="c1", name="f", args="{}")]),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            events = await _collect([{"role": "user", "content": "q"}], client=client)
        assert [e["type"] for e in events] == ["tool_calls", "done"]

    async def test_tools_schema_passed_in_payload(self):
        """tools попадают в JSON-тело запроса; stream=True; model default."""
        schema = {
            "type": "function",
            "function": {"name": "search_knowledge", "parameters": {"type": "object"}},
        }
        client, cap = _client(_sse_bytes([_chunk("ок")]))
        async with client:
            await _collect(
                [{"role": "user", "content": "q"}], tools=[schema], client=client,
                api_key="k-test", session_id="s-1",
            )
        assert cap["body"]["tools"] == [schema]
        assert cap["body"]["stream"] is True
        assert cap["body"]["model"] == "local"
        assert cap["auth"] == "Bearer k-test"
        assert "tools" not in cap["body"] or isinstance(cap["body"]["tools"], list)

    async def test_no_tools_key_in_payload_when_none(self):
        """tools не передан → ключа tools в теле НЕТ (чистый чат)."""
        client, cap = _client(_sse_bytes([_chunk("ок")]))
        async with client:
            await _collect([{"role": "user", "content": "q"}], client=client)
        assert "tools" not in cap["body"]

    async def test_non_200_raises_llm_stream_error_without_secret(self):
        """429 → LLMStreamError (как stream_chat); ключ НЕ утекает."""
        body = json.dumps({"error": {"message": "rate limited"}}).encode()
        client, _cap = _client(body, status_code=429)
        async with client:
            with pytest.raises(LLMStreamError) as ei:
                async for _e in stream_chat_with_tools(
                    [{"role": "user", "content": "q"}],
                    api_key="k-super-secret",
                    client=client,
                ):
                    pass
        assert "429" in str(ei.value)
        assert "rate limited" in str(ei.value)
        assert "k-super-secret" not in str(ei.value)


# ── Адаптер для tool_loop.run_turn (контракт _drain_llm) ──────


class TestRunTurnStreamAdapter:
    """run_turn_stream: события stream_chat_with_tools → str/{"tool_calls": …}."""

    async def test_events_converted_to_drain_llm_contract(self):
        payload = _sse_bytes(
            [
                _chunk(role="assistant", content="Дум"),
            ]
            + [_chunk(content="аю…")]
            + [
                _chunk(tool_calls=[_tool_delta(0, id="c1", name="search_knowledge", args='{"query": "x"}')]),
                _chunk(finish="tool_calls"),
            ]
        )
        client, _cap = _client(payload)
        async with client:
            out = [
                e
                async for e in run_turn_stream(
                    [{"role": "user", "content": "q"}],
                    tools=[{"type": "function", "function": {"name": "search_knowledge"}}],
                    client=client,
                )
            ]
        # str-дельты + один dict с tool_calls (OpenAI-вид для _normalize_tool_calls)
        strs = [e for e in out if isinstance(e, str)]
        dicts = [e for e in out if isinstance(e, dict)]
        assert strs == ["Дум", "аю…"]
        assert len(dicts) == 1
        assert dicts[0]["tool_calls"][0]["id"] == "c1"
        assert dicts[0]["tool_calls"][0]["function"]["name"] == "search_knowledge"
        assert json.loads(dicts[0]["tool_calls"][0]["function"]["arguments"]) == {
            "query": "x"
        }

    async def test_adapter_propagates_llm_stream_error(self):
        """Ошибка стрима пробрасывается как LLMStreamError (доминируется run_turn)."""
        client, _cap = _client(b"{}", status_code=500)
        async with client:
            with pytest.raises(LLMStreamError):
                async for _e in run_turn_stream(
                    [{"role": "user", "content": "q"}], client=client
                ):
                    pass
