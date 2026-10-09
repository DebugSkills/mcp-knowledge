"""В3-B (шаг 3c Ф7, arch-2026-10-08-f7-calibration): тесты CLI variant_pair.

Живая пара base-vs-variant (``modes/statya`` × ``modes/statya.deep``) —
ОФФЛАЙН-контур: 0 HTTP, 0 GPU (``run_probe`` подменён фейком на РЕАЛЬНОМ
контракте ``ProbeReport``; ping полки подменён; ``write_report`` —
настоящий, персистенция 3a проверяется на носителе).

Покрытие (якорь — analiz-Ф7-robust-remaining.md §3 строка 3c):
- гейт живого прогона: ``--live`` без ``--confirm-live`` → exit 2,
  ``run_probe`` не вызывается НИ РАЗУ (живое не стартует);
- dry-run: без ``--confirm-live`` печатается ПЛАН (exit 0), 0 запусков;
- предусловие 2f: без ``--cc1-confirmed`` печатается ПРЕДУПРЕЖДЕНИЕ о
  negative-control CC1 (с флагом — нет);
- стаб-путь: два прогона (base/variant) → ``evaluate_promotion`` →
  reasons оператору; отчёты пары пишутся в ``--reports-dir``;
- ``--record`` без ``--confirm-record`` → отказ (exit 2), реестр не тронут;
- ``--record --confirm-record`` → ``record_decision`` с ``reports_dir``
  (CV7-existing: оба ``probe-<run_id>.json`` существуют на носителе);
- CV7 fail-closed: отчёт не существует → ``ValueError`` → exit 2, реестр
  НЕ записан;
- расхождение «записывается promoted, критерий не пройден» — предупреждение
  (решение и ответственность оператора P5).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.tools import probe_run, variant_pair
from ai_workspace.tools.golden_run import StubShelfLLM
from ai_workspace.tools.vp_ab_pilot import OLLAMA_MODEL, OllamaClient

AI_DIR = Path(__file__).resolve().parents[1]
MODES_DIR = AI_DIR / "modes"
BASE_MODE = MODES_DIR / "statya.yaml"
VARIANT_MODE = MODES_DIR / "statya.deep.yaml"

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


def _report(**over) -> ProbeReport:
    """ProbeReport по реальному контракту probe.py (не выдуманная форма)."""
    base: dict = {
        "run_id": "probe-tst00000001", "model_id": "", "digest": "",
        "golden_manifest": "g" * 64, "pricing_manifest": "p" * 64,
        "golden_median_score": 0.5, "golden_dispersion": 0.0,
        "heldout_score": 0.5, "parse_rate": 1.0, "rub": 0.0,
        "wall_s": 0.0, "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


def _base_report() -> ProbeReport:
    """База проваливает пол public 0.80 — вариант имеет смысл (§7.3.1)."""
    return _report(run_id="probe-base00000001",
                   golden_median_score=0.7, heldout_score=0.7)


def _variant_report(passed: bool = True) -> ProbeReport:
    return _report(
        run_id="probe-variant00001",
        golden_median_score=0.9 if passed else 0.75,
        heldout_score=0.88 if passed else 0.74,
        golden_dispersion=0.05,
    )


def _fake_pair(monkeypatch, base: ProbeReport, variant: ProbeReport,
               calls: list | None = None) -> None:
    """``run_probe`` подмена: плечо выбирается по mode (statya → base)."""

    def fake_run_probe(**kwargs) -> ProbeReport:
        if calls is not None:
            calls.append(kwargs)
        return base if Path(kwargs["mode"]).stem == "statya" else variant

    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)


def _argv(heldout: Path, tmp_path: Path, *extra: str) -> list[str]:
    """Арги по умолчанию: variants.yaml ВСЕГДА в tmp (реестр не трогаем)."""
    return [
        "--base", str(BASE_MODE), "--variant", str(VARIANT_MODE),
        "--heldout", str(heldout), "--class", "fast",
        "--variants-yaml", str(tmp_path / "ws" / "calibration" / "variants.yaml"),
        *extra,
    ]


def _ws(tmp_path: Path) -> Path:
    """tmp layout SSOT: ``<ws>/modes{statya,statya.deep}.yaml`` + calibration/
    (modes_dir выводится record_decision из пути variants.yaml)."""
    modes = tmp_path / "ws" / "modes"
    modes.mkdir(parents=True)
    (modes / "statya.yaml").write_text("id: statya\n", encoding="utf-8")
    (modes / "statya.deep.yaml").write_text("id: statya-deep\n", encoding="utf-8")
    return tmp_path / "ws"


# ── гейт живого прогона: --live без --confirm-live → exit 2 ────────────────


def test_live_without_confirm_live_refused(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    calls: list = []
    _fake_pair(monkeypatch, _base_report(), _variant_report(), calls)
    code = variant_pair.main(_argv(heldout, tmp_path, "--live"))
    assert code == 2
    assert "--confirm-live" in capsys.readouterr().err
    assert calls == []                       # живое не стартовало
    assert not (tmp_path / "ws").exists()    # ничего не написано


def test_dry_run_prints_plan_and_returns_zero(tmp_path, monkeypatch, capsys) -> None:
    """Положительный контроль: без --live/--confirm-live — ПЛАН, exit 0."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    calls: list = []
    _fake_pair(monkeypatch, _base_report(), _variant_report(), calls)
    code = variant_pair.main(_argv(heldout, tmp_path))
    assert code == 0
    assert "ПЛАН" in capsys.readouterr().out
    assert calls == []                       # план ничего не запускает


def test_resume_requires_reports_dir(tmp_path, monkeypatch) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report())
    with pytest.raises(SystemExit) as ei:
        variant_pair.main(_argv(heldout, tmp_path, "--resume"))
    assert ei.value.code == 2                # parser.error (как probe_run)


# ── предусловие 2f: предупреждение о negative-control CC1 ──────────────────


def test_plan_warns_2f_without_cc1_confirmed(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report())
    variant_pair.main(_argv(heldout, tmp_path))
    out = capsys.readouterr().out
    assert "ПРЕДУПРЕЖДЕНИЕ 2f" in out
    assert "CC1" in out and "negative-control" in out


def test_plan_cc1_confirmed_no_warning(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report())
    variant_pair.main(_argv(heldout, tmp_path, "--cc1-confirmed"))
    assert "ПРЕДУПРЕЖДЕНИЕ" not in capsys.readouterr().out


def test_run_path_warns_2f_without_cc1_confirmed(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Предупреждение печатается и на пути прогона (не только в плане)."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report())
    variant_pair.main(_argv(heldout, tmp_path, "--confirm-live",
                            "--reports-dir", str(tmp_path / "reports")))
    assert "ПРЕДУПРЕЖДЕНИЕ 2f" in capsys.readouterr().out


# ── стаб-путь: два прогона → evaluate_promotion → reasons ──────────────────


def test_stub_pair_two_probes_evaluate_reasons(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    calls: list = []
    _fake_pair(monkeypatch, _base_report(), _variant_report(passed=False), calls)
    reports_dir = tmp_path / "reports"
    code = variant_pair.main(_argv(heldout, tmp_path, "--confirm-live",
                                   "--reports-dir", str(reports_dir)))
    assert code == 0
    assert len(calls) == 2                   # base + variant, последовательно
    assert [Path(c["mode"]).name for c in calls] == [
        "statya.yaml", "statya.deep.yaml",
    ]
    assert isinstance(calls[0]["llm"], StubShelfLLM)   # контурный стаб CLI
    assert calls[0]["reports_dir"] == reports_dir
    assert calls[0]["needle"] is None and calls[1]["needle"] is None
    out = capsys.readouterr().out
    assert "passed=False" in out
    assert "вариант ниже quality_floor" in out   # reason из evaluate_promotion
    assert "НЕ записано" in out                  # решение по умолчанию dry-run
    # 3a: оба отчёта пары на носителе (это резолвит CV7-existing при записи)
    names = sorted(p.name for p in reports_dir.glob("probe-*.json"))
    assert names == ["probe-base00000001.json", "probe-variant00001.json"]


def test_stub_pair_passed_verdict(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report(passed=True))
    code = variant_pair.main(_argv(heldout, tmp_path, "--confirm-live"))
    assert code == 0
    out = capsys.readouterr().out
    assert "passed=True" in out
    assert "вариант ниже" not in out


def test_live_path_passes_ollama_client_to_probes(
    tmp_path, monkeypatch,
) -> None:
    """--live --confirm-live: preflight-ping (подменён) → ОБА плеча получают
    реальный OllamaClient (0 HTTP); факт полки — resolve-путь probe_run."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    calls: list = []
    _fake_pair(monkeypatch, _base_report(), _variant_report(), calls)
    monkeypatch.setattr(OllamaClient, "ping", lambda self: [OLLAMA_MODEL])
    monkeypatch.setattr(probe_run, "_resolve_live_facts", lambda: None)
    code = variant_pair.main(_argv(heldout, tmp_path, "--live", "--confirm-live",
                                   "--reports-dir", str(tmp_path / "reports")))
    assert code == 0
    assert isinstance(calls[0]["llm"], OllamaClient)
    assert isinstance(calls[1]["llm"], OllamaClient)
    assert calls[0]["model_facts"] is None   # факт не разрешён → честный None


# ── --record: отдельный Operator Gate ──────────────────────────────────────


def test_record_without_confirm_record_refused(tmp_path, monkeypatch, capsys) -> None:
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    calls: list = []
    _fake_pair(monkeypatch, _base_report(), _variant_report(), calls)
    variants = tmp_path / "ws" / "calibration" / "variants.yaml"
    code = variant_pair.main(_argv(heldout, tmp_path,
                                   "--reports-dir", str(tmp_path / "reports"),
                                   "--record", "promoted"))
    assert code == 2
    err = capsys.readouterr().err
    assert "--confirm-record" in err
    assert calls == []                       # гейт срабатывает ДО прогона
    assert not variants.exists()             # реестр не тронут


def test_record_requires_reports_dir(tmp_path, monkeypatch) -> None:
    """CV7-existing требует носитель: запись без --reports-dir — ошибка CLI."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report())
    with pytest.raises(SystemExit) as ei:
        variant_pair.main(_argv(heldout, tmp_path, "--record", "promoted",
                                "--confirm-record"))
    assert ei.value.code == 2


def test_record_writes_decision_with_cv7_reports(tmp_path, monkeypatch, capsys) -> None:
    ws = _ws(tmp_path)
    variants = ws / "calibration" / "variants.yaml"
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report(passed=True))
    reports_dir = tmp_path / "reports"
    code = variant_pair.main(_argv(heldout, tmp_path, "--confirm-live",
                                   "--reports-dir", str(reports_dir),
                                   "--record", "promoted", "--confirm-record"))
    assert code == 0
    doc = yaml.safe_load(variants.read_text(encoding="utf-8"))
    assert len(doc["entries"]) == 1
    entry = doc["entries"][0]
    assert entry["variant"] == "statya.deep"        # stem варианта
    assert entry["variant_of"] == "statya"
    assert entry["status"] == "promoted"
    assert entry["decided_by"] == "operator"
    # probe_pair = run_id ОБ ОБОИХ плеч; CV7-existing резолвит их в файлы
    assert entry["probe_pair"] == {"base": "probe-base00000001",
                                   "variant": "probe-variant00001"}
    for run_id in entry["probe_pair"].values():
        assert (reports_dir / f"{run_id}.json").is_file()
    assert "решение записано" in capsys.readouterr().out


def test_record_cv7_fail_closed_when_report_missing(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Отчёт плеча не существует → CV7 ValueError → exit 2, реестр НЕ записан."""
    ws = _ws(tmp_path)
    variants = ws / "calibration" / "variants.yaml"
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report(passed=True))
    # персистенция отключена → probe-<run_id>.json нет на носителе
    monkeypatch.setattr(
        probe_mod, "write_report",
        lambda report, reports_dir: Path(reports_dir) / "ghost.json",
    )
    code = variant_pair.main(_argv(heldout, tmp_path, "--confirm-live",
                                   "--reports-dir", str(tmp_path / "reports"),
                                   "--record", "promoted", "--confirm-record"))
    assert code == 2
    err = capsys.readouterr().err
    assert "CV7" in err
    assert not variants.exists()             # fail-closed: реестр не тронут


def test_record_mismatch_warns_but_records(tmp_path, monkeypatch, capsys) -> None:
    """Запись promoted при passed=False — предупреждение (решение за P5),
    но запись проходит: evaluate_promotion — бумажка, не решение."""
    ws = _ws(tmp_path)
    variants = ws / "calibration" / "variants.yaml"
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    _fake_pair(monkeypatch, _base_report(), _variant_report(passed=False))
    code = variant_pair.main(_argv(heldout, tmp_path, "--confirm-live",
                                   "--reports-dir", str(tmp_path / "reports"),
                                   "--record", "promoted", "--confirm-record"))
    assert code == 0
    out = capsys.readouterr().out
    assert "расхождение" in out
    doc = yaml.safe_load(variants.read_text(encoding="utf-8"))
    assert doc["entries"][0]["status"] == "promoted"
