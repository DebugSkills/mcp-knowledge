"""Э3-2 Ф7: тесты ЖИВОГО пути probe-run (``--live``) — БЕЗ реальных запросов к модели.

Проверяется контракт, а не сеть:
- CLI-гейт: ``--live`` без ``--confirm-live`` → exit 2 (как ``--ext``), 0 вызовов;
- ``--live --confirm-live``: probe-измерителю (``run_probe``, fix §10
  LIVE-PROBE-1) передаётся РЕАЛЬНЫЙ ``OllamaClient`` (импорт из
  ``vp_ab_pilot``) после живого preflight-ping (ping подменён — без HTTP);
  контракт kwargs проверяется на подменённом ``run_probe`` (без прогона);
  без ``--live`` — контурный стаб ``StubShelfLLM``;
- ``statya.local`` грузится ``load_mode`` и проходит S+L (modes_validate) без error;
- ``heldout-set.yaml`` валиден: парсится, 5 задач h01..h05, id не пересекаются
  с golden g01..g05.

Живой микро-смоук (1 ``OllamaClient.complete``) — операторский шаг
(``.trash/probe-live-smoke-*.md``), НЕ часть offline-сьюта.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.orchestrator.engine import load_mode
from ai_workspace.orchestrator.mode_lint import validate_lint
from ai_workspace.orchestrator.mode_schema import validate_schema
from ai_workspace.registry import Registry
from ai_workspace.tools import golden_run, probe_run
from ai_workspace.tools.vp_ab_pilot import OLLAMA_MODEL, OllamaClient

AI_DIR = Path(__file__).resolve().parents[1]
MODES_DIR = AI_DIR / "modes"
GOLDEN_DIR = AI_DIR / "tests" / "golden"
REGISTRY_DIR = AI_DIR / "registry"
REPO_ROOT = AI_DIR.parent
LOCAL_MODE = MODES_DIR / "statya.local.yaml"
BASE_MODE = MODES_DIR / "statya.yaml"
HELDOUT = GOLDEN_DIR / "heldout-set.yaml"
GOLDEN = GOLDEN_DIR / "golden-set.yaml"

_SMALLEST_SET = {
    "version": 1,
    "tasks": [{"id": "x01", "zone": "public", "prompt": "п",
               "expect_keywords": ["п"]}],
}


def _write_heldout(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(_SMALLEST_SET, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# ── CLI-гейт: --live без --confirm-live → exit 2 (как --ext) ──────────────


def test_cli_live_without_confirm_live_is_refused(tmp_path: Path, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast", "--live",
         "--profiles-dir", str(tmp_path / "profiles")],
    )
    assert code == 2
    assert "--confirm-live" in capsys.readouterr().err
    assert not (tmp_path / "profiles").exists()   # ничего не запущено и не написано


def test_cli_plan_without_live_still_returns_zero(tmp_path: Path) -> None:
    """Positive-контроль: без --live план печатается, выход 0 (гейт режет только --live)."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast",
         "--profiles-dir", str(tmp_path / "profiles")],
    )
    assert code == 0


def test_cli_live_dry_run_plan_mentions_live_engine(tmp_path: Path, capsys) -> None:
    """--live без --confirm-live отказывает ДО плана; план (без --live) нейтрален."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    probe_run.main(["--heldout", str(heldout), "--class", "fast"])
    assert "ЖИВОЙ" not in capsys.readouterr().out


# ── живой путь: run_probe получает РЕАЛЬНОГО OllamaClient (без HTTP) ──────


def _low_report(**over) -> ProbeReport:
    """ProbeReport с качеством ниже пола — рамка D6 не применяется (exit 1)."""
    base: dict = {
        "run_id": "probe-low00000001", "model_id": "", "digest": "",
        "golden_manifest": "g" * 64, "pricing_manifest": "p" * 64,
        "golden_median_score": 0.1, "golden_dispersion": 0.0, "heldout_score": 0.1,
        "parse_rate": 1.0, "rub": 0.0, "wall_s": 0.0, "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


def test_live_path_passes_ollama_client_to_run_probe(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """--live --confirm-live: живой preflight (ping подменён) → измерителю
    передан ОllamaClient из vp_ab_pilot (без единого HTTP-запроса)."""
    seen: dict[str, object] = {}

    def fake_run_probe(**kwargs) -> ProbeReport:
        seen.update(kwargs)
        return _low_report()

    monkeypatch.setattr(OllamaClient, "ping", lambda self: [OLLAMA_MODEL])
    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast", "--live",
         "--confirm-live", "--runs", "3",
         "--profiles-dir", str(tmp_path / "profiles")],
    )
    assert code == 1                     # низкое качество → D6 не применена
    assert isinstance(seen["llm"], OllamaClient)
    assert "engine_factory" not in seen  # старый контракт фабрики удалён
    assert seen["runs"] == 3 and seen["zone"] == "public"
    assert not (tmp_path / "profiles").exists()


def test_stab_path_passes_stub_llm_to_run_probe(
    tmp_path: Path, monkeypatch,
) -> None:
    """--confirm-live без --live: контурный стаб StubShelfLLM (0 HTTP, 0 модели)."""
    seen: dict[str, object] = {}

    def fake_run_probe(**kwargs) -> ProbeReport:
        seen.update(kwargs)
        return _low_report()

    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast",
         "--confirm-live", "--profiles-dir", str(tmp_path / "profiles")],
    )
    assert code == 1
    assert isinstance(seen["llm"], golden_run.StubShelfLLM)
    assert not (tmp_path / "profiles").exists()


# ── statya.local: load_mode + S/L-валидация (как make modes-validate) ─────


def _error_findings(doc: dict) -> list:
    registry = Registry(REGISTRY_DIR)
    findings = list(validate_schema(doc, registry))
    findings.extend(validate_lint(doc, registry, base_dir=REPO_ROOT))
    return [f for f in findings if f.severity == "error"]


def test_statya_local_loads_via_load_mode() -> None:
    graph = load_mode(LOCAL_MODE)
    assert graph.doc["id"] == "statya-local"
    assert {n.id for n in graph.nodes.values()} == {
        "analyst", "structure", "critic", "editor", "citer", "publish",
    }


def test_statya_local_passes_schema_and_lint_without_errors() -> None:
    doc = yaml.safe_load(LOCAL_MODE.read_text(encoding="utf-8"))
    assert doc["id"] == "statya-local"
    assert doc["zone"] == "public"
    assert "variant_of" not in doc          # НЕ ось decomposition → без variant_of
    assert _error_findings(doc) == []


def test_statya_local_matches_base_shape_and_fast_classes() -> None:
    base = yaml.safe_load(BASE_MODE.read_text(encoding="utf-8"))
    local = yaml.safe_load(LOCAL_MODE.read_text(encoding="utf-8"))
    for key in ("version", "shape", "contract", "output", "board", "edges",
                "gates", "tools"):
        assert local[key] == base[key], f"расхождение с базой по {key!r}"
    assert [n["id"] for n in local["nodes"]] == [n["id"] for n in base["nodes"]]
    for node in local["nodes"]:
        # llm-step → fast; critic-gate → local-only (L14: fast-критику запрещён,
        # local-only — local-полка ~0₽); оба класса резолвятся в полку local
        if node["kind"] == "llm-step":
            assert node["model_class"] == "fast", f"{node['id']} не на fast"
        if node["kind"] == "critic-gate":
            assert node["model_class"] == "local-only", f"{node['id']} не на local-only"


# ── heldout-set: схема golden-set, 5 задач, без пересечений ───────────────


def test_heldout_set_valid_and_disjoint_from_golden() -> None:
    doc = yaml.safe_load(HELDOUT.read_text(encoding="utf-8"))
    assert doc["owner"] == "operator"
    assert doc["decide_by"] == "Ф7-живой-прогон-2026-10-08"
    assert doc["min_runs"] == 2
    assert doc["floor"] == {"public": 0.80, "private": 0.85}
    tasks = doc["tasks"]
    assert len(tasks) == 5
    assert [t["id"] for t in tasks] == ["h01", "h02", "h03", "h04", "h05"]
    golden_ids = {
        t["id"] for t in yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))["tasks"]
    }
    assert not ({t["id"] for t in tasks} & golden_ids)   # h0* ∩ g0* = ∅
    for task in tasks:
        assert task["zone"] == "public"
        assert task["prompt"].strip()
        assert task["expect_keywords"]
        assert int(task["max_latency_s"]) > 0
