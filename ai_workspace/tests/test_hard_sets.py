"""Ф7 (3c, снятие ceiling): трудные golden/heldout-наборы — контракт сетов.

hard-golden.yaml (hg01..hg04) и hard-heldout.yaml (hh01..hh04) — авторские
ТРУДНЫЕ сеты, чьё назначение — гарантированно снять флаг ``ceiling``
(calibration/probe.py: ``golden_median>=0.999 ∧ dispersion==0``), которым
базовые golden-set/heldout-set (1.0/0 на живом прогоне qwen2.5:7b) блокируют
запись профиля. Механика — трудность, не подгонка:
- brevity (по 2 на сет): brief требует 2-3 предложения / лимит слов ≤60 ⇒
  документ < MIN_DOC_CHARS=400 ⇒ length_ok=False ⇒ score ≤ 0.8;
- hard (по 2 на сет): ≥3 именованных разделов + обязательные сущности +
  цитаты src- ⇒ слабая модель вариативно недодаёт секции/цитаты/объём.

Проверки: сеты грузятся ``probe._load_tasks``; по 4 задания; brevity-промпты
содержат требование краткости с числовым лимитом слов ≤60; hard-промпты
требуют ≥3 именованных разделов и цитирование src-; id hh* не пересекаются
с hg* и с базовыми g*/h*; метаданные draft-схемы (owner/min_runs/floor);
математика сноса ceiling — на НАСТОЯЩЕЙ метрике (vp_ab_pilot.score_run).
Живой прогон — НЕ здесь (операторский шаг, probe-run --live).
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

import yaml

from ai_workspace.calibration.probe import _load_tasks
from ai_workspace.tools.vp_ab_pilot import (
    MIN_DOC_CHARS,
    document_checks,
    score_run,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
HARD_GOLDEN = GOLDEN_DIR / "hard-golden.yaml"
HARD_HELDOUT = GOLDEN_DIR / "hard-heldout.yaml"
BASE_GOLDEN = GOLDEN_DIR / "golden-set.yaml"
BASE_HELDOUT = GOLDEN_DIR / "heldout-set.yaml"


def _tasks(path: Path) -> list[dict]:
    return _load_tasks(path)


# ── схема draft-сета: метаданные как у golden-set + status: draft ──────────


def _assert_draft_schema(doc: dict, raw: str) -> None:
    assert doc["version"] == 1
    assert doc["owner"] == "operator"
    assert doc["status"] == "draft"          # до рецензии оператора — без live
    assert doc["min_runs"] == 3              # спека probe-suite: N>=3
    assert doc["floor"] == {"public": 0.80, "private": 0.85}
    # шапка-комментарий про назначение (снятие ceiling) — на месте
    assert "снятие ceiling" in raw
    assert "Автор-ИИ" in raw


def test_hard_golden_schema_and_ids() -> None:
    raw = HARD_GOLDEN.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    _assert_draft_schema(doc, raw)
    tasks = _tasks(HARD_GOLDEN)
    assert [t["id"] for t in tasks] == ["hg01", "hg02", "hg03", "hg04"]
    for task in tasks:
        assert task["zone"] == "public"
        assert task["prompt"].strip()
        assert task["expect_keywords"]
        assert int(task["max_latency_s"]) > 0


def test_hard_heldout_schema_and_ids() -> None:
    raw = HARD_HELDOUT.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    _assert_draft_schema(doc, raw)
    tasks = _tasks(HARD_HELDOUT)
    assert [t["id"] for t in tasks] == ["hh01", "hh02", "hh03", "hh04"]
    for task in tasks:
        assert task["zone"] == "public"
        assert task["prompt"].strip()
        assert task["expect_keywords"]
        assert int(task["max_latency_s"]) > 0


# ── классы трудности: brevity — краткость, hard — разделы и цитаты ─────────


def _brevity(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("kind") == "brevity"]


def _hard(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("kind") == "hard"]


def test_two_brevity_tasks_per_set_with_word_cap() -> None:
    """По 2 brevity-задания на сет; в промпте явный лимит слов ≤60.

    Лимит ≤60 слов держит документ редактора ниже MIN_DOC_CHARS=400 ⇒
    length_ok=False ⇒ score ≤ 0.8 (детерминированный снос ceiling).
    """
    for path in (HARD_GOLDEN, HARD_HELDOUT):
        brevity = _brevity(_tasks(path))
        assert len(brevity) == 2, f"{path.name}: ожидались 2 brevity-задания"
        for task in brevity:
            prompt = task["prompt"]
            assert "предложен" in prompt.lower(), (
                f"{path.name}/{task['id']}: нет требования 2-3 предложений"
            )
            m = re.search(r"не более\s+(\d+)\s+слов", prompt, flags=re.I)
            assert m, f"{path.name}/{task['id']}: нет лимита слов в промпте"
            assert int(m.group(1)) <= 60, (
                f"{path.name}/{task['id']}: лимит {m.group(1)} слов не "
                "гарантирует документ < 400 символов"
            )


def test_two_hard_tasks_per_set_with_sections_and_citations() -> None:
    """По 2 объёмных задания: ≥3 именованных разделов («...») + цитата src-."""
    for path in (HARD_GOLDEN, HARD_HELDOUT):
        hard = _hard(_tasks(path))
        assert len(hard) == 2, f"{path.name}: ожидались 2 hard-задания"
        for task in hard:
            prompt = task["prompt"]
            named = re.findall(r"«[^»]+»", prompt)
            assert len(named) >= 3, (
                f"{path.name}/{task['id']}: именованных разделов < 3 ({named})"
            )
            assert "src-" in prompt, (
                f"{path.name}/{task['id']}: нет требования цитаты src-"
            )


# ── held-out: hh* не пересекаются ни с hg*, ни с базовыми g*/h* ────────────


def test_hard_sets_disjoint_from_each_other_and_base_sets() -> None:
    hg = {t["id"] for t in _tasks(HARD_GOLDEN)}
    hh = {t["id"] for t in _tasks(HARD_HELDOUT)}
    assert not (hg & hh), "hh* не должны пересекаться с hg* (held-out)"
    base_g = {
        t["id"] for t in yaml.safe_load(BASE_GOLDEN.read_text(encoding="utf-8"))["tasks"]
    }
    base_h = {
        t["id"] for t in yaml.safe_load(BASE_HELDOUT.read_text(encoding="utf-8"))["tasks"]
    }
    assert not (hg & base_g) and not (hg & base_h)
    assert not (hh & base_g) and not (hh & base_h)


def test_base_sets_untouched_schema_still_valid() -> None:
    """Инвариант: базовые сеты не изменились и валидны (5+5 заданий)."""
    assert len(_tasks(BASE_GOLDEN)) == 5
    assert len(_tasks(BASE_HELDOUT)) == 5


# ── математика сноса ceiling — на настоящей метрике vp_ab_pilot ────────────


def test_brevity_design_breaks_ceiling_condition() -> None:
    """Короткий документ ⇒ length_ok=False ⇒ score 0.8 ⇒ ceiling не ставится.

    Воспроизводит условие флага (probe.py) на реальных ``document_checks`` /
    ``score_run``: 300-символьный документ (итог brevity-brief'а) при
    остальных критериях в норме даёт 0.8; 12 скоров (4 задания × 3 прогона,
    brevity=0.8) — медиана 0.9 / dispersion 0.2 ⇒
    ``not (median >= 0.999 and disp == 0.0)`` — флаг снят.
    """
    checks = document_checks(
        {"sections": ["document", "document+refs"]},
        {"document": "к" * 300, "document+refs": "цитата src-abc123"},
    )
    assert checks.doc_chars < MIN_DOC_CHARS
    assert checks.sections_ok and checks.citation_ok
    assert not checks.length_ok
    brevity_score = score_run(verdict_parse_ok=True, checks=checks)
    assert brevity_score == 0.8

    scores = [brevity_score] * 6 + [1.0] * 6   # 2 brevity × 3 прогона + hard
    median = statistics.median(scores)
    dispersion = max(scores) - min(scores)
    assert not (median >= 0.999 and dispersion == 0.0)
