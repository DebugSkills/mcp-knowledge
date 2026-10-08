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
        "pricing",  # P0-1 ревизии Ф4: прайс ext-моделей (registry/pricing.py)
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


# ── Ф7-0b (arch-2026-10-08-f7-calibration): поля-основа drift-детекта ──────


def test_calibration_defaults_on_shelf_classes() -> None:
    """heavy/fast: дефолты калибровки — uncalibrated + calibrated_for=null."""
    classes = Registry(REGISTRY_DIR).get("model_classes")
    for name in ("heavy", "fast"):
        spec = classes[name]
        assert spec.get("calibration_status") == "uncalibrated", name
        assert spec.get("calibrated_for") is None, name


def test_local_only_has_no_calibration_fields() -> None:
    """local-only — зонное правило (rule: zone), калибровке не подлежит."""
    spec = Registry(REGISTRY_DIR).get("model_classes")["local-only"]
    assert spec == {"rule": "zone"}


def test_calibration_fields_do_not_touch_f7_0a() -> None:
    """Ф7-0b не меняет shaping/retries/max_chars_per_section (это Ф7-0a)."""
    classes = Registry(REGISTRY_DIR).get("model_classes")
    assert classes["heavy"]["shelf"] == "ext"
    assert classes["heavy"]["shaping"] == "full-context"
    assert classes["heavy"]["retries"] == 2
    assert classes["fast"]["shelf"] == "local"
    assert classes["fast"]["shaping"] == "compressed"
    assert classes["fast"]["max_chars_per_section"] == 4000
    assert classes["fast"]["retries"] == 1


def test_registry_loads_without_calibration_fields(tmp_path: Path) -> None:
    """Back-compat: YAML классов без новых полей (легаси-копии) грузится
    без ошибок — валидатора набора полей model_classes нет (Ф3.5a-2
    валидирует режимы, не реестры), Registry generic по построению."""
    work = _copy_registry(tmp_path)
    mc = work / "model_classes.yaml"
    data = yaml.safe_load(mc.read_text(encoding="utf-8"))
    for name in ("heavy", "fast"):
        data[name].pop("calibration_status", None)
        data[name].pop("calibrated_for", None)
    mc.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    reg = Registry(work)
    reg.load()  # RegistryError не ожидается: поля опциональны
    assert reg.get("model_classes")["heavy"].get("calibration_status") is None
    assert reg.get("model_classes")["fast"].get("calibrated_for") is None


def test_calibrated_state_roundtrip(tmp_path: Path) -> None:
    """Контракт calibrated-состояния: status=calibrated + {model_id, digest}
    переживает загрузку реестра (основа drift-детекта Ф7)."""
    work = _copy_registry(tmp_path)
    mc = work / "model_classes.yaml"
    data = yaml.safe_load(mc.read_text(encoding="utf-8"))
    data["fast"]["calibration_status"] = "calibrated"
    data["fast"]["calibrated_for"] = {"model_id": "qwen2.5:7b", "digest": "sha256:abc123"}
    mc.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    spec = Registry(work).get("model_classes")["fast"]
    assert spec["calibration_status"] == "calibrated"
    assert spec["calibrated_for"]["model_id"] == "qwen2.5:7b"
    assert spec["calibrated_for"]["digest"] == "sha256:abc123"
