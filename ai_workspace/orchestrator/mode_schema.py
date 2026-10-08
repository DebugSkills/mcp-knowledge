"""Схема-валидатор режимов AI-верстака — контур (а) спеки mode-engine §4 (Ф3.5a-2).

Проверяет YAML-документ режима (§1): обязательные поля, enum kind/contract,
совместимость shape x contract из реестра shapes, уникальность id узлов,
существование концов edges (формы ``a->b`` и ``critic|PASS->editor``).

Чистая функция без I/O: реестр передаётся снаружи (контракт ``registry.get``).
Рантайм-линт «по задачам» (§4б, 10 правил) — отдельный контур Ф3.5a-3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ai_workspace.orchestrator.context_delta import CONTEXT_MODES

__all__ = [
    "CALIBRATION_PIN_PARAMS",
    "CONTRACTS",
    "CONTEXT_MODES",
    "NODE_KINDS",
    "REQUIRED_FIELDS",
    "Finding",
    "validate_schema",
]

#: Спека §1: допустимые nodes[].kind
NODE_KINDS: frozenset[str] = frozenset(
    {"llm-step", "tool-step", "critic-gate", "human-gate", "fork", "join"}
)
#: Спека §1/§14: выходные контракты режимов
CONTRACTS: frozenset[str] = frozenset(
    {"document", "verdict", "decision-record", "transcript"}
)
#: Спека §1: обязательные поля документа режима
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "version",
    "shape",
    "contract",
    "nodes",
    "edges",
)
#: Ф7 (arch-2026-10-08-f7-calibration): допустимые значения nodes[].calibration_pin.
#: Пиновать можно только скаляры, живущие в узле (у узла нет shaping-ключа).
CALIBRATION_PIN_PARAMS: frozenset[str] = frozenset(
    {"retries", "max_iterations", "context_mode"}
)

SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Finding:
    """Замечание валидатора; в контуре (а) все severity — ``error``."""

    code: str
    severity: str
    message: str
    path: str


def _err(code: str, message: str, path: str) -> Finding:
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


def _edge_endpoints(edge: Any) -> tuple[str, str] | None:
    """(source, target) из форм ``a->b`` / ``critic|PASS->editor``; None — битый edge."""
    if not isinstance(edge, str) or "->" not in edge:
        return None
    source, target = edge.split("->", 1)
    return (source.split("|", 1)[0].strip(), target.strip())


def validate_schema(doc: dict, registry: Any) -> list[Finding]:
    """Проверить документ режима по контуру (а).

    ``registry`` — объект с ``get("shapes")`` (см. ``ai_workspace.registry``):
    отображение shape -> список допустимых contract.
    """
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("S1", "режим должен быть YAML-отображением (mapping)", "$")]

    # S1: обязательные поля.
    for field in REQUIRED_FIELDS:
        if field not in doc:
            findings.append(_err("S1", f"обязательное поле отсутствует: {field}", field))

    # S8: version — целое >= 1 (bool целым режимного поля не считается).
    if "version" in doc:
        version = doc["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            findings.append(
                _err(
                    "S8",
                    f"version должен быть целым >= 1, получено: {version!r}",
                    "version",
                )
            )

    # S7: contract — enum.
    contract = doc.get("contract")
    if "contract" in doc and contract not in CONTRACTS:
        findings.append(
            _err(
                "S7",
                f"неизвестный contract: {contract!r}; ожидается один из "
                f"{sorted(CONTRACTS)}",
                "contract",
            )
        )

    # S3: shape существует в каталоге shapes и допускает contract.
    shape = doc.get("shape")
    if "shape" in doc:
        shapes = registry.get("shapes")
        if not isinstance(shape, str) or shape not in shapes:
            findings.append(
                _err(
                    "S3",
                    f"shape отсутствует в каталоге shapes: {shape!r}; известны: "
                    f"{sorted(shapes)}",
                    "shape",
                )
            )
        elif isinstance(contract, str) and contract in CONTRACTS:
            allowed = shapes.get(shape, [])
            if contract not in allowed:
                findings.append(
                    _err(
                        "S3",
                        f"shape {shape!r} не допускает contract {contract!r}; "
                        f"допустимо: {list(allowed)}",
                        "contract",
                    )
                )

    # S6: nodes — непустой список отображений, каждый узел с непустым id.
    nodes = doc.get("nodes")
    if "nodes" in doc and not isinstance(nodes, list):
        findings.append(_err("S6", "nodes должен быть списком", "nodes"))
        nodes = None
    if nodes is not None and not nodes:
        findings.append(_err("S6", "nodes пуст — режим без узлов", "nodes"))

    seen_ids: set[str] = set()
    node_ids: set[str] = set()
    if isinstance(nodes, list):
        for i, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                findings.append(
                    _err(
                        "S6",
                        f"узел должен быть отображением: {type(node).__name__}",
                        f"nodes[{i}]",
                    )
                )
                continue
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                findings.append(
                    _err("S6", "узел без id (или id не непустая строка)", f"nodes[{i}]")
                )
            else:
                if node_id in seen_ids:
                    findings.append(
                        _err("S4", f"дубль id узла: {node_id!r}", f"nodes[{i}].id")
                    )
                seen_ids.add(node_id)
                node_ids.add(node_id)
            context = node.get("context")
            if context is not None and context not in CONTEXT_MODES:
                findings.append(
                    _err(
                        "S9",
                        f"неизвестный context: {context!r}; ожидается один из "
                        f"{sorted(CONTEXT_MODES)}",
                        f"nodes[{i}].context",
                    )
                )
            # S10: calibration_pin — list подмножества enum (Ф7, Э1).
            pin = node.get("calibration_pin")
            if pin is not None and (
                not isinstance(pin, list)
                or any(item not in CALIBRATION_PIN_PARAMS for item in pin)
            ):
                findings.append(
                    _err(
                        "S10",
                        "calibration_pin должен быть списком из "
                        f"{sorted(CALIBRATION_PIN_PARAMS)}, получено: {pin!r}",
                        f"nodes[{i}].calibration_pin",
                    )
                )
            if node.get("kind") not in NODE_KINDS:
                findings.append(
                    _err(
                        "S2",
                        f"неизвестный kind: {node.get('kind')!r}; ожидается один из "
                        f"{sorted(NODE_KINDS)}",
                        f"nodes[{i}].kind",
                    )
                )

    # S5: концы каждого edge существуют среди id узлов.
    edges = doc.get("edges")
    if isinstance(edges, list):
        for i, edge in enumerate(edges):
            endpoints = _edge_endpoints(edge)
            if endpoints is None:
                findings.append(
                    _err(
                        "S5",
                        "edge должен быть строкой с '->' (a->b, critic|PASS->editor), "
                        f"получено: {edge!r}",
                        f"edges[{i}]",
                    )
                )
                continue
            src, dst = endpoints
            if src not in node_ids:
                findings.append(
                    _err("S5", f"начало edge не среди id узлов: {src!r}", f"edges[{i}]")
                )
            if dst not in node_ids:
                findings.append(
                    _err("S5", f"конец edge не среди id узлов: {dst!r}", f"edges[{i}]")
                )

    return findings
