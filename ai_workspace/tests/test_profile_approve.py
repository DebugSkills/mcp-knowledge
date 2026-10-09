"""В1-2 «Применимость» Ф7 (arch-2026-10-08-f7-calibration): шаги 1d+1e+хвост 1f.

- 1d: approve-CLI fail-closed (F-2а) — пустой/неполный ``calibrated_for``
  (обязательны ``model_id`` И ``digest``) → отказ exit 2, носители не тронуты
  (в dry-run И с ``--confirm``); ``--force`` — только с ``--reason`` и записью
  решения в аудит ``approve_audit.jsonl``; атомарность паттерном t1_writeback
  (F3): сбой записи реестра откатывает профиль к исходным байтам — реестр и
  профиль консистентны, половинчатого состояния нет; ``--stale --confirm``
  делегирует ``drift.t1_writeback``;
- 1e: guard перезаписи (F6) — ``write_profile`` не затирает существующий
  не-draft профиль; ``bump_revision`` → ``version+1`` со сохранением статуса;
  повторный probe того же ``profile_id`` (``run_id`` детерминирован,
  probe.py) без ``--bump-revision`` → отказ exit 2, calibrated-профиль жив.

Все утверждения — ПО НОСИТЕЛЮ (yaml.safe_load содержимого файлов), не по
тексту отчёта CLI; реестр — tmp-копия структуры прода (прод-
``registry/model_classes.yaml`` правит только CLI оператора, тесты его
не трогают). Сети/LLM нет: ``run_probe`` подменён, ProbeReport — по
реальному контракту ``probe.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import profiles
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.tools import profile_approve, probe_run

FIXED_NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
FIXED_STAMP = "2026-10-08T12:00:00Z"

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


def _registry_doc() -> dict:
    """Копия структуры прод-реестра классов (heavy/fast/local-only)."""
    return {
        "model_classes": {
            "heavy": {
                "shelf": "ext", "shaping": "full-context", "retries": 2,
                "calibration_status": "uncalibrated",
                "calibrated_for": None, "active_profile": None,
            },
            "fast": {
                "shelf": "local", "shaping": "compressed", "retries": 1,
                "calibration_status": "uncalibrated",
                "calibrated_for": None, "active_profile": None,
            },
            "local-only": {"rule": "zone"},
        },
    }


def _carriers(tmp_path: Path) -> tuple[Path, Path]:
    """tmp-носители: каталог профилей + YAML реестра (не прод)."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    reg_path = tmp_path / "model_classes.yaml"
    reg_path.write_text(
        yaml.safe_dump(_registry_doc(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return profiles_dir, reg_path


def _profile_doc(profile_id: str = "cal-fast-qwen25-7b-12ab", **over) -> dict:
    """Валидный draft-профиль по схеме calibration-profile/1 (§5.1)."""
    doc: dict = {
        "schema": "calibration-profile/1",
        "profile_id": profile_id,
        "model_class": "fast",
        "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "sha256:q"},
        "status": "draft",
        "version": 1,
        "evidence": {
            "probe_run": "probe-abc1234xyzw",
            "golden_manifest": "g" * 64,
            "pricing_manifest": "p" * 64,
            "metrics": {
                "golden_median_score": 0.9, "heldout_score": 0.9,
                "parse_rate": 1.0, "rub": 0.0, "wall_s": 1.0,
            },
        },
        "scalars": {
            "retries": 0, "max_iterations": 1,
            "shaping": "full-context", "context_mode": "full",
        },
        "constraints": {"quality_floor": 0.8},
        "created_at": "2026-10-08T00:00:00Z",
        "updated_at": "2026-10-08T00:00:00Z",
    }
    doc.update(over)
    return doc


def _place_profile(profiles_dir: Path, doc: dict) -> Path:
    path = profiles_dir / f"{doc['profile_id']}.yaml"
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _promote(path: Path, status: str, version: int) -> None:
    """Оператор утвердил профиль (P5): правка носителя, как делает approve-CLI."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["status"], doc["version"] = status, version
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _argv(profiles_dir: Path, reg_path: Path, profile_id: str, *extra: str) -> list[str]:
    return [
        "--profile", profile_id, "--profiles-dir", str(profiles_dir),
        "--registry", str(reg_path), *extra,
    ]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── 1d: approve fail-closed (F-2а) ────────────────────────────────────────


def test_approve_refuses_incomplete_calibrated_for_nothing_written(
    tmp_path: Path, capsys,
) -> None:
    """Пустые model_id/digest → exit 2 (dry-run И --confirm); носители и аудит
    не тронуты — approve без применимости не легитимизируется (F-2а)."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(
        profiles_dir, _profile_doc(calibrated_for={"model_id": "", "digest": ""}),
    )
    before_p, before_r = ppath.read_bytes(), reg_path.read_bytes()
    for extra in ([], ["--confirm"]):
        code = profile_approve.main(
            _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", *extra),
            now=FIXED_NOW,
        )
        assert code == 2
        assert "calibrated_for" in capsys.readouterr().err
    assert ppath.read_bytes() == before_p
    assert reg_path.read_bytes() == before_r
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()


def test_approve_refuses_missing_profile(tmp_path: Path, capsys) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-none-0000"), now=FIXED_NOW,
    )
    assert code == 2
    assert "не найден" in capsys.readouterr().err


def test_approve_force_without_reason_refused_nothing_written(
    tmp_path: Path, capsys,
) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(
        profiles_dir, _profile_doc(calibrated_for={"model_id": "", "digest": ""}),
    )
    before_p, before_r = ppath.read_bytes(), reg_path.read_bytes()
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              "--force", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 2
    assert "--reason" in capsys.readouterr().err
    assert ppath.read_bytes() == before_p
    assert reg_path.read_bytes() == before_r
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()


def test_approve_force_with_reason_applies_and_audits(
    tmp_path: Path, capsys,
) -> None:
    """--force --reason: dry-run ещё без аудита; --confirm — аудит решения
    (до носителей) + оба носителя применены с фактом «как есть»."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(
        profiles_dir, _profile_doc(calibrated_for={"model_id": "", "digest": ""}),
    )
    common = ("--force", "--reason", "полка временно недоступна, замер действителен")

    # dry-run с force+reason: план, ничего не пишется (аудита ещё нет)
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", *common),
        now=FIXED_NOW,
    )
    assert code == 0
    assert "аудит" in capsys.readouterr().out
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()

    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              *common, "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 0
    # носитель 1: профиль → calibrated, version 2
    prof = _load(ppath)
    assert prof["status"] == "calibrated"
    assert prof["version"] == 2
    # носитель 2: реестр — факт «как есть» (пустоты задокументированы аудитом)
    reg = _load(reg_path)
    assert reg["model_classes"]["fast"]["calibration_status"] == "calibrated"
    assert reg["model_classes"]["fast"]["calibrated_for"] == {
        "model_id": "", "digest": "",
    }
    assert reg["model_classes"]["fast"]["active_profile"] == "cal-fast-qwen25-7b-12ab"
    # аудит: одна строка JSON с решением оператора
    audit_path = profiles_dir / profile_approve.AUDIT_FILENAME
    lines = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines == [{
        "ts": FIXED_STAMP, "action": "force_approve",
        "profile_id": "cal-fast-qwen25-7b-12ab", "model_class": "fast",
        "reason": "полка временно недоступна, замер действителен",
    }]


def test_approve_dry_run_default_writes_nothing(tmp_path: Path, capsys) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _profile_doc())
    before_p, before_r = ppath.read_bytes(), reg_path.read_bytes()
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab"), now=FIXED_NOW,
    )
    assert code == 0
    assert "ПЛАН" in capsys.readouterr().out
    assert ppath.read_bytes() == before_p
    assert reg_path.read_bytes() == before_r
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()


def test_approve_confirm_applies_both_carriers(tmp_path: Path, capsys) -> None:
    """Happy path: профиль calibrated/version+1 И реестр
    calibration_status/calibrated_for/active_profile — одной операцией."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _profile_doc())
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 0
    assert "ПРИМЕНЕНО" in capsys.readouterr().out
    prof = _load(ppath)
    assert prof["status"] == "calibrated"
    assert prof["version"] == 2                      # 1 → 2
    assert prof["updated_at"] == FIXED_STAMP         # now инъектирован
    assert prof["created_at"] == "2026-10-08T00:00:00Z"   # не тронут
    assert profiles.validate_profile(prof) == []     # на носителе — валиден
    reg = _load(reg_path)
    fast = reg["model_classes"]["fast"]
    assert fast["calibration_status"] == "calibrated"
    assert fast["calibrated_for"] == {"model_id": "qwen2.5:7b", "digest": "sha256:q"}
    assert fast["active_profile"] == "cal-fast-qwen25-7b-12ab"
    # соседний класс и rule-класс не затронуты
    heavy = reg["model_classes"]["heavy"]
    assert heavy["calibration_status"] == "uncalibrated"
    assert heavy["active_profile"] is None
    assert reg["model_classes"]["local-only"] == {"rule": "zone"}


def test_approve_refuses_rule_class_local_only(tmp_path: Path, capsys) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    _place_profile(profiles_dir, _profile_doc(model_class="local-only"))
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab"), now=FIXED_NOW,
    )
    assert code == 2
    assert "не подлежит калибровке" in capsys.readouterr().err


# ── 1d: атомарность двух носителей (паттерн t1_writeback, F3) ─────────────


def test_approve_atomic_rollback_on_registry_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """Сбой записи реестра ⇒ откат профиля: оба носителя в исходных байтах,
    никаких tmp-файлов; CLI — exit 1 (fail-loud), не молча."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _profile_doc())
    before_p, before_r = ppath.read_bytes(), reg_path.read_bytes()

    real_write = profile_approve._write_yaml_atomic

    def flaky_write(path: Path, doc: dict) -> None:
        if Path(path) == Path(reg_path):
            raise OSError("registry write failed")
        real_write(path, doc)

    monkeypatch.setattr(profile_approve, "_write_yaml_atomic", flaky_write)
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 1
    assert "не применена" in capsys.readouterr().err
    assert ppath.read_bytes() == before_p    # откат: реестр и профиль консистентны
    assert reg_path.read_bytes() == before_r
    assert not list(tmp_path.rglob("*.t1tmp"))


# ── 1d: --stale делегирует drift.t1_writeback ─────────────────────────────


def test_approve_stale_confirm_delegates_t1_writeback(
    tmp_path: Path, capsys,
) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _profile_doc())
    _promote(ppath, "calibrated", 2)
    reg = _load(reg_path)
    reg["model_classes"]["fast"].update({
        "calibration_status": "calibrated",
        "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "sha256:q"},
        "active_profile": "cal-fast-qwen25-7b-12ab",
    })
    reg_path.write_text(
        yaml.safe_dump(reg, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )

    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              "--stale", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 0
    assert _load(ppath)["status"] == "stale"
    fast = _load(reg_path)["model_classes"]["fast"]
    assert fast["calibration_status"] == "stale"
    # t1_writeback трогает только статус: ссылка профиля не потеряна
    assert fast["active_profile"] == "cal-fast-qwen25-7b-12ab"


# ── 1e: guard перезаписи в write_profile (F6) ─────────────────────────────


def test_write_profile_refuses_non_draft_overwrite(tmp_path: Path) -> None:
    """Не-draft (calibrated) не затирается новым замером: ValueError, носитель
    жив; повторный probe (run_id детерминирован → тот же profile_id) роняет
    только явно подтверждённую ревизию."""
    doc = _profile_doc()
    ppath = profiles.write_profile(tmp_path, doc)    # draft
    _promote(ppath, "calibrated", 3)                 # оператор утвердил (P5)

    fresh = _profile_doc()                           # новый замер, тот же profile_id
    fresh["evidence"] = {**fresh["evidence"], "probe_run": "probe-new0009999"}
    with pytest.raises(ValueError, match="F6"):
        profiles.write_profile(tmp_path, fresh)
    kept = _load(ppath)
    assert kept["status"] == "calibrated"
    assert kept["version"] == 3
    assert kept["evidence"]["probe_run"] == "probe-abc1234xyzw"  # замер не затёрт


def test_write_profile_bump_revision_version_up_status_kept(tmp_path: Path) -> None:
    doc = _profile_doc()
    ppath = profiles.write_profile(tmp_path, doc)
    _promote(ppath, "calibrated", 3)

    fresh = _profile_doc()
    fresh["evidence"] = {**fresh["evidence"], "probe_run": "probe-new0009999"}
    path = profiles.write_profile(tmp_path, fresh, bump_revision=True)
    assert path == ppath
    bumped = _load(ppath)
    assert bumped["status"] == "calibrated"   # статус сохранён, не сброшен
    assert bumped["version"] == 4             # 3 → 4
    assert bumped["evidence"]["probe_run"] == "probe-new0009999"  # новый замер
    assert profiles.validate_profile(bumped) == []


def test_write_profile_draft_overwrite_still_free(tmp_path: Path) -> None:
    """Паритет: повторная запись draft идемпотентна (guard режет только не-draft)."""
    doc = _profile_doc()
    profiles.write_profile(tmp_path, doc)
    path = profiles.write_profile(tmp_path, _profile_doc())
    kept = _load(path)
    assert kept["status"] == "draft"
    assert kept["version"] == 1


def test_write_profile_refuses_bump_with_unknown_existing_status(
    tmp_path: Path,
) -> None:
    """Ревизия обязана проходить схему: сохранить неизвестный status нельзя."""
    doc = _profile_doc()
    ppath = profiles.write_profile(tmp_path, doc)
    _promote(ppath, "proposed", 2)   # вне enum — носитель испорчен руками
    with pytest.raises(ValueError, match="ревизия профиля не проходит схему"):
        profiles.write_profile(tmp_path, _profile_doc(), bump_revision=True)
    assert _load(ppath)["status"] == "proposed"      # носитель не тронут


def test_write_profile_refuses_corrupt_carrier(tmp_path: Path) -> None:
    (tmp_path / "cal-fast-qwen25-7b-12ab.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="повреждён"):
        profiles.write_profile(tmp_path, _profile_doc())
    assert _load(tmp_path / "cal-fast-qwen25-7b-12ab.yaml") == ["a", "b"]


# ── 1e: проводка --bump-revision в CLI probe-run ──────────────────────────


def _fake_report(**over) -> ProbeReport:
    """ProbeReport выше пола public (0.80): рамка D6 применяется, факты полны."""
    base: dict = {
        "run_id": "probe-fixed12ab", "model_id": "qwen2.5:7b", "digest": "sha256:q",
        "golden_manifest": "g" * 64, "pricing_manifest": "p" * 64,
        "golden_median_score": 0.9, "golden_dispersion": 0.0, "heldout_score": 0.9,
        "parse_rate": 1.0, "rub": 0.0, "wall_s": 0.0, "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


def test_probe_run_reprobe_keeps_calibrated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """run_id детерминирован → повторный probe даёт тот же profile_id: без
    --bump-revision guard режет перезапись (exit 2); с флагом — ревизия
    version+1 со сохранением статуса calibrated."""
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setattr(probe_mod, "run_probe", lambda **kw: _fake_report())
    argv = [
        "--heldout", str(heldout), "--class", "fast", "--confirm-live",
        "--profiles-dir", str(profiles_dir),
    ]

    assert probe_run.main(argv) == 0                 # первый прогон → draft v1
    pid = profiles.list_profiles(profiles_dir)[0]
    ppath = profiles_dir / f"{pid}.yaml"
    _promote(ppath, "calibrated", 3)                 # оператор утвердил (P5)

    code = probe_run.main(argv)                      # тот же замер → тот же id
    assert code == 2
    assert "--bump-revision" in capsys.readouterr().err
    kept = _load(ppath)
    assert kept["status"] == "calibrated"            # calibrated не роняется
    assert kept["version"] == 3

    code = probe_run.main([*argv, "--bump-revision"])
    assert code == 0
    bumped = _load(ppath)
    assert bumped["status"] == "calibrated"          # никакой тихой downgrade
    assert bumped["version"] == 4
    assert profiles.validate_profile(bumped) == []
