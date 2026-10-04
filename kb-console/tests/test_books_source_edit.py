"""Unit-тесты привязки Source к секции в UI книг (bibliography T2b).

Тестируемое:
  - MCPClient.update_fragment(source_id=...): прокидывается в params
    (строка = привязать, "" = отвязать, None = параметр не отправляется);
  - MCPClient.update_entry(source_refs=...): опциональный прокос (записи);
  - Чистые функции: _source_field_prefill (prefill из citation payload
    get_entry), _source_save_value (diff-решение «отправлять ли source_id»),
    _source_error_message (человекочитаемый отказ сервера errors[]);
  - Source-assertion: диалог «Изменить раздел» содержит поле, шлет
    source_id, при ошибке не закрывается (ввод не теряется), после
    привязки перечитывает секцию (блок цитаты P3-1).

Контракт ответов сервера — из кода-источника (не выдуман):
  - fragments.py:358-372 — success {"fragment_id", "version", "updated_at",
    "indexed", "source_id"(эффективный, при явном параметре)};
  - fragments.py:261 — fail-closed отказ {"error": "; ".join, "errors": [...]};
  - crud.py:62-64 — формат сообщений _validate_source_refs.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from kb_console.core.mcp_client import MCPClient
from kb_console.pages import books
from kb_console.pages.books import (
    _source_error_message,
    _source_field_prefill,
    _source_save_value,
)

SID = "src-abcdef1234567890"

# Реальный текст ошибок _validate_source_refs (crud.py:82, crud.py:86)
ERR_NOT_FOUND = f"source_id: source '{SID}' not found"
ERR_NOT_SOURCE = f"source_id: '{SID}' is not a Source (content_type='book')"


# ── Client: capture-транспорт (паттерн test_books_fragments.py) ──


@pytest.fixture
def source_client():
    """MCPClient с транспортом, захватывающим args tools/call.

    Ответы зеркалят реальный контракт update_fragment (см. докстринг модуля).
    """
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        rid = body.get("id", 1)
        if body.get("method") != "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": {}})
        params = body.get("params", {})
        tool = params.get("name", "")
        args = params.get("arguments", {})
        captured.append({"tool": tool, "args": dict(args)})

        if tool == "update_fragment":
            sid = args.get("source_id")
            if isinstance(sid, str) and sid and not sid.startswith("src-"):
                # fail-closed отказ сервера — реальная форма fragments.py:261
                errs = [ERR_NOT_FOUND]
                inner: dict = {"error": "; ".join(errs), "errors": errs}
            else:
                inner = {
                    "fragment_id": args.get("fragment_id"),
                    "version": 3,
                    "updated_at": "2026-10-04T00:00:00",
                    "indexed": True,
                    **({"source_id": sid or None} if sid is not None else {}),
                }
        elif tool == "update_entry":
            inner = {"knowledge_id": args.get("knowledge_id"), "version": 2}
        else:
            inner = {"ok": True}

        wrapped = {"content": [{"type": "text", "text": json.dumps(inner)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": wrapped})

    transport = httpx.MockTransport(handler)
    transport.captured = captured  # type: ignore[attr-defined]
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    client = MCPClient(base_url="http://test", client=http)
    client._captured = captured  # type: ignore[attr-defined]
    return client


# ── MCPClient.update_fragment: source_id в params ────────────


@pytest.mark.asyncio
async def test_update_fragment_source_id_sent_when_given(source_client):
    """Заданный source_id уходит в params (привязка)."""
    result = await source_client.update_fragment(
        "sec-1", content="body", version=2, source_id=SID,
    )
    call = source_client._captured[0]
    assert call["tool"] == "update_fragment"
    assert call["args"]["source_id"] == SID
    assert result["source_id"] == SID, "эффективный source_id из ответа"


@pytest.mark.asyncio
async def test_update_fragment_empty_source_id_sent_for_unbind(source_client):
    """source_id='' отправляется явно (сервер: '' → отвязать)."""
    await source_client.update_fragment("sec-1", content="body", source_id="")
    call = source_client._captured[0]
    assert call["args"]["source_id"] == ""


@pytest.mark.asyncio
async def test_update_fragment_source_id_absent_when_none(source_client):
    """source_id=None → параметр НЕ отправляется (сервер: не менять)."""
    await source_client.update_fragment("sec-1", content="body", version=2)
    call = source_client._captured[0]
    assert "source_id" not in call["args"], (
        "None не должен попадать в params — иначе сервер отвяжет/привяжет источник"
    )


# ── MCPClient.update_entry: source_refs (опц., записи) ───────


@pytest.mark.asyncio
async def test_update_entry_source_refs_sent_when_given(source_client):
    """source_refs прокидывается в params (fail-closed на сервере)."""
    await source_client.update_entry(
        "kb-1", content="# T", source_refs=[{"source_id": SID}],
    )
    call = source_client._captured[0]
    assert call["tool"] == "update_entry"
    assert call["args"]["source_refs"] == [{"source_id": SID}]


@pytest.mark.asyncio
async def test_update_entry_source_refs_absent_when_none(source_client):
    """Без source_refs params не меняются (обратная совместимость)."""
    await source_client.update_entry("kb-1", content="# T")
    call = source_client._captured[0]
    assert "source_refs" not in call["args"]
    assert call["args"] == {"knowledge_id": "kb-1", "content": "# T"}


# ── _source_field_prefill: текущий source_id из payload get_entry ──


def test_prefill_from_citation():
    """get_entry секции отдаёт source_id внутри citation (read.py:230-237)."""
    sec = {"citation": {"source_id": SID, "formatted": "Иванов И. И. Книга. — М., 2023."}}
    assert _source_field_prefill(sec) == SID


def test_prefill_empty_without_citation():
    """Нет citation (источник не привязан / скрыт) → префилл пуст, не выдумываем."""
    assert _source_field_prefill({}) == ""
    assert _source_field_prefill(None) == ""
    assert _source_field_prefill({"citation": "не-dict"}) == ""
    assert _source_field_prefill({"citation": {"formatted": "..."}}) == ""


# ── _source_save_value: diff-решение «отправлять ли source_id» ──


def test_save_value_unchanged_not_sent():
    """Поле не трогали → None (параметр не отправляется, привязка не меняется)."""
    assert _source_save_value(SID, SID) is None
    assert _source_save_value("", "") is None


def test_save_value_cleared_unbinds():
    """Видимая привязка очищена → '' (отвязать)."""
    assert _source_save_value(SID, "") == ""


def test_save_value_new_binds():
    """Введён новый источник → строка (привязать)."""
    assert _source_save_value("", SID) == SID


def test_save_value_rebind_and_strip():
    """Замена значения + обрезка пробелов по краям."""
    assert _source_save_value("src-old", f"  {SID}  ") == SID


def test_save_value_non_string_input_is_empty():
    """None/clearable-очистка → пустая строка (как пользовательский ввод)."""
    assert _source_save_value(SID, None) == ""
    assert _source_save_value("", None) is None


# ── _source_error_message: отказ сервера → человекочитаемо ───


def test_error_message_joins_errors():
    """errors[] показываются целиком (все причины, не только первая)."""
    result = {"error": f"{ERR_NOT_FOUND}; {ERR_NOT_SOURCE}",
              "errors": [ERR_NOT_FOUND, ERR_NOT_SOURCE]}
    assert _source_error_message(result) == f"{ERR_NOT_FOUND}; {ERR_NOT_SOURCE}"


def test_error_message_fallback_to_error_field():
    assert _source_error_message({"error": "Missing required parameter"}) == (
        "Missing required parameter"
    )


def test_error_message_never_empty():
    assert _source_error_message({}) != ""
    assert _source_error_message("мусор") != ""


# ── Source-assertion: UI-диалог «Изменить раздел» (inspect) ──


def test_edit_dialog_has_source_field_with_prefill_and_hint():
    """Диалог содержит input «Источник (source_id)»: префилл + подсказка формата."""
    src = inspect.getsource(books.render_book_detail)
    assert "Источник (source_id)" in src, "поле с человекочитаемым label"
    assert "_source_field_prefill" in src, "префилл текущим source_id секции"
    assert "src-<sha256_16>" in src, "подсказка формата (без выдуманных списков)"


def test_edit_dialog_sends_source_diff_on_save():
    """Сохранение шлёт source_id=_source_save_value(...) в update_fragment."""
    src = inspect.getsource(books.render_book_detail)
    assert "_source_save_value(" in src
    assert "source_id=source_save" in src


def test_edit_dialog_server_error_keeps_dialog_open():
    """Ошибка сервера (errors[]) → notify-negative, диалог не закрывается,
    кнопка возвращается — ввод не теряется."""
    src = inspect.getsource(books.render_book_detail)
    assert 'result.get("error")' in src, "error-ветка до закрытия диалога"
    assert "_source_error_message(result)" in src
    # error-ветка не должна закрывать диалог: close только после неё
    err_branch = src.split('result.get("error")', 1)[1]
    assert "edit_dialog.close()" not in err_branch.split("if", 1)[0], (
        "error-ветка обязана вернуть управление ДО edit_dialog.close()"
    )


def test_edit_dialog_rereads_section_after_source_change():
    """После привязки/отвязки — перечитать секцию (блок цитаты P3-1)."""
    src = inspect.getsource(books.render_book_detail)
    assert "if source_save is not None:" in src
    assert "_show_section(section)" in src, (
        "изменённая привязка → get_entry секции (не только TOC книги)"
    )
