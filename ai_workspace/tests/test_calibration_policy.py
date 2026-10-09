"""Э3-2 Ф7 (arch-2026-10-08-f7-calibration): policy D6 + draft-профиль + CLI.

- ``propose_scalars``: качество-first (D6) — пол качества обязателен, ₽/wall —
  ОГРАНИЧЕНИЯ (не взвешенная сумма): нарушение любого → ``applied=False`` с
  точным ``reason``;
- ``build_draft_profile``: схема ``calibration-profile/1`` проходит
  ``validate_profile`` без findings; ``calibrated_for`` — из отчёта;
  ``write_profile`` → ``load_profile`` round-trip;
- CLI ``probe_run``: dry-run по умолчанию (0 вызовов), ``--ext`` без
  ``--confirm-live`` — отказ; живой путь — на инъектированном скриптованном
  LLM-клиенте (контур fix §10 LIVE-PROBE-1: измеритель ``vp_ab_pilot.run_one``
  сам собирает движок; без живого LLM).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import policy, profiles
from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.calibration.profiles import (
    PROFILE_SCHEMA,
    REQUIRED_SCALARS,
    validate_profile,
)
from ai_workspace.orchestrator.engine import LLMResult
from ai_workspace.tools import probe_run

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-10-08T12:00:00Z"


def _report(**over) -> ProbeReport:
    """ProbeReport с качеством выше пола и нулевой ценой (по умолчанию)."""
    base: dict = {
        "run_id": "probe-abc123def456",
        "model_id": "qwen2.5:7b",
        "digest": "sha256:abc",
        "golden_manifest": "g" * 64,
        "pricing_manifest": "p" * 64,
        "golden_median_score": 0.9,
        "golden_dispersion": 0.05,
        "heldout_score": 0.85,
        "parse_rate": 1.0,
        "rub": 5.0,
        "wall_s": 100.0,
        "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


# ── propose_scalars: рамка D6 (качество-first, ₽/wall — ограничения) ───────


def test_propose_applied_when_quality_meets_floor() -> None:
    result = policy.propose_scalars(_report(), quality_floor=0.8)
    assert result["applied"] is True
    assert result["scalars"] == policy.DEFAULT_PROPOSED_SCALARS
    assert result["scalars"]["shaping"] == "full-context"
    assert result["scalars"]["context_mode"] == "full"
    assert result["constraints"] == {"quality_floor": 0.8}
    assert "quality_floor" in result["reason"]


def test_propose_not_applied_below_quality_floor() -> None:
    result = policy.propose_scalars(
        _report(golden_median_score=0.5), quality_floor=0.8
    )
    assert result["applied"] is False
    assert result["scalars"] == {}
    assert "качество ниже пола" in result["reason"]
    assert "golden_median_score=0.5000" in result["reason"]


def test_propose_not_applied_when_rub_cap_exceeded() -> None:
    result = policy.propose_scalars(_report(rub=15.0), quality_floor=0.8, rub_cap=12.0)
    assert result["applied"] is False
    assert "превышен rub_cap" in result["reason"]
    assert result["constraints"] == {"quality_floor": 0.8, "rub_cap": 12.0}


def test_propose_not_applied_when_wall_cap_exceeded() -> None:
    result = policy.propose_scalars(
        _report(wall_s=700.0), quality_floor=0.8, wall_cap_s=610.0
    )
    assert result["applied"] is False
    assert "превышен wall_cap_s" in result["reason"]
    assert result["constraints"] == {"quality_floor": 0.8, "wall_cap_s": 610.0}


def test_propose_quality_first_caps_are_frame_not_penalty() -> None:
    """D6: НЕ взвешенная сумма — качество у пола и ₽ вплотную к капу → применяем."""
    result = policy.propose_scalars(
        _report(golden_median_score=0.80, rub=11.99, wall_s=609.9),
        quality_floor=0.8,
        rub_cap=12.0,
        wall_cap_s=610.0,
    )
    assert result["applied"] is True
    assert result["scalars"] == policy.DEFAULT_PROPOSED_SCALARS


def test_propose_cap_equality_is_allowed() -> None:
    """Рамка — нестрогие неравенства: rub == rub_cap / wall == cap не нарушение."""
    result = policy.propose_scalars(
        _report(rub=12.0, wall_s=610.0),
        quality_floor=0.8,
        rub_cap=12.0,
        wall_cap_s=610.0,
    )
    assert result["applied"] is True


def test_propose_lists_all_violated_constraints() -> None:
    result = policy.propose_scalars(
        _report(golden_median_score=0.4, rub=99.0, wall_s=999.0),
        quality_floor=0.8,
        rub_cap=12.0,
        wall_cap_s=610.0,
    )
    assert result["applied"] is False
    assert "качество ниже пола" in result["reason"]
    assert "превышен rub_cap" in result["reason"]
    assert "превышен wall_cap_s" in result["reason"]


# ── build_draft_profile: схема §5.1, calibrated_for из отчёта ──────────────


def _scalars() -> dict:
    return dict(policy.DEFAULT_PROPOSED_SCALARS)


def test_build_draft_profile_validates_clean() -> None:
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=_scalars(),
        quality_floor=0.8, now=NOW,
    )
    assert validate_profile(doc) == []
    assert doc["schema"] == PROFILE_SCHEMA
    assert doc["status"] == "draft"      # §6.3: draft ставит модуль после probe
    assert doc["version"] == 1
    assert doc["model_class"] == "fast"
    assert doc["created_at"] == NOW_ISO  # now инъектируем → ISO/Z детерминизм
    assert doc["updated_at"] == NOW_ISO
    assert doc["profile_id"] == "cal-fast-qwen25-7b-f456"  # cal-<class>-<slug>-<seq>


def test_build_draft_profile_takes_calibrated_for_from_report() -> None:
    report = _report(model_id="llama3:8b", digest="sha256:xyz", run_id="probe-112233445566")
    doc = profiles.build_draft_profile(
        report, model_class="heavy", scalars=_scalars(), quality_floor=0.8, now=NOW,
    )
    assert doc["calibrated_for"] == {"model_id": "llama3:8b", "digest": "sha256:xyz"}
    assert doc["profile_id"].startswith("cal-heavy-llama3-8b-")


def test_build_draft_profile_evidence_from_report() -> None:
    report = _report()
    doc = profiles.build_draft_profile(
        report, model_class="fast", scalars=_scalars(), quality_floor=0.8, now=NOW,
    )
    assert doc["evidence"]["probe_run"] == report.run_id
    assert doc["evidence"]["golden_manifest"] == report.golden_manifest
    assert doc["evidence"]["pricing_manifest"] == report.pricing_manifest
    assert doc["evidence"]["metrics"] == {
        "golden_median_score": report.golden_median_score,
        "heldout_score": report.heldout_score,   # F6: раздельно
        "parse_rate": report.parse_rate,
        "rub": report.rub,
        "wall_s": report.wall_s,
    }


def test_build_draft_profile_keeps_only_effective_scalars() -> None:
    scalars = _scalars()
    scalars["critic_threshold"] = None   # reserved (P3-2) — не эффективен
    scalars["temperature"] = 0.7         # посторонний
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=scalars, quality_floor=0.8, now=NOW,
    )
    assert set(doc["scalars"]) == set(REQUIRED_SCALARS)
    assert validate_profile(doc) == []


def test_build_draft_profile_missing_required_scalar_is_value_error() -> None:
    scalars = _scalars()
    del scalars["retries"]
    with pytest.raises(ValueError, match="retries"):
        profiles.build_draft_profile(
            _report(), model_class="fast", scalars=scalars, quality_floor=0.8,
        )


def test_build_draft_profile_constraints_from_policy_echo() -> None:
    proposal = policy.propose_scalars(
        _report(), quality_floor=0.8, rub_cap=12.0, wall_cap_s=610.0
    )
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=proposal["scalars"],
        quality_floor=0.8, constraints=proposal["constraints"], now=NOW,
    )
    assert doc["constraints"] == {
        "quality_floor": 0.8, "rub_cap": 12.0, "wall_cap_s": 610.0,
    }
    assert validate_profile(doc) == []


# ── write_profile → load_profile: round-trip (tmp_path) ────────────────────


def test_write_profile_roundtrip(tmp_path: Path) -> None:
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=_scalars(), quality_floor=0.8, now=NOW,
    )
    path = profiles.write_profile(tmp_path, doc)
    assert path == tmp_path / f"{doc['profile_id']}.yaml"
    assert path.is_file()

    loaded = profiles.load_profile(tmp_path, doc["profile_id"])
    assert isinstance(loaded, dict)
    assert set(loaded) == set(doc)                       # равенство ключей
    for key, value in doc.items():
        assert loaded[key] == value                      # и значений (YAML round-trip)
    assert validate_profile(loaded) == []                # на носителе — валиден


def test_write_profile_writes_yaml_mapping_in_schema_order(tmp_path: Path) -> None:
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=_scalars(), quality_floor=0.8, now=NOW,
    )
    path = profiles.write_profile(tmp_path, doc)
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk == doc
    assert list(on_disk) == list(doc)                    # sort_keys=False: порядок схемы


def test_write_profile_invalid_doc_is_value_error(tmp_path: Path) -> None:
    doc = profiles.build_draft_profile(
        _report(), model_class="fast", scalars=_scalars(), quality_floor=0.8, now=NOW,
    )
    doc["status"] = "утверждён"   # вне enum → CP2
    with pytest.raises(ValueError, match="calibration-profile/1"):
        profiles.write_profile(tmp_path, doc)
    assert list(tmp_path.iterdir()) == []                # fail-closed: файла нет


# ── CLI probe_run: dry-run по умолчанию, --ext-гейт, живой путь на стабах ──


class _RepeatLLM:
    """Скриптованный LLM-клиент probe (fix LIVE-PROBE-1): постоянные ответы по
    ролям — измеритель ``vp_ab_pilot.run_one`` сам собирает движок на fakes.
    ``answers`` — свой сценарий (например, провальный критик → скор 0)."""

    def __init__(self, calls: list[str] | None = None,
                 answers: dict[str, str] | None = None) -> None:
        self._calls = calls
        self._answers = answers if answers is not None else _ANSWERS

    def complete(self, *, role, model_class, prompt, inputs, params=None,
                 job_id=None) -> LLMResult:
        if self._calls is not None:
            self._calls.append(str(job_id or role))
        return LLMResult(output=self._answers[role])


_ANSWERS = {
    "analyst": "План: 1) структура 2) черновик 3) цитаты.",
    "critic": "PASS\nРУБРИКА: полнота 1.0",
    "editor": "# Статья\n\n" + "Раздел с содержанием про MCP-RAG. src-0123 " * 60,
}


GOLDEN_TASKS = [
    {"id": "p01-structure", "zone": "public",
     "prompt": "Составь структуру статьи про MCP-RAG.",
     "expect_keywords": ["структура", "разделы", "MCP"]},
    {"id": "p02-draft", "zone": "public",
     "prompt": "Напиши черновик раздела про семафор K и VRAM.",
     "expect_keywords": ["семафор", "VRAM", "K"]},
]
HELDOUT_TASKS = [
    {"id": "h01-cite", "zone": "public",
     "prompt": "Процитируй источники по human-gate.",
     "expect_keywords": ["цитата", "источник"]},
]


def _write_yaml(path: Path, tasks: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _cli_args(tmp_path: Path, *extra: str) -> list[str]:
    golden = _write_yaml(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_yaml(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    return [
        "--mode", str(MODE), "--golden", str(golden), "--heldout", str(heldout),
        "--class", "fast", "--runs", "3", "--zone", "public",
        *extra,
    ]


def test_cli_dry_run_starts_nothing_and_returns_zero(tmp_path: Path) -> None:
    calls: list[str] = []
    code = probe_run.main(
        _cli_args(tmp_path, "--dry-run", "--profiles-dir", str(tmp_path / "profiles")),
        llm=_RepeatLLM(calls),
    )
    assert code == 0
    assert calls == []                       # 0 вызовов фабрики: план не запускает
    assert not (tmp_path / "profiles").exists() or list((tmp_path / "profiles").iterdir()) == []


def test_cli_dry_run_is_default_without_confirm_live(tmp_path: Path) -> None:
    calls: list[str] = []
    code = probe_run.main(
        _cli_args(tmp_path, "--profiles-dir", str(tmp_path / "profiles")),
        llm=_RepeatLLM(calls),
    )
    assert code == 0
    assert calls == []                       # без --confirm-live тоже только план


def test_cli_ext_without_confirm_live_is_refused(tmp_path: Path) -> None:
    calls: list[str] = []
    code = probe_run.main(
        _cli_args(tmp_path, "--ext", "--profiles-dir", str(tmp_path / "profiles")),
        llm=_RepeatLLM(calls),
    )
    assert code == 2
    assert calls == []


def test_cli_live_writes_draft_profile(tmp_path: Path, capsys) -> None:
    calls: list[str] = []
    profiles_dir = tmp_path / "profiles"
    code = probe_run.main(
        _cli_args(
            tmp_path, "--confirm-live", "--quality-floor", "0.5",
            "--profiles-dir", str(profiles_dir),
        ),
        llm=_RepeatLLM(calls),
    )
    assert code == 0
    assert calls                                   # живой путь действительно гонял харнесс

    written = list(profiles_dir.glob("cal-fast-*.yaml"))
    assert len(written) == 1
    doc = profiles.load_profile(profiles_dir, written[0].stem)
    assert doc is not None and doc["status"] == "draft"
    assert doc["scalars"] == policy.DEFAULT_PROPOSED_SCALARS
    assert doc["evidence"]["metrics"]["heldout_score"] > 0.0   # F6: замер был
    assert validate_profile(doc) == []

    out = capsys.readouterr().out
    assert "ProbeReport" in out and "applied=True" in out
    assert str(written[0]) in out                 # путь draft-профиля напечатан


def test_cli_live_below_floor_writes_nothing(tmp_path: Path, capsys) -> None:
    calls: list[str] = []
    profiles_dir = tmp_path / "profiles"
    # провальный критик: вердикт не распознаётся → скор прогонов 0 < пола 0.99
    bad_answers = {
        "analyst": "План: …",
        "critic": "Не могу оценить текст: не хватает деталей.",
        "editor": "# Документ",
    }
    code = probe_run.main(
        _cli_args(
            tmp_path, "--confirm-live", "--quality-floor", "0.99",   # недостижимо
            "--profiles-dir", str(profiles_dir),
        ),
        llm=_RepeatLLM(calls, answers=bad_answers),
    )
    assert code == 1
    assert calls
    assert not profiles_dir.exists() or list(profiles_dir.iterdir()) == []
    assert "качество ниже пола" in capsys.readouterr().out
