"""Э1 Ф7 (arch-2026-10-08-f7-calibration): врезка resolver'а в движок — offline.

Невакуумность: прогон РЕАЛЬНОГО движка на фейках (образец — ``test_engine.py``)
по реальным режимам ``ai_workspace/modes/*.yaml`` и реестру ``ai_workspace/registry``
(сегодня все классы uncalibrated → режим Б).

Покрытие (план Э1 §3 п.6 / §4 приёмка A1–A3):
- A1 — паритет F1: без активного профиля read-точки движка ведут себя как до
  врезки. Ролл-колл по 4 режимам × {без реестра, реальный реестр}: эффективный
  скаляр == сегодняшней семантике (``node.get``), классовые retries/
  max_iterations НЕ читаются (sources ∈ {node, default}); REVISE-петля даёт
  то же число вызовов LLM с реестром и без (critic — 1 попытка на вызов);
  форма события on_node_usage в режиме Б — прежний контракт EVENT_KEYS.
- A2 — профиль влияет: ``calibration_profile`` в конструкторе + откалиброванный
  реестр (active_profile) → узел без ``retry`` делает 2 попытки (LLM-стаб
  считает вызовы); usage-агрегат и событие несут ``scalars_sources ==
  "profile"`` (только в режиме П).
- A3 — пин: ``calibration_pin: [retries]`` удерживает узловое ``retry`` при
  активном профиле (P > C): попыток — по узлу, не по профилю.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.orchestrator.context_delta import (
    CONTEXT_DELTA,
    CONTEXT_FULL,
    DELTA_CONSUMER_KINDS,
)
from ai_workspace.orchestrator.engine import (
    LLMResult,
    MemoryLedger,
    ModeEngine,
    load_mode,
)
from ai_workspace.registry import Registry
from ai_workspace.tests.test_engine import (
    EPOCH,
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

MODES_DIR = Path(__file__).resolve().parents[1] / "modes"
REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"
MODE_IDS = ("statya", "metodichka", "issledovanie", "statya-private")

#: Контракт формы события on_node_usage (копия test_node_usage.EVENT_KEYS):
#: в режиме Б врезка НЕ добавляет ключей — паритет формы события (F1).
EVENT_KEYS = {
    "job", "trace_id", "node", "kind", "role", "model_class", "shelf", "cached",
    "prompt_chars", "output_chars", "tokens", "wall_s", "tokens_estimated",
}


class FlakyLLM:
    """Скриптованный LLM: роль из ``falls`` падает N раз перед успехом; считает попытки."""

    def __init__(self, script: dict[str, list[str]], falls: dict[str, int] | None = None) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.falls = dict(falls or {})
        self.calls: list[str] = []

    def complete(self, *, role, model_class, prompt, inputs, params=None,
                 job_id=None) -> LLMResult:
        self.calls.append(role)
        left = self.falls.get(role, 0)
        if left > 0:
            self.falls[role] = left - 1
            raise RuntimeError(f"flaky-сбой шлюза: {role}")
        queue = self.script.get(role)
        if not queue:
            raise AssertionError(f"нет скриптованного ответа для роли {role!r}")
        return LLMResult(output=queue.pop(0))


def _real_registry() -> Registry:
    reg = Registry(REGISTRY_DIR)
    reg.load()  # сегодня все классы uncalibrated → режим Б
    return reg


def _calibrated_registry() -> dict:
    """Dict-реестр с откалиброванным heavy (active_profile; как test_calibration_resolve)."""
    return {"model_classes": {
        "heavy": {"shelf": "ext", "shaping": "full-context", "retries": 5,
                  "max_iterations": 9, "calibration_status": "calibrated",
                  "active_profile": "p-heavy-1"},
        "fast": {"shelf": "local", "shaping": "compressed",
                 "max_chars_per_section": 4000, "retries": 1,
                 "calibration_status": "uncalibrated"},
        "local-only": {"rule": "zone"},
    }}


PROFILE_RETRIES2 = {
    "profile_id": "p-heavy-1",
    "status": "calibrated",
    "scalars": {"retries": 2},
}


def make_cal_engine(*, script=None, llm=None, graph=None, registry=None,
                    calibration_profile=None, events=None) -> ModeEngine:
    """Движок на фейках как ``make_engine`` в test_engine (+ реестр/профиль/события)."""
    jobs = FakeJobs()
    llm = llm or FakeLLM(script or {})
    ledger = MemoryLedger()
    boards = FakeBoards()
    engine = ModeEngine(
        jobs=jobs, boards=boards, graph=graph or load_mode(VALID),
        llm=llm, mcp=FakeMCP(), ledger=ledger, registry=registry,
        calibration_profile=calibration_profile,
        on_node_usage=events.append if events is not None else None,
    )
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=EPOCH)
    engine._test = (jobs, llm, ledger, boards)  # type: ignore[attr-defined]
    return engine


def _today_shaping(engine: ModeEngine, node) -> str:
    """Сегодняшняя семантика shaping: только классовая (до врезки)."""
    return engine.shaping_for(str(node.get("model_class", "fast")))[0]


def _today_context_mode(node) -> str:
    """Сегодняшняя семантика context-mode: kind-gate + node.get("context")."""
    if node.kind not in DELTA_CONSUMER_KINDS:
        return CONTEXT_FULL
    return CONTEXT_DELTA if node.get("context") == CONTEXT_DELTA else CONTEXT_FULL


# ------------------------------------------------------ A1: паритет (режим Б)

@pytest.mark.parametrize("mode_id", MODE_IDS)
@pytest.mark.parametrize("with_registry", [False, True], ids=["no-registry", "real-registry"])
def test_a1_rollcall_mode_b_matches_today_semantics(mode_id: str, with_registry: bool) -> None:
    """Ролл-колл по 4 режимам: эффективный скаляр == сегодняшней семантике
    по всем узлам/параметрам; класс НЕ читается (sources ∈ {node, default})."""
    engine = make_cal_engine(
        graph=load_mode(MODES_DIR / f"{mode_id}.yaml"),
        registry=_real_registry() if with_registry else None,
    )
    engine._cal_scalars = engine._resolve_calibration()
    assert engine._cal_scalars, "режим Б всё равно резолвит все узлы (без профиля)"
    for node in engine.graph.nodes.values():
        today = {
            "retries": int(node.get("retry", 0)),
            "max_iterations": int(node.get("max_iterations", 1)),
            "shaping": _today_shaping(engine, node),
            "context_mode": _today_context_mode(node),
        }
        scalars = engine._cal_scalars[node.id]
        assert scalars.profile_id is None  # режим Б: гейт П не прошёл
        for param, declared in today.items():
            assert engine._scalar(node, param, declared) == declared, (node.id, param)
            assert scalars.sources[param] in ("node", "default"), (node.id, param)


def test_a1_revise_loop_same_calls_with_and_without_registry() -> None:
    """REVISE-петля на statya: с (uncalibrated) реестром и без — одинаковые
    вызовы LLM, секции, статус; critic — 1 попытка на каждый вызов."""
    script = {"analyst": ["черновик v1", "черновик v2"],
              "critic": ["REVISE — мало фактов", "PASS"],
              "editor": ["doc"]}
    results = []
    for registry in (None, _real_registry()):
        engine = make_cal_engine(script=script, registry=registry)
        res = engine.run("j1", epoch=EPOCH)
        _, llm, _, boards = engine._test  # type: ignore[attr-defined]
        results.append((res, list(llm.calls), dict(boards.sections)))
    (res_a, calls_a, sections_a), (res_b, calls_b, sections_b) = results
    assert res_a.status == res_b.status == "paused"
    assert res_a.node == res_b.node == "publish"
    assert calls_a == calls_b == ["analyst", "critic", "analyst", "critic", "editor"]
    assert calls_a.count("critic") == 2  # REVISE и PASS — без ретраев (у узла нет retry)
    assert sections_a == sections_b


def test_a1_mode_b_event_keeps_contract_shape() -> None:
    """Режим Б: событие on_node_usage — ровно EVENT_KEYS (без ключей калибровки);
    агрегат usage:{node} — с пустыми scalars_sources/scalars_stale."""
    events: list[dict] = []
    engine = make_cal_engine(
        script={"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]},
        registry=_real_registry(), events=events,
    )
    res = engine.run("j1", epoch=EPOCH)
    assert res.status == "paused" and events
    for event in events:
        assert set(event) == EVENT_KEYS
    _, _, ledger, _ = engine._test  # type: ignore[attr-defined]
    agg = ledger.get("j1", "usage:critic")
    assert agg is not None
    assert agg["scalars_sources"] == {} and agg["scalars_stale"] == ()


def test_a1_broken_registry_falls_back_to_mode_b() -> None:
    """Битый реестр (get кидает) НЕ валит прогон: resolver глушит его внутри
    (fail-closed) → все узлы в режиме Б, исполнение живёт обычным путём."""

    class BombRegistry:
        def get(self, kind):
            raise RuntimeError("registry boom")

    engine = make_cal_engine(
        script={"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]},
        registry=BombRegistry(),
    )
    scalars = engine._resolve_calibration()
    assert scalars and all(s.profile_id is None for s in scalars.values())
    res = engine.run("j1", epoch=EPOCH)
    assert res.status == "paused"


# ------------------------------------------------- A2: профиль влияет (режим П)

def test_a2_profile_retries_drive_attempts_and_report_sources() -> None:
    """Активный профиль (retries=2) + откалиброванный heavy: critic БЕЗ retry
    делает 2 попытки; usage-агрегат и событие несут sources[retries]==profile."""
    script = {"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}
    events: list[dict] = []
    llm = FlakyLLM(script, falls={"critic": 1})  # 1-й вызов критика падает
    engine = make_cal_engine(
        llm=llm, registry=_calibrated_registry(),
        calibration_profile=PROFILE_RETRIES2, events=events,
    )
    res = engine.run("j1", epoch=EPOCH)

    assert res.status == "paused"  # провал swallowed ретраем, дальше обычный путь
    assert llm.calls == ["analyst", "critic", "critic", "editor"]
    assert llm.calls.count("critic") == 2  # падение + успех = 2 попытки из профиля

    _, _, ledger, _ = engine._test  # type: ignore[attr-defined]
    agg = ledger.get("j1", "usage:critic")
    assert agg is not None and agg["calls"] == 1  # узел посещён 1 раз (usage после успеха)
    assert agg["scalars_sources"]["retries"] == "profile"
    assert agg["scalars_sources"]["max_iterations"] == "node"  # у critic max_iterations:3 (N > C? нет: C>N, но в профиле НЕТ max_iterations → N)
    assert agg["scalars_stale"] == ()

    critic_events = [e for e in events if e["node"] == "critic"]
    assert len(critic_events) == 1
    assert critic_events[0]["scalars_sources"]["retries"] == "profile"
    assert critic_events[0]["scalars_stale"] == ()


def test_a2_mode_b_same_setup_makes_single_attempt() -> None:
    """Контроль: тот же откалиброванный реестр БЕЗ профиля — режим Б, critic
    без retry делает 1 попытку → шлюзовый сбой валит узел (сегодняшнее поведение)."""
    llm = FlakyLLM({"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]},
                   falls={"critic": 1})
    engine = make_cal_engine(llm=llm, registry=_calibrated_registry())
    res = engine.run("j1", epoch=EPOCH)
    assert res.status == "failed"
    assert engine.llm.calls == ["analyst", "critic"]  # 1 попытка, ретраев нет


# ------------------------------------------------------ A3: пин (P > C)

def _pin_mode(tmp_path: Path, *, pin: bool):
    """statya с analyst retry:0 (+/- calibration_pin:[retries]) — изоляция пина."""
    doc = yaml.safe_load(VALID.read_text(encoding="utf-8"))
    doc["id"] = "statya-pin" if pin else "statya-nopin"
    for node in doc["nodes"]:
        if node["id"] == "analyst":
            node["retry"] = 0
            if pin:
                node["calibration_pin"] = ["retries"]
    path = tmp_path / f"{doc['id']}.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return load_mode(path)


def test_a3_pin_keeps_node_retries_under_profile(tmp_path: Path) -> None:
    """Пин удерживает узловое retry:0 при профиле retries=2 → 1 попытка;
    без пина тот же узел берёт профиль → 3 попытки (attempts=retries+1)."""
    # unit-уровень resolver'а: источники и значения
    engine = make_cal_engine(graph=_pin_mode(tmp_path, pin=True),
                             registry=_calibrated_registry(),
                             calibration_profile=PROFILE_RETRIES2)
    engine._cal_scalars = engine._resolve_calibration()
    scalars = engine._cal_scalars["analyst"]
    assert scalars.profile_id is not None  # гейт П прошёл (класс calibrated)
    assert scalars.retries == 0 and scalars.sources["retries"] == "pin"
    assert engine._scalar(engine.graph.node("analyst"), "retries", int(
        engine.graph.node("analyst").get("retry", 0))) == 0

    # уровень движка: analyst всегда падает на шлюзе
    always = FlakyLLM({"critic": ["PASS"]}, falls={"analyst": 99})
    pinned = make_cal_engine(graph=_pin_mode(tmp_path, pin=True), llm=always,
                             registry=_calibrated_registry(),
                             calibration_profile=PROFILE_RETRIES2)
    res_p = pinned.run("j1", epoch=EPOCH)
    assert res_p.status == "failed"
    assert pinned.llm.calls == ["analyst"]  # retry:0 удержан пином → 1 попытка

    free = make_cal_engine(graph=_pin_mode(tmp_path, pin=False),
                           llm=FlakyLLM({"critic": ["PASS"]}, falls={"analyst": 99}),
                           registry=_calibrated_registry(),
                           calibration_profile=PROFILE_RETRIES2)
    res_f = free.run("j1", epoch=EPOCH)
    assert res_f.status == "failed"
    assert free.llm.calls == ["analyst", "analyst", "analyst"]  # профиль 2 → 3 попытки
