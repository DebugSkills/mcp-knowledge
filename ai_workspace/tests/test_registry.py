"""Реестры режимов (Ф3.5a-1): kinds, ключи, hot-reload по mtime, fail-closed (offline)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from ai_workspace.registry import Registry, RegistryError

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

EXPECTED_ROLES = {"analyst", "critic", "editor", "researcher"}
EXPECTED_TOOLS = {
    "mcp.search_knowledge",
    "mcp.get_entry",
    "mcp.write_knowledge",
    "mcp.citation_attach",
}
EXPECTED_GATES = {"critic-gate", "human-gate"}
EXPECTED_MODEL_CLASSES = {"heavy", "fast", "local-only"}
EXPECTED_SHAPES = {
    "analyst-critic",
    "analyst-critic-editor",
    "analyst-critic-researcher",
    "analyst-strategic",
    "brainstorm",
    "critic",
}


def _copy_registry(tmp_path: Path) -> Path:
    """Рабочая копия реестров в tmp (мутации mtime/контента бьют только в копию)."""
    work = tmp_path / "registry"
    shutil.copytree(REGISTRY_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    return work


def test_load_and_get_all_kinds() -> None:
    reg = Registry(REGISTRY_DIR)
    reg.load()
    assert set(Registry.kinds) == {
        "roles",
        "tools",
        "gates",
        "model_classes",
        "shapes",
        "quotas",
    }
    for kind in Registry.kinds:
        data = reg.get(kind)
        assert isinstance(data, dict) and data, f"реестр {kind} пуст"


def test_expected_keys_present() -> None:
    reg = Registry(REGISTRY_DIR)  # get() лениво грузит при первом обращении
    assert EXPECTED_ROLES <= set(reg.get("roles"))
    assert EXPECTED_TOOLS <= set(reg.get("tools"))
    assert EXPECTED_GATES <= set(reg.get("gates"))
    assert EXPECTED_MODEL_CLASSES <= set(reg.get("model_classes"))
    assert EXPECTED_SHAPES <= set(reg.get("shapes"))


def test_unknown_kind_rejected() -> None:
    reg = Registry(REGISTRY_DIR)
    with pytest.raises(RegistryError):
        reg.get("modes")


def test_hot_reload_by_mtime(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    reg = Registry(work)
    reg.load()
    new_seed = ".knowledge/skills/analyst-qa/SKILL.md"
    roles = work / "roles.yaml"
    data = yaml.safe_load(roles.read_text(encoding="utf-8"))
    data["analyst"]["seed_skill"] = new_seed
    roles.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # Гранулярность mtime ФС может не заметить быструю перезапись — сдвигаем явно.
    st = roles.stat()
    os.utime(roles, (st.st_atime, st.st_mtime + 10))

    assert reg.reload_if_changed() is True
    assert reg.get("roles")["analyst"]["seed_skill"] == new_seed
    assert reg.reload_if_changed() is False


def test_broken_yaml_raises_registry_error(tmp_path: Path) -> None:
    work = _copy_registry(tmp_path)
    (work / "gates.yaml").write_text("critic-gate: [PASS, REVISE\n", encoding="utf-8")
    reg = Registry(work)
    with pytest.raises(RegistryError) as excinfo:
        reg.load()
    assert "gates.yaml" in str(excinfo.value)


def test_missing_registry_file_raises(tmp_path: Path) -> None:
    empty = tmp_path / "registry"
    empty.mkdir()
    reg = Registry(empty)
    with pytest.raises(RegistryError) as excinfo:
        reg.get("roles")
    assert "roles.yaml" in str(excinfo.value)
