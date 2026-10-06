"""Conformance-гейт AI-верстака: T/I/Q + zone→egress (Ф3.9, паттерн Local-First).

Паттерн (план REV.11, `.boardData.md` §8): local — обязательный и самый рискованный
провайдер, ext — drop-in по parity. «Работает» = три РАЗДЕЛЬНЫХ вердикта:

- **T (transport)** — маршрут/зона/очередь, model-agnostic, переносится 1:1;
- **I (interface)** — tool-calling/JSON/context/streaming, **parity обеих полок
  обязательна** (ассерт): JSON, tool-call по jsonschema, tool-loop ≤ N, context-reserve,
  streaming; здесь — **prompt-hash parity** (hash РЕЗОЛВНУТОГО промпта, R2) и
  **decoding-pin** (temp=0/seed/thinking=off, P1-1: parity только при равных параметрах);
- **Q (quality)** — golden-set 5–10 заданий, **ОТЧЁТ-таблица, не ассерт** (Q non-parity):
  порог Q-floor отдельно для public/private, ниже порога → маркер ``local-draft``
  (P0-1); N≥2 прогонов + флаг вариативности (R6).

**zone→egress** (P1-4/R1, red-first): unit на РЕАЛЬНОМ маршруте — двойной ассерт
«отказ при private вне local» **И** «счётчик ext-egress == 0» (отсутствие отказа =
мутация зонного предиката).

Промпты — **model-agnostic** (P1-3): никаких 7B-хаков и веток по модели; механизм
проверки — lint L11 в ``make modes-validate`` + parity-ассерт здесь.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DECODING_PIN",
    "FLOOR_DECIDE_BY",
    "FLOOR_OWNER",
    "Q_FLOOR",
    "SHELVES",
    "ConformanceError",
    "DecodingPin",
    "DecodingPinViolation",
    "EgressViolation",
    "ExtStub",
    "ParityReport",
    "QReport",
    "QRow",
    "assert_no_ext_egress",
    "guarded_route",
    "prompt_hash",
    "prompt_parity",
    "q_floor_for",
]

SHELVES: tuple[str, ...] = ("local", "ext")
"""Полки контура: local (обязательный, дефицитный) и ext (drop-in по parity)."""


class ConformanceError(RuntimeError):
    """Базовая ошибка conformance-гейта."""


class DecodingPinViolation(ConformanceError):
    """Параметры декодинга не совпадают с пином (parity недостоверен)."""


class EgressViolation(ConformanceError):
    """Нарушение зонного маршрута: private не может уходить в ext."""


# ── I: decoding-pin (P1-1) ───────────────────────────────────────────────


@dataclass(frozen=True)
class DecodingPin:
    """Пин декодинга: parity сравнивается ТОЛЬКО при равных параметрах."""

    temperature: float = 0.0
    seed: int | None = 42
    thinking: bool = False

    def as_params(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "seed": self.seed, "thinking": self.thinking}


DECODING_PIN = DecodingPin()
"""Канонический пин: temp=0, seed=42, thinking=off (ассерты воспроизводимы)."""


def assert_decoding_pin(params: Mapping[str, Any], *, pin: DecodingPin = DECODING_PIN) -> None:
    """Параметры прогона обязаны совпадать с пином (иначе parity недействителен)."""
    expected = pin.as_params()
    for key, value in expected.items():
        actual = params.get(key, "<отсутствует>")
        if actual != value:
            raise DecodingPinViolation(
                f"декодинг не запинен: {key}={actual!r}, ожидалось {value!r} "
                f"(parity допустим только при равных параметрах)"
            )


# ── I: prompt-hash parity (P1-3, R2) ─────────────────────────────────────


@dataclass(frozen=True)
class ParityReport:
    """Результат parity: хэши РЕЗОЛВНУТОГО промпта по полкам."""

    hashes: dict[str, str]
    equal: bool

    def mismatch(self) -> list[str]:
        """Полки, чей хэш отличается от эталона (local) — для сообщения об ошибке."""
        if self.equal or not self.hashes:
            return []
        reference = next(iter(self.hashes.values()))
        return [shelf for shelf, h in self.hashes.items() if h != reference]


def prompt_hash(prompt: str) -> str:
    """Хэш промпта: нормализация переводов строк/пробелов → sha256 (R2)."""
    normalized = "\n".join(line.rstrip() for line in prompt.strip().splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()


def prompt_parity(
    prompt: str,
    *,
    shelves: Sequence[str] = SHELVES,
    resolve: Callable[[str, str], str] | None = None,
) -> ParityReport:
    """Parity промпта между полками. ``resolve`` по умолчанию — тождество.

    Тождество кодирует требование «промпты model-agnostic»: любая ветка по полке
    (7B-хак) делает хэши разными и валит parity-ассерт.
    """
    resolve = resolve or (lambda _shelf, text: text)
    hashes = {shelf: prompt_hash(resolve(shelf, prompt)) for shelf in shelves}
    return ParityReport(hashes=hashes, equal=len(set(hashes.values())) <= 1)


# ── Q: golden-set отчёт (P0-1, R3, R6) ───────────────────────────────────

Q_FLOOR: dict[str, float] = {"public": 0.80, "private": 0.85}
"""Порог качества по зонам. **Владелец и решение — оператор** (см. ниже)."""

FLOOR_OWNER = "operator"
"""Q-floor задаёт оператор, не агент: порог private выше (дефицитный local)."""

FLOOR_DECIDE_BY = "Ф3.9-старт"
"""Порог/владелец фиксируются перед первой private-«статьёй» на local (Operator Gate)."""

MARKER_FINAL = "final"
MARKER_DRAFT = "local-draft"
"""Маркер ниже порога (P0-1): черновик локальной модели не выдаётся за финал."""


def q_floor_for(zone: str, *, floors: Mapping[str, float] = Q_FLOOR) -> float:
    """Порог Q-floor для зоны (неизвестная зона → самый строгий из известных)."""
    if zone in floors:
        return float(floors[zone])
    return max(floors.values()) if floors else 0.0


@dataclass(frozen=True)
class QRow:
    """Строка Q-отчёта: один (задание, полка) с N прогонами."""

    task_id: str
    shelf: str
    zone: str
    scores: tuple[float, ...]
    floor: float
    marker: str

    @property
    def mean(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def variability(self) -> float:
        """Разброс прогонов (max−min) — флаг вариативности (R6)."""
        return (max(self.scores) - min(self.scores)) if self.scores else 0.0

    @property
    def passed(self) -> bool:
        return self.mean >= self.floor


@dataclass
class QReport:
    """Q-отчёт: таблица + сводка. Данные, НЕ ассерт (Q — non-parity)."""

    rows: list[QRow] = field(default_factory=list)
    min_runs: int = 2
    variability_flag: float = 0.15

    def add(
        self,
        task_id: str,
        shelf: str,
        zone: str,
        scores: Sequence[float],
        *,
        floors: Mapping[str, float] = Q_FLOOR,
    ) -> QRow:
        """Добавить строку. ``N>=min_runs`` обязательно (R6: одиночный прогон не отчёт)."""
        if len(scores) < self.min_runs:
            raise ConformanceError(
                f"Q-отчёт требует N>={self.min_runs} прогонов на (задание, полку); "
                f"получено {len(scores)} для {task_id!r}/{shelf!r}"
            )
        floor = q_floor_for(zone, floors=floors)
        mean = sum(scores) / len(scores)
        marker = MARKER_DRAFT if mean < floor else MARKER_FINAL
        row = QRow(task_id=task_id, shelf=shelf, zone=zone, scores=tuple(scores),
                   floor=floor, marker=marker)
        self.rows.append(row)
        return row

    @property
    def flagged(self) -> list[QRow]:
        """Строки с высокой вариативностью (нестабильный прогон)."""
        return [r for r in self.rows if r.variability > self.variability_flag]

    def by_shelf(self) -> dict[str, float]:
        """Средний балл по полкам (для сравнения local vs ext)."""
        out: dict[str, list[float]] = {}
        for row in self.rows:
            out.setdefault(row.shelf, []).append(row.mean)
        return {shelf: sum(v) / len(v) for shelf, v in out.items()}

    def to_table(self) -> str:
        """Markdown-таблица отчёта (для артефакта/доски)."""
        head = "| задание | полка | зона | N | средний | разброс | порог | маркер |"
        sep = "|---|---|---|---|---|---|---|---|"
        lines = [head, sep]
        for r in self.rows:
            lines.append(
                f"| {r.task_id} | {r.shelf} | {r.zone} | {len(r.scores)} | "
                f"{r.mean:.3f} | {r.variability:.3f} | {r.floor:.2f} | {r.marker} |"
            )
        return "\n".join(lines)


# ── T: zone→egress (P1-4, R1, red-first) ─────────────────────────────────


class ExtStub:
    """Sink ВНЕШНЕЙ полки: считает реальные попытки egress в ext (R1: счётчик == 0)."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[str] = []

    def call(self, *, shelf: str, prompt: str = "") -> str:
        self.calls += 1
        self.seen.append(f"{shelf}:{prompt[:32]}")
        return f"stub:{shelf}"


def guarded_route(shelf: str, zone: str, *, ext: ExtStub | None = None) -> str:
    """Зонный маршрут: private вне local → отказ ДО любого egress."""
    if zone == "private" and shelf != "local":
        raise EgressViolation(f"zone=private недопустим вне local: полка {shelf!r}")
    if ext is not None and shelf != "local":
        ext.call(shelf=shelf)  # sink внешней полки; local в ext не ходит
    return shelf


def assert_no_ext_egress(zone: str, shelf: str, ext: ExtStub) -> None:
    """Двойной ассерт (R1): отказ при private-в-ext **И** счётчик ext == 0.

    Вызывается для ПРОВЕРЯЕМОГО маршрута (``shelf``):
    * ``private`` + не-local → обязан быть отказ, и ни одного egress;
    * ``private`` + local → отказ НЕ ожидается (это легальный маршрут), но egress в ext
      всё равно должен быть нулевым;
    * иначе (public) → отказа быть не должно.

    Если отказ при запрещённой полке НЕ произошёл — это мутация зонного предиката
    (red-first: unit обязан упасть).
    """
    forbidden = zone == "private" and shelf != "local"
    refused = False
    try:
        guarded_route(shelf, zone, ext=ext)
    except EgressViolation:
        refused = True

    if forbidden and not refused:
        raise EgressViolation(f"мутация зонного предиката: private ушёл в {shelf!r} без отказа")
    if not forbidden and refused:
        raise ConformanceError(f"ложный отказ допустимого маршрута: {shelf!r}/{zone!r}")
    if zone == "private" and ext.calls != 0:
        raise EgressViolation(f"egress в ext при zone=private состоялся (calls={ext.calls})")
