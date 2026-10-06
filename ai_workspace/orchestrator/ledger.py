"""Durable per-job ledger AI-верстака: эффекты (I4) и resume-токены (Ф3.5b-2).

``ws:fx:{job}`` HASH — кэш эффектов по ``effect_id`` + счётчики (итерации критика);
``ws:resume:{job}`` HASH — single-use токены human-gate (HGET+HDEL одной Lua).
``MemoryLedger`` — та же семантика без Redis (юнит-тесты).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

__all__ = ["Ledger", "MemoryLedger", "RedisLedger"]


class Ledger(Protocol):
    """Durable per-job KV: эффекты (I4), счётчики, single-use resume-токены."""

    def get(self, job_id: str, key: str) -> dict | None: ...
    def put(self, job_id: str, key: str, value: dict) -> None: ...
    def issue_token(self, job_id: str, node: str) -> str: ...
    def consume_token(self, job_id: str, token: str) -> str | None: ...


class MemoryLedger:
    """In-memory ledger (юнит-тесты; интерфейс идентичен RedisLedger)."""

    def __init__(self) -> None:
        self.kv: dict[tuple[str, str], dict] = {}
        self.tokens: dict[str, dict[str, str]] = {}

    def get(self, job_id: str, key: str) -> dict | None:
        val = self.kv.get((job_id, key))
        return dict(val) if val is not None else None

    def put(self, job_id: str, key: str, value: dict) -> None:
        self.kv[(job_id, key)] = dict(value)

    def issue_token(self, job_id: str, node: str) -> str:
        token = uuid.uuid4().hex
        self.tokens.setdefault(job_id, {})[token] = node
        return token

    def consume_token(self, job_id: str, token: str) -> str | None:
        return self.tokens.get(job_id, {}).pop(token, None)


class RedisLedger:
    """Ledger на ws-redis: ``ws:fx:{job}`` (эффекты/счётчики) + ``ws:resume:{job}``."""

    _CONSUME_LUA = """
local node = redis.call('HGET', KEYS[1], ARGV[1])
if not node then return false end
redis.call('HDEL', KEYS[1], ARGV[1])
return node
"""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._consume = client.register_script(self._CONSUME_LUA)

    @staticmethod
    def _fx(job_id: str) -> str:
        return f"ws:fx:{job_id}"

    @staticmethod
    def _resume(job_id: str) -> str:
        return f"ws:resume:{job_id}"

    def get(self, job_id: str, key: str) -> dict | None:
        raw = self.client.hget(self._fx(job_id), key)
        return json.loads(raw) if raw else None

    def put(self, job_id: str, key: str, value: dict) -> None:
        self.client.hset(
            self._fx(job_id), key, json.dumps(value, ensure_ascii=False, sort_keys=True)
        )

    def issue_token(self, job_id: str, node: str) -> str:
        token = uuid.uuid4().hex
        self.client.hset(self._resume(job_id), token, node)
        return token

    def consume_token(self, job_id: str, token: str) -> str | None:
        """Single-use: HGET+HDEL одной Lua — узел отдаётся ровно один раз."""
        raw = self._consume(keys=[self._resume(job_id)], args=[token])
        if not raw:
            return None
        return raw if isinstance(raw, str) else raw.decode()
