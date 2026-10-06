"""Граф режима AI-верстака: узлы, переходы, условные рёбра (Ф3.5b-2).

Спека: plans/_provenance/arch-2026-10-05-ai-workspace/…-mode-engine-spec.md §1, §3.

Рёбра — строки ``a->b`` (безусловные) и ``critic|PASS->editor`` (условные по
вердикту). Условное ребро выбирается только при совпавшем verdict; безусловное —
только для шагов без вердикта. Схемную проверку документа делает Ф3.5a-2/3,
здесь — только интерпретация.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["EngineError", "ModeGraph", "Node", "UnknownNode", "UnsupportedNode", "load_mode"]


class EngineError(RuntimeError):
    """Базовая ошибка движка."""


class UnknownNode(EngineError):
    """Узел не найден в графе режима."""


class UnsupportedNode(EngineError):
    """kind не поддерживается текущей фазой (fork/join — Ф3.8+)."""


@dataclass(frozen=True)
class Node:
    """Узел графа: id + kind + исходный spec (поля YAML как есть)."""

    id: str
    kind: str
    spec: Mapping[str, Any]

    def get(self, field: str, default: Any = None) -> Any:
        return self.spec.get(field, default)


class ModeGraph:
    """Декларативный граф режима: узлы, переходы (в т.ч. условные ``node|VERDICT->x``)."""

    def __init__(self, doc: Mapping[str, Any]) -> None:
        self.doc = doc
        self.nodes: dict[str, Node] = {}
        for raw in doc.get("nodes") or []:
            node_id = raw.get("id")
            self.nodes[node_id] = Node(id=node_id, kind=raw.get("kind"), spec=raw)
        self.edges: list[str] = list(doc.get("edges") or [])

    @classmethod
    def from_file(cls, path: str | Path) -> ModeGraph:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise UnknownNode(f"узел {node_id!r} отсутствует в графе режима") from exc

    def start(self) -> str:
        """Стартовый узел: первый источник среди edges, иначе первый узел."""
        for edge in self.edges:
            src = self._split(edge)[0]
            if src in self.nodes:
                return src
        if not self.nodes:
            raise EngineError("граф режима пуст")
        return next(iter(self.nodes))

    def next_for(self, node_id: str, verdict: str | None = None) -> str | None:
        """Следующий узел. Условные рёбра (``node|PASS->x``) требуют verdict."""
        for edge in self.edges:
            src, dst = self._split(edge)
            base, _, cond = src.partition("|")
            if base != node_id:
                continue
            if cond:
                if verdict is None or cond != verdict:
                    continue
            else:
                if verdict is not None:
                    continue  # безусловное ребро — только для шагов без вердикта
            return dst
        return None

    @staticmethod
    def _split(edge: str) -> tuple[str, str]:
        src, _, dst = edge.partition("->")
        return src.strip(), dst.strip()


def load_mode(path: str | Path) -> ModeGraph:
    """Загрузить режим из YAML (схемную валидацию делает Ф3.5a-2/3)."""
    return ModeGraph.from_file(path)


