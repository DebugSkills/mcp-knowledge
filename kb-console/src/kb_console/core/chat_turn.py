"""Один ход чата через tool-loop: токен-стрим в on_delta + персист истории.

Без глобального состояния: LLM-стрим, MCP-клиент и store инжектируются,
поэтому модуль тестируется без сети и Redis.
"""

from .llm_stream import run_turn_stream
from .tool_loop import run_turn, text_tool_call_display_hold


async def chat_turn(
    messages,
    *,
    session_id,
    zone,
    user=None,
    store=None,
    mcp_client=None,
    llm_stream=None,
    on_delta=None,
    max_iters=4,
) -> dict:
    """Один ход чата через tool-loop: стрим дельт в on_delta + персист.

    Возврат: ``{"text": str, "saved": bool}``.
    """
    base_stream = llm_stream or run_turn_stream

    if on_delta is None:
        stream = base_stream
    else:

        async def _tee(msgs, **kwargs):
            # Ф6-5/S1: tool-итерация в текстовой форме (деградация LiteLLM
            # stream+tools) не показывается — on_delta получает только дельты
            # естественно-языкового ответа; носитель гарантирован fallback'ом
            # страницы (пустой acc → показ финального text).
            mode: str | None = None  # None = решаем; далее "stream" | "hold"
            buf = ""
            async for event in base_stream(msgs, **kwargs):
                if isinstance(event, str) and event:
                    if mode is None:
                        buf += event
                        verdict = text_tool_call_display_hold(buf)
                        if verdict is False:
                            mode = "stream"
                            on_delta(buf)
                            buf = ""
                        elif verdict is True:
                            mode = "hold"
                    elif mode == "stream":
                        on_delta(event)
                yield event

        stream = _tee

    text = await run_turn(
        messages,
        session_id=session_id,
        zone=zone,
        mcp_client=mcp_client,
        llm_stream=stream,
        max_iters=max_iters,
    )

    saved = False
    if store is not None and user:
        try:
            last = messages[-1] if messages else None
            if isinstance(last, dict) and last.get("role") == "user":
                store.append(user, session_id, last)
            store.append(user, session_id, {"role": "assistant", "content": text})
            saved = True
        except Exception:  # noqa: BLE001 — fail-soft персист по контракту
            saved = False

    return {"text": text, "saved": saved}
