"""Асинхронный SSE-клиент к LiteLLM (OpenAI-совместимый /chat/completions).

Ф2 (arch-2026-10-05-ai-workspace) шаг #1: чат-ядро верстака получает текст
инкрементально — yield'ит дельты по мере поступления чанков, БЕЗ буферизации
всего ответа (спека §8 «Ф2-спека» E: httpx-stream → element.update() по
чанкам).

Контракт:
- endpoint: ``{WS_LLM_URL}/chat/completions``; default — имя сервиса LiteLLM
  из compose.gateway.yml (``http://litellm:4000/v1``, сеть mcp-knowledge);
- auth: ``Bearer $LITELLM_MASTER_KEY`` — ключ читается из env, НИКОГДА не
  логируется и не попадает в текст исключений;
- SSE: строки ``data: {...}`` с ``choices[0].delta.content``; терминатор
  ``data: [DONE]``; пустые строки и keep-alive-комментарии (``: ...``)
  пропускаются; чанки без content (role/finish_reason) не yield'ятся;
- не-200 → LLMStreamError с HTTP-кодом и причиной (ключ/сырое тело НЕ входят);
- httpx-клиент инжектируемый (hermetic-тесты идут по реальному httpx-пути
  через MockTransport); собственный клиент закрывается сам, инжектированный
  вызывающий закрывает сам.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Any

import httpx

DEFAULT_MODEL = "local"
DEFAULT_LLM_URL = "http://litellm:4000/v1"
DEFAULT_TIMEOUT_S = 120.0

_DONE: object = object()  # sentinel терминатора data: [DONE]

logger = logging.getLogger("kb_console.llm_stream")


class LLMStreamError(RuntimeError):
    """Стрим-запрос не удался (HTTP-код/транспорт). Без ключа и тела-секрета."""


def default_llm_url() -> str:
    """Базовый URL LiteLLM из env WS_LLM_URL (default — сервис litellm:4000)."""
    return os.environ.get("WS_LLM_URL") or DEFAULT_LLM_URL


def default_api_key() -> str:
    """Ключ из env LITELLM_MASTER_KEY (в логи/исключения НЕ попадает)."""
    return os.environ.get("LITELLM_MASTER_KEY", "")


def _parse_sse_line(line: str) -> object:
    """SSE-строка → текстовая дельта (str) | ``_DONE`` | None (пропуск).

    Реальный контракт LiteLLM/OpenAI: ``data: {"choices":[{"delta":{...}}]}``.
    Пустые строки, keep-alive ``: comment`` и не-data строки — пропуск;
    мусорный/обрезанный JSON — пропуск (стрим не роняем).
    """
    line = line.rstrip("\r")
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    if data == "[DONE]":
        return _DONE
    try:
        chunk = json.loads(data)
    except ValueError:
        return None
    if not isinstance(chunk, dict):
        return None
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def _error_detail(body: bytes) -> str:
    """Короткая причина из тела ошибки (``{"error": {"message": …}}``, ≤200 симв.).

    Сырое тело и заголовки запроса (там ключ) в исключение НЕ входят — только
    распарсенное error.message/detail.
    """
    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"][:200]
        if isinstance(payload.get("detail"), str):
            return payload["detail"][:200]
    return ""


async def stream_chat(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """Забрать чат-ответ LiteLLM как поток текстовых дельт (async-generator).

    Yield'ит только непустые ``choices[0].delta.content`` в порядке прихода;
    ``data: [DONE]`` завершает итерацию. Не-200 (вкл. 429) → LLMStreamError
    («HTTP <код>» + причина, без секрета); сетевые сбои пробрасываются как
    httpx.HTTPError — вызывающий (страница) обязан показать их в UI без
    падения. ``stream=True`` и в JSON-пейлоаде, и в httpx (потоковая передача).
    """
    base = (url or default_llm_url()).rstrip("/")
    endpoint = f"{base}/chat/completions"
    key = api_key if api_key is not None else default_api_key()
    headers = {"Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "stream": True,
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        async with client.stream(
            "POST", endpoint, json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                msg = f"LiteLLM HTTP {response.status_code}"
                reason = response.reason_phrase or ""
                if reason:
                    msg += f" {reason}"
                detail = _error_detail(body)
                if detail:
                    msg += f": {detail}"
                raise LLMStreamError(msg)
            async for line in response.aiter_lines():
                parsed = _parse_sse_line(line)
                if parsed is None:
                    continue
                if parsed is _DONE:
                    return
                yield str(parsed)
    finally:
        if own_client:
            await client.aclose()


# ── Tool-aware стриминг (Ф2 шаг #2b-1) ──────────────────────────────


def accumulate_tool_calls(
    chunks: Iterable[Iterable[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Склеить дельты ``delta.tool_calls`` SSE-чанков в OpenAI-вид вызовов.

    Реальный контракт LiteLLM/OpenAI: каждый SSE-чанк несёт список
    ``choices[0].delta.tool_calls[i]``, где ``index`` — слот вызова;
    ``id`` и ``function.name`` приходят только в ПЕРВОЙ дельте слота,
    ``function.arguments`` — фрагментами (склеиваются в строку по порядку
    прихода). Возвращает список ``{"id", "type": "function", "function":
    {"name", "arguments"}}`` упорядоченный по ``index``; отсутствие ``id``
    → fallback ``call_<index>``. Чистая функция (сеть не нужна).
    """
    slots: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for piece in chunk:
            if not isinstance(piece, Mapping):
                continue
            try:
                idx = int(piece.get("index", 0))
            except (TypeError, ValueError):
                idx = 0
            slot = slots.setdefault(idx, {"id": None, "name": "", "args": []})
            if piece.get("id"):
                slot["id"] = str(piece["id"])
            fn = piece.get("function")
            if isinstance(fn, Mapping):
                if fn.get("name"):
                    slot["name"] = str(fn["name"])
                fragment = fn.get("arguments")
                if isinstance(fragment, str) and fragment:
                    slot["args"].append(fragment)
    return [
        {
            "id": slot["id"] or f"call_{idx}",
            "type": "function",
            "function": {
                "name": slot["name"],
                "arguments": "".join(slot["args"]),
            },
        }
        for idx, slot in sorted(slots.items())
    ]


def _parse_sse_tool_event(line: str) -> dict[str, Any] | object | None:
    """SSE-строка → ``{content?, tool_calls?, finish_reason?}`` | ``_DONE`` | None.

    Расширенный парсер для tool-aware стрима (в дополнение к
    ``_parse_sse_line``, которая остаётся текстовой — регресс #1/#2a):
    вынимает ``choices[0].delta.{content,tool_calls}`` и
    ``choices[0].finish_reason``; мусорный/обрезанный JSON — пропуск.
    """
    line = line.rstrip("\r")
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    if data == "[DONE]":
        return _DONE
    try:
        chunk = json.loads(data)
    except ValueError:
        return None
    if not isinstance(chunk, dict):
        return None
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    event: dict[str, Any] = {}
    delta = choices[0].get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str) and content:
            event["content"] = content
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            event["tool_calls"] = tool_calls
    finish = choices[0].get("finish_reason")
    if isinstance(finish, str) and finish:
        event["finish_reason"] = finish
    return event or None


async def stream_chat_with_tools(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    tools: Sequence[Mapping[str, Any]] | None = None,
    session_id: str | None = None,
    url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Tool-aware SSE-стрим: типизированные события function-calling (Ф2 #2b-1).

    Yield'ит события-дикты в порядке прихода:

    - ``{"type": "text", "delta": "<строка>"}`` — контентные дельты;
    - ``{"type": "tool_calls", "tool_calls": [{"id", "type": "function",
      "function": {"name", "arguments"}}]}`` — ОДИН раз при
      ``finish_reason == "tool_calls"`` (аргументы — склеенные строки;
      страховка: если вызовы накоплены, но [DONE] пришёл без
      finish_reason — событие выдаётся на терминаторе);
    - ``{"type": "done"}`` — по ``data: [DONE]``.

    Ошибки — согласованно со ``stream_chat``: НЕ-200 (вкл. 429) → raise
    ``LLMStreamError`` (без ключа/секрета); событий ``{"type": "error"}``
    генератор НЕ выдаёт — доминирование ошибок делает вызывающий
    (tool_loop.run_turn ловит LLMStreamError и превращает в ToolLoopError).

    ``tools`` (OpenAI-схемы) при наличии попадают в JSON-тело; ``session_id``
    — только для трассировки в debug-логе; прочие kwargs (temperature и
    т.п.) прокидываются в пейлоад как есть. httpx-клиент инжектируемый
    (hermetic-тесты через MockTransport).
    """
    base = (url or default_llm_url()).rstrip("/")
    endpoint = f"{base}/chat/completions"
    key = api_key if api_key is not None else default_api_key()
    headers = {"Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(m) for m in messages],
        "stream": True,
    }
    if tools:
        payload["tools"] = [dict(t) for t in tools]
    payload.update(kwargs)
    logger.debug(
        "tool-aware stream start (session=%s, model=%s, tools=%d)",
        session_id,
        model,
        len(tools) if tools else 0,
    )

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        async with client.stream(
            "POST", endpoint, json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                msg = f"LiteLLM HTTP {response.status_code}"
                reason = response.reason_phrase or ""
                if reason:
                    msg += f" {reason}"
                detail = _error_detail(body)
                if detail:
                    msg += f": {detail}"
                raise LLMStreamError(msg)
            pending: list[list[Any]] = []
            emitted_tools = False
            async for line in response.aiter_lines():
                parsed = _parse_sse_tool_event(line)
                if parsed is None or parsed is _DONE:
                    if parsed is _DONE:
                        if pending and not emitted_tools:
                            yield {
                                "type": "tool_calls",
                                "tool_calls": accumulate_tool_calls(pending),
                            }
                        yield {"type": "done"}
                        return
                    continue
                assert isinstance(parsed, dict)
                if parsed.get("content"):
                    yield {"type": "text", "delta": parsed["content"]}
                if parsed.get("tool_calls"):
                    pending.append(parsed["tool_calls"])
                if (
                    parsed.get("finish_reason") == "tool_calls"
                    and pending
                    and not emitted_tools
                ):
                    yield {
                        "type": "tool_calls",
                        "tool_calls": accumulate_tool_calls(pending),
                    }
                    emitted_tools = True
    finally:
        if own_client:
            await client.aclose()


async def _adapt_events_for_run_turn(
    events: AsyncIterator[Mapping[str, Any]],
) -> AsyncIterator[Any]:
    """Типизированные события → контракт ``tool_loop._drain_llm``.

    ``{"type": "text"}`` → str-дельта; ``{"type": "tool_calls"}`` → dict с
    ключом ``tool_calls`` (OpenAI-вид); ``done`` — пропуск (итерация и так
    завершится). ``error``-событий нет — LLMStreamError пробрасывается.
    """
    async for event in events:
        etype = event.get("type")
        if etype == "text":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield delta
        elif etype == "tool_calls":
            calls = event.get("tool_calls")
            if isinstance(calls, list) and calls:
                yield {"tool_calls": calls}


def run_turn_stream(
    messages: Sequence[Mapping[str, Any]], **kwargs: Any
) -> AsyncIterator[Any]:
    """Адаптер stream_chat_with_tools под инжект ``tool_loop.run_turn``.

    Использование (#2b-2): ``await run_turn(..., llm_stream=run_turn_stream)``.
    Подпись ``(messages, **kwargs)`` выдерживает kwarg ``tools`` —
    ``tool_loop._accepts_kwarg`` распознает её и передаст схему инструмента
    (нативный function-calling); итерация даёт str-дельты и/или
    ``{"tool_calls": […]}`` — ровно то, что читает ``_drain_llm``.
    """
    return _adapt_events_for_run_turn(stream_chat_with_tools(messages, **kwargs))
