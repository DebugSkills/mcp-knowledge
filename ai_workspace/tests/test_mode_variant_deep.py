"""Тесты первого mode-варианта «статья.deep» (Ф7 Э4-2, ось decomposition, H1).

Проверяет: вариант грузится load_mode, проходит S-контур (S1–S11) и линт
L1–L16 без error (включая CLI-валидатор), наследует базе по зоне (L16) и
контракту (shape/contract/zone/tools/узлы базы), содержит узел decompose
перед analyst и ребро decompose->analyst; реестр calibration/variants.yaml
проходит validate_variants с записью baseline.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ai_workspace.calibration.variants import validate_variants
from ai_workspace.orchestrator.engine import load_mode
from ai_workspace.orchestrator.mode_lint import lint_l16, validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry
from ai_workspace.tools.modes_validate import main as modes_validate_main

AI_WORKSPACE = Path(__file__).resolve().parents[1]
MODES = AI_WORKSPACE / "modes"
VARIANT = MODES / "statya.deep.yaml"
BASE = MODES / "statya.yaml"
VARIANTS_YAML = AI_WORKSPACE / "calibration" / "variants.yaml"
REGISTRY_DIR = AI_WORKSPACE / "registry"
REPO_ROOT = AI_WORKSPACE.parent


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_deep_variant_is_schema_and_lint_clean() -> None:
    doc = _load(VARIANT)
    registry = Registry(REGISTRY_DIR)
    findings = validate_schema(doc, registry) + validate_lint(doc, registry, base_dir=REPO_ROOT)
    assert findings == []


def test_deep_variant_passes_cli_validator() -> None:
    assert modes_validate_main(["--file", str(VARIANT)]) == 0


def test_deep_variant_l16_base_exists_and_zone_inherits() -> None:
    doc = _load(VARIANT)
    assert lint_l16(doc, Registry(REGISTRY_DIR), REPO_ROOT) == []
    assert doc.get("zone", "public") == _load(BASE).get("zone", "public")


def test_deep_variant_has_decompose_before_analyst() -> None:
    graph = load_mode(VARIANT)
    decompose = graph.node("decompose")
    assert decompose.kind == "llm-step"
    assert decompose.get("role") == "analyst"
    assert decompose.get("model_class") == "heavy"
    assert list(decompose.get("inputs")) == ["brief"]
    assert list(decompose.get("outputs")) == ["outline"]
    assert "decompose->analyst" in graph.edges
    assert "outline" in graph.node("analyst").get("inputs")
    assert "brief" in graph.node("analyst").get("inputs")
    assert graph.start() == "decompose"  # decompose — первый источник среди edges


def test_deep_variant_preserves_base_parity() -> None:
    doc, base = _load(VARIANT), _load(BASE)
    for field in ("shape", "contract", "zone", "output", "board", "gates", "tools"):
        assert doc[field] == base[field], f"поле {field} разошлось с базой"
    assert doc["variant_of"] == "statya"
    assert doc["variant_axis"] == "decomposition"
    assert isinstance(doc["variant_rationale"], str) and doc["variant_rationale"]
    base_nodes = {n["id"] for n in base["nodes"]}
    variant_nodes = {n["id"] for n in doc["nodes"]}
    assert base_nodes < variant_nodes  # все узлы базы сохранены + decompose
    for edge in base["edges"]:
        assert edge in graph_edges(VARIANT), f"ребро базы потеряно: {edge}"


def graph_edges(path: Path) -> list[str]:
    return list(load_mode(path).edges)


def test_variants_registry_valid_with_baseline_entry() -> None:
    doc = _load(VARIANTS_YAML)
    assert validate_variants(doc, MODES) == []
    entry = next(e for e in doc["entries"] if e.get("variant") == "statya.deep")
    assert entry["variant_of"] == "statya"
    assert entry["status"] == "baseline"
    assert entry["decided_by"] == "operator"
    assert entry["decided_at"] == "2026-10-08"
