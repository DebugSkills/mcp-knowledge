"""Э2-1 Ф7 (arch-2026-10-08-f7-calibration): схема-валидатор профилей калибровки.

Профиль — полный снимок калибровки класса (дизайн §5.1, схема
``calibration-profile/1``): evidence замера + эффективные скаляры + ограничения.
Валидатор fail-closed: отсутствие обязательного поля, неизвестное поле
верхнего уровня, запрещённые поля (§12 A4: ``zone``/``decoding``/``q_floor``) —
ошибки.

``model_class`` в профиле — привязка применения (какой класс калиброван),
НЕ мутация узлов режимов (A4). ``validate_profile`` — чистая функция без I/O;
чтение каталога профилей — ``load_profile``/``list_profiles``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "PROFILE_SCHEMA",
    "PROFILE_STATUSES",
    "Finding",
    "list_profiles",
    "load_profile",
    "validate_profile",
]

#: Схема профилей калибровки (дизайн §5.1)
PROFILE_SCHEMA = "calibration-profile/1"

#: Носители статуса (§6.3): ``calibrated`` ставит только оператор (P5),
#: ``stale`` — модуль (T1-writeback), ``draft`` — модуль после probe.
PROFILE_STATUSES: frozenset[str] = frozenset({"calibrated", "stale", "draft"})

#: Обязательные поля документа профиля (§5.1)
REQUIRED_FIELDS: tuple[str, ...] = (
    "schema",
    "profile_id",
    "model_class",
    "calibrated_for",
    "status",
    "version",
    "evidence",
    "scalars",
    "constraints",
    "created_at",
    "updated_at",
)

#: Разрешённые поля верхнего уровня (fail-closed: всё вне набора — ошибка)
ALLOWED_FIELDS: frozenset[str] = frozenset(REQUIRED_FIELDS)

#: §12 A4: калибровка НЕ управляет зоной, decoding или Q-floor —
#: присутствие этих полей в профиле запрещено (error).
FORBIDDEN_FIELDS: frozenset[str] = frozenset({"zone", "decoding", "q_floor"})

REQUIRED_CALIBRATED_FOR: tuple[str, ...] = ("model_id", "digest")
REQUIRED_EVIDENCE: tuple[str, ...] = (
    "probe_run",
    "golden_manifest",
    "pricing_manifest",
    "metrics",
)
#: F6: golden и held-out — РАЗДЕЛЬНЫЕ обязательные поля
REQUIRED_METRICS: tuple[str, ...] = (
    "golden_median_score",
    "heldout_score",
    "parse_rate",
    "rub",
    "wall_s",
)
REQUIRED_SCALARS: tuple[str, ...] = (
    "retries",
    "max_iterations",
    "shaping",
    "context_mode",
)
REQUIRED_CONSTRAINTS: tuple[str, ...] = ("quality_floor",)

SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Finding:
    """Замечание валидатора; в контуре схемы все severity — ``error``."""

    code: str
    severity: str
    message: str
    path: str


def _err(code: str, message: str, path: str) -> Finding:
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


def _check_block(
    findings: list[Finding],
    doc: Mapping,
    key: str,
    subfields: tuple[str, ...],
    prefix: str = "",
) -> Mapping | None:
    """Обязательные подполя блока ``key``; сам блок — отображение."""
    full = f"{prefix}{key}"
    if key not in doc:
        return None  # отсутствие блока верхнего уровня уже сообщил CP1
    block = doc[key]
    if not isinstance(block, Mapping):
        findings.append(_err("CP6", f"{full} должен быть отображением (mapping)", full))
        return None
    for sub in subfields:
        if sub not in block:
            findings.append(
                _err("CP6", f"обязательное поле отсутствует: {full}.{sub}", f"{full}.{sub}")
            )
    return block


def validate_profile(doc: dict) -> list[Finding]:
    """Проверить документ профиля по схеме ``calibration-profile/1`` (fail-closed)."""
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("CP0", "профиль должен быть YAML-отображением (mapping)", "$")]

    # CP1: обязательные поля верхнего уровня.
    for field in REQUIRED_FIELDS:
        if field not in doc:
            findings.append(_err("CP1", f"обязательное поле отсутствует: {field}", field))

    # CP5: запрещённые поля (§12 A4) — калибровка ими не управляет.
    for field in sorted(FORBIDDEN_FIELDS):
        if field in doc:
            findings.append(
                _err(
                    "CP5",
                    f"запрещённое поле профиля (A4): {field!r}",
                    field,
                )
            )

    # CP4: неизвестные поля верхнего уровня (fail-closed);
    # запрещённые уже отмечены CP5 — без дублей.
    for field in doc:
        if field not in ALLOWED_FIELDS and field not in FORBIDDEN_FIELDS:
            findings.append(
                _err(
                    "CP4",
                    f"неизвестное поле верхнего уровня: {field!r}; разрешены: "
                    f"{sorted(ALLOWED_FIELDS)}",
                    field,
                )
            )

    # CP3: литерал схемы.
    if "schema" in doc and doc["schema"] != PROFILE_SCHEMA:
        findings.append(
            _err(
                "CP3",
                f"schema должен быть {PROFILE_SCHEMA!r}, получено: {doc['schema']!r}",
                "schema",
            )
        )

    # CP2: status — enum (§6.3).
    if "status" in doc and doc["status"] not in PROFILE_STATUSES:
        findings.append(
            _err(
                "CP2",
                f"неизвестный status: {doc['status']!r}; ожидается один из "
                f"{sorted(PROFILE_STATUSES)}",
                "status",
            )
        )

    # CP6: обязательные подполя блоков (§5.1).
    _check_block(findings, doc, "calibrated_for", REQUIRED_CALIBRATED_FOR)
    evidence = _check_block(findings, doc, "evidence", REQUIRED_EVIDENCE)
    if evidence is not None:
        _check_block(findings, evidence, "metrics", REQUIRED_METRICS, prefix="evidence.")
    _check_block(findings, doc, "scalars", REQUIRED_SCALARS)
    _check_block(findings, doc, "constraints", REQUIRED_CONSTRAINTS)

    return findings


def load_profile(profiles_dir: Path | str, profile_id: str) -> dict | None:
    """Прочитать ``profiles_dir/<profile_id>.yaml`` (yaml.safe_load); нет файла → None."""
    path = Path(profiles_dir) / f"{profile_id}.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_profiles(profiles_dir: Path | str) -> list[str]:
    """Id профилей (stems) по ``*.yaml`` в каталоге; нет каталога → пустой список."""
    directory = Path(profiles_dir)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.yaml") if path.is_file())
