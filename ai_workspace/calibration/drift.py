"""Э2-2 Ф7 (arch-2026-10-08-f7-calibration): drift-детект T1–T3 + статус-гейт + T1-writeback.

Триггеры (дизайн §6.1): **T1** — смена model_id/digest полки (блокирующий:
профиль НЕ применяется → режим Б); **T2** — дрейф golden-манифеста (профиль
применяется с пометкой ``evidence_stale``); **T3** — смена прайса (применяется
с пометкой ``price_stale``). Только T1 означает «профиль калибровался под
ДРУГУЮ модель» (ollama-теги мутабельны).

Статус-гейт (F3): расхождение носителей ``profile.status`` vs
``registry.model_classes.<class>.calibration_status`` — fail-closed
(``blocked=True``, ``reason="status_divergence"``); ярус drift (ok/t1/t2/t3)
расхождением НЕ меняется, самовосстановления без оператора нет (§6.3).

``t1_writeback`` — переход в stale АТОМНО в оба носителя (F3): профиль И
реестр; сбой второй записи откатывает первую (fail-loud, половинчатого
состояния не остаётся).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "MARK_EVIDENCE_STALE",
    "MARK_PRICE_STALE",
    "REASON_DIGEST_MISMATCH",
    "REASON_MODEL_MISMATCH",
    "REASON_STATUS_DIVERGENCE",
    "STATUS_OK",
    "STATUS_T1",
    "STATUS_T2",
    "STATUS_T3",
    "DriftResult",
    "detect",
    "t1_writeback",
]

STATUS_OK = "ok"
STATUS_T1 = "t1"
STATUS_T2 = "t2"
STATUS_T3 = "t3"

REASON_DIGEST_MISMATCH = "digest_mismatch"
REASON_MODEL_MISMATCH = "model_mismatch"
REASON_STATUS_DIVERGENCE = "status_divergence"

MARK_EVIDENCE_STALE = "evidence_stale"
MARK_PRICE_STALE = "price_stale"

_STALE = "stale"


@dataclass(frozen=True)
class DriftResult:
    """Вердикт drift-детекта: ярус T1–T3 + пометки + причина + fail-closed.

    ``status`` — ярус drift (``ok|t1|t2|t3``); ``blocked=True`` — fail-closed
    статус-гейта (расхождение носителей, F3): профиль не применяется независимо
    от яруса. ``reason`` — первопричина: у T1 приоритет над расхождением
    (writeback лечит и его), иначе — ``status_divergence``.
    """

    status: str
    marks: tuple[str, ...]
    reason: str | None
    blocked: bool = False


def _facts_get(facts: Any, key: str) -> Any:
    """Поле фактов полки (``ModelFacts.get`` или Mapping); нет доступа — None."""
    getter = getattr(facts, "get", None)
    if callable(getter):
        return getter(key)
    return None


def _registry_class_status(registry: Any, model_class: Any) -> str | None:
    """Статус класса в реестре (dict/Registry); реестр недоступен — None."""
    if registry is None:
        return None
    try:
        classes = registry.get("model_classes") or {}
    except Exception:  # noqa: BLE001 — реестр недоступен: верифицировать нечем
        return None
    spec = classes.get(model_class) if hasattr(classes, "get") else None
    if isinstance(spec, Mapping):
        status = spec.get("calibration_status")
        return str(status) if status is not None else None
    return None


def detect(
    profile: Mapping | None,
    registry: Any = None,
    model_facts: Any = None,
    *,
    golden_manifest_hash: str | None = None,
    pricing_manifest_hash: str | None = None,
    registry_class_status: str | None = None,
) -> DriftResult:
    """Сверить профиль с фактом полки/манифестами/реестром (T1–T3 + гейт F3).

    ``model_facts`` — факт полки (``ModelFacts``/Mapping); ``None`` = полка
    не наблюдаема → T1 не заявляется (сбой сети ≠ drift, §6.2).
    ``golden_manifest_hash``/``pricing_manifest_hash`` — фактические хеши
    манифестов; переданы и ≠ evidence профиля → пометки T2/T3 (профиль
    применяется). ``registry_class_status`` — статус класса реестра; не
    передан явно — выводится из ``registry`` по ``profile.model_class``.
    """
    if not isinstance(profile, Mapping):
        return DriftResult(status=STATUS_OK, marks=(), reason=None)

    # --- T1 (блокирующий): смена model_id/digest полки (§6.1) ---
    status, reason = STATUS_OK, None
    if model_facts is not None:
        cal = profile.get("calibrated_for")
        cal = cal if isinstance(cal, Mapping) else {}
        fact_model = _facts_get(model_facts, "model_id")
        fact_digest = _facts_get(model_facts, "digest")
        if fact_model != cal.get("model_id"):
            status, reason = STATUS_T1, REASON_MODEL_MISMATCH
        elif fact_digest != cal.get("digest"):
            status, reason = STATUS_T1, REASON_DIGEST_MISMATCH

    # --- T2/T3: дрейф манифестов (профиль применяется, §6.1) ---
    marks: list[str] = []
    evidence = profile.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if (
        golden_manifest_hash is not None
        and golden_manifest_hash != evidence.get("golden_manifest")
    ):
        marks.append(MARK_EVIDENCE_STALE)
    if (
        pricing_manifest_hash is not None
        and pricing_manifest_hash != evidence.get("pricing_manifest")
    ):
        marks.append(MARK_PRICE_STALE)
    if status != STATUS_T1:  # t1 приоритетнее; ярус — по старшей пометке
        if MARK_EVIDENCE_STALE in marks:
            status = STATUS_T2
        elif MARK_PRICE_STALE in marks:
            status = STATUS_T3

    # --- статус-гейт (F3): расхождение носителей — fail-closed, ярус не трогаем ---
    if registry_class_status is None:
        registry_class_status = _registry_class_status(registry, profile.get("model_class"))
    blocked = False
    if registry_class_status is not None and registry_class_status != profile.get("status"):
        blocked = True
        if status != STATUS_T1:  # причина T1 приоритетнее: writeback лечит и расхождение
            reason = REASON_STATUS_DIVERGENCE

    return DriftResult(status=status, marks=tuple(marks), reason=reason, blocked=blocked)


# ----------------------------------------------- T1-writeback (F3, атомарно оба)


def _load_yaml_mapping(path: Path) -> dict:
    """Прочитать YAML-отображение; нет файла/битый/не mapping — fail-loud."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"ожидается YAML-отображение: {path}")
    return doc


def _write_yaml_atomic(path: Path, doc: Mapping) -> None:
    """Атомарная замена файла: tmp в том же каталоге + ``os.replace``."""
    tmp = path.with_name(path.name + ".t1tmp")
    tmp.write_text(
        yaml.safe_dump(dict(doc), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def t1_writeback(
    profiles_dir: Path | str,
    profile_id: str,
    registry_path: Path | str,
    model_class: str,
) -> None:
    """Перевести профиль И класс реестра в ``stale`` одной операцией (F3, §6.3).

    Носители: ``profiles_dir/<profile_id>.yaml`` → ``status: stale`` И
    ``registry_path: model_classes.<model_class>.calibration_status: stale``.
    Сначала готовятся ОБА документа, затем запись профиля → запись реестра;
    сбой записи реестра откатывает профиль к исходным байтам и падает дальше
    (fail-loud): половинчатого состояния не остаётся.
    """
    profile_path = Path(profiles_dir) / f"{profile_id}.yaml"
    reg_path = Path(registry_path)
    profile_doc = _load_yaml_mapping(profile_path)
    registry_doc = _load_yaml_mapping(reg_path)

    classes = registry_doc.get("model_classes")
    if not isinstance(classes, Mapping) or model_class not in classes:
        raise ValueError(f"класс модели отсутствует в реестре: {model_class!r}")

    new_profile = dict(profile_doc)
    new_profile["status"] = _STALE
    new_registry = dict(registry_doc)
    new_classes = dict(classes)
    new_class = dict(new_classes[model_class])
    new_class["calibration_status"] = _STALE
    new_classes[model_class] = new_class
    new_registry["model_classes"] = new_classes

    profile_backup = profile_path.read_bytes()
    _write_yaml_atomic(profile_path, new_profile)
    try:
        _write_yaml_atomic(reg_path, new_registry)
    except Exception:
        # откат первой записи: оба носителя либо вместе, либо никак (F3)
        profile_path.write_bytes(profile_backup)
        raise
