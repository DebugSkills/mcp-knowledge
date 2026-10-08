"""Э1 Ф7 (arch-2026-10-08-f7-calibration): resolver калибровки — pure unit, offline.

Невакуумность: A1-паритет считается по реальным режимам ``ai_workspace/modes/*.yaml``
(узлы — list of dict, обёрнуты в ``{n["id"]: n}``) и реальному реестру
``ai_workspace/registry`` (сегодня все классы uncalibrated -> режим Б).

Покрытие (план Э1 §2):
- A1 — режим Б: resolved == сегодняшней семантике ``node.get``; без registry
  == с registry(uncalibrated); контроль critic/editor/critic-max_iter.
- A2 — режим П: профиль влияет (sources == "profile"), C > N > R,
  явный ``retry: 0`` в узле НЕ перекрывается классом.
- A3 — пин выигрывает (P > C), действует только при ключе в узле.
- Гейт П (fail-closed): draft / uncalibrated / digest_mismatch /
  status_divergence / registry=None -> режим Б + drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import ResolvedScalars, resolve
from ai_workspace.registry import Registry

MODES_DIR = Path(__file__).resolve().parents[1] / "modes"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
MODE_IDS = ("statya", "metodichka", "issledovanie", "statya-private")


def _nodes(mode_id: str) -> dict[str, dict]:
    """Узлы режима как Mapping[id -> node]: yaml-узлы — list of dict."""
    doc = yaml.safe_load((MODES_DIR / f"{mode_id}.yaml").read_text(encoding="utf-8"))
    return {n["id"]: n for n in doc["nodes"]}


def _calibrated_registry() -> dict:
    """Dict-реестр с откалиброванным heavy (active_profile задан, Э1-схема)."""
    return {
        "model_classes": {
            "heavy": {
                "shelf": "ext",
                "shaping": "full-context",
                "retries": 5,
                "max_iterations": 9,
                "calibration_status": "calibrated",
                "active_profile": "p-heavy-1",
            },
            "fast": {
                "shelf": "local",
                "shaping": "compressed",
                "retries": 1,
                "calibration_status": "uncalibrated",
            },
            "local-only": {"rule": "zone"},
        }
    }


def _profile(**over: object) -> dict:
    prof: dict = {
        "profile_id": "p-heavy-1",
        "status": "calibrated",
        "calibrated_for": {"model_id": "glm-5.2", "digest": "abc123"},
        "scalars": {"retries": 4, "max_iterations": 2},
    }
    prof.update(over)
    return prof


FACTS = {"model_id": "glm-5.2", "digest": "abc123"}


# ---------------------------------------------------------------- A1: паритет


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_a1_mode_b_matches_node_get_semantics(mode_id: str) -> None:
    """Режим Б: value = N если key in node иначе E; класс НЕ читается вовсе."""
    nodes = _nodes(mode_id)
    out = resolve(nodes, Registry(REGISTRY_DIR))
    assert set(out) == set(nodes)
    for nid, node in nodes.items():
        r = out[nid]
        assert isinstance(r, ResolvedScalars)
        assert r.retries == int(node.get("retry", 0))
        assert r.max_iterations == int(node.get("max_iterations", 1))
        assert r.context_mode == str(node.get("context", "full"))
        # у узлов нет shaping-ключа, класс не читается -> дефолт E
        assert r.shaping == "full-context"
        assert r.sources["retries"] == ("node" if "retry" in node else "default")
        assert r.sources["max_iterations"] == (
            "node" if "max_iterations" in node else "default"
        )
        assert r.sources["context_mode"] == ("node" if "context" in node else "default")
        assert r.sources["shaping"] == "default"
        assert r.profile_id is None
        assert r.drift is None
        assert r.stale_marks == ()


@pytest.mark.parametrize("mode_id", MODE_IDS)
def test_a1_without_registry_equals_uncalibrated_registry(mode_id: str) -> None:
    """registry=None байт-в-байт == реальному реестру (все классы uncalibrated)."""
    nodes = _nodes(mode_id)
    assert resolve(nodes, None) == resolve(nodes, Registry(REGISTRY_DIR))


def test_a1_control_critic_and_retry_nodes() -> None:
    """Контроль по плану: critic без retry -> 0; analyst/editor retry:2 -> 2;
    critic max_iterations:3 -> 3."""
    out = resolve(_nodes("statya"), None)
    assert out["critic"].retries == 0
    assert out["critic"].sources["retries"] == "default"
    assert out["critic"].max_iterations == 3
    assert out["critic"].sources["max_iterations"] == "node"
    for nid in ("analyst", "editor"):
        assert out[nid].retries == 2
        assert out[nid].sources["retries"] == "node"
    out_issl = resolve(_nodes("issledovanie"), None)
    assert out_issl["researcher"].retries == 2


def test_a1_empty_nodes_give_empty_mapping() -> None:
    assert resolve({}, Registry(REGISTRY_DIR)) == {}


# ------------------------------------------------------------ A2: режим П


def test_a2_profile_overrides_node_scalars() -> None:
    """C(profile.scalars) > N(node): профильные 4/2 перекрывают узловые retry:2 /
    max_iterations:3; sources == "profile"."""
    out = resolve(_nodes("statya"), _calibrated_registry(), FACTS, profile=_profile())
    analyst = out["analyst"]
    assert analyst.retries == 4
    assert analyst.sources["retries"] == "profile"
    assert analyst.max_iterations == 2
    assert analyst.sources["max_iterations"] == "profile"
    assert analyst.profile_id == "p-heavy-1"
    assert analyst.drift is None
    critic = out["critic"]
    assert critic.max_iterations == 2
    assert critic.sources["max_iterations"] == "profile"


def test_a2_class_fallback_only_in_mode_p() -> None:
    """R(класс) работает только в режиме П: analyst без max_iterations ни в узле,
    ни в профиле -> классовый 9; в режиме Б тот же узел -> дефолт 1."""
    reg = _calibrated_registry()
    prof = _profile(scalars={"retries": 4})  # без max_iterations
    out_p = resolve(_nodes("statya"), reg, FACTS, profile=prof)
    assert out_p["analyst"].max_iterations == 9
    assert out_p["analyst"].sources["max_iterations"] == "class"
    # shaping у узлов отсутствует -> класс (уже читается движком и в Б, здесь R)
    assert out_p["analyst"].shaping == "full-context"
    assert out_p["analyst"].sources["shaping"] == "class"
    out_b = resolve(_nodes("statya"), None)
    assert out_b["analyst"].max_iterations == 1
    assert out_b["analyst"].sources["max_iterations"] == "default"


def test_a2_explicit_node_zero_not_shadowed_by_class() -> None:
    """Явный ``retry: 0`` в узле (предикат «задано» = key in node) старше R(класс)."""
    nodes = _nodes("statya")
    analyst = dict(nodes["analyst"])
    analyst["retry"] = 0
    nodes["analyst"] = analyst
    prof = _profile(scalars={"max_iterations": 2})  # без retries в профиле
    out = resolve(nodes, _calibrated_registry(), FACTS, profile=prof)
    assert out["analyst"].retries == 0
    assert out["analyst"].sources["retries"] == "node"


def test_a2_mode_p_passes_without_model_facts() -> None:
    """model_facts=None -> calibrated_for считается совпавшим (гейт по плану)."""
    out = resolve(_nodes("statya"), _calibrated_registry(), None, profile=_profile())
    assert out["analyst"].profile_id == "p-heavy-1"
    assert out["analyst"].sources["retries"] == "profile"


# ------------------------------------------------------------ A3: пин


def test_a3_pin_wins_over_profile() -> None:
    """P > C: пин max_iterations сохраняет узловое 3 против профильного 2."""
    nodes = _nodes("statya")
    critic = dict(nodes["critic"])
    critic["calibration_pin"] = ["max_iterations"]
    nodes["critic"] = critic
    out = resolve(nodes, _calibrated_registry(), FACTS, profile=_profile())
    assert out["critic"].max_iterations == 3
    assert out["critic"].sources["max_iterations"] == "pin"
    # непиннованный параметр по-прежнему из профиля (у critic нет node.retry)
    assert out["critic"].retries == 4
    assert out["critic"].sources["retries"] == "profile"


def test_a3_pin_requires_node_key() -> None:
    """Пин без ключа в узле не действует: пин retries на critic (нет node.retry)
    -> C(профиль); пин max_iterations на analyst (нет ключа) -> C."""
    nodes = _nodes("statya")
    critic = dict(nodes["critic"])
    critic["calibration_pin"] = ["retries"]
    nodes["critic"] = critic
    analyst = dict(nodes["analyst"])
    analyst["calibration_pin"] = ["max_iterations"]
    nodes["analyst"] = analyst
    out = resolve(nodes, _calibrated_registry(), FACTS, profile=_profile())
    assert out["critic"].retries == 4
    assert out["critic"].sources["retries"] == "profile"
    assert out["analyst"].max_iterations == 2
    assert out["analyst"].sources["max_iterations"] == "profile"


def test_a3_pin_value_in_mode_b_equals_node_value() -> None:
    """В режиме Б пин не меняет значение: узловое 3 остаётся 3
    (источник помечается как pin — значение идентично N)."""
    nodes = _nodes("statya")
    critic = dict(nodes["critic"])
    critic["calibration_pin"] = ["max_iterations"]
    nodes["critic"] = critic
    out = resolve(nodes, Registry(REGISTRY_DIR))  # uncalibrated, профиль не передан
    assert out["critic"].max_iterations == 3
    assert out["critic"].sources["max_iterations"] == "pin"


# ------------------------------------------------------- гейт П (fail-closed)


def test_gate_draft_profile_falls_back_to_b() -> None:
    out = resolve(
        _nodes("statya"), _calibrated_registry(), FACTS, profile=_profile(status="draft")
    )
    analyst = out["analyst"]
    assert analyst.profile_id is None  # режим Б
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
    assert analyst.drift == "status_divergence"


def test_gate_uncalibrated_class_ignores_profile() -> None:
    """Реальный реестр: heavy uncalibrated -> профиль не применяется."""
    out = resolve(_nodes("statya"), Registry(REGISTRY_DIR), FACTS, profile=_profile())
    analyst = out["analyst"]
    assert analyst.profile_id is None
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
    assert analyst.drift == "status_divergence"


def test_gate_no_active_profile_falls_back_to_b() -> None:
    reg = _calibrated_registry()
    del reg["model_classes"]["heavy"]["active_profile"]
    out = resolve(_nodes("statya"), reg, FACTS, profile=_profile())
    analyst = out["analyst"]
    assert analyst.profile_id is None
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
    assert analyst.drift is None


def test_gate_digest_mismatch_falls_back_to_b() -> None:
    facts = {"model_id": "glm-5.2", "digest": "OTHER"}
    out = resolve(_nodes("statya"), _calibrated_registry(), facts, profile=_profile())
    analyst = out["analyst"]
    assert analyst.profile_id is None
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
    assert analyst.drift == "digest_mismatch"


def test_gate_registry_none_falls_back_to_b() -> None:
    out = resolve(_nodes("statya"), None, FACTS, profile=_profile())
    analyst = out["analyst"]
    assert analyst.profile_id is None
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
    assert analyst.max_iterations == 1
    assert analyst.sources["max_iterations"] == "default"
    assert analyst.drift == "status_divergence"


def test_gate_broken_registry_object_falls_back_to_b() -> None:
    """registry без .get (AttributeError внутри) -> режим Б, не падаем."""
    out = resolve(_nodes("statya"), object(), FACTS, profile=_profile())
    assert out["analyst"].profile_id is None
    assert out["analyst"].retries == 2


def test_gate_local_only_class_never_mode_p() -> None:
    """local-only (rule: zone) калибровке не подлежит: statya-private весь в Б."""
    out = resolve(
        _nodes("statya-private"), _calibrated_registry(), FACTS, profile=_profile()
    )
    analyst = out["analyst"]
    assert analyst.profile_id is None
    assert analyst.retries == 2
    assert analyst.sources["retries"] == "node"
