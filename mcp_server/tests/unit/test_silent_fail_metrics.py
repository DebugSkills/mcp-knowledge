"""Unit-тесты для silent-fail hardening — метрики quality_gate_skipped (Фаза 11).

Проверяет, что при сбоях quality-gate (collision check, dup-gate) метрика
mcp_quality_gate_skipped_total инкрементируется с правильными лейблами.

Тесты:
- test_collision_check_skip_metrics: H1 — замена except:pass (Qdrant unavailable)
- test_dup_gate_skip_metrics:    H3 — dup-gate exception (check_duplicates упал)
- test_embedder_unavailable_metrics: H3 — embedder/qdrant None в check_duplicates
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from mcp_server.metrics import quality_gate_skipped
from mcp_server.quality.dup_gate import check_duplicates
from mcp_server.tools.crud import write_knowledge

pytestmark = pytest.mark.asyncio

# ── Helpers ────────────────────────────────────────────────────


def _get_metric(gate: str, reason: str) -> int:
    """Безопасное чтение значения labelled Counter (0 если метка не задана)."""
    return quality_gate_skipped.labels(gate=gate, reason=reason)._value.get() or 0


# ── Tests ──────────────────────────────────────────────────────


class TestSilentFailMetrics:
    """Метрики quality_gate_skipped при сбоях quality-gate."""

    async def test_collision_check_skip_metrics(self, app_state):
        """H1: collision check skip → metric(gate=collision, reason=exception).

        Qdrant недоступен → get_all_knowledge_ids бросает исключение →
        collision check молча пропускается, метрика инкрементируется.
        """
        # _get_qdrant() checks qdrant_client first; MagicMock auto-creates it.
        # Set to None so fallback to app_state.qdrant (mock_qdrant) works.
        app_state.qdrant_client = None
        app_state.qdrant.get_all_knowledge_ids.side_effect = Exception(
            "Qdrant unavailable"
        )

        result = await write_knowledge(
            {
                "content": "# Test\nTest content.",
                "domain": "engineering",
                "subject": "testing",
                "knowledge_id": "test-collision-001",
            },
            app_state,
        )

        # Скип non-fatal — запись должна пройти
        assert "error" not in result
        assert result["knowledge_id"] is not None
        assert _get_metric("collision", "exception") >= 1

    async def test_dup_gate_skip_metrics(self, app_state):
        """H3: dup-gate skip → metric(gate=dup_gate, reason=exception).

        check_duplicates бросает неожиданное исключение →
        dup-gate молча пропускается, метрика инкрементируется.
        """
        with patch(
            "mcp_server.quality.dup_gate.check_duplicates",
            side_effect=Exception("Unexpected dup-gate error"),
        ):
            result = await write_knowledge(
                {
                    "content": "# Test\nTest content.",
                    "domain": "engineering",
                    "subject": "testing",
                },
                app_state,
            )

        # Скип non-fatal — запись должна пройти
        assert "error" not in result
        assert result["knowledge_id"] is not None
        assert _get_metric("dup_gate", "exception") >= 1

    async def test_embedder_unavailable_metrics(self):
        """H3: embedder/qdrant None → metric(gate=dup_gate, reason=embedder_unavailable).

        Вызов check_duplicates с embedder=None, qdrant_client=None →
        ранний return [], метрика инкрементируется.
        """
        result = await check_duplicates(
            content="test content",
            domain="eng",
            knowledge_id=None,
            embedder=None,
            qdrant_client=None,
        )

        # Возвращает пустой список — non-fatal
        assert result == []
        assert _get_metric("dup_gate", "embedder_unavailable") >= 1
