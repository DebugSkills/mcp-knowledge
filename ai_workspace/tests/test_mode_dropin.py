"""Ф5: drop-in-режимы «методичка» и «исследование» — оффлайн-регресс (без Redis).

Каждый новый режим обязан проходить schema+lint теми же хелперами, что и
канонические тесты (test_mode_schema.py / test_mode_lint.py) — код движка
не правится, значит линт должен быть зелёным «из коробки».
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.orchestrator.mode_lint import validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry

MODES_DIR = Path(__file__).resolve().parents[1] / "modes"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
REPO_ROOT = Path(__file__).resolve().parents[2]

DROPIN_MODES = [
    MODES_DIR / "metodichka.yaml",
    MODES_DIR / "issledovanie.yaml",
]


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry(REGISTRY_DIR)
    reg.load()
    return reg


@pytest.mark.parametrize(
    "mode_path",
    DROPIN_MODES,
    ids=lambda p: p.stem,
)
def test_dropin_mode_passes_schema_and_lint(mode_path: Path, registry: Registry) -> None:
    """metodichka/issledovanie: schema- и lint-проверки пусты (0 правок движка)."""
    doc = yaml.safe_load(mode_path.read_text(encoding="utf-8"))
    schema_findings = validate_schema(doc, registry)
    assert schema_findings == [], [str(f) for f in schema_findings]
    lint_findings = validate_lint(doc, registry, base_dir=REPO_ROOT)
    assert lint_findings == [], [str(f) for f in lint_findings]
    assert doc["id"] == mode_path.stem
