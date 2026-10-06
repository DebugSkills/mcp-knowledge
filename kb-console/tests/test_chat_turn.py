"""Тесты chat_turn: on_delta-стрим, fail-soft персист, проброс kwargs в run_turn."""

import asyncio

from kb_console.core import chat_turn as chat_turn_mod
from kb_console.core.chat_turn import chat_turn

USER_MSG = {"role": "user", "content": "привет"}


class FakeRunTurn:
    """Фейк tool-loop: потребляет llm_stream, собирает текст из str-дельт."""

    def __init__(self):
        self.calls = []

    async def __call__(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        parts = []
        stream = kwargs.get("llm_stream")
        if stream is not None:
            async for event in stream(messages):
                if isinstance(event, str):
                    parts.append(event)
        return "".join(parts)


def llm_stream_yielding(deltas):
    async def llm_stream(messages, **kwargs):
        for delta in deltas:
            yield delta

    return llm_stream


class FakeStore:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def append(self, user, session, message, **kwargs):
        if self.exc is not None:
            raise self.exc
        self.calls.append({"user": user, "session": session, "message": message})
        return len(self.calls)


def test_happy_path_delta_stream_in_order(monkeypatch):
    fake = FakeRunTurn()
    monkeypatch.setattr(chat_turn_mod, "run_turn", fake)
    got = []
    result = asyncio.run(
        chat_turn(
            [USER_MSG],
            session_id="s1",
            zone="private",
            llm_stream=llm_stream_yielding(["При", "вет"]),
            on_delta=got.append,
        )
    )
    assert got == ["При", "вет"]
    assert result == {"text": "Привет", "saved": False}


def test_default_stream_is_teed_on_delta(monkeypatch):
    fake = FakeRunTurn()
    monkeypatch.setattr(chat_turn_mod, "run_turn", fake)
    monkeypatch.setattr(
        chat_turn_mod, "run_turn_stream", llm_stream_yielding(["При", "вет"])
    )
    got = []
    result = asyncio.run(
        chat_turn([USER_MSG], session_id="s1", zone="private", on_delta=got.append)
    )
    assert got == ["При", "вет"]
    assert result["text"] == "Привет"


def test_persist_user_and_assistant_once(monkeypatch):
    fake = FakeRunTurn()
    monkeypatch.setattr(chat_turn_mod, "run_turn", fake)
    store = FakeStore()
    result = asyncio.run(
        chat_turn(
            [USER_MSG],
            session_id="s1",
            zone="private",
            user="alice",
            store=store,
            llm_stream=llm_stream_yielding(["При", "вет"]),
        )
    )
    assert result["saved"] is True
    assert len(store.calls) == 2
    assert store.calls[0] == {"user": "alice", "session": "s1", "message": USER_MSG}
    assert store.calls[1] == {
        "user": "alice",
        "session": "s1",
        "message": {"role": "assistant", "content": "Привет"},
    }


def test_no_store_saved_false(monkeypatch):
    monkeypatch.setattr(chat_turn_mod, "run_turn", FakeRunTurn())
    result = asyncio.run(
        chat_turn(
            [USER_MSG],
            session_id="s1",
            zone="private",
            llm_stream=llm_stream_yielding(["О", "к"]),
        )
    )
    assert result == {"text": "Ок", "saved": False}


def test_store_failure_is_fail_soft(monkeypatch):
    monkeypatch.setattr(chat_turn_mod, "run_turn", FakeRunTurn())
    store = FakeStore(exc=RuntimeError("redis down"))
    result = asyncio.run(
        chat_turn(
            [USER_MSG],
            session_id="s1",
            zone="private",
            user="bob",
            store=store,
            llm_stream=llm_stream_yielding(["При", "вет"]),
        )
    )
    assert result == {"text": "Привет", "saved": False}
    assert store.calls == []


def test_run_turn_receives_kwargs(monkeypatch):
    fake = FakeRunTurn()
    monkeypatch.setattr(chat_turn_mod, "run_turn", fake)
    mcp = object()
    asyncio.run(
        chat_turn(
            [USER_MSG],
            session_id="sess-42",
            zone="public",
            mcp_client=mcp,
            max_iters=7,
            llm_stream=llm_stream_yielding(["a"]),
        )
    )
    assert len(fake.calls) == 1
    kw = fake.calls[0]["kwargs"]
    assert kw["session_id"] == "sess-42"
    assert kw["zone"] == "public"
    assert kw["max_iters"] == 7
    assert kw["mcp_client"] is mcp
    assert fake.calls[0]["messages"] == [USER_MSG]
