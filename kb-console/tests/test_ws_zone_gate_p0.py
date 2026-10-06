"""P0 зонный гейт read-пути AI-верстака: матрица роль × попытка модели.

Трасса: arch-2026-10-05-ai-workspace, Ф2 #3. Контекст: сервисный ключ
верстака ``WS_MCP_KEY`` = read + zone=both — на стороне MCP он видит и
private. Единственный барьер между contributor и private — серверная
инъекция зоны из роли (``ws_zone.zone_for_role`` → ``run_turn(zone=...)`` →
``_search_params``) плюс отсутствие ``zone`` в схеме инструмента.
``tests/test_role_zone_matrix.py`` покрывает UI-поверхности консоли
(прокси/поиск/``/documents``), но НЕ чат/tool-loop — этот файл закрывает
дыру покрытия и является регресс-барьером P0-пути.

Мутационный барьер M5 (стиль «демо+откат» из test_role_zone_matrix.py):
подмена ``_search_params`` на чтение зоны из ``arguments`` модели →
перехваченный MCP-вызов уходит в private, т.е. ассерты матрицы краснеют —
гейт держится серверной инжекцией, а не случайностью.

Моки — реальные типы (правило 10): FakeMCP — подкласс настоящего
MCPClient; llm_stream — скриптованный async-gen по контракту модуля
(str-дельты | {"tool_calls": [...]}). Без сети.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from kb_console.core import chat_turn as chat_turn_module
from kb_console.core import tool_loop as tool_loop_module
from kb_console.core.chat_turn import chat_turn
from kb_console.core.mcp_client import MCPClient
from kb_console.core.tool_loop import (
    SEARCH_TOOL_NAME,
    SEARCH_TOOL_SCHEMA,
    run_turn,
)
from kb_console.core.ws_zone import (
    PRIVATE_ZONE,
    PUBLIC_ZONE,
    zone_for_identity,
    zone_for_role,
)

# Матрица роль → серверная зона (SSOT: ws_zone.WS_ZONE_BY_ROLE; согласована с
# tests/test_role_zone_matrix.py — private только у admin, ниже admin гейта нет).
ROLE_ZONE_MATRIX: list[tuple[str, str]] = [
    ("admin", PRIVATE_ZONE),
    ("editor", PUBLIC_ZONE),
    ("contributor", PUBLIC_ZONE),
]


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
    """Fake MCPClient: перехватывает все tools_call; isinstance — настоящий."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result if result is not None else {"results": []}

    async def tools_call(  # type: ignore[override]
        self, name: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        self.calls.append((name, dict(params or {})))
        return self._result


class FakeLLM:
    """Скриптованный LLM: сценарий на каждый вызов (последний — повторяется).

    Сценарий — list событий (str | dict); пишет снимки messages и tools.
    """

    def __init__(self, scripts: list[Any]) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[Any] = []

    async def __call__(
        self, messages: list[dict[str, Any]], *, tools: Any = None
    ) -> AsyncIterator[Any]:
        idx = len(self.calls)
        self.calls.append([dict(m) for m in messages])
        if tools is not None:
            self.tools_seen.append(tools)
        for event in self._scripts[min(idx, len(self._scripts) - 1)]:
            yield event


async def _run_attempt(model_args: Any, *, server_zone: str | None, tag: str) -> FakeMCP:
    """Один ход tool-loop: модель шлёт tool-call с model_args, серверная зона —
    server_zone. Возвращает FakeMCP с перехваченными вызовами tools_call."""
    fake_mcp = FakeMCP(result={"results": [{"knowledge_id": "k1"}]})
    assert isinstance(fake_mcp, MCPClient)  # мок — реальный тип (правило 10)
    llm = FakeLLM([[_tool_call_event(f"c-{tag}", model_args)], [f"ответ-{tag}"]])
    text = await run_turn(
        [{"role": "user", "content": f"запрос {tag}"}],
        session_id=f"p0-{tag}",
        zone=server_zone,
        mcp_client=fake_mcp,
        llm_stream=llm,
    )
    assert text == f"ответ-{tag}"
    assert len(fake_mcp.calls) == 1
    return fake_mcp


# ── (1)+(2) матрица: модель не может повысить зону; admin — positive control ──


@pytest.mark.parametrize("role,expected_zone", ROLE_ZONE_MATRIX)
@pytest.mark.parametrize(
    "model_args",
    [
        {"query": "секретные материалы", "zone": "private"},
        {"query": "секретные материалы", "zone": "PRIVATE"},
        {"query": "секретные материалы", "top_k": 5, "zone": "private"},
    ],
    ids=["zone-private", "zone-UPPER", "zone+top_k"],
)
async def test_model_cannot_escalate_zone(
    role: str, expected_zone: str, model_args: dict[str, Any]
) -> None:
    """Ядро P0: модель шлёт zone=private — MCP-вызов идёт с зоной РОЛИ из
    zone_for_role. Contributor/editor не видят private (утечки нет), admin —
    видит (positive control: гейт не «глухой»)."""
    fake_mcp = await _run_attempt(
        model_args,
        server_zone=zone_for_role(role),
        tag=f"{role}-{model_args.get('top_k', 0)}",
    )
    name, params = fake_mcp.calls[0]
    assert name == SEARCH_TOOL_NAME
    assert params["zone"] == expected_zone  # зона роли, НЕ из arguments модели
    assert params["query"] == "секретные материалы"
    assert set(params) <= {"query", "top_k", "zone"}  # чужие ключи модели не текут


async def test_model_zone_attempt_as_json_string() -> None:
    """Тот же детектор для arguments JSON-строкой (частый транспорт-вид)."""
    fake_mcp = await _run_attempt(
        json.dumps({"query": "x", "zone": "private"}),
        server_zone=zone_for_role("contributor"),
        tag="json-str",
    )
    assert fake_mcp.calls[0][1]["zone"] == PUBLIC_ZONE


# ── (3) схема инструмента не раскрывает зону модели ──


def test_search_tool_schema_hides_zone() -> None:
    """В схеме search_knowledge нет поля zone — ни свойством, ни текстом."""
    props = SEARCH_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert isinstance(props, dict)
    assert "zone" not in props
    assert "zone" not in json.dumps(SEARCH_TOOL_SCHEMA, ensure_ascii=False)


async def test_tools_payload_to_llm_hides_zone() -> None:
    """Поверхность, реально ушедшая модели (tools=...), зоны не содержит."""
    fake_mcp = FakeMCP(result={"results": []})
    llm = FakeLLM([[_tool_call_event("c-sch", {"query": "x"})], ["ok"]])
    await run_turn(
        [{"role": "user", "content": "x"}],
        session_id="p0-sch",
        zone=PRIVATE_ZONE,
        mcp_client=fake_mcp,
        llm_stream=llm,
    )
    assert fake_mcp.calls and llm.tools_seen, "инструмент не дошёл до LLM"
    assert "zone" not in json.dumps(llm.tools_seen, ensure_ascii=False)


# ── (4) fail-closed: неизвестная/испорченная роль и мусорная зона → public ──


@pytest.mark.parametrize("bad_role", [None, "unknown", "", "Administrator", "admin ", "root"])
def test_zone_for_role_fail_closed(bad_role: str | None) -> None:
    """None/неизвестная/искажённая роль → public: ниже admin private НЕТ."""
    assert zone_for_role(bad_role) == PUBLIC_ZONE


def test_zone_for_identity_matrix() -> None:
    """Обёртка identity: admin → private; нет identity при непустом users-сторе
    → contributor → public (fail-closed); legacy (пустой стор) → admin →
    private — бит-в-бит с identity.effective_role."""
    assert zone_for_identity({"username": "a", "role": "admin"}, has_users=True) == PRIVATE_ZONE
    assert zone_for_identity({"username": "e", "role": "editor"}, has_users=True) == PUBLIC_ZONE
    assert zone_for_identity(None, has_users=True) == PUBLIC_ZONE
    assert zone_for_identity(None, has_users=False) == PRIVATE_ZONE  # legacy


@pytest.mark.parametrize("zone_in", ["public", "not-a-zone", "", "PRIVATE", None, "both"])
async def test_run_turn_zone_fail_closed(zone_in: str | None) -> None:
    """run_turn: невалидная зона (мусор/регистр/None/«both» — имя scope ключа)
    деградирует в public; модель при этом всё равно пытается подсунуть
    zone=private — безуспешно."""
    fake_mcp = await _run_attempt(
        {"query": "q", "zone": "private"},
        server_zone=zone_in,
        tag=f"fc-{zone_in!r}",
    )
    assert fake_mcp.calls[0][1]["zone"] == PUBLIC_ZONE


def test_search_params_zone_only_from_server_unit() -> None:
    """Юнит: _search_params ставит zone ТОЛЬКО из серверного аргумента —
    явный zone в arguments игнорируется (в обе стороны)."""
    real = tool_loop_module._search_params
    assert real({"query": "q", "zone": "private"}, PUBLIC_ZONE) == {
        "query": "q",
        "zone": PUBLIC_ZONE,
    }
    assert real({"query": "q", "zone": "public"}, PRIVATE_ZONE) == {
        "query": "q",
        "zone": PRIVATE_ZONE,
    }


# ── (5) проброс zone: chat_turn → run_turn (путь страниц чата) ──


@pytest.mark.parametrize("zone", [PUBLIC_ZONE, PRIVATE_ZONE])
async def test_chat_turn_forwards_zone_kwarg(
    monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    """chat_turn прокидывает zone в run_turn бит-в-бит (monkeypatch-capture)."""
    captured: dict[str, Any] = {}

    async def fake_run_turn(
        messages, *, session_id, zone, mcp_client=None, llm_stream=None, max_iters=4
    ):
        captured["zone"] = zone
        captured["session_id"] = session_id
        return "ок"

    monkeypatch.setattr(chat_turn_module, "run_turn", fake_run_turn)
    result = await chat_turn([{"role": "user", "content": "q"}], session_id="p0-ct", zone=zone)
    assert result == {"text": "ок", "saved": False}
    assert captured["zone"] == zone


async def test_chat_turn_full_chain_zone_gate() -> None:
    """Полный P0-путь чата страниц: chat_turn → run_turn → MCP. Роль
    contributor (public), модель шлёт zone=private → MCP видит public."""
    fake_mcp = FakeMCP(result={"results": []})
    llm = FakeLLM(
        [
            [_tool_call_event("c-ct", {"query": "q", "zone": "private"})],
            ["итог"],
        ]
    )
    res = await chat_turn(
        [{"role": "user", "content": "q"}],
        session_id="p0-ct-chain",
        zone=zone_for_role("contributor"),
        mcp_client=fake_mcp,
        llm_stream=llm,
    )
    assert res["text"] == "итог"
    assert fake_mcp.calls[0][1]["zone"] == PUBLIC_ZONE


# ── (6) мутационный «зуб» M5: гейт держится серверной инжекцией ──


async def test_mutation_m5_zone_from_arguments_would_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5 «зуб» (демо+откат, стиль test_role_zone_matrix.py): мутант
    _search_params читает зону из arguments модели. Под мутантом
    перехваченный MCP-вызов УТЕКАЕТ в private — т.е. ассерт матрицы
    (``params["zone"] == "public"``) краснеет: детектор ловит дыру, значит
    зелёный результат на реальном коде — заслуга серверной инжекции.
    monkeypatch откатывает мутанта по завершении теста."""

    def mutant_search_params(arguments: Any, zone: str) -> dict[str, Any] | None:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return None
        params: dict[str, Any] = dict(arguments)  # МУТАЦИЯ: зона модели течёт в вызов
        params.setdefault("zone", zone)
        return params

    monkeypatch.setattr(tool_loop_module, "_search_params", mutant_search_params)
    fake_mcp = await _run_attempt(
        {"query": "секрет", "zone": "private"},
        server_zone=zone_for_role("contributor"),
        tag="mutant",
    )
    name, params = fake_mcp.calls[0]
    assert name == SEARCH_TOOL_NAME
    assert params["zone"] == PRIVATE_ZONE, (
        "мутант обязан показать утечку — иначе детектор матрицы без зубов"
    )
