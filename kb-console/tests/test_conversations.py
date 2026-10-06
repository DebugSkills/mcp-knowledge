"""Тесты session-store верстака (arch-2026-10-05-ai-workspace, Ф2 шаг #5a).

Спека: ``.boardData.md`` §8 «Ф2-спека» D + инвариант I11 (план Фаза 2 #5).
fakeredis в окружении НЕТ и зависимость не вводим → минимальный in-memory
FakeRedis: РОВНО команды, используемые store (hset/hgetall/expire/ttl/
zadd/zrange/zrem/delete) + injectable clock для детерминированных
TTL-порядков. ``scan``/``keys`` поднимают AssertionError — отрицательный
контроль точечности (спека D: обход БЕЗ SCAN/KEYS).

Изоляция (I11): user в тестах — «серверный слой»; ассерты — на реальном
контенте чужих ключей, не на счётчиках (спека H «ложные зелёные»).
"""

from __future__ import annotations

from typing import Any

import pytest

from kb_console.core.conversations import DEFAULT_TTL_SEC, ConversationStore


def _d(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


class FakeRedis:
    """In-memory клиент: ровно использованные store-команды + clock.

    Семантика redis-py: hgetall → dict (нет ключа → {}), ttl → секунды
    (нет ключа -2, без expiry -1), zrange end ВКЛЮЧИТЕЛЬНО (-1 → до конца).
    scan/scan_iter/keys — AssertionError (точечность, спека D).
    """

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._expiry: dict[str, float] = {}
        self.now: float = 1000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    # ── точечные команды (используемые store) ────────────────────────

    def hset(self, name: Any, mapping: dict[Any, Any] | None = None) -> None:
        target = self._hashes.setdefault(_d(name), {})
        target.update({_d(k): _d(v) for k, v in (mapping or {}).items()})

    def hgetall(self, name: Any) -> dict[str, str]:
        return dict(self._hashes.get(_d(name), {}))

    def expire(self, name: Any, seconds: float) -> bool:
        self._expiry[_d(name)] = self.now + float(seconds)
        return True

    def ttl(self, name: Any) -> int:
        key = _d(name)
        if key not in self._hashes and key not in self._zsets:
            return -2
        expiry = self._expiry.get(key)
        if expiry is None:
            return -1
        remaining = expiry - self.now
        return int(remaining) if remaining > 0 else -2

    def zadd(self, name: Any, mapping: dict[Any, float]) -> None:
        zset = self._zsets.setdefault(_d(name), {})
        for member, score in mapping.items():
            zset[_d(member)] = float(score)

    def zrange(
        self,
        name: Any,
        start: int = 0,
        end: int = -1,
        desc: bool = False,
        withscores: bool = False,
    ) -> list[Any]:
        zset = self._zsets.get(_d(name), {})
        ordered = sorted(zset.items(), key=lambda kv: (kv[1], kv[0]), reverse=desc)
        selected = ordered[start:] if end == -1 else ordered[start : end + 1]
        return list(selected) if withscores else [m for m, _ in selected]

    def zrem(self, name: Any, *members: Any) -> int:
        zset = self._zsets.get(_d(name), {})
        removed = sum(1 for m in members if zset.pop(_d(m), None) is not None)
        return removed

    def delete(self, *names: Any) -> int:
        removed = 0
        for name in names:
            key = _d(name)
            existed = any(
                key in mapping for mapping in (self._hashes, self._zsets, self._expiry)
            )
            self._hashes.pop(key, None)
            self._zsets.pop(key, None)
            self._expiry.pop(key, None)
            removed += 1 if existed else 0
        return removed

    # ── запрещённые команды (отрицательный контроль, спека D) ────────

    def scan(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("SCAN запрещён (спека Ф2 D): store точечен")

    def scan_iter(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("SCAN_ITER запрещён (спека Ф2 D): store точечен")

    def keys(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("KEYS запрещён (спека Ф2 D): store точечен")


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def store(fake: FakeRedis) -> ConversationStore:
    return ConversationStore(fake, clock=fake.time)


# ── round-trip / формат ────────────────────────────────────────────────


def test_append_history_round_trip(store: ConversationStore) -> None:
    assert store.append("alice", "s1", {"role": "user", "content": "привет"}) == 1
    assert (
        store.append("alice", "s1", {"role": "assistant", "content": "здравствуй"}) == 2
    )
    assert store.append("alice", "s1", "сырая строка") == 3
    assert store.history("alice", "s1") == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
        "сырая строка",
    ]


def test_history_limit_window(store: ConversationStore) -> None:
    for i in range(4):
        store.append("alice", "s1", f"m{i}")
    assert store.history("alice", "s1", limit=2) == ["m2", "m3"]
    assert store.history("alice", "s1", limit=0) == []
    assert store.history("alice", "s1", limit=99) == ["m0", "m1", "m2", "m3"]


def test_history_missing_session_empty(store: ConversationStore) -> None:
    assert store.history("alice", "nope") == []
    assert store.sessions("alice") == []


# ── изоляция (I11, hard) ───────────────────────────────────────────────


def test_cross_user_isolation(store: ConversationStore) -> None:
    store.append("alice", "s1", {"role": "user", "content": "тайна алисы"})
    # B не видит НИ истории, ни списка сессий A — даже с тем же именем сессии
    assert store.history("bob", "s1") == []
    assert store.sessions("bob") == []
    store.append("bob", "s1", {"role": "user", "content": "письмо боба"})
    # ключи разошлись: каждый видит ТОЛЬКО свой контент (носитель, не счётчик)
    assert store.history("alice", "s1") == [{"role": "user", "content": "тайна алисы"}]
    assert store.history("bob", "s1") == [{"role": "user", "content": "письмо боба"}]
    assert store.sessions("alice") == ["s1"]
    assert store.sessions("bob") == ["s1"]


def test_key_parts_validation(store: ConversationStore) -> None:
    for bad in ("", "a:b", None, 42):
        with pytest.raises(ValueError):
            store.append(bad, "s1", "m")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            store.append("alice", bad, "m")  # type: ignore[arg-type]


# ── TTL (I11: 30d sliding) ─────────────────────────────────────────────


def test_default_ttl_and_sliding(fake: FakeRedis, store: ConversationStore) -> None:
    store.append("alice", "s1", "m1")
    assert fake.ttl("ws:sess:alice:s1") == DEFAULT_TTL_SEC == 30 * 24 * 3600
    fake.advance(3600)
    assert fake.ttl("ws:sess:alice:s1") == DEFAULT_TTL_SEC - 3600
    store.append("alice", "s1", "m2")  # sliding: append продлевает на полный TTL
    assert fake.ttl("ws:sess:alice:s1") == DEFAULT_TTL_SEC


# ── индекс сессий ──────────────────────────────────────────────────────


def test_sessions_fresh_first_own_only(
    fake: FakeRedis, store: ConversationStore
) -> None:
    store.append("alice", "s1", "a1")
    fake.advance(10)
    store.append("alice", "s2", "a2")
    fake.advance(10)
    store.append("alice", "s3", "a3")
    fake.advance(10)
    store.append("alice", "s1", "a1-again")  # touch → s1 снова свежайшая
    store.append("bob", "b1", "b1")
    assert store.sessions("alice") == ["s1", "s3", "s2"]
    assert store.sessions("bob") == ["b1"]


def test_delete_removes_history_and_index(
    fake: FakeRedis, store: ConversationStore
) -> None:
    store.append("alice", "s1", "a")
    store.append("alice", "s2", "b")
    store.delete("alice", "s1")
    assert store.history("alice", "s1") == []
    assert store.sessions("alice") == ["s2"]
    assert fake.ttl("ws:sess:alice:s1") == -2  # ключа нет (носитель)


def test_purge_user_cascade_only_own(store: ConversationStore) -> None:
    store.append("alice", "s1", "a1")
    store.append("alice", "s2", "a2")
    store.append("bob", "s1", "b1")
    assert store.purge_user("alice") == 2
    assert store.sessions("alice") == []
    assert store.history("alice", "s1") == []
    assert store.history("alice", "s2") == []
    # B не тронут cascade-purge'ем (контент, не счётчик)
    assert store.history("bob", "s1") == ["b1"]
    assert store.sessions("bob") == ["s1"]
    assert store.purge_user("alice") == 0  # идемпотентно


# ── точечность (спека D: без SCAN/KEYS) и эфемерность процесса ────────


def test_pointed_commands_only_no_scan_keys(store: ConversationStore) -> None:
    # scan/keys/scan_iter в фейке поднимают AssertionError: полный прогон
    # API без падений доказывает, что store обходится точечными командами
    store.append("alice", "s1", "m")
    store.history("alice", "s1")
    store.sessions("alice")
    store.delete("alice", "s1")
    store.append("alice", "s2", "m")
    store.purge_user("alice")


def test_fake_rejects_scan_and_keys() -> None:
    fake = FakeRedis()
    with pytest.raises(AssertionError):
        fake.scan(0)
    with pytest.raises(AssertionError):
        fake.scan_iter()
    with pytest.raises(AssertionError):
        fake.keys("ws:sess:*")


def test_process_ephemeral_new_store_same_data(
    fake: FakeRedis, store: ConversationStore
) -> None:
    store.append("alice", "s1", "m1")
    fake.advance(60)
    restarted = ConversationStore(fake, clock=fake.time)  # «новый процесс»
    assert restarted.history("alice", "s1") == ["m1"]
    assert restarted.sessions("alice") == ["s1"]
