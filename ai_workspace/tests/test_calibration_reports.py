"""В3-A «Lifecycle» Ф7 (arch-2026-10-08-f7-calibration, шаги 3a/3b).

Покрытие:
- 3a: ``write_report`` создаёт ``probe-<run_id>.json`` по канону
  ``report_filename`` (dataclasses.asdict; q_report → null);
- 3a CV7-existing (F-5): ``validate_variants(reports_dir=…)``,
  fail-closed «отчёт не существует» при отсутствии файла, PASS при наличии;
  ``record_decision`` с ``reports_dir`` не пишет реестр при провале;
  паритет: без ``reports_dir`` — только наличие ключей (прежнее поведение);
- 3b (F-7, приватность I5 — риск 8): partial-снапшот пишется по мере
  прогона и содержит ТОЛЬКО разрешённые ключи — тексты
  document/draft/critic_fragment/verdict/detail в файл НЕ попадают;
- 3b resume: ``--resume``/``run_probe(resume=True)`` дочитывает partial и
  НЕ перезапускает завершённые сегменты; битый partial → ValueError;
- git-политика: ``ai_workspace/calibration/reports/`` покрыт .gitignore
  (git check-ignore);
- CLI: ``--reports-dir`` пишет отчёт даже при непройденной рамке D6 (exit 1),
  ``--resume`` без ``--reports-dir`` — ошибка CLI (exit 2).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import probe as probe_mod
from ai_workspace.calibration.probe import (
    PARTIAL_SEGMENT_KEYS,
    PARTIAL_TEXT_KEYS,
    ProbeReport,
    load_partial,
    report_filename,
    write_report,
)
from ai_workspace.calibration.variants import (
    record_decision,
    validate_variants,
)
from ai_workspace.tools import probe_run
from ai_workspace.tools.vp_ab_pilot import RunOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]

GOLDEN_TASKS = [
    {"id": "g01-cite", "zone": "public", "prompt": "Собери статью с цитатами."},
    {"id": "g02-plan", "zone": "public", "prompt": "Собери план статьи."},
]
HELDOUT_TASKS = [
    {"id": "h01-cite", "zone": "public", "prompt": "Held-out: цитаты."},
]
NEEDLE_TASKS = [
    {"id": "n01-needle", "zone": "public", "prompt": "Найди иглу.",
     "expect_needle": "NEEDLE-MARK-42"},
]

# «Секретные» тексты прогона — если попадут в снапшот, тест это поймает
SECRET_DOC = "SECRET-DOC квадрат полного текста документа"
SECRET_DRAFT = "SECRET-DRAFT черновик аналитика"
SECRET_CRITIC = "SECRET-CRITIC фрагмент критика"
SECRET_DETAIL = "SECRET-DETAIL текст ошибки"


def _write_set(path: Path, tasks: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"version": 1, "tasks": tasks}, allow_unicode=True,
                       sort_keys=False),
        encoding="utf-8",
    )
    return path


def _outcome(
    task_id: str, run_idx: int, *, score: float = 0.5, parse_ok: bool = True,
    tokens: int = 10, tokens_in: int = 4, tokens_out: int = 6,
    wall_s: float = 0.1, status: str = "done",
) -> RunOutcome:
    """RunOutcome с ТЕКСТАМИ: снапшот обязан их выбросить (I5)."""
    from ai_workspace.tools.vp_ab_pilot import DocumentChecks

    return RunOutcome(
        variant="base", task_id=task_id, mode="statya", run=run_idx,
        status=status, detail=SECRET_DETAIL if status == "error" else "",
        verdict_parse_ok=parse_ok, score=score,
        document=SECRET_DOC, draft=SECRET_DRAFT,
        critic_fragment=SECRET_CRITIC,
        tokens=tokens, tokens_in=tokens_in, tokens_out=tokens_out,
        job_wall_s=wall_s,
        checks=DocumentChecks(
            sections_ok=True, citation_ok=True, length_ok=True, doc_chars=42,
        ),
    )


def _fake_run_one(script: dict | None = None, calls: list | None = None):
    """``probe_mod.run_one`` подмена: (task_id, run_idx) → спецификация."""
    def fake(mode_path, task, variant, run_idx, llm, *,
             base_registry=None, seed_reader=None) -> RunOutcome:
        if calls is not None:
            calls.append((str(task["id"]), int(run_idx)))
        spec = (script or {}).get((str(task["id"]), int(run_idx)), {})
        # needle-задачи «находят» иглу в document — если не сказано иное
        if "document" not in spec and task.get("expect_needle"):
            merged = dict(spec)
            merged["document"] = f"текст с {task['expect_needle']} внутри"
            spec = merged
        default = {"score": 0.5}
        kwargs = {**default, **spec}
        if "document" in kwargs and kwargs["document"] != SECRET_DOC:
            # подмена document для needle — собираем вручную
            doc = kwargs.pop("document")
            out = _outcome(str(task["id"]), int(run_idx), **kwargs)
            out.document = doc
            return out
        return _outcome(str(task["id"]), int(run_idx), **kwargs)
    return fake


def _run(tmp_path: Path, monkeypatch, *, script: dict | None = None,
         calls: list | None = None, needle: Path | None = None,
         reports_dir: Path | str | None = None, resume: bool = False,
         runs: int = 2) -> ProbeReport:
    """Живой ``run_probe`` с подменённым измерителем (реальный контракт)."""
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one(script, calls))
    return probe_mod.run_probe(
        mode=REPO_ROOT / "ai_workspace" / "modes" / "statya.yaml",
        golden=golden, heldout=heldout, needle=needle,
        model_class="fast", registry={}, llm=object(),
        runs=runs, zone="public",
        reports_dir=reports_dir, resume=resume,
    )


def _report(run_id: str = "probe-testrun0001") -> ProbeReport:
    return ProbeReport(
        run_id=run_id, model_id="qwen2.5:7b", digest="sha256:abc",
        golden_manifest="g" * 64, pricing_manifest="p" * 64,
        golden_median_score=0.75, golden_dispersion=0.1, heldout_score=0.7,
        parse_rate=1.0, rub=0.0, wall_s=12.5, n_runs=3,
    )


# ── 3a: write_report ────────────────────────────────────────────────────────


def test_write_report_creates_file_by_run_id(tmp_path: Path) -> None:
    """Отчёт лежит в reports_dir/probe-<run_id>.json; asdict; q_report → null."""
    report = _report()
    path = write_report(report, tmp_path)
    assert path == tmp_path / report_filename(report.run_id)
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["run_id"] == report.run_id
    assert doc["golden_median_score"] == 0.75
    assert doc["n_runs"] == 3
    assert doc["flags"] == []            # tuple → список
    assert doc["q_report"] is None       # рантайм-агрегат не сериализуется
    assert doc["needle_rate"] is None


def test_report_filename_canon_no_double_prefix() -> None:
    """Канон имени: run_id уже с префиксом probe- → без дубля; пустой — отказ."""
    assert report_filename("probe-abc") == "probe-abc.json"
    assert report_filename("abc") == "probe-abc.json"
    with pytest.raises(ValueError, match="run_id"):
        report_filename("  ")


# ── 3a: CV7-existing (F-5) ─────────────────────────────────────────────────


def _modes_tree(tmp_path: Path) -> Path:
    modes = tmp_path / "modes"
    modes.mkdir(exist_ok=True)
    for name in ("statya", "statya.deep"):
        (modes / f"{name}.yaml").write_text(f"id: {name}\n", encoding="utf-8")
    return modes


def _variants_doc(pair: dict | None) -> dict:
    entry: dict = {
        "variant": "statya.deep", "variant_of": "statya",
        "status": "promoted", "decided_by": "operator",
        "decided_at": "2026-10-08T21:00:00Z",
    }
    if pair is not None:
        entry["probe_pair"] = pair
    return {"schema": "calibration-variants/1", "entries": [entry]}


def test_cv7_fail_closed_when_report_missing(tmp_path: Path) -> None:
    """run_id пары не резолвится в файл → CV7 «отчёт не существует»."""
    modes = _modes_tree(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    doc = _variants_doc({"base": "probe-base0001", "variant": "probe-deep0001"})
    findings = validate_variants(doc, modes, reports_dir=reports)
    assert [f.code for f in findings] == ["CV7", "CV7"]
    assert all("не существует" in f.message for f in findings)


def test_cv7_pass_when_reports_exist(tmp_path: Path) -> None:
    """Оба файла отчётов на месте → CV7 молчит (findings пусты)."""
    modes = _modes_tree(tmp_path)
    reports = tmp_path / "reports"
    write_report(_report("probe-base0001"), reports)
    write_report(_report("probe-deep0001"), reports)
    doc = _variants_doc({"base": "probe-base0001", "variant": "probe-deep0001"})
    assert validate_variants(doc, modes, reports_dir=reports) == []


def test_cv7_parity_without_reports_dir(tmp_path: Path) -> None:
    """Паритет F1: без reports_dir — только наличие ключей (прежнее поведение)."""
    modes = _modes_tree(tmp_path)
    doc = _variants_doc({"base": "любой-id", "variant": "другой-id"})
    assert validate_variants(doc, modes) == []
    assert validate_variants(doc, modes, reports_dir=None) == []


def test_record_decision_fail_closed_without_reports(tmp_path: Path) -> None:
    """record_decision(reports_dir=…): отчёта нет → ValueError, файл НЕ записан."""
    modes = _modes_tree(tmp_path)
    assert modes.is_dir()
    variants_path = tmp_path / "calibration" / "variants.yaml"
    with pytest.raises(ValueError, match="не существует"):
        record_decision(
            variants_path, "statya.deep", "promoted",
            {"base": "probe-base0001", "variant": "probe-deep0001"},
            "operator", "2026-10-08T21:00:00Z",
            reports_dir=tmp_path / "calibration" / "reports",
        )
    assert not variants_path.exists()


def test_record_decision_writes_when_reports_exist(tmp_path: Path) -> None:
    """Отчёты записаны → decision проходит CV7-existing и пишется на носитель."""
    _modes_tree(tmp_path)
    reports = tmp_path / "calibration" / "reports"
    write_report(_report("probe-base0001"), reports)
    write_report(_report("probe-deep0001"), reports)
    variants_path = tmp_path / "calibration" / "variants.yaml"
    record_decision(
        variants_path, "statya.deep", "promoted",
        {"base": "probe-base0001", "variant": "probe-deep0001"},
        "operator", "2026-10-08T21:00:00Z", reports_dir=reports,
    )
    doc = yaml.safe_load(variants_path.read_text(encoding="utf-8"))
    assert doc["entries"][0]["status"] == "promoted"


# ── 3b: partial-снапшот — приватность I5 (строгий состав) ──────────────────


def _walk_keys(node, acc: set) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            acc.add(k)
            _walk_keys(v, acc)
    elif isinstance(node, list):
        for item in node:
            _walk_keys(item, acc)


def test_partial_contains_only_allowed_keys(tmp_path: Path, monkeypatch) -> None:
    """Снапшот: ТОЛЬКО метрики/скаляры — тексты прогона НЕ попадают (I5)."""
    reports = tmp_path / "reports"
    needle = _write_set(tmp_path / "needle.yaml", NEEDLE_TASKS)
    report = _run(
        tmp_path, monkeypatch, needle=needle, reports_dir=reports, runs=2,
    )
    partial = reports / f"{report.run_id}.partial.json"
    assert partial.is_file()
    raw = partial.read_text(encoding="utf-8")
    doc = json.loads(raw)

    # негативный тест на приватность: секретные тексты отсутствуют и как
    # ключи, и как значения
    for secret in (SECRET_DOC, SECRET_DRAFT, SECRET_CRITIC, SECRET_DETAIL):
        assert secret not in raw, f"утечка текста в partial: {secret}"
    keys: set = set()
    _walk_keys(doc, keys)
    assert keys & PARTIAL_TEXT_KEYS == set()
    allowed = {"run_id", "segments", *PARTIAL_SEGMENT_KEYS, "verdict_parse_ok",
               "sections_ok", "citation_ok", "length_ok"}
    assert keys <= allowed, keys - allowed

    # состав: все сегменты (golden 2×2 + heldout 1×2 + needle 1×2)
    assert len(doc["segments"]) == 8
    assert doc["run_id"] == report.run_id
    seg = doc["segments"][0]
    assert set(seg) == set(PARTIAL_SEGMENT_KEYS)
    assert set(seg["checks"]) == {"verdict_parse_ok", "sections_ok",
                                  "citation_ok", "length_ok"}
    assert all(seg["checks"].values())
    # needle-сегмент нашёл иглу (булево, без текста haystack)
    needle_segs = [s for s in doc["segments"] if s["task_id"].startswith("n01")]
    assert len(needle_segs) == 2
    assert all(s["needle_found"] is True for s in needle_segs)
    # сегменты без expect_needle — needle_found None (не измерялся)
    plain = [s for s in doc["segments"] if s["task_id"].startswith("g0")]
    assert all(s["needle_found"] is None for s in plain)


def test_partial_written_incrementally(tmp_path: Path, monkeypatch) -> None:
    """Partial пишется ПО МЕРЕ прогона: после каждого сегмента файл актуален."""
    reports = tmp_path / "reports"
    seen_counts: list[int] = []
    real_write = probe_mod._write_partial

    def counting_write(path, run_id, segments):
        seen_counts.append(len(segments))
        real_write(path, run_id, segments)

    monkeypatch.setattr(probe_mod, "_write_partial", counting_write)
    golden = _write_set(tmp_path / "golden.yaml", GOLDEN_TASKS)
    heldout = _write_set(tmp_path / "heldout.yaml", HELDOUT_TASKS)
    monkeypatch.setattr(probe_mod, "run_one", _fake_run_one())
    probe_mod.run_probe(
        mode=REPO_ROOT / "ai_workspace" / "modes" / "statya.yaml",
        golden=golden, heldout=heldout, model_class="fast",
        registry={}, llm=object(), runs=2, zone="public", reports_dir=reports,
    )
    # 6 сегментов (2×2 golden + 1×2 heldout), каждый дописывается: 1..6
    assert seen_counts == [1, 2, 3, 4, 5, 6]


# ── 3b: resume ──────────────────────────────────────────────────────────────


def test_resume_skips_completed_segments(tmp_path: Path, monkeypatch) -> None:
    """resume: сегменты из partial НЕ перезапускаются, скаляры берутся из него."""
    reports = tmp_path / "reports"
    # прогон 1: все скоры 0.5 → полный partial
    calls1: list = []
    report1 = _run(tmp_path, monkeypatch, script={}, calls=calls1,
                   reports_dir=reports)
    assert len(calls1) == 6
    partial = reports / f"{report1.run_id}.partial.json"
    assert partial.is_file()

    # «крушение»: оставляем в partial только сегменты задачи g01
    doc = json.loads(partial.read_text(encoding="utf-8"))
    doc["segments"] = [s for s in doc["segments"] if s["task_id"] == "g01-cite"]
    partial.write_text(json.dumps(doc), encoding="utf-8")

    # прогон 2 (resume): НОВЫЙ скрипт даёт скор 0.9 — но g01 берётся из
    # partial (0.5), перезапускаются только g02/h01
    calls2: list = []
    report2 = _run(tmp_path, monkeypatch, script={
        ("g02-plan", 1): {"score": 0.9},
        ("g02-plan", 2): {"score": 0.9},
        ("h01-cite", 1): {"score": 0.9},
        ("h01-cite", 2): {"score": 0.9},
    }, calls=calls2, reports_dir=reports, resume=True)
    assert sorted(calls2) == [("g02-plan", 1), ("g02-plan", 2),
                              ("h01-cite", 1), ("h01-cite", 2)]
    # медиана golden: {0.5, 0.5, 0.9, 0.9} → 0.7; heldout: {0.9, 0.9} → 0.9
    assert report2.golden_median_score == pytest.approx(0.7)
    assert report2.heldout_score == pytest.approx(0.9)
    # run_id детерминирован: тот же прогон (манифесты/режим/N не менялись)
    assert report2.run_id == report1.run_id
    # partial снова полный — 6 сегментов, g01-сегменты не задвоены
    doc2 = json.loads(partial.read_text(encoding="utf-8"))
    assert len(doc2["segments"]) == 6
    assert len({(s["task_id"], s["run"]) for s in doc2["segments"]}) == 6


def test_load_partial_fail_closed_on_corrupt(tmp_path: Path) -> None:
    """Битый partial → ValueError (тихий рестарт дорогих прогонов запрещён)."""
    bad = tmp_path / "probe-x.partial.json"
    bad.write_text("{не json", encoding="utf-8")
    with pytest.raises(ValueError, match="partial"):
        load_partial(tmp_path, "probe-x")
    bad.write_text(json.dumps({"segments": "не список"}), encoding="utf-8")
    with pytest.raises(ValueError, match="segments"):
        load_partial(tmp_path, "probe-x")
    # файла нет → {} (resume «если есть»)
    assert load_partial(tmp_path, "probe-absent") == {}


def test_resume_without_partial_runs_full(tmp_path: Path, monkeypatch) -> None:
    """resume без partial = обычный полный прогон («дочитать, если есть»)."""
    calls: list = []
    _run(tmp_path, monkeypatch, script={}, calls=calls,
         reports_dir=tmp_path / "reports", resume=True)
    assert len(calls) == 6


def test_run_probe_parity_without_reports_dir(tmp_path: Path, monkeypatch) -> None:
    """Паритет F1: без reports_dir на носитель ничего не пишется."""
    calls: list = []
    _run(tmp_path, monkeypatch, script={}, calls=calls)
    assert len(calls) == 6
    assert not (tmp_path / "reports").exists()
    assert list(tmp_path.glob("*.partial.json")) == []


# ── git-политика каталога отчётов ───────────────────────────────────────────


def test_gitignore_covers_reports_dir() -> None:
    """``ai_workspace/calibration/reports/`` покрыт .gitignore (check-ignore)."""
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "ai_workspace/calibration/reports/probe-x.json"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
            check=False,  # returncode и есть предмет проверки (0 = игнорируется)
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("git недоступен — проверка .gitignore вручную")
    assert proc.returncode == 0, (
        "файл отчёта НЕ игнорируется: "
        + (proc.stdout + proc.stderr).strip()
    )


# ── CLI: --reports-dir / --resume ──────────────────────────────────────────


def _cli_run(tmp_path: Path, monkeypatch, argv_extra: list[str]) -> int:
    heldout = _write_set(tmp_path / "heldout-cli.yaml", HELDOUT_TASKS)
    golden = _write_set(tmp_path / "golden-cli.yaml", GOLDEN_TASKS)
    monkeypatch.setattr(
        probe_mod, "run_one",
        _fake_run_one({(t["id"], r): {"score": 0.5}
                       for t in GOLDEN_TASKS + HELDOUT_TASKS
                       for r in (1, 2, 3)}),
    )
    return probe_run.main(
        ["--golden", str(golden), "--heldout", str(heldout),
         "--class", "fast", "--runs", "3", "--confirm-live",
         "--profiles-dir", str(tmp_path / "profiles"),
         *argv_extra],
    )


def test_cli_reports_dir_persists_report_even_when_d6_fails(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """--reports-dir: отчёт пишется ДО рамки D6 (exit 1 — а файл есть)."""
    reports = tmp_path / "reports"
    code = _cli_run(tmp_path, monkeypatch, ["--reports-dir", str(reports)])
    assert code == 1  # скор 0.5 ниже пола → D6 не применена
    files = sorted(
        p.name for p in reports.glob("probe-*.json")
        if not p.name.endswith(".partial.json")
    )
    assert len(files) == 1 and files[0].startswith("probe-")
    doc = json.loads((reports / files[0]).read_text(encoding="utf-8"))
    assert doc["golden_median_score"] == pytest.approx(0.5)
    out = capsys.readouterr().out
    assert "отчёт probe (В3-A)" in out
    # partial тоже записан и лежит рядом
    assert len(list(reports.glob("*.partial.json"))) == 1


def test_cli_resume_requires_reports_dir(
    tmp_path: Path, capsys,
) -> None:
    """--resume без --reports-dir — ошибка CLI (SystemExit 2, capsys)."""
    heldout = _write_set(tmp_path / "heldout-r.yaml", HELDOUT_TASKS)
    with pytest.raises(SystemExit) as exc:
        probe_run.main(
            ["--golden", str(_write_set(tmp_path / "g-r.yaml", GOLDEN_TASKS)),
             "--heldout", str(heldout), "--class", "fast", "--resume"],
        )
    assert exc.value.code == 2
    assert "--resume требует --reports-dir" in capsys.readouterr().err


def test_cli_parity_no_writes_without_reports_dir(
    tmp_path: Path, monkeypatch,
) -> None:
    """Без --reports-dir CLI ничего на носитель не пишет (паритет F1)."""
    code = _cli_run(tmp_path, monkeypatch, [])
    assert code == 1
    assert not (tmp_path / "reports").exists()
    assert list(tmp_path.glob("*.json")) == []
