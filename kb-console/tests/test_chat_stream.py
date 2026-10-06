"""Ф2 (ai-workspace) шаг #1: SSE-стриминг llm_stream + страница «Чат».

Hermetic-транспорт — НАСТОЯЩИЙ httpx (AsyncClient + MockTransport): стрим
идёт по реальному код-пути httpx (client.stream → aiter_lines), отличается
только сеть. Форма SSE-чанков — реальный контракт LiteLLM/OpenAI
(``data: {"choices":[{"delta":{"content": …}}]}``, терминатор data: [DONE]).

Спека §8 «Ф2-спека» H «ложные зелёные»: «стриминг без носителя» — в тестах
страницы считаются set_content-вызовы (по одному на дельту), а не факт
возврата строки.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from kb_console.core.llm_stream import LLMStreamError, stream_chat
from kb_console.pages import ROUTES

# ── SSE-фикстуры (реальная раскладка потока LiteLLM) ──────────


def _chunk(
    content: str | None = None,
    *,
    role: str | None = None,
    finish: str | None = None,
) -> dict:
    """Чанк chat.completion.chunk: первый — role, далее content, финал — finish."""
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    choice: dict = {"index": 0, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    return {"id": "c0", "object": "chat.completion.chunk", "choices": [choice]}


def _sse_bytes(
    chunks: list[dict], *, done: bool = True, trailing: dict | None = None
) -> bytes:
    """SSE-поток: keep-alive/пустые строки + data-чанки + терминатор [DONE]."""
    out = [b": keep-alive\n\n", b"\n"]
    for c in chunks:
        out.append(b"data: " + json.dumps(c).encode() + b"\n\n")
    if done:
        out.append(b"data: [DONE]\n\n")
    if trailing is not None:  # чанк ПОСЛЕ терминатора — обязан игнорироваться
        out.append(b"data: " + json.dumps(trailing).encode() + b"\n\n")
    return b"".join(out)


def _client(payload: bytes, status_code: int = 200):
    """httpx.AsyncClient на MockTransport + захват запроса (реальный httpx-путь)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["accept"] = request.headers.get("Accept")
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            status_code,
            content=payload,
            headers={"Content-Type": "text/event-stream"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


# ── stream_chat: hermetic SSE ─────────────────────────────────


class TestStreamChat:
    async def test_yields_deltas_in_order_and_concatenates(self):
        """≥2 дельт в порядке прихода, склеиваются в текст; role/finish — мимо."""
        payload = _sse_bytes(
            [
                _chunk(role="assistant"),  # первый чанк: только role
                _chunk("Привет"),
                _chunk(", "),
                _chunk("мир"),
                _chunk(finish="stop"),  # финальный: без content
            ]
        )
        client, _cap = _client(payload)
        async with client:
            deltas = [
                d
                async for d in stream_chat(
                    [{"role": "user", "content": "привет"}],
                    api_key="k-test",
                    client=client,
                )
            ]
        assert deltas == ["Привет", ", ", "мир"]
        assert len(deltas) >= 2
        assert "".join(deltas) == "Привет, мир"

    async def test_done_terminates_stream(self):
        """data: [DONE] завершает; чанки после терминатора не yield'ятся."""
        payload = _sse_bytes([_chunk("a"), _chunk("b")], trailing=_chunk("после"))
        client, _cap = _client(payload)
        async with client:
            deltas = [
                d
                async for d in stream_chat(
                    [{"role": "user", "content": "?"}], api_key="k", client=client
                )
            ]
        assert deltas == ["a", "b"]

    async def test_request_contract_url_and_key_from_env(self, monkeypatch):
        """url из WS_LLM_URL, ключ из LITELLM_MASTER_KEY, модель local, stream=True."""
        monkeypatch.setenv("WS_LLM_URL", "http://litellm-test:4000/v1")
        monkeypatch.setenv("LITELLM_MASTER_KEY", "k-env-secret")
        client, cap = _client(_sse_bytes([_chunk("ок")]))
        async with client:
            deltas = [
                d
                async for d in stream_chat(
                    [{"role": "user", "content": "q"}], client=client
                )
            ]
        assert deltas == ["ок"]
        assert cap["url"] == "http://litellm-test:4000/v1/chat/completions"
        assert cap["method"] == "POST"
        assert cap["auth"] == "Bearer k-env-secret"
        assert cap["accept"] == "text/event-stream"
        assert cap["body"]["model"] == "local"
        assert cap["body"]["stream"] is True
        assert cap["body"]["messages"] == [{"role": "user", "content": "q"}]

    async def test_no_auth_header_without_key(self, monkeypatch):
        """Нет ключа (env пуст) → Authorization-заголовок не ставится."""
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
        client, cap = _client(_sse_bytes([_chunk("ок")]))
        async with client:
            deltas = [
                d
                async for d in stream_chat(
                    [{"role": "user", "content": "q"}], client=client
                )
            ]
        assert deltas == ["ок"]
        assert cap["auth"] is None

    async def test_non_200_raises_without_secret(self):
        """429 → LLMStreamError с кодом и причиной; ключ НЕ утекает."""
        body = json.dumps({"error": {"message": "rate limited"}}).encode()
        client, _cap = _client(body, status_code=429)
        async with client:
            with pytest.raises(LLMStreamError) as ei:
                async for _d in stream_chat(
                    [{"role": "user", "content": "q"}],
                    api_key="k-super-secret",
                    client=client,
                ):
                    pass
        assert "429" in str(ei.value)
        assert "rate limited" in str(ei.value)
        assert "k-super-secret" not in str(ei.value)


# ── Страница «Чат»: smoke + зона/роль + носитель ──────────────


class TestChatPageSmoke:
    def _build(self, *, identity, has_users: bool, role: str):
        from kb_console.pages import chat

        with (
            patch.object(chat, "current_identity", return_value=identity),
            patch.object(chat, "current_role", return_value=role),
            patch.object(chat, "_has_users", return_value=has_users),
            patch.object(chat, "ui") as mock_ui,
        ):
            chat.build_chat()
        labels = [str(c.args[0]) for c in mock_ui.label.call_args_list if c.args]
        return mock_ui, labels

    def test_admin_zone_private(self):
        """admin → зона private в подписи; input+button построены."""
        mock_ui, labels = self._build(
            identity={"id": "1", "username": "admin", "role": "admin"},
            has_users=False,
            role="admin",
        )
        assert any("private" in t for t in labels)
        assert any("admin" in t for t in labels)
        assert mock_ui.input.called
        assert mock_ui.button.called

    def test_contributor_zone_public(self):
        """contributor → public; private в подписи зоны НЕТ."""
        _mock_ui, labels = self._build(
            identity={"id": "2", "username": "u", "role": "contributor"},
            has_users=True,
            role="contributor",
        )
        assert any("зона выборки: public" in t for t in labels)
        assert not any("зона выборки: private" in t for t in labels)

    def test_editor_zone_public(self):
        """editor → public (fail-closed матрица: private только у admin)."""
        _mock_ui, labels = self._build(
            identity={"id": "3", "username": "e", "role": "editor"},
            has_users=True,
            role="editor",
        )
        assert any("зона выборки: public" in t for t in labels)

    def test_chat_route_registered_min_role(self):
        """/chat в ROUTES, min_role=contributor (гейт страницы)."""
        matches = [r for r in ROUTES if r[0] == "/chat"]
        assert len(matches) == 1
        assert matches[0][1] == "Чат"
        assert matches[0][3] == "contributor"
        assert callable(matches[0][2])


class TestRunTurnCarrier:
    """Носитель (H): каждая дельта → set_content; сбои не роняют страницу.

    Ф2 #2b-2a: ход идёт через ``chat_turn`` (tool-loop); стрим-дельты
    приходят в ``on_delta`` — считаются set_content-вызовы как раньше.
    """

    @staticmethod
    def _chat_turn_fake(deltas, error=None):
        async def fake(
            messages,
            *,
            session_id,
            zone,
            user=None,
            store=None,
            on_delta=None,
            **kwargs,
        ):
            if error is not None:
                raise error
            for d in deltas:
                if on_delta is not None:
                    on_delta(d)
            return {"text": "".join(deltas), "saved": False}

        return fake

    async def test_each_delta_updates_carrier(self):
        """3 дельты → 3 обновления носителя (плюс сброс) + история дополнена."""
        from kb_console.pages import chat

        output, status = MagicMock(), MagicMock()
        history = [{"role": "user", "content": "посчитай"}]
        with (
            patch.object(
                chat, "chat_turn", self._chat_turn_fake(("Раз ", "два ", "три"))
            ),
            patch.object(chat, "ui"),
        ):
            await chat._run_turn(history, output, status)
        assert output.set_content.call_count == 4  # сброс + по одному на дельту
        texts = [str(c.args[0]) for c in output.set_content.call_args_list]
        assert texts[-1] == "Раз два три"
        assert history[-1] == {"role": "assistant", "content": "Раз два три"}
        assert status.set_text.call_args_list[-1].args[0] == "готово"

    async def test_stream_error_keeps_page_alive(self):
        """LLMStreamError (429) → notify, ход откачен, исключение НЕ пробито."""
        from kb_console.pages import chat

        output, status = MagicMock(), MagicMock()
        history = [{"role": "user", "content": "q"}]
        with (
            patch.object(
                chat,
                "chat_turn",
                self._chat_turn_fake(
                    (), error=LLMStreamError("LiteLLM HTTP 429: rate")
                ),
            ),
            patch.object(chat, "ui") as mock_ui,
        ):
            await chat._run_turn(history, output, status)
        mock_ui.notify.assert_called_once()
        assert history == []  # оборванный ход откачен

    async def test_http_error_keeps_page_alive(self):
        """Сетевой сбой httpx.ConnectError → notify, без проброса."""
        from kb_console.pages import chat

        output, status = MagicMock(), MagicMock()
        history = [{"role": "user", "content": "q"}]
        with (
            patch.object(
                chat,
                "chat_turn",
                self._chat_turn_fake((), error=httpx.ConnectError("refused")),
            ),
            patch.object(chat, "ui") as mock_ui,
        ):
            await chat._run_turn(history, output, status)
        mock_ui.notify.assert_called_once()
        assert history == []
