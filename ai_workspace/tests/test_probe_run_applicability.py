"""В1-1 «Применимость» Ф7 (arch-2026-10-08-f7-calibration): шаги 1a/1b/1c/1f.

- 1a: ``--live`` резолвит факт полки НАПРЯМУЮ ``fetch_local_facts`` (не
  ``facts_for``) с endpoint, деривированным из ``OLLAMA_MODELS_URL`` полки
  ``OllamaClient`` (:11435, F-9), и передаёт его ``run_probe(model_facts=…)``:
  digest доходит до ``ProbeReport`` и ``calibrated_for`` профиля;
- 1b (F-9): ``DEFAULT_TAGS_ENDPOINT`` деривируется из ``OLLAMA_MODELS_URL`` —
  resolve-путь фактов НЕ ходит на :11434 (host-ollama других проектов);
- 1c (F-2в): ``--live`` + применённая рамка D6 + пустые model_id/digest →
  запись профиля ЗАПРЕЩЕНА (exit 2 — отказ, не warning); escape-хатч
  ``--allow-no-facts`` пропускает запись с пометкой в отчёте;
- паритет F1: без ``--live`` в ``run_probe`` уходит ``model_facts=None``.

Без HTTP: ping/http-провайдер/``run_probe`` подменены; фейк ``run_probe``
собирает ProbeReport по контракту ``probe.py`` (model_id/digest — из
``model_facts``), не выдуманной форме (правило реального контракта).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.model_facts import (
    DEFAULT_TAGS_ENDPOINT,
    ModelFacts,
    tags_endpoint_from_models_url,
)
from ai_workspace.calibration.probe import ProbeReport
from ai_workspace.tools import probe_run
from ai_workspace.tools.vp_ab_pilot import (
    OLLAMA_MODEL,
    OLLAMA_MODELS_URL,
    OllamaClient,
)

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
    """ProbeReport выше пола public (0.80) — рамка D6 применяется (до записи)."""
    base: dict = {
        "run_id": "probe-tst00000001", "model_id": "", "digest": "",
        "golden_manifest": "g" * 64, "pricing_manifest": "p" * 64,
        "golden_median_score": 0.9, "golden_dispersion": 0.0, "heldout_score": 0.9,
        "parse_rate": 1.0, "rub": 0.0, "wall_s": 0.0, "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


def _live_argv(heldout: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--heldout", str(heldout), "--class", "fast", "--live", "--confirm-live",
        "--profiles-dir", str(tmp_path / "profiles"), *extra,
    ]


def _no_live_facts() -> None:
    """Симуляция «факт полки не разрешен» (сеть/тег недоступны → None)."""


# ── 1b (F-9): endpoint фактов един с полкой OllamaClient (:11435) ─────────


def test_tags_endpoint_derived_from_shelf_models_url() -> None:
    assert tags_endpoint_from_models_url("http://127.0.0.1:11435/v1/models") == (
        "http://127.0.0.1:11435/api/tags"
    )
    # resolve-путь фактов НЕ ходит на :11434 (host-ollama других проектов)
    assert DEFAULT_TAGS_ENDPOINT == tags_endpoint_from_models_url(OLLAMA_MODELS_URL)
    assert DEFAULT_TAGS_ENDPOINT == "http://127.0.0.1:11435/api/tags"
    assert ":11434" not in DEFAULT_TAGS_ENDPOINT


# ── 1a: digest полки доходит до run_probe и calibrated_for профиля ────────


def test_live_passes_resolved_facts_to_run_probe_and_profile(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    seen: dict[str, object] = {}
    endpoints: list[str] = []

    def fake_http_get(url: str, timeout_s: float = 10.0) -> dict:
        endpoints.append(url)
        return {"models": [{"name": OLLAMA_MODEL, "digest": "sha256:live11"}]}

    def fake_run_probe(**kwargs) -> ProbeReport:
        seen.update(kwargs)
        facts = kwargs["model_facts"]
        assert isinstance(facts, ModelFacts)   # реальный тип, не авто-attr мока
        # контракт probe.py: model_id/digest ProbeReport — из model_facts
        return _report(model_id=facts.model_id, digest=facts.digest)

    monkeypatch.setattr(OllamaClient, "ping", lambda self: [OLLAMA_MODEL])
    monkeypatch.setattr(probe_run, "urllib_http_get", fake_http_get)
    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    heldout = _write_heldout(tmp_path / "heldout.yaml")

    code = probe_run.main(_live_argv(heldout, tmp_path))
    assert code == 0
    assert endpoints == ["http://127.0.0.1:11435/api/tags"]   # только полка :11435
    facts = seen["model_facts"]
    assert isinstance(facts, ModelFacts)
    assert (facts.model_id, facts.digest) == (OLLAMA_MODEL, "sha256:live11")

    out = capsys.readouterr().out
    assert "--allow-no-facts" not in out          # факты есть — пометки нет
    written = list((tmp_path / "profiles").glob("*.yaml"))
    assert len(written) == 1
    doc = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert doc["calibrated_for"] == {
        "model_id": OLLAMA_MODEL, "digest": "sha256:live11",
    }
    assert doc["status"] == "draft"


# ── 1c (F-2в): fail-closed на запись + escape-хатч ────────────────────────


def test_live_refuses_profile_write_without_facts(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """--live, рамка D6 применена, факт полки не разрешен → профиль НЕ
    пишется: отказ (exit 2), не warning."""
    monkeypatch.setattr(OllamaClient, "ping", lambda self: [OLLAMA_MODEL])
    monkeypatch.setattr(probe_run, "_resolve_live_facts", _no_live_facts)
    monkeypatch.setattr(
        probe_mod, "run_probe", lambda **kwargs: _report()  # пустые id/digest
    )
    heldout = _write_heldout(tmp_path / "heldout.yaml")

    code = probe_run.main(_live_argv(heldout, tmp_path))
    assert code == 2
    err = capsys.readouterr().err
    assert "fail-closed" in err and "--allow-no-facts" in err
    assert not (tmp_path / "profiles").exists()  # запись запрещена ДО mkdir


def test_live_allow_no_facts_writes_profile_with_note(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Escape-хатч --allow-no-facts: запись проходит, в отчёте пометка."""
    monkeypatch.setattr(OllamaClient, "ping", lambda self: [OLLAMA_MODEL])
    monkeypatch.setattr(probe_run, "_resolve_live_facts", _no_live_facts)
    monkeypatch.setattr(
        probe_mod, "run_probe", lambda **kwargs: _report()  # пустые id/digest
    )
    heldout = _write_heldout(tmp_path / "heldout.yaml")

    code = probe_run.main(_live_argv(heldout, tmp_path, "--allow-no-facts"))
    assert code == 0
    out = capsys.readouterr().out
    assert "--allow-no-facts" in out              # пометка в отчёте
    written = list((tmp_path / "profiles").glob("*.yaml"))
    assert len(written) == 1
    doc = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert doc["calibrated_for"] == {"model_id": "", "digest": ""}


# ── паритет F1: без --live факты в измеритель не уходят ───────────────────


def test_stab_path_passes_model_facts_none(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run_probe(**kwargs) -> ProbeReport:
        seen.update(kwargs)
        return _report(golden_median_score=0.1, heldout_score=0.1)  # ниже пола

    monkeypatch.setattr(probe_mod, "run_probe", fake_run_probe)
    heldout = _write_heldout(tmp_path / "heldout.yaml")
    code = probe_run.main(
        ["--heldout", str(heldout), "--class", "fast", "--confirm-live",
         "--profiles-dir", str(tmp_path / "profiles")],
    )
    assert code == 1
    assert seen.get("model_facts") is None        # паритет F1 (без --live)
