"""Ф5c1: тесты admin-страницы «Документы» (kb-console).

Покрывает:
  (а) admin-гейт: ROUTES min_role=admin + runtime-отказ non-admin;
  (б) GC: dry-run дефолт True; dry_run=False только после confirm;
  (в) check: create_issues=False дефолт;
  (г) retry: documents_retry с введённым source_id;
  (д) рендер stats/check/gc/retry без падения на пустых/спарс-данных;
  (е) роль-ключ (mcp_api_key), НЕ base (MCP_API_KEY).
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kb_console.pages import ROUTES, documents

# ── (а) admin-гейт ───────────────────────────────────────────


def test_documents_route_admin_only():
    """`/documents` в ROUTES с min_role="admin" (скрыто из навигации ниже admin)."""
    matches = [r for r in ROUTES if r[0] == "/documents"]
    assert len(matches) == 1
    assert matches[0][1] == "Документы"
    assert matches[0][3] == "admin"


def test_build_documents_refuses_non_admin():
    """Non-admin: 403-label + ранний return (MCPClient не создаётся)."""
    with (
        patch.object(documents, "is_admin", return_value=False),
        patch.object(documents, "ui") as mock_ui,
        patch.object(documents, "MCPClient") as mock_client_cls,
    ):
        documents.build_documents()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("403" in t for t in labels)
    mock_client_cls.assert_not_called()


def test_build_documents_renders_for_admin():
    """Admin: страница строится (заголовок + блоки), не ранний return."""
    with (
        patch.object(documents, "is_admin", return_value=True),
        patch.object(documents, "ui") as mock_ui,
    ):
        documents.build_documents()
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Документы (хранилище)" in t for t in labels)


# ── (б) GC: dry-run дефолт + confirm-гейт ────────────────────


@pytest.mark.asyncio
async def test_gc_default_dry_run_true():
    """`_run_gc()` без аргументов → documents_gc(dry_run=True)."""
    mock_client = MagicMock()
    mock_client.documents_gc = AsyncMock(return_value={"candidates": 1})
    mock_client.close = AsyncMock()
    with patch.object(documents, "MCPClient", return_value=mock_client):
        await documents._run_gc()
    mock_client.documents_gc.assert_awaited_once_with(dry_run=True)


def test_gc_dry_run_button_safe_default():
    """Кнопка dry-run шлёт dry_run=True; удаление — через confirm-флоу."""
    src = inspect.getsource(documents.build_documents)
    assert "_do_gc(dry_run=True" in src, "dry-run кнопка должна использовать dry_run=True"
    assert "_confirm_gc_delete" in src, "кнопка удаления должна идти через confirm"


def test_gc_delete_gated_by_confirm():
    """dry_run=False вызывается только внутри confirm-обработчика."""
    confirm_src = inspect.getsource(documents._confirm_gc_delete)
    assert "dry_run=False" in confirm_src, "удаление (dry_run=False) обязано быть в confirm"
    run_src = inspect.getsource(documents._run_gc)
    assert "dry_run: bool = True" in run_src, "дефолт GC обязан быть dry_run=True"


# ── (в) check: create_issues дефолт False ────────────────────


@pytest.mark.asyncio
async def test_check_default_create_issues_false():
    """`_run_check()` без аргументов → documents_check(create_issues=False)."""
    mock_client = MagicMock()
    mock_client.documents_check = AsyncMock(return_value={"counts": {}})
    mock_client.close = AsyncMock()
    with patch.object(documents, "MCPClient", return_value=mock_client):
        await documents._run_check()
    mock_client.documents_check.assert_awaited_once_with(create_issues=False)


def test_check_fix_issues_gated_by_confirm():
    """create_issues=True вызывается только в confirm-обработчике."""
    confirm_src = inspect.getsource(documents._confirm_check)
    assert "create_issues=True" in confirm_src
    build_src = inspect.getsource(documents.build_documents)
    assert "_do_check(create_issues=False" in build_src, "read-only кнопка обязана слать create_issues=False"


# ── (г) retry с source_id ────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_passes_source_id():
    """`_run_retry(src)` → documents_retry(source_id=src)."""
    mock_client = MagicMock()
    mock_client.documents_retry = AsyncMock(return_value={"status": "ok"})
    mock_client.close = AsyncMock()
    with patch.object(documents, "MCPClient", return_value=mock_client):
        await documents._run_retry("src-123")
    mock_client.documents_retry.assert_awaited_once_with(source_id="src-123")


# ── (д) рендер без падения на спарс-данных ───────────────────


def test_render_storage_stats_sparse_no_crash():
    """stats с отсутствующими quota/blobs/jobs/grace_days не падает."""
    with patch.object(documents, "ui") as mock_ui:
        documents._render_storage_stats({})
        documents._render_storage_stats(None)
        documents._render_storage_stats({"quota": {}, "blobs": {}})
        documents._render_storage_stats(
            {"quota": {"used_bytes": 1024, "max_bytes": 2048, "used_pct": 0.5},
             "blobs": {"total": 3, "orphans": 0}, "jobs": {}, "grace_days": 7}
        )
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    assert any("Хранилище документов" in t for t in labels)


def test_render_check_gc_retry_sparse_no_crash():
    """Результаты check/gc/retry с пустыми данными не падают."""
    with patch.object(documents, "ui") as mock_ui:
        documents._render_check_result(None)
        documents._render_check_result({"counts": {}, "issues": [], "samples": []})
        documents._render_gc_result(None, dry_run=True)
        documents._render_gc_result({"deleted": 0, "freed_bytes": 0, "errors": []}, dry_run=False)
        documents._render_retry_result(None)
        documents._render_retry_result({"status": "failed", "reason": "quota_exceeded"})
    assert mock_ui.label.called


def test_render_check_result_real_server_shape():
    """R1: рендер на РЕАЛЬНОЙ форме сервера documents_check (dict по категориям)."""
    result = {
        "ok": False,
        "counts": {"sources": 5, "refs": 7, "blobs": 8, "referenced": 7},
        "issues": {"missing_blob": 1, "orphans": 2, "sha_mismatch": 0,
                   "canonical_missing": 0, "provenance_incomplete": 0,
                   "dangling_source_refs": 0, "errors": 0},
        "samples": {
            "missing_blob": [{"source_id": "src-x", "sha": "a" * 64, "ref": "original"}],
            "orphans": [{"sha256": "b" * 64, "size": 10},
                        {"sha256": "c" * 64, "size": 20}],
            "sha_mismatch": [], "canonical_missing": [],
            "provenance_incomplete": [], "dangling_source_refs": [], "errors": [],
        },
    }
    with patch.object(documents, "ui") as mock_ui:
        documents._render_check_result(result)
        documents._render_check_result({
            "ok": True,
            "counts": {"sources": 0, "refs": 0},
            "issues": {"missing_blob": 0, "orphans": 0, "sha_mismatch": 0,
                       "canonical_missing": 0, "provenance_incomplete": 0,
                       "dangling_source_refs": 0, "errors": 0},
            "samples": {},
        })
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    joined = "\n".join(labels)
    assert "Найдено проблем: 3" in joined  # сумма проблем (1 + 2)
    assert "missing_blob: 1" in joined
    assert "orphans: 2" in joined
    assert "Проблем не обнаружено" in joined  # ok-кейс


def test_render_gc_preview_uses_count_and_human_size():
    """R4: dry-run превью — счётчик (candidates_total) + human_size, без repr списка."""
    cands = [{"sha": "a" * 64, "size": 100}]
    with patch.object(documents, "ui") as mock_ui:
        documents._render_gc_result(
            {"candidates": cands, "candidates_total": 7,
             "reclaimable_bytes": 1024, "kept_fresh": 1, "kept_referenced": 2},
            dry_run=True,
        )
    labels = [" ".join(str(a) for a in c.args) for c in mock_ui.label.call_args_list]
    joined = "\n".join(labels)
    assert "Кандидатов на удаление: 7" in joined
    assert "KB" in joined or "B" in joined  # human_size(1024) = "1.0 KB"
    assert "[{" not in joined  # нет сырого repr списка
    assert "a" * 64 not in joined  # и нет сырых sha


def test_retry_reason_uses_canonical_text():
    """reason=quota_exceeded → человекочитаемый текст через canonical_reason_text."""
    from kb_console.core.utils import canonical_reason_text
    assert "квота" in canonical_reason_text("quota_exceeded")


# ── (е) роль-ключ, не base ───────────────────────────────────


def test_client_uses_role_key_not_base_source():
    """`_client()` собирает MCPClient через mcp_api_key(), без MCP_API_KEY."""
    src = inspect.getsource(documents._client)
    assert "mcp_api_key()" in src
    assert "MCP_API_KEY" not in src


@pytest.mark.asyncio
async def test_action_uses_role_key_not_base():
    """Действие строит клиент с роль-ключом (mcp_api_key), не base."""
    from kb_console.config import MCP_API_KEY

    mock_client = MagicMock()
    mock_client.documents_stats = AsyncMock(return_value={})
    mock_client.close = AsyncMock()
    with (
        patch.object(documents, "mcp_api_key", return_value="role-key-xyz"),
        patch.object(documents, "MCPClient", return_value=mock_client) as mock_cls,
    ):
        await documents._run_stats()
    assert mock_cls.call_args.kwargs["api_key"] == "role-key-xyz"
    assert mock_cls.call_args.kwargs["api_key"] != MCP_API_KEY
