"""В1a Ф7 (arch-2026-10-08-f7-calibration): проводка профиля в прод-конструкторы.

- В1a.1 — helper ``active_calibration`` (``calibration/runtime.py``): селектор
  — реестр ``model_classes`` (``active_profile``), факт — провайдер ollama
  ``/api/tags`` (реестр селектор, НЕ факт — самозамыкания нет);
- В1a.2 — три прод-конструктора движка (``golden_run`` / ``vp_ab_pilot`` /
  ``f47_acceptance``) получают ``calibration_profile``/``calibration_model_facts``
  из helper; без профиля — ``None`` (паритет F1);
- В1a.3 — порт сборки боевого движка ``scheduler/wiring.build_mode_engine``:
  инъектирует профиль/провайдер в конструктор для job-раннера (движок в
  job.py получают инъекцией); боевого ctor-call нет — порт покрыт тестом;
- В1a.5 — grep-инвариант: вне тестов каждый ``ModeEngine(`` либо передаёт
  калибровку, либо снабжён комментарием-осознанием ``no-calibration-by-design``
  (боевых точек сборки вне tools сегодня нет — n1; инвариант покрывает
  фактические вызовы).

Невакуумность: реальные YAML-носители (реестр/профили в tmp-каталогах),
реальные функции инструментов; сеть/LLM/Redis — фейки и monkeypatch.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import ai_workspace.tools.f47_acceptance as f47
import ai_workspace.tools.golden_run as golden_run
import ai_workspace.tools.vp_ab_pilot as vp
from ai_workspace.calibration.runtime import DEFAULT_PROFILES_DIR, active_calibration
from ai_workspace.orchestrator.engine import MemoryLedger, load_mode
from ai_workspace.tests.test_engine import (
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

AI_WS = Path(__file__).resolve().parents[1]

#: Комментарий-осознание для grep-инварианта (В1a.5): конструктор движка без
#: калибровки ОБЯЗАН объяснить, почему её нет (спец-сборка/тест-контур).
MARKER = "no-calibration-by-design"

#: Окно строк до вызова, в котором ищем маркер (конструкторы многострочные).
WINDOW = 15

MODEL_ID = "qwen2.5:7b"
DIGEST = "sha256:deadbeef"


def _write_registry(root: Path, classes: dict) -> Path:
    """Записать tmp-реестр model_classes.yaml; вернуть каталог реестра."""
    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "model_classes.yaml").write_text(
        yaml.safe_dump(classes, allow_unicode=True), encoding="utf-8")
    return reg


def _calibrated_classes() -> dict:
    """Реестр с откалиброванным fast (local-полка) + активным профилем p-1."""
    return {
        "fast": {
            "shelf": "local",
            "calibration_status": "calibrated",
            "calibrated_for": {"model_id": MODEL_ID, "digest": DIGEST},
            "active_profile": "p-1",
        },
        "heavy": {"shelf": "ext", "calibration_status": "uncalibrated",
                  "active_profile": None},
    }


def _write_profile(root: Path) -> Path:
    """Записать profiles/p-1.yaml (минимальная схема §5.1); вернуть каталог."""
    profiles = root / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "p-1.yaml").write_text(yaml.safe_dump({
        "schema": "calibration-profile/1",
        "profile_id": "p-1",
        "model_class": "fast",
        "calibrated_for": {"model_id": MODEL_ID, "digest": DIGEST},
        "status": "calibrated",
        "scalars": {"retries": 2},
    }, allow_unicode=True), encoding="utf-8")
    return profiles


def _fake_tags_http(calls: list[str]):
    """http_get → ollama /api/tags с единственным тегом MODEL_ID; считает вызовы."""
    def http_get(url: str) -> dict:
        calls.append(url)
        return {"models": [{"name": MODEL_ID, "digest": DIGEST}]}
    return http_get


# ── В1a.1: helper ──────────────────────────────────────────────────────────

def test_helper_without_active_profile_returns_none_pair(tmp_path: Path) -> None:
    """Нет active_profile (null во всех классах) → (None, None) — паритет F1."""
    reg = _write_registry(tmp_path, {"fast": {"shelf": "local",
                                               "active_profile": None}})
    assert active_calibration(reg, tmp_path / "profiles") == (None, None)


def test_helper_missing_registry_file_returns_none_pair(tmp_path: Path) -> None:
    """Нет файла реестра (и каталога) → (None, None), без исключения."""
    assert active_calibration(
        tmp_path / "nope", tmp_path / "profiles") == (None, None)


def test_helper_with_active_profile_returns_profile_and_provider(
    tmp_path: Path,
) -> None:
    """active_profile → (dict профиля, callable-провайдер); провайдер читает
    факт полки из /api/tags по селектору calibrated_for.model_id."""
    reg = _write_registry(tmp_path, _calibrated_classes())
    profiles = _write_profile(tmp_path)
    calls: list[str] = []

    profile, provider = active_calibration(
        reg, profiles, http_get=_fake_tags_http(calls))

    assert isinstance(profile, dict) and profile["profile_id"] == "p-1"
    assert callable(provider)
    assert calls == [], "селектор читается из реестра БЕЗ сети"
    facts = provider()
    assert calls, "провайдер ходит в /api/tags за фактом (не из реестра)"
    assert facts is not None
    assert facts.model_id == MODEL_ID and facts.digest == DIGEST


def test_helper_provider_caches_tags_fetch(tmp_path: Path) -> None:
    """TTL-кэш провайдера общий для вызовов: два вызова → ОДИН /api/tags."""
    reg = _write_registry(tmp_path, _calibrated_classes())
    profiles = _write_profile(tmp_path)
    calls: list[str] = []

    _, provider = active_calibration(reg, profiles, http_get=_fake_tags_http(calls))

    assert provider() is not None and provider() is not None
    assert len(calls) == 1, "ModelFactsCache живёт в замыкании (Э2-2)"


def test_helper_profile_file_missing_returns_none_pair(tmp_path: Path) -> None:
    """active_profile есть, файла профиля нет → (None, None) (гейт П требует
    profile — один провайдер без смысла)."""
    reg = _write_registry(tmp_path, _calibrated_classes())
    assert active_calibration(
        reg, tmp_path / "profiles", http_get=_fake_tags_http([])) == (None, None)


def test_helper_without_http_get_provider_returns_none(tmp_path: Path) -> None:
    """http_get=None → провайдер честно даёт None (полка не наблюдаема,
    поведение Э1 — паритет F1; семантика факта для calibrated-класса — В1a.4)."""
    reg = _write_registry(tmp_path, _calibrated_classes())
    profiles = _write_profile(tmp_path)

    _, provider = active_calibration(reg, profiles)

    assert provider is not None and provider() is None


def test_helper_accepts_model_classes_yaml_file_path(tmp_path: Path) -> None:
    """registry_path можно передать и файлом, и каталогом реестра."""
    profiles = _write_profile(tmp_path)
    _write_registry(tmp_path, _calibrated_classes())
    by_file = active_calibration(
        tmp_path / "registry" / "model_classes.yaml", profiles)
    by_dir = active_calibration(tmp_path / "registry", profiles)
    assert by_file[0] == by_dir[0] and callable(by_file[1]) == callable(by_dir[1])


# ── В1a.2: проводка в три прод-конструктора ───────────────────────────────

def test_golden_run_default_factory_wires_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:ref:`make golden-run`-фабрика: движок получает профиль/провайдер."""
    profile = {"profile_id": "p-1", "status": "calibrated"}
    provider = lambda: None  # noqa: E731 — sentinel
    monkeypatch.setattr(
        "ai_workspace.calibration.runtime.active_calibration",
        lambda *a, **k: (profile, provider))

    class FakeRedis:
        def register_script(self, script):  # noqa: ANN001
            return lambda *a, **k: None
        def scan_iter(self, match=None):
            return iter(())
        def delete(self, *keys):
            return 0

    class FakeJobStore:
        def __init__(self, client) -> None:  # noqa: ANN001
            pass
        def create(self, **kw) -> None:
            pass

    made: dict = {}

    class FakeEngine:
        def __init__(self, **kw) -> None:
            made.update(kw)

    monkeypatch.setattr("ai_workspace.redis_client.make_ws_redis",
                        lambda *a, **k: FakeRedis())
    monkeypatch.setattr("ai_workspace.orchestrator.job.JobStore", FakeJobStore)
    monkeypatch.setattr("ai_workspace.orchestrator.board.BoardStore",
                        lambda client, job_id: object())
    monkeypatch.setattr("ai_workspace.orchestrator.ledger.RedisLedger",
                        lambda client: object())
    monkeypatch.setattr("ai_workspace.artifacts.RedisBackend",
                        lambda client: object())
    monkeypatch.setattr("ai_workspace.artifacts.ArtifactStore",
                        lambda backend, **k: object())
    monkeypatch.setattr("ai_workspace.scheduler.wiring.make_on_node_usage",
                        lambda client, store=None: (lambda event: None))
    monkeypatch.setattr(golden_run, "ModeEngine", FakeEngine)

    factory = golden_run._default_engine_factory(golden_run.RunConfig())
    factory("local", "j-cal-1", object(), "ответ", "public",
            golden_run.DEFAULT_MODE)

    assert made["calibration_profile"] is profile
    assert made["calibration_model_facts"] is provider


def test_golden_run_default_factory_without_profile_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper дал (None, None) → конструктор получает прежние дефолты
    (явный None == дефолту — поведение Э1 байт-в-байт, паритет F1)."""
    monkeypatch.setattr(
        "ai_workspace.calibration.runtime.active_calibration",
        lambda *a, **k: (None, None))

    class FakeRedis:
        def register_script(self, script):  # noqa: ANN001
            return lambda *a, **k: None
        def scan_iter(self, match=None):
            return iter(())
        def delete(self, *keys):
            return 0

    class FakeJobStore:
        def __init__(self, client) -> None:  # noqa: ANN001
            pass
        def create(self, **kw) -> None:
            pass

    made: dict = {}

    class FakeEngine:
        def __init__(self, **kw) -> None:
            made.update(kw)

    monkeypatch.setattr("ai_workspace.redis_client.make_ws_redis",
                        lambda *a, **k: FakeRedis())
    monkeypatch.setattr("ai_workspace.orchestrator.job.JobStore", FakeJobStore)
    monkeypatch.setattr("ai_workspace.orchestrator.board.BoardStore",
                        lambda client, job_id: object())
    monkeypatch.setattr("ai_workspace.orchestrator.ledger.RedisLedger",
                        lambda client: object())
    monkeypatch.setattr("ai_workspace.artifacts.RedisBackend",
                        lambda client: object())
    monkeypatch.setattr("ai_workspace.artifacts.ArtifactStore",
                        lambda backend, **k: object())
    monkeypatch.setattr("ai_workspace.scheduler.wiring.make_on_node_usage",
                        lambda client, store=None: (lambda event: None))
    monkeypatch.setattr(golden_run, "ModeEngine", FakeEngine)

    factory = golden_run._default_engine_factory(golden_run.RunConfig())
    factory("local", "j-cal-2", object(), "ответ", "public",
            golden_run.DEFAULT_MODE)

    assert made["calibration_profile"] is None
    assert made["calibration_model_facts"] is None


def test_vp_ab_pilot_run_one_wires_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_one (реальный прогон на скриптованном LLM, паттерн
    test_run_one_offline_done_path): движок собран с профилем/провайдером."""
    from ai_workspace.tests.test_engine import FakeLLM

    profile = {"profile_id": "p-1", "status": "calibrated"}
    provider = lambda: None  # noqa: E731 — sentinel
    monkeypatch.setattr(
        "ai_workspace.calibration.runtime.active_calibration",
        lambda *a, **k: (profile, provider))

    engines = []
    real_engine = vp.ModeEngine

    class RecordingEngine(real_engine):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            engines.append(self)

    monkeypatch.setattr(vp, "ModeEngine", RecordingEngine)

    task = {"id": "g01-structure", "zone": "public",
            "prompt": "Составь структуру статьи про MCP-RAG (разделы, порядок)."}
    llm = FakeLLM({
        "analyst": ["План: 1) … 2) …\nВарианты и сравнение…"],
        "critic": ["PASS\nРУБРИКА: полнота 1.0"],
        "editor": ["# Статья\n\n" + "Раздел с содержанием. " * 60],
    })
    outcome = vp.run_one(vp.DEFAULT_MODE, task, "contract", 1, llm)

    assert outcome.status == "done", outcome.detail
    assert engines, "движок построен"
    assert engines[0].calibration_profile is profile
    assert engines[0].calibration_model_facts is provider


def test_f47_run_one_job_wires_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_one_job (S1-прогон job): движок получает профиль/провайдер."""
    profile = {"profile_id": "p-1", "status": "calibrated"}
    provider = lambda: None  # noqa: E731 — sentinel
    monkeypatch.setattr(
        "ai_workspace.calibration.runtime.active_calibration",
        lambda *a, **k: (profile, provider))

    made: dict = {}

    class FakeEngine:
        def __init__(self, **kw) -> None:
            made.update(kw)
        def seed(self, *a, **kw) -> None:
            pass
        def run(self, *a, **kw):
            return SimpleNamespace(status="done", detail="ok")

    class FakeClient:
        def register_script(self, script):  # noqa: ANN001
            return lambda *a, **k: None

    class FakeJobs:
        def get(self, job_id):  # noqa: ANN001
            return SimpleNamespace(state=SimpleNamespace(value="done"))

    monkeypatch.setattr(f47, "ModeEngine", FakeEngine)
    ctx = SimpleNamespace(
        client=FakeClient(), jobs=FakeJobs(), ledger=object(),
        registry=SimpleNamespace(dir=Path("/nonexistent")),
        graphs={"public": object()},
    )
    entry = {"job_id": "f47-cal-1", "zone": "public"}

    result = f47._run_one_job(ctx, port=None, shelf="local", entry=entry)

    assert made["calibration_profile"] is profile
    assert made["calibration_model_facts"] is provider
    assert result["final_status"] == "done"


# ── В1a.3: порт сборки движка (scheduler/wiring) ───────────────────────────

SCRIPT = {"analyst": ["d"], "critic": ["PASS"], "editor": ["doc"]}


def test_port_builds_engine_with_profile_and_provider(tmp_path: Path) -> None:
    """Порт с активным профилем: движок несёт профиль и РАБОЧИЙ провайдер
    (факт читается инъектированным http_get; провайдер ленивый — до вызова
    сети нет)."""
    from ai_workspace.scheduler.wiring import build_mode_engine

    reg = _write_registry(tmp_path, _calibrated_classes())
    profiles = _write_profile(tmp_path)
    calls: list[str] = []

    engine = build_mode_engine(
        jobs=FakeJobs(), boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=MemoryLedger(),
        cal_registry_path=reg, cal_profiles_dir=profiles,
        cal_http_get=_fake_tags_http(calls),
    )

    assert isinstance(engine.calibration_profile, dict)
    assert engine.calibration_profile["profile_id"] == "p-1"
    assert callable(engine.calibration_model_facts)
    assert calls == [], "провайдер ленивый: сеть не тронута до вызова"
    facts = engine.calibration_model_facts()
    assert facts is not None and facts.get("model_id") == MODEL_ID
    assert calls, "факт читается через инъектированный http_get"


def test_port_without_profile_passes_none_and_runs_mode_b(tmp_path: Path) -> None:
    """Паритет F1: нет active_profile → конструктор получает прежние дефолты
    (None, None) — и собранный портом движок РЕАЛЬНО исполняется (режим Б,
    путь Э1 байт-в-байт; порт не мёртвый код)."""
    from ai_workspace.scheduler.wiring import build_mode_engine

    reg = _write_registry(tmp_path, {"fast": {"shelf": "local",
                                              "active_profile": None}})
    engine = build_mode_engine(
        jobs=FakeJobs(), boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=MemoryLedger(),
        cal_registry_path=reg, cal_profiles_dir=tmp_path / "profiles",
        cal_http_get=_fake_tags_http([]),
    )

    assert engine.calibration_profile is None
    assert engine.calibration_model_facts is None
    engine.jobs.create("j1")
    engine.seed("j1", {"brief": "тема: MCP-RAG"}, epoch=1)
    res = engine.run("j1", epoch=1)
    assert res.status == "paused"  # обычный путь Э1 (human-gate publish)


def test_port_defaults_registry_profiles_and_urllib_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Дефолты порта: упакованный реестр ai_workspace/registry,
    DEFAULT_PROFILES_DIR, прод-провайдер urllib_http_get (порт — прод-точка:
    полка наблюдаема по умолчанию, тесты передают фейк)."""
    from ai_workspace.calibration.model_facts import urllib_http_get
    from ai_workspace.scheduler.wiring import build_mode_engine

    seen: dict = {}

    def fake_active_calibration(reg, prof, http_get=None):
        seen.update(reg=reg, prof=prof, http=http_get)
        return (None, None)

    monkeypatch.setattr(
        "ai_workspace.calibration.runtime.active_calibration",
        fake_active_calibration)

    engine = build_mode_engine(
        jobs=FakeJobs(), boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=MemoryLedger(),
    )
    assert engine.calibration_profile is None  # (None, None) из фейка — сборка жива
    assert seen["reg"] == AI_WS / "registry"
    assert seen["prof"] == DEFAULT_PROFILES_DIR
    assert seen["http"] is urllib_http_get


def test_port_passes_engine_kwargs_through(tmp_path: Path) -> None:
    """Прочие kwargs движка (registry/quota/on_node_usage/...) идут
    насквозь — порт добавляет ТОЛЬКО калибровку."""
    from ai_workspace.scheduler.wiring import build_mode_engine

    reg = _write_registry(tmp_path, {"fast": {"shelf": "local",
                                              "active_profile": None}})
    runtime_registry = {"model_classes": {"fast": {"shelf": "ext"}}}
    seen_events: list[dict] = []

    engine = build_mode_engine(
        jobs=FakeJobs(), boards=FakeBoards(), graph=load_mode(VALID),
        llm=FakeLLM(dict(SCRIPT)), mcp=FakeMCP(), ledger=MemoryLedger(),
        cal_registry_path=reg, cal_profiles_dir=tmp_path / "profiles",
        cal_http_get=_fake_tags_http([]),
        registry=runtime_registry,
        on_node_usage=seen_events.append,
    )
    assert engine.registry is runtime_registry
    engine.on_node_usage({"probe": 1})  # bound method не идентичен — проверяем вызовом
    assert seen_events == [{"probe": 1}]


# ── В1a.5: grep-инвариант ──────────────────────────────────────────────────

def _ctor_violations(source: str) -> list[int]:
    """Номера строк вызовов ``ModeEngine(...)`` без калибровки и без маркера.

    AST (не текст): упоминания в docstring/комментариях (scheduler/wiring.py)
    вызовами НЕ считаются. Маркер ищем в окне WINDOW строк до конца вызова.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "ModeEngine":
            continue
        if any(kw.arg == "calibration_profile" for kw in node.keywords):
            continue
        window = "\n".join(
            lines[max(0, node.lineno - 1 - WINDOW): node.end_lineno])
        if MARKER not in window:
            bad.append(node.lineno)
    return bad


def test_ctor_checker_flags_plain_ctor_and_accepts_kwarg_or_marker() -> None:
    """Сама проверка: без калибровки → нарушение; kwarg/маркер/docstring → ок."""
    assert _ctor_violations("e = ModeEngine(jobs=j)\n") == [1]
    assert _ctor_violations(
        "e = ModeEngine(jobs=j, calibration_profile=p,\n"
        "               calibration_model_facts=f)\n") == []
    marked = f"# {MARKER}: спец-сборка без профиля (осознанно)\n" \
             "e = ModeEngine(jobs=j)\n"
    assert _ctor_violations(marked) == []
    assert _ctor_violations('"""ModeEngine(quota=port) — docstring"""\n') == []


def test_every_non_test_modeengine_ctor_wires_calibration() -> None:
    """Инвариант В1a.5: вне tests/ каждый ctor-call калиброван или осознан."""
    violators: list[str] = []
    for path in sorted(AI_WS.rglob("*.py")):
        rel = path.relative_to(AI_WS).parts
        if "tests" in rel or "__pycache__" in rel:
            continue
        for lineno in _ctor_violations(path.read_text(encoding="utf-8")):
            violators.append(f"{path.relative_to(AI_WS)}:{lineno}")
    assert not violators, (
        "ModeEngine(...) без калибровки вне тестов: передай "
        "calibration_profile=/calibration_model_facts= из active_calibration "
        f"или пометь комментарием {MARKER!r}: {violators}")
