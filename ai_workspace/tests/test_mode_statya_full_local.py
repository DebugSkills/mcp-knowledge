"""В2-B 2f (CC1, arch-2026-10-08-f7-calibration): плечо «full» negative-control.

Режим ``modes/statya.full.local.yaml`` — вариант ``statya-local`` по оси
``shaping``: тот же граф/гейты/рёбра/инструменты, llm-узлы на классе
``fast-full`` (локальная полка, БЕЗ сжатия секций). Вместе с базовым
``statya.local.yaml`` (класс ``fast``, compressed) даёт пару плеч CC1 на
ОДНОЙ модели qwen2.5:7b (~0₽): различие плеч ТОЛЬКО по рычагу shaping.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ai_workspace.orchestrator.graph import load_mode
from ai_workspace.orchestrator.mode_lint import validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry

AI_DIR = Path(__file__).resolve().parents[1]
MODES_DIR = AI_DIR / "modes"
REGISTRY_DIR = AI_DIR / "registry"
REPO_ROOT = AI_DIR.parent
FULL_MODE = MODES_DIR / "statya.full.local.yaml"
BASE_MODE = MODES_DIR / "statya.local.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    reg = Registry(REGISTRY_DIR)
    reg.load()
    return reg


def test_full_mode_loads_via_load_mode() -> None:
    graph = load_mode(FULL_MODE)
    assert graph.doc["id"] == "statya-full-local"
    assert {n.id for n in graph.nodes.values()} == {
        "analyst", "structure", "critic", "editor", "citer", "publish",
    }


def test_full_mode_variant_triple_and_clean_validation() -> None:
    """Тройка варианта (S11) корректна; schema+lint без error (вкл. L16)."""
    doc = _load(FULL_MODE)
    assert doc["variant_of"] == "statya-local"     # id базового режима (S11)
    assert doc["variant_axis"] == "shaping"        # новая ось (S11)
    assert isinstance(doc["variant_rationale"], str) and doc["variant_rationale"]
    reg = _registry()
    findings = list(validate_schema(doc, reg))
    findings.extend(validate_lint(doc, reg, base_dir=REPO_ROOT))
    assert [f for f in findings if f.severity == "error"] == []


def test_full_mode_parity_with_statya_local() -> None:
    """Паритет F1: отличие от базы ТОЛЬКО model_class llm-узлов (fast→fast-full)."""
    base, full = _load(BASE_MODE), _load(FULL_MODE)
    for key in ("version", "shape", "contract", "zone", "output", "board",
                "edges", "gates", "tools"):
        assert full[key] == base[key], f"поле {key} разошлось с базой"
    assert [n["id"] for n in full["nodes"]] == [n["id"] for n in base["nodes"]]
    for b, f in zip(base["nodes"], full["nodes"], strict=True):
        if b["kind"] == "llm-step":
            assert f == dict(b, model_class="fast-full"), f"узел {b['id']}: только model_class"
        else:
            assert f == b, f"узел {b['id']} должен быть идентичен базе"


def test_full_mode_critic_stays_local_only() -> None:
    """L14 protected: вердикт качества НЕ выносит weak-класс — critic на local-only."""
    doc = _load(FULL_MODE)
    critics = [n for n in doc["nodes"] if n["kind"] == "critic-gate"]
    assert critics and all(n["model_class"] == "local-only" for n in critics)


def test_fast_full_class_local_shelf_full_context() -> None:
    """fast-full: локальная полка, full-context; от fast отличается ТОЛЬКО shaping."""
    classes = _registry().get("model_classes")
    spec = classes["fast-full"]
    assert spec["shelf"] == "local"
    assert spec["shaping"] == "full-context"
    fast = classes["fast"]
    assert fast["shelf"] == spec["shelf"]          # та же полка — паритет плеч CC1
    assert fast["shaping"] == "compressed"         # различие — только рычаг shaping
    assert spec["retries"] == fast["retries"]
