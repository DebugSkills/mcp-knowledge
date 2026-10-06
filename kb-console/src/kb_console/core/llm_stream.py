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
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

DEFAULT_MODEL = "local"
DEFAULT_LLM_URL = "http://litellm:4000/v1"
DEFAULT_TIMEOUT_S = 120.0

_DONE: object = object()  # sentinel терминатора data: [DONE]


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
