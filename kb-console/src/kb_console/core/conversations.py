"""Серверный session-store чат-сессий верстака (arch-2026-10-05-ai-workspace, Ф2 шаг #5a).

Спека: план Фаза 2 (шаг #5, стр. 100-117), ``.boardData.md`` §8 «Ф2-спека» D,
инвариант I11. Шаг 5b (отдельный ws-redis noeviction+AOF, I12) — отдельный
шаг; здесь только модуль store + тесты, без compose и новых зависимостей.

Контракт изоляции (HARD, I11 + спека D):
- ``user`` в каждый метод подаёт СЕРВЕРНЫЙ слой ИЗ identity —
  ``current_identity()["username"]`` / ws-слой; НИКОГДА из пользовательского
  ввода. Store не принимает и не читает user из payload сообщения; методов
  «без user» нет → подсунуть чужой user извне невозможно по API.
- user входит в ключ ``ws:sess:{user}:{session}`` → cross-user чтение
  недостижимо по построению: чужой ключ не резолвится без чужого user.
- Пустые ``user``/``session`` или содержащие ``:`` → ValueError ДО обращения
  к Redis (защита от крафта ключа в чужом пространстве имён).

Схема ключей (I11):
- ``ws:sess:{user}:{session}`` — hash: ``created_at``, ``updated_at``,
  сообщения ``m:1..m:N`` (str — как есть; dict/list — JSON).
- ``ws:sessidx:{user}`` — ZSET ``session -> updated_at`` (индекс сессий
  юзера, свежие первыми; обход zrange — без SCAN/KEYS).

TTL: ``DEFAULT_TTL_SEC`` = 30 дней, SLIDING — каждый append продлевает
expire ключа сессии на полный ttl. Чат-сессия (30d) — НЕ UI-cookie-сессия
(12ч); это разные сущности (спека H «ложные зелёные»).

Процесс эфемерен (I11): store не держит per-user состояния — всё в Redis;
рестарт процесса (новый ConversationStore на том же клиенте) ничего не
теряет. Cascade-purge при удалении учётки — ``purge_user``.

Точечные команды (инвариант спеки D — БЕЗ SCAN/KEYS): hset/hgetall/hdel,
expire/ttl, zadd/zrange/zrem/zcard, delete. Клиент инжектируемый,
duck-typed (протокол синхронного redis-py-подобного клиента); импорт
``redis`` НЕ делается — 5a не вводит зависимостей.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

DEFAULT_TTL_SEC = 30 * 24 * 3600
"""Sliding-TTL чат-сессии: 30 дней (I11; конфигурируемо через ``ttl_sec``)."""

_SESS_PREFIX = "ws:sess:"
_IDX_PREFIX = "ws:sessidx:"


def _decode(value: Any) -> str:
    """bytes → str (клиент без decode_responses); прочее — str(value)."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _validate_part(*parts: Any) -> None:
    """user/session: непустые str без ``:`` (гигиена схемы ключей I11).

    ``:`` — разделитель ``ws:sess:{user}:{session}``: «пустая» или
    содержащая ``:`` часть позволяет скрафтить ключ в чужом пространстве
    имён → hard-fail до любого обращения к Redis.
    """
    for part in parts:
        if not isinstance(part, str) or not part or ":" in part:
            raise ValueError(
                f"invalid key part {part!r}: ожидается непустая str без ':'"
            )


def _revive(raw: str) -> Any:
    """JSON dict/list → объект; скаляры/не-JSON → исходная строка."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    return parsed if isinstance(parsed, (dict, list)) else raw


class ConversationStore:
    """Per-user хранилище чат-сессий верстака в Redis (I11; спека Ф2 D).

    Изоляция (hard): ``user`` — параметр серверного слоя ИЗ identity
    (``current_identity()``/ws_zone), НИКОГДА из пользовательского ввода;
    cross-user-чтение невозможно: user входит в ключ, store не принимает
    user из payload сообщения. Состояние только в Redis (процесс эфемерен),
    обход — точечными командами, без SCAN/KEYS.
    """

    def __init__(self, client: Any, *, clock: Callable[[], float] = time.time) -> None:
        """``client`` — duck-typed redis-клиент (без импорта redis, шаг 5a).

        ``clock`` — DI-хук времени для unit-тестов (детерминированные
        TTL/порядок); прод-дефолт — time.time.
        """
        self._client = client
        self._clock = clock

    # ── Схема ключей (I11) ────────────────────────────────────────────

    @staticmethod
    def _sess_key(user: str, session: str) -> str:
        _validate_part(user, session)
        return f"{_SESS_PREFIX}{user}:{session}"

    @staticmethod
    def _idx_key(user: str) -> str:
        _validate_part(user)
        return f"{_IDX_PREFIX}{user}"

    # ── API ───────────────────────────────────────────────────────────

    def append(
        self,
        user: str,
        session: str,
        message: Any,
        *,
        ttl_sec: int = DEFAULT_TTL_SEC,
    ) -> int:
        """Дописать сообщение в сессию; вернуть порядковый номер (seq).

        Sliding-TTL (I11): каждый append продлевает expire ключа сессии на
        полный ``ttl_sec`` и обновляет ``updated_at`` + score сессии в
        индексе юзера. str хранится как есть; dict/list — JSON (ensure_ascii
        отключён — кириллица читаема в носителе).
        """
        key = self._sess_key(user, session)
        existing = self._client.hgetall(key) or {}
        seq = sum(1 for field in existing if _decode(field).startswith("m:")) + 1
        now = self._clock()
        payload = (
            message
            if isinstance(message, str)
            else json.dumps(message, ensure_ascii=False)
        )
        fields: dict[str, str] = {f"m:{seq}": payload, "updated_at": str(now)}
        if seq == 1:
            fields["created_at"] = str(now)
        self._client.hset(key, mapping=fields)
        self._client.expire(key, ttl_sec)
        self._client.zadd(self._idx_key(user), {session: now})
        return seq

    def history(
        self,
        user: str,
        session: str,
        limit: int | None = None,
    ) -> list[Any]:
        """Сообщения сессии в хронологическом порядке; ``limit`` — окно
        последних N (0 → []). Нет ключа/пусто → []. JSON-объекты
        восстанавливаются, скаляры остаются строками.
        """
        data = self._client.hgetall(self._sess_key(user, session)) or {}
        items = sorted(
            (int(_decode(field)[2:]), _decode(value))
            for field, value in data.items()
            if _decode(field).startswith("m:")
        )
        if limit is not None:
            items = items[-limit:] if limit > 0 else []
        return [_revive(value) for _, value in items]

    def sessions(self, user: str) -> list[str]:
        """Сессии юзера, свежие первыми (ZSET-индекс по updated_at).

        Только СВОИ сессии: индекс per-user (``ws:sessidx:{user}``), чужие
        недостижимы без чужого user. Без SCAN/KEYS (спека D).
        """
        members = self._client.zrange(self._idx_key(user), 0, -1, desc=True)
        return [_decode(member) for member in (members or [])]

    def delete(self, user: str, session: str) -> None:
        """Удалить сессию: ключ истории + запись в индексе юзера."""
        self._client.delete(self._sess_key(user, session))
        self._client.zrem(self._idx_key(user), session)

    def purge_user(self, user: str) -> int:
        """Cascade-purge (I11): ВСЕ сессии юзера по индексу, без SCAN/KEYS.

        Возвращает число удалённых сессий. Серверный слой вызывает при
        удалении учётки; чужие юзеры не затрагиваются (индекс per-user,
        ключи содержат user). Идемпотентен (повтор → 0).
        """
        idx = self._idx_key(user)
        removed = 0
        for session in self.sessions(user):
            self._client.delete(self._sess_key(user, session))
            self._client.zrem(idx, session)
            removed += 1
        return removed
