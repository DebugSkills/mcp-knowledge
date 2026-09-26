"""026: самолечение legacy-ключа `updated_at` у fieldless-записей (quality-скан).

До 025 payload fieldless-записи получал синтетическую дату (время индексации);
после 025 такие даты больше не пишутся, но у ранее проиндексированных записей
ложное значение осталось (в корпусе — `knowledge-readme`). Скан чистит ключ.

RED-мутация: M1 (снять вызов `delete_payload_keys`) → T26-1 красный.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from mcp_server.models import KnowledgeFrontmatter
from mcp_server.quality.scanner import _update_qdrant_payloads

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fm(kid: str, **kw) -> KnowledgeFrontmatter:
    return KnowledgeFrontmatter(
        knowledge_id=kid, domain="d", subject="s", zone="private", **kw
    )


async def test_t26_1_fieldless_triggers_payload_key_delete():
    """T26-1: fieldless-запись → удаление legacy-ключа; явная — только set_payload."""
    client = MagicMock()
    scored = [
        (Path("/k/demo/kid-a.md"), _fm("kid-a", updated_at_explicit=False), 0.1),
        (Path("/k/demo/kid-b.md"), _fm("kid-b", updated_at=T0), 0.1),
    ]

    await _update_qdrant_payloads(client, scored)

    assert client.set_payload.call_count == 2
    assert client.delete_payload_keys.call_count == 1

    kwargs = client.delete_payload_keys.call_args.kwargs
    assert kwargs["keys"] == ["updated_at"]
    cond = kwargs["points_filter"].must[0]
    assert cond.match.value == "kid-a"  # чистим ровно fieldless-запись


async def test_t26_2_explicit_only_no_delete():
    """T26-2: явные записи — удаления ключа не происходит."""
    client = MagicMock()
    scored = [(Path("/k/demo/kid-c.md"), _fm("kid-c", updated_at=T0), 0.1)]

    await _update_qdrant_payloads(client, scored)

    assert client.set_payload.call_count == 1
    assert client.delete_payload_keys.call_count == 0
