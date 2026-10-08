"""Э2-2 Ф7 (arch-2026-10-08-f7-calibration): drift T1–T3 + статус-гейт + T1-writeback (A5).

Невакуумность: T1-гейт сквозной — ``drift.detect`` + ``resolve`` Э1 на
РЕАЛЬНЫХ узлах режима ``statya`` (узлы — list of dict из modes/*.yaml);
интеграция движка — на фейках ``test_engine`` (как ``test_engine_calibration``);
``t1_writeback`` — на tmp_path, ОБА носителя проверяются содержимым файлов
(yaml.safe_load), не текстом отчёта. Сети нет: ``http_get``/``fetch``
инъекцируемые.

Покрытие (план Э2 §2, дизайн §6/§12-A5):
- T1 digest/model mismatch → ``status="t1"``; профиль через resolve → режим Б;
- T2/T3 → профиль применяется + ``marks`` (``evidence_stale``/``price_stale``);
- статус-гейт F3: расхождение носителей → ``blocked=True``, fail-closed;
- сетевой сбой → last-known (``last_known=True``), НЕ drift;
- ``t1_writeback`` пишет ``stale`` в ОБА носителя; сбой второй записи — откат;
- хук движка: без провайдера — паритет Э1; провайдер даёт факты гейта T1.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_workspace.calibration import drift as drift_mod
from ai_workspace.calibration.api import resolve
from ai_workspace.calibration.drift import DriftResult, detect, t1_writeback
from ai_workspace.calibration.model_facts import (
    ModelFacts,
    ModelFactsCache,
    facts_for,
    fetch_local_facts,
)
from ai_workspace.orchestrator.engine import MemoryLedger, ModeEngine, load_mode
from ai_workspace.tests.test_engine import (
    VALID,
    FakeBoards,
    FakeJobs,
    FakeLLM,
    FakeMCP,
)

MODES_DIR = Path(__file__).resolve().parents[1] / "modes"

GOLDEN_HASH = "sha256:golden-1"
PRICING_HASH = "sha256:pricing-1"

TAGS = {
    "models": [
        {"name": "qwen2.5:7b", "digest": "sha256:q"},
        {"name": "llama3:8b", "digest": "sha256:l"},
    ]
}


def _nodes(mode_id: str = "statya") -> dict[str, dict]:
    """Узлы реального режима как Mapping[id -> node] (как test_calibration_resolve)."""
    doc = yaml.safe_load((MODES_DIR / f"{mode_id}.yaml").read_text(encoding="utf-8"))
    return {n["id"]: n for n in doc["nodes"]}


def _calibrated_registry() -> dict:
    return {
        "model_classes": {
            "heavy": {
                "shelf": "ext",
                "shaping": "full-context",
                "retries": 5,
                "max_iterations": 9,
                "calibration_status": "calibrated",
                "active_profile": "p-heavy-1",
            },
            "fast": {
                "shelf": "local",
                "shaping": "compressed",
                "retries": 1,
                "calibration_status": "uncalibrated",
            },
        }
    }


def _profile(**over: object) -> dict:
    prof: dict = {
        "profile_id": "p-heavy-1",
        "model_class": "heavy",
        "status": "calibrated",
        "calibrated_for": {"model_id": "glm-5.2", "digest": "sha256:abc"},
        "scalars": {"retries": 4, "max_iterations": 2},
        "evidence": {"golden_manifest": GOLDEN_HASH, "pricing_manifest": PRICING_HASH},
    }
    prof.update(over)
    return prof


FACTS_OK = ModelFacts(model_id="glm-5.2", digest="sha256:abc")
FACTS_BAD_DIGEST = ModelFacts(model_id="glm-5.2", digest="sha256:OTHER")
FACTS_OTHER_MODEL = ModelFacts(model_id="qwen2.5:7b", digest="sha256:abc")


# ------------------------------------------------------------------ T1: блокирующий


def test_t1_digest_mismatch() -> None:
    res = detect(_profile(), _calibrated_registry(), FACTS_BAD_DIGEST)
    assert isinstance(res, DriftResult)
    assert res.status == "t1"
    assert res.reason == "digest_mismatch"
    assert res.blocked is False  # T1 блокирует ярусом, не статус-гейтом


def test_t1_model_mismatch() -> None:
    res = detect(_profile(), _calibrated_registry(), FACTS_OTHER_MODEL)
    assert res.status == "t1"
    assert res.reason == "model_mismatch"


def test_t1_facts_match_is_ok() -> None:
    res = detect(_profile(), _calibrated_registry(), FACTS_OK)
    assert res.status == "ok"
    assert res.reason is None
    assert res.marks == ()


def test_t1_no_facts_no_drift() -> None:
    """Полка не наблюдаема (None) → T1 не заявляется (сбой сети ≠ drift, §6.2)."""
    res = detect(_profile(), _calibrated_registry(), None)
    assert res.status == "ok"
    assert res.reason is None


def test_t1_profile_falls_to_mode_b_via_resolve() -> None:
    """Сквозной T1: digest не совпал → resolve даёт режим Б (node-семантика)."""
    out = resolve(_nodes(), _calibrated_registry(), FACTS_BAD_DIGEST, profile=_profile())
    analyst = out["analyst"]
    assert analyst.profile_id is None  # режим Б
    assert analyst.retries == 2  # statya: node retry:2
    assert analyst.sources["retries"] == "node"
    assert analyst.drift == "digest_mismatch"


# ------------------------------------------------------------------ T2/T3: пометки


def test_t2_golden_stale_profile_applies() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        golden_manifest_hash="sha256:golden-2",
    )
    assert res.status == "t2"
    assert "evidence_stale" in res.marks
    assert res.blocked is False  # профиль применяется (T2 не блокирует)
    out = resolve(_nodes(), _calibrated_registry(), FACTS_OK, profile=_profile())
    assert out["analyst"].profile_id == "p-heavy-1"  # режим П
    assert out["analyst"].sources["retries"] == "profile"


def test_t3_price_stale() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        pricing_manifest_hash="sha256:pricing-2",
    )
    assert res.status == "t3"
    assert "price_stale" in res.marks
    assert res.blocked is False


def test_t2_prioritized_over_t3_when_both() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        golden_manifest_hash="sha256:g2",
        pricing_manifest_hash="sha256:p2",
    )
    assert res.status == "t2"
    assert set(res.marks) == {"evidence_stale", "price_stale"}


def test_matching_manifests_no_marks() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        golden_manifest_hash=GOLDEN_HASH,
        pricing_manifest_hash=PRICING_HASH,
    )
    assert res.status == "ok"
    assert res.marks == ()


def test_t1_priority_over_t2t3_marks() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_BAD_DIGEST,
        golden_manifest_hash="sha256:g2",
    )
    assert res.status == "t1"
    assert res.reason == "digest_mismatch"
    assert "evidence_stale" in res.marks  # пометка информативна, ярус — t1


# ------------------------------------------------------------- статус-гейт (F3)


def test_status_divergence_fail_closed() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        registry_class_status="uncalibrated",
    )
    assert res.status == "ok"
    assert res.reason == "status_divergence"
    assert res.blocked is True


def test_status_divergence_derived_from_registry() -> None:
    """Статус класса не передан явно — выводится из реестра по model_class."""
    reg = _calibrated_registry()
    res = detect(_profile(status="draft"), reg, FACTS_OK)  # реестр: calibrated
    assert res.reason == "status_divergence"
    assert res.blocked is True


def test_status_divergence_with_t1_keeps_t1_reason() -> None:
    """T1 + расхождение: ярус t1 и причина digest (writeback лечит и гейт)."""
    res = detect(
        _profile(status="draft"),
        _calibrated_registry(),
        FACTS_BAD_DIGEST,
    )
    assert res.status == "t1"
    assert res.reason == "digest_mismatch"
    assert res.blocked is True


def test_statuses_agree_no_divergence() -> None:
    res = detect(
        _profile(),
        _calibrated_registry(),
        FACTS_OK,
        registry_class_status="calibrated",
    )
    assert res.reason is None
    assert res.blocked is False


# ------------------------------------------- кэш фактов: last-known-fallback (§6.2)


def test_cache_network_fail_returns_last_known_not_drift() -> None:
    clock = {"now": 0.0}
    cache = ModelFactsCache(ttl_s=60, clock=lambda: clock["now"])
    good = ModelFacts("qwen2.5:7b", "sha256:q")
    cache.put("local:qwen2.5:7b", good)
    clock["now"] = 61.0  # fresh-запись протухла -> идём в сеть; сеть упала

    def broken_fetch() -> ModelFacts:
        raise ConnectionError("ollama down")

    facts, last_known = cache.resolve_or_cached("local:qwen2.5:7b", broken_fetch)
    assert facts == good
    assert last_known is True
    # последний известный факт сверки НЕ роняет: это не drift
    res = detect(_profile(calibrated_for={"model_id": "qwen2.5:7b", "digest": "sha256:q"}),
                 _calibrated_registry(), facts)
    assert res.status == "ok"


def test_cache_network_fail_without_history_is_none_last_known() -> None:
    cache = ModelFactsCache(ttl_s=60)

    def broken_fetch() -> ModelFacts:
        raise ConnectionError("ollama down")

    facts, last_known = cache.resolve_or_cached("k", broken_fetch)
    assert facts is None
    assert last_known is True


def test_cache_fresh_fetch_stored_and_not_last_known() -> None:
    cache = ModelFactsCache(ttl_s=60)
    calls: list[int] = []

    def fetch() -> ModelFacts:
        calls.append(1)
        return ModelFacts("m", "d1")

    facts, last_known = cache.resolve_or_cached("k", fetch)
    assert (facts, last_known) == (ModelFacts("m", "d1"), False)
    facts2, last_known2 = cache.resolve_or_cached("k", fetch)  # из кэша
    assert (facts2, last_known2) == (ModelFacts("m", "d1"), False)
    assert len(calls) == 1
    assert cache.get("k") == facts


def test_cache_ttl_expiry_refetches() -> None:
    clock = {"now": 0.0}
    cache = ModelFactsCache(ttl_s=10, clock=lambda: clock["now"])
    cache.put("k", ModelFacts("m", "d1"))
    assert cache.get("k") == ModelFacts("m", "d1")
    clock["now"] = 11.0
    assert cache.get("k") is None  # просрочен
    facts, last_known = cache.resolve_or_cached("k", lambda: ModelFacts("m", "d2"))
    assert (facts, last_known) == (ModelFacts("m", "d2"), False)


def test_cache_fetch_none_no_fallback() -> None:
    """fetch вернул None (тега нет) — честный None, last-known не подставляется."""
    clock = {"now": 0.0}
    cache = ModelFactsCache(ttl_s=60, clock=lambda: clock["now"])
    cache.put("k", ModelFacts("m", "d1"))
    clock["now"] = 61.0  # протухли -> fetch; тега нет -> None без fallback

    def no_tag() -> ModelFacts | None:
        return None

    facts, last_known = cache.resolve_or_cached("k", no_tag)
    assert facts is None
    assert last_known is False


# ------------------------------------------------------------ fetch_local_facts


def test_fetch_local_facts_selects_requested_tag() -> None:
    facts = fetch_local_facts(
        lambda url: TAGS, endpoint="http://x/api/tags", model_id="llama3:8b"
    )
    assert facts == ModelFacts("llama3:8b", "sha256:l")


def test_fetch_local_facts_default_takes_first_tag() -> None:
    facts = fetch_local_facts(lambda url: TAGS)
    assert facts == ModelFacts("qwen2.5:7b", "sha256:q")


def test_fetch_local_facts_missing_tag_none() -> None:
    assert fetch_local_facts(lambda url: TAGS, model_id="nope:latest") is None


def test_fetch_local_facts_network_error_none() -> None:
    def boom(url: str) -> dict:
        raise ConnectionError("ollama down")

    assert fetch_local_facts(boom) is None


def test_model_facts_mapping_get_for_resolver() -> None:
    """ModelFacts.get — контракт resolve Э1 (Mapping-доступ без конверсии)."""
    assert FACTS_OK.get("model_id") == "glm-5.2"
    assert FACTS_OK.get("digest") == "sha256:abc"
    assert FACTS_OK.get("nope", "d") == "d"


# ------------------------------------------------------------------ facts_for


def test_facts_for_local_uses_registry_target_and_cache() -> None:
    reg = {
        "model_classes": {
            "fast": {
                "shelf": "local",
                "calibrated_for": {"model_id": "qwen2.5:7b", "digest": "sha256:q"},
            }
        }
    }
    cache = ModelFactsCache(ttl_s=60)
    facts = facts_for("fast", reg, http_get=lambda url: TAGS, cache=cache)
    assert facts == ModelFacts("qwen2.5:7b", "sha256:q")
    assert cache.get("local:qwen2.5:7b") == facts


def test_facts_for_ext_from_class_config() -> None:
    reg = {
        "model_classes": {
            "heavy": {
                "shelf": "ext",
                "calibrated_for": {"model_id": "glm-5.2", "digest": "sha256:abc"},
            }
        }
    }
    assert facts_for("heavy", reg) == ModelFacts("glm-5.2", "sha256:abc")


def test_facts_for_unknown_localonly_or_no_target_none() -> None:
    reg = {
        "model_classes": {
            "local-only": {"rule": "zone"},
            "fast": {"shelf": "local"},  # нет calibrated_for.model_id
        }
    }
    assert facts_for("local-only", reg) is None
    assert facts_for("fast", reg) is None
    assert facts_for("missing", reg) is None


# ------------------------------------------------------------------ t1_writeback


def _write_carriers(tmp_path: Path) -> tuple[Path, Path]:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "p-heavy-1.yaml").write_text(
        yaml.safe_dump(_profile(), allow_unicode=True), encoding="utf-8"
    )
    reg_path = tmp_path / "model_classes.yaml"
    reg_path.write_text(
        yaml.safe_dump(_calibrated_registry(), allow_unicode=True), encoding="utf-8"
    )
    return profiles, reg_path


def test_t1_writeback_marks_both_carriers(tmp_path: Path) -> None:
    profiles, reg_path = _write_carriers(tmp_path)
    t1_writeback(profiles, "p-heavy-1", reg_path, "heavy")
    # носитель 1: профиль (содержимым файла, не отчётом)
    prof_doc = yaml.safe_load((profiles / "p-heavy-1.yaml").read_text(encoding="utf-8"))
    assert prof_doc["status"] == "stale"
    assert prof_doc["profile_id"] == "p-heavy-1"  # остальное не тронуто
    assert prof_doc["calibrated_for"] == {"model_id": "glm-5.2", "digest": "sha256:abc"}
    # носитель 2: реестр
    reg_doc = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert reg_doc["model_classes"]["heavy"]["calibration_status"] == "stale"
    # соседний класс не затронут
    assert reg_doc["model_classes"]["fast"]["calibration_status"] == "uncalibrated"
    # повторный вызов идемпотентен
    t1_writeback(profiles, "p-heavy-1", reg_path, "heavy")
    assert yaml.safe_load((profiles / "p-heavy-1.yaml").read_text(encoding="utf-8"))["status"] == "stale"
    assert not list(profiles.glob("*.t1tmp")) and not list(tmp_path.glob("*.t1tmp"))


def test_t1_writeback_rollback_on_second_carrier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles, reg_path = _write_carriers(tmp_path)
    profile_before = (profiles / "p-heavy-1.yaml").read_bytes()
    registry_before = reg_path.read_bytes()
    real_write = drift_mod._write_yaml_atomic

    def flaky_write(path: Path, doc: dict) -> None:
        if Path(path) == Path(reg_path):
            raise OSError("registry write failed")
        real_write(path, doc)

    monkeypatch.setattr(drift_mod, "_write_yaml_atomic", flaky_write)
    with pytest.raises(OSError, match="registry write failed"):
        t1_writeback(profiles, "p-heavy-1", reg_path, "heavy")
    # откат: профиль возвращён к исходным байтам — половинчатого состояния нет
    assert (profiles / "p-heavy-1.yaml").read_bytes() == profile_before
    assert reg_path.read_bytes() == registry_before


def test_t1_writeback_missing_class_fails_loud_writes_nothing(tmp_path: Path) -> None:
    profiles, reg_path = _write_carriers(tmp_path)
    profile_before = (profiles / "p-heavy-1.yaml").read_bytes()
    with pytest.raises(ValueError, match="класс модели отсутствует"):
        t1_writeback(profiles, "p-heavy-1", reg_path, "nope")
    assert (profiles / "p-heavy-1.yaml").read_bytes() == profile_before


# ------------------------------------------------- хук движка (Э2-2, паритет F1)


def _engine(*, registry: object, profile: dict | None, model_facts=None) -> ModeEngine:
    return ModeEngine(
        jobs=FakeJobs(),
        boards=FakeBoards(),
        graph=load_mode(VALID),
        llm=FakeLLM({"analyst": ["ok"], "critic": ["PASS"], "editor": ["doc"]}),
        mcp=FakeMCP(),
        ledger=MemoryLedger(),
        registry=registry,
        calibration_profile=profile,
        calibration_model_facts=model_facts,
    )


def test_engine_hook_without_provider_keeps_e1_parity() -> None:
    """Без провайдера facts — model_facts=None → поведение Э1 в точности."""
    engine = _engine(registry=_calibrated_registry(), profile=_profile())
    assert engine.calibration_model_facts is None
    scalars = engine._resolve_calibration()
    # Э1: facts нет → calibrated_for считается совпавшим → режим П
    assert scalars["analyst"].profile_id == "p-heavy-1"
    assert scalars["analyst"].sources["retries"] == "profile"


def test_engine_hook_provider_facts_drive_t1_gate() -> None:
    engine = _engine(
        registry=_calibrated_registry(), profile=_profile(), model_facts=lambda: FACTS_BAD_DIGEST
    )
    scalars = engine._resolve_calibration()
    assert scalars["analyst"].profile_id is None  # режим Б
    assert scalars["analyst"].drift == "digest_mismatch"
    assert scalars["analyst"].retries == 2  # node retry:2 (valid_statya)
    assert scalars["analyst"].sources["retries"] == "node"


def test_engine_hook_provider_matching_facts_keep_mode_p() -> None:
    engine = _engine(
        registry=_calibrated_registry(), profile=_profile(), model_facts=lambda: FACTS_OK
    )
    scalars = engine._resolve_calibration()
    assert scalars["analyst"].profile_id == "p-heavy-1"


def test_engine_hook_provider_crash_is_soft() -> None:
    """Сбой провайдера = сетевой сбой (§6.2): факты недоступны, job не роняем;
    гейт проходит без верификации (mode_facts=None — семантика Э1)."""

    def boom():
        raise ConnectionError("ollama down")

    engine = _engine(registry=_calibrated_registry(), profile=_profile(), model_facts=boom)
    scalars = engine._resolve_calibration()
    assert scalars["analyst"].profile_id == "p-heavy-1"
