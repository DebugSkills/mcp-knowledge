"""В2-A «Достоверность» Ф7 (arch-2026-10-08-f7-calibration): шаги 2b/2c/2d/2g.

- 2b ceiling: медиана >= 0.999 ∧ dispersion == 0 → флаг ``ceiling``; чуть
  ниже порога ИЛИ ненулевой разброс → флага нет (порог не «примерно»);
  флаги замера персистятся в ``evidence.flags`` профиля (additive, Г6);
  approve при ``ceiling`` — отказ (dry-run И ``--confirm``) без явного
  решения оператора; ``--ceiling-ok --reason`` — аудит ``ceiling_approve`` +
  носители применены; ``--stale`` ceiling не блокируется (понижение);
- 2c in/out-токены: ``RunOutcome.tokens_in/tokens_out`` из usage ответов
  (журнал клиента в форме ``OllamaClient.calls``); ₽ — точная оценка
  (вход 0.30 / выход 1.20 USD за 1M, pricing.yaml), local → 0.0;
- 2d parse_rate-честность: режим без critic-узла → ``parse_rate_defined``
  False, отчёт CLI печатает «—», значение не публикуется;
- 2g капы D6 из CLI: ``--rub-cap/--wall-cap`` → ``propose_scalars``
  (нарушение → exit 1, профиль не пишется; капы — эхом в constraints).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ai_workspace.calibration import profiles as profiles_mod
from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.probe import run_probe
from ai_workspace.orchestrator.engine import LLMResult
from ai_workspace.registry import Registry
from ai_workspace.registry.pricing import MICRO_PER_UNIT, PricingRegistry
from ai_workspace.tests.test_profile_approve import (
    FIXED_NOW,
    FIXED_STAMP,
    _argv,
    _carriers,
    _load,
    _place_profile,
    _profile_doc,
    _promote,
)
from ai_workspace.tools import probe_run, profile_approve, vp_ab_pilot
from ai_workspace.tools.vp_ab_pilot import RunOutcome, run_one

import json

MODE = Path(__file__).resolve().parents[1] / "modes" / "statya.yaml"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

GOLDEN_TASKS = [
    {"id": "g01-structure", "zone": "public",
     "prompt": "Составь структуру статьи про MCP-RAG."},
    {"id": "g02-draft", "zone": "public",
     "prompt": "Напиши черновик раздела про семафор K и VRAM."},
]
HELDOUT_TASKS = [
    {"id": "h01-cite", "zone": "public",
     "prompt": "Процитируй источники по human-gate."},
]

#: минимальный режим БЕЗ critic-gate (2d): verdict_parse_ok у run_one
#: нейтрален — parse_rate не измерение
NO_CRITIC_MODE = """\
id: nocritic
version: 1
shape: solo
contract: document
zone: public
output: {type: article, sections: [document]}
board: {buffer: board.md, store: job}
nodes:
  - id: analyst
    kind: llm-step
    role: analyst
    model_class: fast
    inputs: [brief]
    outputs: [draft]
    writes: [draft]
  - id: publish
    kind: human-gate
    actor: operator
    prompt: "Утвердить документ?"
    timeout: 24h
    on_timeout: sleep
    on_approve: null
    on_edit: analyst
    writes: [publish]
edges:
  - analyst->publish
gates:
  publish: {type: human, blocks_output: true}
tools: []
"""


def _write_set(path: Path, tasks: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _outcome(
    task_id: str, run_idx: int, *, score: float = 0.5, parse_ok: bool = True,
    tokens: int = 0, tokens_in: int = 0, tokens_out: int = 0,
    wall_s: float = 0.1, status: str = "done",
) -> RunOutcome:
    """RunOutcome живого измерителя с нужными скалярами (контракт vp_ab_pilot)."""
    return RunOutcome(
        variant="base", task_id=task_id, mode="statya", run=run_idx,
        status=status, verdict_parse_ok=parse_ok, score=score,
        tokens=tokens, tokens_in=tokens_in, tokens_out=tokens_out,
        job_wall_s=wall_s,
    )


def _fake_run_one(script: dict):
    """``probe_mod.run_one`` подмена: (task_id, run_idx) → параметры прогона."""
    def fake(mode_path, task, variant, run_idx, llm, *,
             base_registry=None, seed_reader=None) -> RunOutcome:
        spec = script.get((task["id"], run_idx), {})
        return _outcome(task["id"], run_idx, **spec)
    return fake


def _probe(tmp_path, monkeypatch, script, *, model_class="fast", mode=MODE):
    """run_probe на фейковом измерителе (сеть/LLM нет)."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(script))
    return run_probe(
        mode=mode, golden=golden, heldout=heldout, model_class=model_class,
        registry=Registry(REGISTRY_DIR), llm=object(), runs=3,
    )


# ── 2b: ceiling-флаг замера ───────────────────────────────────────────────


def test_ceiling_flag_on_flat_perfect_scores(tmp_path, monkeypatch) -> None:
    """Все скоры 1.0 (медиана >= 0.999, disp = 0) → флаг ceiling."""
    script = {
        (t["id"], r): {"score": 1.0}
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script)
    assert report.golden_median_score == 1.0
    assert report.golden_dispersion == 0.0
    assert report.flags == ("ceiling",)  # ровно один флаг, никаких других


def test_ceiling_not_set_just_below_threshold(tmp_path, monkeypatch) -> None:
    """0.998 < 0.999 при нулевом разбросе — порог не «примерно», флага нет."""
    script = {
        (t["id"], r): {"score": 0.998}
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script)
    assert report.golden_median_score == 0.998
    assert report.golden_dispersion == 0.0
    assert report.flags == ()


def test_ceiling_requires_zero_dispersion(tmp_path, monkeypatch) -> None:
    """Медиана 1.0, но разброс 0.002 ≠ 0 → конфигурации различаются, флага нет."""
    script = {
        (t["id"], r): {"score": 1.0 if r < 3 else 0.998}
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script)
    assert report.golden_median_score == 1.0
    assert report.golden_dispersion == pytest.approx(1.0 - 0.998)
    assert report.golden_dispersion > 0.0  # ненулевой разброс — не ceiling
    assert "ceiling" not in report.flags


def test_draft_profile_carries_flags_additive(tmp_path, monkeypatch) -> None:
    """Флаги замера → evidence.flags (additive-ключ, Г6); схема чистая."""
    from ai_workspace.calibration import policy
    script = {
        (t["id"], r): {"score": 1.0}
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script)
    assert report.flags == ("ceiling",)
    proposal = policy.propose_scalars(report, quality_floor=0.8)
    assert proposal["applied"] is True  # D6 проходит — гейт переносится на approve
    doc = profiles_mod.build_draft_profile(
        report, model_class="fast", scalars=proposal["scalars"],
        quality_floor=0.8, constraints=proposal["constraints"],
    )
    assert doc["evidence"]["flags"] == ["ceiling"]
    assert profiles_mod.validate_profile(doc) == []


# ── 2b: approve-гейт на ceiling ────────────────────────────────────────────


def _ceiling_profile_doc() -> dict:
    doc = _profile_doc()
    doc["evidence"] = {**doc["evidence"], "flags": ["ceiling"]}
    return doc


def test_approve_refuses_ceiling_without_explicit_decision(
    tmp_path, capsys,
) -> None:
    """Ceiling-профиль: отказ exit 2 (dry-run И --confirm); носители и аудит
    не тронуты — применение неразличающего замера не легитимизируется."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _ceiling_profile_doc())
    before_p, before_r = ppath.read_bytes(), reg_path.read_bytes()
    for extra in ([], ["--confirm"]):
        code = profile_approve.main(
            _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", *extra),
            now=FIXED_NOW,
        )
        assert code == 2
        assert "ceiling" in capsys.readouterr().err
    assert ppath.read_bytes() == before_p
    assert reg_path.read_bytes() == before_r
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()


def test_approve_ceiling_ok_without_reason_refused(tmp_path, capsys) -> None:
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _ceiling_profile_doc())
    before = ppath.read_bytes()
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              "--ceiling-ok", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 2
    assert "--reason" in capsys.readouterr().err
    assert ppath.read_bytes() == before
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()


def test_approve_ceiling_ok_with_reason_applies_and_audits(
    tmp_path, capsys,
) -> None:
    """--ceiling-ok --reason: dry-run — план без аудита; --confirm — аудит
    решения (до носителей) + оба носителя применены."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _ceiling_profile_doc())
    common = ("--ceiling-ok", "--reason",
              "ceiling-сет осознан: ручная проверка различимости проведена")

    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab", *common),
        now=FIXED_NOW,
    )
    assert code == 0
    assert "ceiling" in capsys.readouterr().out
    assert not (profiles_dir / profile_approve.AUDIT_FILENAME).exists()

    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              *common, "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 0
    assert _load(ppath)["status"] == "calibrated"
    assert _load(ppath)["version"] == 2
    assert _load(reg_path)["model_classes"]["fast"][
        "calibration_status"] == "calibrated"
    lines = [
        json.loads(line)
        for line in (profiles_dir / profile_approve.AUDIT_FILENAME)
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines == [{
        "ts": FIXED_STAMP, "action": "ceiling_approve",
        "profile_id": "cal-fast-qwen25-7b-12ab", "model_class": "fast",
        "reason": "ceiling-сет осознан: ручная проверка различимости проведена",
    }]


def test_approve_stale_not_blocked_by_ceiling(tmp_path, capsys) -> None:
    """--stale — понижение, не применение: ceiling не блокирует."""
    profiles_dir, reg_path = _carriers(tmp_path)
    ppath = _place_profile(profiles_dir, _ceiling_profile_doc())
    _promote(ppath, "calibrated", 2)
    code = profile_approve.main(
        _argv(profiles_dir, reg_path, "cal-fast-qwen25-7b-12ab",
              "--stale", "--confirm"),
        now=FIXED_NOW,
    )
    assert code == 0
    assert _load(ppath)["status"] == "stale"


# ── 2c: in/out-токены и точный ₽ ──────────────────────────────────────────


class _UsageLLM:
    """Скриптованный LLM с журналом в форме ``OllamaClient.calls``:
    каждая запись несёт usage «ответа» (контракт ollama
    /v1/chat/completions — зонд 2026-10-07, test_vp_ab_pilot)."""

    def __init__(self, usage: dict[str, int]) -> None:
        self._usage = dict(usage)
        self.calls: list[dict] = []

    def complete(self, *, role, model_class, prompt, inputs, params=None,
                 job_id=None) -> LLMResult:
        self.calls.append({
            "role": role, "wall_s": 0.001, "prompt_chars": len(prompt),
            "usage": dict(self._usage), "fragment": "…",
        })
        answers = {
            "analyst": "План: 1) структура 2) черновик.",
            "critic": "PASS\nРУБРИКА: полнота 1.0",
            "editor": "# Статья\n\n" + "Раздел с содержанием. src-0123 " * 60,
        }
        return LLMResult(output=answers[role], usage=dict(self._usage))


TASK = {"id": "g01-structure", "zone": "public",
        "prompt": "Составь структуру статьи про MCP-RAG (разделы, порядок)."}


def test_run_one_splits_tokens_in_out_from_usage_journal() -> None:
    llm = _UsageLLM(
        {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    )
    outcome = run_one(MODE, TASK, "contract", 1, llm)
    n_calls = len(llm.calls)
    assert n_calls > 0
    assert outcome.tokens_in == 100 * n_calls
    assert outcome.tokens_out == 40 * n_calls
    assert outcome.tokens > 0  # суммарная оценка движка осталась


def test_run_one_tokens_in_out_count_only_own_calls() -> None:
    """Клиент один на весь probe: второй прогон считает только СВОИ вызовы."""
    llm = _UsageLLM(
        {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    )
    run_one(MODE, TASK, "contract", 1, llm)
    after_first = len(llm.calls)
    second = run_one(MODE, TASK, "contract", 2, llm)
    assert second.tokens_in == 100 * (len(llm.calls) - after_first)
    assert second.tokens_out == 40 * (len(llm.calls) - after_first)


def test_probe_rub_exact_with_in_out_split(tmp_path, monkeypatch) -> None:
    """heavy → ext: ₽ = вход×0.30 + выход×1.20 (за 1M, pricing.yaml) — точная
    оценка, строго больше прежней нижней (все токены входные)."""
    script = {
        (t["id"], r): {
            "score": 0.9, "tokens_in": 1_000_000, "tokens_out": 500_000,
        }
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script, model_class="heavy")

    price = PricingRegistry(Registry(REGISTRY_DIR)).price_for("ext")
    # golden: 6 прогонов × (1M вход / 0.5M выход); heldout в ₽ не входит
    expected = price.cost_micro(6_000_000, 3_000_000) / MICRO_PER_UNIT
    assert report.rub == expected  # целочисленная микро-₽ арифметика — точно
    lower_bound = price.cost_micro(9_000_000, 0) / MICRO_PER_UNIT
    assert report.rub == pytest.approx(lower_bound * 2)  # 1.20/0.30 = 4× на выходе
    assert report.rub > lower_bound  # выход дороже входа — оценка выросла


def test_probe_local_rub_stays_zero_with_in_out_split(
    tmp_path, monkeypatch,
) -> None:
    """Инвариант: local-полка → rub = 0.0 и при in/out-разбивке."""
    script = {
        (t["id"], r): {
            "score": 0.9, "tokens_in": 123_456, "tokens_out": 65_432,
        }
        for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)
    }
    report = _probe(tmp_path, monkeypatch, script, model_class="fast")
    assert report.rub == 0.0


# ── 2d: parse_rate-честность без critic-узла ──────────────────────────────


def test_parse_rate_defined_with_critic_mode(tmp_path, monkeypatch) -> None:
    """statya содержит critic-gate → метрика осмысленна (паритет)."""
    script = {(t["id"], r): {"score": 0.8}
              for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)}
    report = _probe(tmp_path, monkeypatch, script)
    assert report.parse_rate_defined is True


def test_parse_rate_not_defined_without_critic(tmp_path, monkeypatch) -> None:
    """Режим без critic-узла → parse_rate_defined=False (нейтральные True)."""
    mode = tmp_path / "nocritic.yaml"
    mode.write_text(NO_CRITIC_MODE, encoding="utf-8")
    script = {(t["id"], r): {"score": 0.8}
              for t in GOLDEN_TASKS + HELDOUT_TASKS for r in (1, 2, 3)}
    report = _probe(tmp_path, monkeypatch, script, mode=mode)
    assert report.parse_rate_defined is False
    assert report.parse_rate == 1.0  # нейтральные True — но это НЕ метрика


def test_print_report_replaces_parse_rate_with_dash(capsys) -> None:
    """Отчёт CLI: без critic-узла вместо значения печатается «—»."""
    report = SimpleNamespace(
        run_id="probe-abc123def456", model_id="qwen2.5:7b", digest="sha256:abc",
        golden_manifest="g" * 64, pricing_manifest="p" * 64,
        golden_median_score=0.9, heldout_score=0.9, golden_dispersion=0.05,
        parse_rate=1.0, parse_rate_defined=False, rub=0.0, wall_s=1.0,
        n_runs=3, flags=(),
    )
    probe_run._print_report(
        report, 0.8, {"applied": True, "reason": "рамка D6 выполнена"}, None,
    )
    out = capsys.readouterr().out
    assert "parse_rate / rub / wall_s: — (" in out
    assert "нейтральна" in out
    assert "parse_rate / rub / wall_s: 1.00" not in out  # значение не публикуется


# ── 2g: капы D6 из CLI ────────────────────────────────────────────────────


def _fake_report(**over):
    """ProbeReport выше пола public (no-ceiling): рамка D6 применяется."""
    from ai_workspace.calibration.probe import ProbeReport
    base: dict = {
        "run_id": "probe-veracity01", "model_id": "qwen2.5:7b",
        "digest": "sha256:q", "golden_manifest": "g" * 64,
        "pricing_manifest": "p" * 64,
        "golden_median_score": 0.9, "golden_dispersion": 0.0,
        "heldout_score": 0.9, "parse_rate": 1.0, "rub": 5.0,
        "wall_s": 100.0, "n_runs": 3,
    }
    base.update(over)
    return ProbeReport(**base)


def _heldout(tmp_path: Path) -> Path:
    return _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)


def test_cli_rub_cap_violation_blocks_profile(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(probe_mod, "run_probe", lambda **kw: _fake_report(rub=15.0))
    profiles_dir = tmp_path / "profiles"
    code = probe_run.main([
        "--heldout", str(_heldout(tmp_path)), "--class", "fast",
        "--confirm-live", "--profiles-dir", str(profiles_dir),
        "--rub-cap", "12.0",
    ])
    assert code == 1
    assert "превышен rub_cap" in capsys.readouterr().out
    assert not profiles_dir.exists() or list(profiles_dir.iterdir()) == []


def test_cli_wall_cap_violation_blocks_profile(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        probe_mod, "run_probe", lambda **kw: _fake_report(wall_s=700.0),
    )
    profiles_dir = tmp_path / "profiles"
    code = probe_run.main([
        "--heldout", str(_heldout(tmp_path)), "--class", "fast",
        "--confirm-live", "--profiles-dir", str(profiles_dir),
        "--wall-cap", "610.0",
    ])
    assert code == 1
    assert "превышен wall_cap_s" in capsys.readouterr().out


def test_cli_caps_applied_and_echoed_in_constraints(
    tmp_path, monkeypatch,
) -> None:
    """Капы соблюдаются → профиль пишется, рамка (пол+капы) — эхом в constraints."""
    monkeypatch.setattr(probe_mod, "run_probe", lambda **kw: _fake_report())
    profiles_dir = tmp_path / "profiles"
    code = probe_run.main([
        "--heldout", str(_heldout(tmp_path)), "--class", "fast",
        "--confirm-live", "--profiles-dir", str(profiles_dir),
        "--rub-cap", "12.0", "--wall-cap", "610.0",
    ])
    assert code == 0
    written = list(profiles_dir.glob("cal-fast-*.yaml"))
    assert len(written) == 1
    doc = profiles_mod.load_profile(profiles_dir, written[0].stem)
    assert doc["constraints"]["rub_cap"] == 12.0
    assert doc["constraints"]["wall_cap_s"] == 610.0
    assert doc["constraints"]["quality_floor"] == 0.8  # Q_FLOOR public
    assert profiles_mod.validate_profile(doc) == []


def test_cli_plan_shows_caps_dry_run(tmp_path, capsys) -> None:
    """Сухой план показывает заданные капы (оператор видит рамку до прогона)."""
    code = probe_run.main([
        "--heldout", str(_heldout(tmp_path)), "--class", "fast",
        "--rub-cap", "12.0", "--wall-cap", "610.0",
        "--profiles-dir", str(tmp_path / "profiles"),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "12.0" in out and "610.0" in out
    assert "--rub-cap" in out and "--wall-cap" in out


def test_cli_without_caps_keeps_old_behaviour(tmp_path, monkeypatch) -> None:
    """Капы не заданы → applied по одному полу (паритет с поведением до 2g)."""
    monkeypatch.setattr(probe_mod, "run_probe", lambda **kw: _fake_report())
    profiles_dir = tmp_path / "profiles"
    code = probe_run.main([
        "--heldout", str(_heldout(tmp_path)), "--class", "fast",
        "--confirm-live", "--profiles-dir", str(profiles_dir),
    ])
    assert code == 0
    doc = profiles_mod.load_profile(
        profiles_dir, profiles_mod.list_profiles(profiles_dir)[0],
    )
    assert set(doc["constraints"]) == {"quality_floor"}  # капов нет — как раньше
