"""Integration-тесты board-store (Ф3.5b-1) — живой ws-redis.

Ключевые доказательства: CAS (stale отвергнут БЕЗ записи), duplicate
(``v_applied == current``) → идемпотентный no-op без новых версий,
single-writer секций, immutable снапшот v1 после записи v2, diff версий.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_workspace.orchestrator.board import (
    BoardError,
    BoardStore,
    SectionConflict,
    StaleBoard,
)
from ai_workspace.tests.conftest import WS_TEST_ID_PREFIX, requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


@pytest.fixture()
def board():
    """BoardStore на изолированном job; уборка только своих ключей."""
    from ai_workspace.redis_client import make_ws_redis

    job = f"{WS_TEST_ID_PREFIX}f35b-{uuid4().hex[:8]}"
    store = BoardStore(make_ws_redis(), job, clock=lambda: 1000.0)
    yield store
    keys = list(store.client.scan_iter(match=f"ws:board:{job}*"))
    if keys:
        store.client.delete(*keys)


def test_first_write_and_read(board: BoardStore):
    """Первая запись (expect 0) → версия 1; read() её видит."""
    assert board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst") == 1
    version, sections = board.read()
    assert version == 1 and sections == {"draft": "v1"}
    assert board.owner("draft") == "analyst"


def test_stale_rejected_without_writes(board: BoardStore):
    """Устаревшее ожидание → StaleBoard, версия и снапшоты НЕ изменились."""
    board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst")
    snaps_before = len(list(board.client.scan_iter(match=f"{board.snap_prefix}*")))
    with pytest.raises(StaleBoard):
        board.write_sections({"draft": "v2"}, expect_version=0, writer_node="analyst")
    version, sections = board.read()
    assert version == 1 and sections == {"draft": "v1"}
    assert len(list(board.client.scan_iter(match=f"{board.snap_prefix}*"))) == snaps_before


def test_duplicate_is_noop(board: BoardStore):
    """Повтор уже применённой записи (expect = current-1, те же значения) → no-op."""
    board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst")
    snaps_before = len(list(board.client.scan_iter(match=f"{board.snap_prefix}*")))
    version = board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst")
    assert version == 1  # ничего не записано
    assert len(list(board.client.scan_iter(match=f"{board.snap_prefix}*"))) == snaps_before


def test_single_writer_enforced(board: BoardStore):
    """Вторая запись в ту же секцию другим узлом → SectionConflict; не меняет доску."""
    board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst")
    with pytest.raises(SectionConflict):
        board.write_sections({"draft": "v2"}, expect_version=1, writer_node="editor")
    assert board.read()[0] == 1
    # явное разрешение множественного писателя
    assert board.write_sections(
        {"draft": "v2"}, expect_version=1, writer_node="editor", single_writer=False
    ) == 2


def test_snapshot_immutable_and_diff(board: BoardStore):
    """Снапшот v1 не меняется после записи v2; diff(1,2) показывает дельту."""
    board.write_sections({"draft": "v1"}, expect_version=0, writer_node="analyst")
    board.write_sections({"verdict": "PASS"}, expect_version=1, writer_node="critic")
    assert board.read_version(1) == {"draft": "v1"}  # v1 неизменен
    assert board.read_version(2) == {"draft": "v1", "verdict": "PASS"}
    assert board.diff(1, 2) == {"verdict": (None, "PASS")}
    with pytest.raises(BoardError):
        board.read_version(99)


def test_missing_snapshot_raises(board: BoardStore):
    """Несуществующая версия → BoardError (не молчаливый пустой dict)."""
    with pytest.raises(BoardError):
        board.read_version(1)
