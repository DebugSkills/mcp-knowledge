"""Board-store job'а: версионируемая доска Mode engine (Ф3.5b-1).

Спека: plans/_provenance/arch-2026-10-05-ai-workspace/
       arch-2026-10-05-ai-workspace-mode-engine-spec.md §3.

Контракт (I9 плана):
- шаг читает последнюю версию, пишет **дельту секции**; версия инкрементится
  атомарно (CAS) — единственный писатель board = Mode engine;
- ``payload`` несёт ``board_version``; **duplicate** (``v_applied == current``)
  → идемпотентный no-op с продолжением; **reject — только stale**;
- версии **immutable**: снапшот ``ws:board:{job}:v:{n}`` → replay/аудит/diff;
- **single-writer по секциям**: секцию пишет один узел (иначе ``SectionConflict``).

Ключи: ``ws:board:{job}`` HASH (``version`` + ``sec:<name>``),
``ws:board:{job}:owner`` HASH (секция→узел),
``ws:board:{job}:v:{n}`` HASH (снапшот версии n).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ai_workspace.redis_client import make_ws_redis

VERSION_FIELD = "version"
SECTION_PREFIX = "sec:"


class BoardError(Exception):
    """Базовая ошибка board-store (нет снапшота, битый запрос)."""


class StaleBoard(BoardError):
    """Запись отвергнута: ожидаемая версия устарела (reject — только stale)."""


class SectionConflict(BoardError):
    """Секцию пытается писать второй узел при single-writer=True."""


_WRITE_LUA = """
local cur = tonumber(redis.call('HGET', KEYS[1], 'version')) or 0
local expect = tonumber(ARGV[1])
local writer = ARGV[2]
local single = ARGV[3] == '1'
local snap_prefix = ARGV[4]
local ts = ARGV[5]
if not expect or expect < 0 then return redis.error_reply('BOARD: bad expect') end
if expect > cur then return redis.error_reply('BOARD: future expect ' .. expect) end
local npairs = tonumber(ARGV[6])
local names, values = {}, {}
for i = 1, npairs do
  names[i] = ARGV[6 + 2 * i - 1]
  values[i] = ARGV[6 + 2 * i]
end
if expect < cur then
  -- Возможен duplicate: ожидание cur-1 и НАШИ значения уже записаны.
  local dup = (cur == expect + 1)
  if dup then
    for i = 1, npairs do
      if redis.call('HGET', KEYS[1], 'sec:' .. names[i]) ~= values[i] then
        dup = false
        break
      end
    end
  end
  if dup then return {0, cur} end
  return redis.error_reply('STALE:' .. cur)
end
-- expect == cur: применяем (в т.ч. первая запись 0 -> 1).
for i = 1, npairs do
  if single then
    local owner = redis.call('HGET', KEYS[2], names[i])
    if owner and owner ~= writer then
      return redis.error_reply('SECTION:' .. names[i])
    end
  end
end
for i = 1, npairs do
  redis.call('HSET', KEYS[1], 'sec:' .. names[i], values[i])
  redis.call('HSET', KEYS[2], names[i], writer)
end
local new = cur + 1
redis.call('HSET', KEYS[1], 'version', new)
-- immutable снапшот полного состояния версии new
local snap = snap_prefix .. new
redis.call('DEL', snap)
local all = redis.call('HGETALL', KEYS[1])
for i = 1, #all, 2 do redis.call('HSET', snap, all[i], all[i + 1]) end
redis.call('HSET', snap, 'ts', ts)
return {1, new}
"""


def _decode(raw: Any) -> tuple[int, int]:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return int(raw[0]), int(raw[1])
    raise BoardError(f"неожиданный ответ Lua: {raw!r}")


class BoardStore:
    """Версионируемая доска одного job'а (CAS + immutable снапшоты)."""

    def __init__(self, client: Any, job_id: str, *, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.job_id = job_id
        self.clock = clock
        self.key = f"ws:board:{job_id}"
        self.owner_key = f"ws:board:{job_id}:owner"
        self.snap_prefix = f"ws:board:{job_id}:v:"
        self._write = client.register_script(_WRITE_LUA)

    # ── запись ───────────────────────────────────────────────────────────

    def write_sections(
        self,
        sections: dict[str, str],
        *,
        expect_version: int,
        writer_node: str,
        single_writer: bool = True,
    ) -> int:
        """Записать секции по CAS ``expect_version → expect_version+1``.

        Возвращает новую версию. Duplicate (всё уже записано) → текущая версия
        без записи. Устаревшее ожидание → ``StaleBoard``; чужой владелец
        секции → ``SectionConflict``.
        """
        flat: list[str] = []
        for name, value in sections.items():
            flat.extend([str(name), str(value)])
        args = [
            int(expect_version),
            writer_node,
            "1" if single_writer else "0",
            self.snap_prefix,
            f"{self.clock():.6f}",
            len(sections),
            *flat,
        ]
        import redis as _redis  # ленивый импорт: модуль живёт без пакета

        try:
            raw = self._write(keys=[self.key, self.owner_key], args=args)
        except _redis.exceptions.ResponseError as exc:  # type: ignore[attr-defined]
            msg = str(exc)
            if msg.startswith("STALE:"):
                raise StaleBoard(msg) from exc
            if msg.startswith("SECTION:"):
                raise SectionConflict(msg) from exc
            raise BoardError(msg) from exc
        _applied, version = _decode(raw)
        return version

    # ── чтение ───────────────────────────────────────────────────────────

    def read(self) -> tuple[int, dict[str, str]]:
        """Текущая версия и секции доски."""
        raw: dict[str, str] = self.client.hgetall(self.key)
        version = int(raw.get(VERSION_FIELD, 0))
        sections = {
            k[len(SECTION_PREFIX):]: v for k, v in raw.items() if k.startswith(SECTION_PREFIX)
        }
        return version, sections

    def read_version(self, version: int) -> dict[str, str]:
        """Immutable-снапшот версии ``version`` (нет → ``BoardError``)."""
        raw: dict[str, str] = self.client.hgetall(f"{self.snap_prefix}{version}")
        if not raw:
            raise BoardError(f"нет снапшота версии {version}")
        return {
            k[len(SECTION_PREFIX):]: v for k, v in raw.items() if k.startswith(SECTION_PREFIX)
        }

    def owner(self, section: str) -> str | None:
        """Узел-владелец секции (single-writer) или ``None``."""
        return self.client.hget(self.owner_key, section)

    def diff(self, v_from: int, v_to: int) -> dict[str, tuple[str | None, str | None]]:
        """Изменённые секции между двумя версиями: ``{name: (old, new)}``."""
        a, b = self.read_version(v_from), self.read_version(v_to)
        names = set(a) | set(b)
        return {n: (a.get(n), b.get(n)) for n in names if a.get(n) != b.get(n)}


def board_for(job_id: str, *, clock: Callable[[], float] = time.time) -> BoardStore:
    """Собрать BoardStore на ws-redis (env ``WS_REDIS_URL``)."""
    return BoardStore(make_ws_redis(), job_id, clock=clock)
