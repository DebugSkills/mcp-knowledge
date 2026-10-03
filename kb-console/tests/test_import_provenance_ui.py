"""Ф5a2: провенанс canonical в импорт-UI (бейдж + роль-ключ).

Ф5a1 сервер отдаёт sparse-провенанс в GET /imports/{id}/progress:
`source_id` / `canonical_present` (bool) / `canonical_sha256` (только при
canonical) / `canonical_error {reason, message, at}` (только persisted-отказ;
reason ∈ 5 whitelist §3.4:192). Ключа нет → статус неизвестен/неприменим.

Консоль (Ф5a2):
- `render_import_progress` (core/utils.py) рендерит бейдж canonical (sparse —
  без ключей ничего не добавляется);
- `import_page` шлёт роль-ключ `mcp_api_key()`, а не base-ключ `MCP_API_KEY` (P1-1).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kb_console.core.utils import canonical_reason_text, render_import_progress

# 5 whitelist-причин → человекочитаемый текст (§3.4:192, citation.py).
WHITELIST_TEXT = {
    "queued": "канонизация поставлена в очередь",
    "conversion_failed": "конвертация в PDF не удалась",
    "conversion_timeout": "конвертация превысила таймаут",
    "converter_unavailable": "конвертер недоступен",
    "quota_exceeded": "квота хранилища документов превышена",
}


def _render(snapshot: dict):
    """Прогнать render_import_progress с моком ui; вернуть mock_ui."""
    container = MagicMock()
    with patch("kb_console.core.utils.ui") as mock_ui:
        render_import_progress(snapshot, container)
    return mock_ui


def _label_texts(mock_ui) -> list[str]:
    """Тексты всех ui.label-вызовов (первый позиционный аргумент)."""
    return [str(c.args[0]) for c in mock_ui.label.call_args_list if c.args]


# ── (a) canonical_present=True → бейдж + sha256 ─────────────


def test_canonical_present_badge_with_sha():
    sha = "ab" * 32
    mock_ui = _render(
        {
            "imported": 1,
            "total": 1,
            "status": "done",
            "messages": [],
            "canonical_present": True,
            "canonical_sha256": sha,
        }
    )
    texts = _label_texts(mock_ui)
    assert any(t.startswith("✅ canonical") for t in texts)
    assert any(sha[:12] in t for t in texts)


# ── (b) canonical_error: 5 причин → человекочитаемый текст + предупреждение ──


@pytest.mark.parametrize("reason,expected", sorted(WHITELIST_TEXT.items()))
def test_canonical_error_reason_renders_text_and_warning(reason, expected):
    mock_ui = _render(
        {
            "imported": 0,
            "total": 0,
            "status": "error",
            "messages": [],
            "canonical_present": False,
            "canonical_error": {
                "reason": reason,
                "message": "x",
                "at": "2026-10-03T00:00:00Z",
            },
        }
    )
    texts = _label_texts(mock_ui)
    assert any("⚠ canonical недоступен" in t and expected in t for t in texts)
    # явное предупреждение о фоллбэке (цитаты/страницы недоступны)
    assert any("недоступны" in t and "pdf_only-фоллбэк" in t for t in texts)


@pytest.mark.parametrize("reason,expected", sorted(WHITELIST_TEXT.items()))
def test_canonical_reason_text_whitelist(reason, expected):
    assert canonical_reason_text(reason) == expected


# ── (в) reason=None / отсутствует / неизвестный → без crash ──


def test_canonical_error_without_reason_no_crash():
    mock_ui = _render(
        {
            "imported": 0,
            "total": 0,
            "status": "error",
            "messages": [],
            "canonical_error": {"message": "x", "at": "2026-10-03T00:00:00Z"},
        }
    )
    texts = _label_texts(mock_ui)
    assert any("⚠ canonical недоступен" in t for t in texts)


def test_canonical_error_reason_none_no_crash():
    mock_ui = _render(
        {
            "imported": 0,
            "total": 0,
            "status": "error",
            "messages": [],
            "canonical_error": {
                "reason": None,
                "message": "x",
                "at": "2026-10-03T00:00:00Z",
            },
        }
    )
    texts = _label_texts(mock_ui)
    assert any("⚠ canonical недоступен" in t for t in texts)


def test_canonical_error_unknown_reason_passthrough_no_crash():
    mock_ui = _render(
        {
            "imported": 0,
            "total": 0,
            "status": "error",
            "messages": [],
            "canonical_error": {
                "reason": "some_future_code",
                "message": "x",
                "at": "...",
            },
        }
    )
    texts = _label_texts(mock_ui)
    assert any("some_future_code" in t for t in texts)


def test_canonical_error_non_dict_no_crash():
    # canonical_error как строка (не dict) — не падаем
    mock_ui = _render(
        {
            "imported": 0,
            "total": 0,
            "status": "error",
            "messages": [],
            "canonical_error": "boom",
        }
    )
    texts = _label_texts(mock_ui)
    assert any("⚠ canonical недоступен" in t for t in texts)


def test_canonical_reason_text_none_and_empty():
    assert canonical_reason_text(None) == ""
    assert canonical_reason_text("") == ""


def test_canonical_reason_text_unknown_passthrough():
    assert canonical_reason_text("some_future_code") == "some_future_code"


# ── (г) sparse (пустой snapshot) → никаких провенанс-элементов ──


def test_sparse_snapshot_no_provenance_elements():
    mock_ui = _render({"imported": 0, "total": 0, "status": "running", "messages": []})
    texts = _label_texts(mock_ui)
    assert not any("canonical" in t for t in texts)
    assert not any("источник:" in t for t in texts)


# ── (д) mcp_api_key используется, MCP_API_KEY — нет ─────────


def test_import_page_uses_role_key_not_base():
    from kb_console.pages import import_page

    src = Path(import_page.__file__).read_text(encoding="utf-8")
    # base-ключ MCP_API_KEY больше не используется в файле (P1-1)
    assert "MCP_API_KEY" not in src
    # mcp_api_key импортирован на уровне модуля (как search.py/books.py)
    assert hasattr(import_page, "mcp_api_key")
    # все 5 клиентов шлют роль-ключ
    assert src.count("api_key=mcp_api_key()") == 5
