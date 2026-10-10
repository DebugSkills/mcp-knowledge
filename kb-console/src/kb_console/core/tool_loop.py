"""Tool-loop: цикл function-calling для чата AI-верстака (Ф2 шаг #2a).

Трасса: arch-2026-10-05-ai-workspace. План: plans/arch-2026-10-05-ai-workspace-plan.md
(Фаза 2). Спека: .boardData.md §8 «Ф2-спека» (E, H, P1-4).

Несущая безопасность (I5/I6):
- параметр ``zone`` приходит ТОЛЬКО из серверного слоя (роль пользователя,
  ws_zone.zone_for_role / zone_for_identity) и инжектится сервером при вызове
  MCP-тула ``search_knowledge``;
- поля ``zone`` в схеме инструмента НЕТ; если модель пришлёт ``zone`` в
  arguments — аргумент игнорируется (модель НЕ может повысить свою зону);
- MCP-вызов идёт ЕДИНЫМ сервисным ключом верстака (env ``WS_MCP_KEY``,
  read+zone=both), зону выборки задаёт ws-слой.

Защиты: лимит итераций ``max_iters`` (зацикливание 7B); single-flight на
сессию (asyncio.Lock-реестр по ``session_id``); 429 от LLM/MCP → доменное
исключение ``ToolLoopError(kind="rate_limit", status=429)``; сырые исключения
наружу не утекают, ключи в тексты ошибок не входят.

Контракт ``llm_stream`` (инжектируемый, дефолт — llm_stream.stream_chat):
async-generator, yield'ит str (текстовая дельта) либо dict с ключом
``tool_calls`` (OpenAI-вид структурных вызовов); прочие события — пропуск.
Дефолтный stream_chat даёт только текст; тул-aware стрим подставляется
инжектом (обе формы совместимы с одним циклом).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from .auth_state import AuthenticationError, ForbiddenError, TransportError
from .llm_stream import LLMStreamError, stream_chat
from .mcp_client import MCPClient

logger = logging.getLogger("kb_console.tool_loop")

SEARCH_TOOL_NAME = "search_knowledge"

SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL_NAME,
        "description": "Семантический поиск по базе знаний сообщества.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос."},
                "top_k": {"type": "integer", "description": "Число результатов (1-50)."},
            },
            "required": ["query"],
        },
    },
}
"""Схема инструмента для модели. Поля ``zone`` НЕТ (I5): зону задаёт сервер."""

MAX_TOOL_CONTENT_CHARS = 8000
"""Ограничение tool-результата в контексте (защита контекста 7B)."""

DEFAULT_MCP_TIMEOUT_S = 30.0

PUBLIC_ZONE = "public"
VALID_ZONES = frozenset({"public", "private"})

_RATE_LIMIT_MSG = "Превышен лимит запросов (429); повторите попытку позже."


class ToolLoopError(Exception):
    """Доменная ошибка tool-loop. Ключи и сырые тела ответов НЕ входят.

    kind: ``rate_limit`` | ``max_iterations`` | ``llm_error`` | ``mcp_error``
    | ``internal``; status — HTTP-код, если применимо (429).
    """

    def __init__(self, message: str, *, kind: str = "internal", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


# ── Single-flight: реестр Lock'ов по session_id ─────────────────────
# Lock'и не удаляются (сессии консоли ограничены); создаются лениво под
# текущий event loop при первом обращении к сессии.

_session_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    """Lock сессии: параллельные run_turn с одним session_id сериализуются."""
    async with _locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_id] = lock
        return lock


# ── Хелперы ─────────────────────────────────────────────────────────


def _tool_system_message() -> dict[str, Any]:
    """Системное описание инструмента для модели (zone не раскрывается)."""
    schema = json.dumps(SEARCH_TOOL_SCHEMA["function"]["parameters"], ensure_ascii=False)
    return {
        "role": "system",
        "content": (
            "Ты можешь обращаться к базе знаний сообщества через инструмент "
            f"``{SEARCH_TOOL_NAME}`` (schema: {schema}). Если нужны материалы — "
            "запроси инструмент, дождись результата и используй его в ответе. "
            "Зона доступа (public/private) назначается сервером автоматически "
            "и от тебя не зависит."
        ),
    }


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Выдерживает ли llm_stream kwarg ``name`` (например, tools)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    param = sig.parameters.get(name)
    if param is None:
        return False
    return param.kind is inspect.Parameter.VAR_KEYWORD or param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _is_rate_limit(text: str) -> bool:
    """429-признак по текстам клиентов (LLMStreamError «HTTP 429» /
    TransportError «rate limit (429)») — фиксированные шаблоны клиентов."""
    t = text.lower()
    return "429" in t or "rate limit" in t or "too many requests" in t


def _normalize_tool_calls(raw_calls: list[Any]) -> list[dict[str, Any]]:
    """OpenAI-вид tool_calls → нормализованные {id, name, arguments, parse_error}."""
    calls: list[dict[str, Any]] = []
    for i, tc in enumerate(raw_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        raw_args = fn.get("arguments")
        parse_error = False
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                parsed, parse_error = {}, True
            else:
                if not isinstance(parsed, dict):
                    parsed, parse_error = {}, True
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        calls.append(
            {
                "id": str(tc.get("id") or f"call_{i}"),
                "name": str(fn.get("name") or ""),
                "arguments": parsed,
                "parse_error": parse_error,
            }
        )
    return calls


def _search_params(arguments: Mapping[str, Any], zone: str) -> dict[str, Any] | None:
    """Аргументы модели → параметры search_knowledge. Зона — ТОЛЬКО из сервера.

    Несущая безопасность (I5/I6): ``params["zone"]`` ставится из серверного
    параметра ``zone``; ключ ``zone`` из arguments модели НЕ читается — даже
    явное ``zone=private`` от модели не меняет зону вызова.
    Возвращает None при невалидном query (вызывающий кормит модели ошибку).
    """
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    params: dict[str, Any] = {"query": query, "zone": zone}
    top_k = arguments.get("top_k")
    if isinstance(top_k, int) and not isinstance(top_k, bool):
        params["top_k"] = max(1, min(50, top_k))
    return params


async def _execute_tool_call(mcp: MCPClient, call: dict[str, Any], zone: str) -> Any:
    """Выполнить один tool-вызов через MCPClient (tools_call)."""
    if call["parse_error"]:
        return {"error": "некорректные аргументы инструмента (ожидался JSON-объект)"}
    if call["name"] != SEARCH_TOOL_NAME:
        return {"error": f"неизвестный инструмент: {call['name']!r}"}
    params = _search_params(call["arguments"], zone)
    if params is None:
        return {"error": "аргумент query обязателен (непустая строка)"}
    return await mcp.tools_call(SEARCH_TOOL_NAME, params, timeout=DEFAULT_MCP_TIMEOUT_S)


def _format_tool_result(result: Any) -> str:
    """Tool-результат → JSON-строка для контекста (с ограничением размера)."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return text[:MAX_TOOL_CONTENT_CHARS]


async def _drain_llm(
    llm_stream: Callable[..., AsyncIterator[Any]], messages: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Один LLM-вызов → (текст, tool_calls).

    llm_stream, выдерживающий kwarg ``tools``, получает схему инструмента
    (нативный function-calling); иначе — вызов только с messages (описание
    инструмента уже есть в системном сообщении).
    """
    if _accepts_kwarg(llm_stream, "tools"):
        events = llm_stream(messages, tools=[SEARCH_TOOL_SCHEMA])
    else:
        events = llm_stream(messages)
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    async for event in events:
        if isinstance(event, str):
            if event:
                text_parts.append(event)
        elif isinstance(event, dict):
            raw = event.get("tool_calls")
            if isinstance(raw, list):
                tool_calls.extend(_normalize_tool_calls(raw))
    return "".join(text_parts), tool_calls


def _default_mcp_client() -> MCPClient:
    """Прод-дефолт (ленивый): сервисный ключ верстака из env WS_MCP_KEY (I6)."""
    return MCPClient(
        base_url=os.environ.get("WS_MCP_URL", "http://localhost:8000"),
        api_key=os.environ.get("WS_MCP_KEY", ""),
    )



# ── Текстовая форма tool-call (Ф6-5/S1: LiteLLM stream+tools) ──────
# Носители: ollama direct и LiteLLM non-stream отдают нативные tool_calls;
# LiteLLM 1.104 при stream=true+tools кладёт вызов в content текстом
# (envelope ``{"name": …, "arguments": …}`` либо fenced/голый JSON с
# аргументами). Обе формы обязаны исполняться, иначе MCP не вызывается,
# а сырой JSON уходит пользователю как «ответ».

_JSON_OBJECT_RE = re.compile(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}")
_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\r?\n?(.*?)```", re.DOTALL)

#: Ключи args-only формы (подмножество схемы search_knowledge без zone —
#: зону инжектит сервер, модель её не видит).
_TEXT_CALL_ARG_KEYS = frozenset({"query", "top_k"})


def _iter_text_call_candidates(text: str) -> list[str]:
    """Кандидаты JSON-объектов из текста итерации: весь текст (обрезанный),
    содержимое ```-fence'ов и вложенные JSON-объекты (до 3 уровней)."""
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    candidates += [m.group(0) for m in _JSON_OBJECT_RE.finditer(text)]
    seen: set[str] = set()
    uniq: list[str] = []
    for cand in candidates:
        if cand and cand not in seen:
            seen.add(cand)
            uniq.append(cand)
    return uniq


def parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
    """Распознать текстовую форму вызова ``search_knowledge`` (Ф6-5/S1).

    Формы (реальные live-наблюдения через LiteLLM stream+tools):
      - envelope: ``{"name": "search_knowledge", "arguments": {…}}``;
      - args-only: JSON из ключей схемы ``{"query": …, "top_k": …}``
        (голый или в ```-fence, возможно с сопроводительным текстом модели).
    Возврат — нормализованные вызовы (контракт ``_normalize_tool_calls``);
    аргументы проходят серверную валидацию ``_search_params`` (зона —
    ТОЛЬКО из сервера, I5/I6 не затронуты). Ограничение ложных срабатываний:
    имя инструмента строго search_knowledge; args-only — только ключи схемы;
    зацикливание ограничено max_iters вызывающего.
    """
    calls: list[dict[str, Any]] = []
    seen_args: list[dict[str, Any]] = []
    for candidate in _iter_text_call_candidates(text):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        arguments: dict[str, Any] | None = None
        if (
            parsed.get("name") == SEARCH_TOOL_NAME
            and isinstance(parsed.get("arguments"), dict)
        ):
            arguments = parsed["arguments"]
        elif (
            isinstance(parsed.get("query"), str)
            and set(parsed) <= _TEXT_CALL_ARG_KEYS
        ):
            arguments = parsed
        if arguments is None or arguments in seen_args:
            continue
        seen_args.append(arguments)
        calls.append(
            {
                "id": f"text_{len(calls)}",
                "name": SEARCH_TOOL_NAME,
                "arguments": arguments,
                "parse_error": False,
            }
        )
    return calls


def text_tool_call_display_hold(buffer: str) -> bool | None:
    """Вердикт отображения начала итерации для стрим-tee (Ф6-5/S1).

    True — держать (похоже на текстовый tool-call: после ws/```-fence
    начинается ``{`` — не показывать), False — стримить пользователю,
    None — решить позже (только пробелы / незавершённый fence-префикс).
    Естественноязычные ответы никогда не начинаются с ``{`` после
    json-fence, поэтому подавление не задевает нормальный стрим; если
    «held»-текст не оказался вызовом, носитель гарантирует fallback
    страницы (``acc`` пуст → показ финального text).
    """
    s = buffer.lstrip()
    if s.startswith("`"):
        return None  # незавершённый fence-префикс
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.lstrip()
        if not s:
            return None
        return s.startswith("{")
    if not s:
        return None
    return s.startswith("{")


# ── Публичный API ───────────────────────────────────────────────────


async def run_turn(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    zone: str,
    mcp_client: MCPClient | None = None,
    llm_stream: Callable[..., AsyncIterator[Any]] | None = None,
    max_iters: int = 4,
) -> str:
    """Один ход диалога с циклом function-calling (search_knowledge).

    Flow: LLM (с описанием инструмента) → tool_calls? → MCP search_knowledge
    (``zone`` инжектится СЕРВЕРОМ из одноимённого параметра; аргументы модели
    на зону НЕ влияют — I5/I6) → tool-результат в контекст → следующая
    итерация; без tool_calls → финальный текст (строка).

    Защиты: ``max_iters`` — максимум LLM-вызовов за ход (зацикливание 7B);
    single-flight — параллельные вызовы с одним ``session_id`` сериализуются
    (модульный реестр asyncio.Lock); 429 LLM/MCP → ToolLoopError(
    kind="rate_limit", status=429); прочие сбои → ToolLoopError без ключей
    и внутренних деталей; наружу сырое исключение не утекает.

    mcp_client/llm_stream инжектируемые (hermetic-тесты); прод-дефолты
    ленивые: MCPClient создаётся при первом tool-вызове (env WS_MCP_KEY /
    WS_MCP_URL) и закрывается по завершении хода.
    """
    if llm_stream is None:
        llm_stream = stream_chat
    safe_zone = zone if zone in VALID_ZONES else PUBLIC_ZONE  # fail-closed (I6)
    iterations = max(1, int(max_iters))
    own_mcp: MCPClient | None = None
    try:
        lock = await _get_session_lock(session_id)
        async with lock:
            working: list[dict[str, Any]] = [_tool_system_message()] + [
                dict(m) for m in messages
            ]
            for _ in range(iterations):
                text, tool_calls = await _drain_llm(llm_stream, working)
                if not tool_calls:
                    # Ф6-5/S1 (arch-2026-10-10-ai-ws-acceptance): LiteLLM
                    # stream+tools деградирует вызов до текста в content —
                    # распознаём текстовую форму (gateway-агностик; нативные
                    # delta.tool_calls остаются приоритетом).
                    tool_calls = parse_text_tool_calls(text)
                if not tool_calls:
                    return text
                assistant: dict[str, Any] = {"role": "assistant", "content": text}
                assistant["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in tool_calls
                ]
                working.append(assistant)
                for call in tool_calls:
                    if mcp_client is None:
                        mcp_client = own_mcp = _default_mcp_client()
                    result = await _execute_tool_call(mcp_client, call, safe_zone)
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": _format_tool_result(result),
                        }
                    )
            raise ToolLoopError(
                f"Превышен лимит итераций tool-loop ({iterations}): "
                "модель не завершила ответ",
                kind="max_iterations",
            )
    except ToolLoopError:
        raise
    except LLMStreamError as e:
        if _is_rate_limit(str(e)):
            raise ToolLoopError(_RATE_LIMIT_MSG, kind="rate_limit", status=429) from e
        raise ToolLoopError(f"Ошибка LLM: {e}", kind="llm_error") from e
    except (AuthenticationError, ForbiddenError, TransportError) as e:
        if _is_rate_limit(str(e)):
            raise ToolLoopError(_RATE_LIMIT_MSG, kind="rate_limit", status=429) from e
        raise ToolLoopError(f"Ошибка MCP: {e}", kind="mcp_error") from e
    except Exception as e:  # доменирование: наружу только ToolLoopError
        logger.exception("tool-loop unexpected error (session=%s)", session_id)
        raise ToolLoopError("Внутренняя ошибка tool-loop", kind="internal") from e
    finally:
        if own_mcp is not None:
            await own_mcp.close()
