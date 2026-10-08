"""Э2-1 Ф7 (arch-2026-10-08-f7-calibration): реестр вариантов mode-variants (§7.4).

``calibration/variants.yaml`` — SSOT promotion-решений вариантов режимов
(``modes/<mode>.<variant>.yaml``, конвенция §7.2). Валидатор fail-closed:
обязательные поля записи, статус-enum, существование файлов варианта и
базового режима в ``modes_dir``, обязательность ``probe_pair{base,variant}``
для ``promoted``/``rejected`` (решение оператора P5 опирается на пару
ProbeReport §7.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ai_workspace.calibration.profiles import SEVERITY_ERROR, Finding

__all__ = [
    "VARIANT_SCHEMA",
    "VARIANT_STATUSES",
    "validate_variants",
]

#: Схема реестра вариантов (дизайн §7.4)
VARIANT_SCHEMA = "calibration-variants/1"

#: baseline — запись при старте эксперимента; promoted/rejected — только оператор (P5)
VARIANT_STATUSES: frozenset[str] = frozenset({"baseline", "promoted", "rejected"})

#: Статусы с принятым решением — требуют probe_pair
DECIDED_STATUSES: frozenset[str] = frozenset({"promoted", "rejected"})

REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "variant",
    "variant_of",
    "status",
    "decided_by",
    "decided_at",
)
REQUIRED_PROBE_PAIR: tuple[str, ...] = ("base", "variant")


def _err(code: str, message: str, path: str) -> Finding:
    return Finding(code=code, severity=SEVERITY_ERROR, message=message, path=path)


def _check_mode_file(
    findings: list[Finding], modes: Path, value: object, field: str, prefix: str, code: str
) -> None:
    """Файл ``modes/<value>.yaml`` существует (value — непустая строка)."""
    if not isinstance(value, str) or not value:
        findings.append(_err(code, f"{field} должен быть непустой строкой", f"{prefix}.{field}"))
        return
    if not (modes / f"{value}.yaml").is_file():
        findings.append(
            _err(code, f"файл режима не найден: {modes / (value + '.yaml')}", f"{prefix}.{field}")
        )


def validate_variants(doc: dict, modes_dir: Path | str) -> list[Finding]:
    """Проверить реестр вариантов по §7.4; ``modes_dir`` — каталог mode-YAML."""
    findings: list[Finding] = []
    if not isinstance(doc, Mapping):
        return [_err("CV0", "реестр вариантов должен быть YAML-отображением (mapping)", "$")]

    # CV1: литерал схемы.
    if doc.get("schema") != VARIANT_SCHEMA:
        findings.append(
            _err(
                "CV1",
                f"schema должен быть {VARIANT_SCHEMA!r}, получено: {doc.get('schema')!r}",
                "schema",
            )
        )

    entries = doc.get("entries")
    if "entries" not in doc:
        findings.append(_err("CV2", "обязательное поле отсутствует: entries", "entries"))
        entries = None
    elif not isinstance(entries, list):
        findings.append(_err("CV3", "entries должен быть списком", "entries"))
        entries = None

    modes = Path(modes_dir)
    for i, entry in enumerate(entries or []):
        prefix = f"entries[{i}]"
        if not isinstance(entry, Mapping):
            findings.append(
                _err(
                    "CV3",
                    f"запись должна быть отображением: {type(entry).__name__}",
                    prefix,
                )
            )
            continue

        # CV2: обязательные поля записи.
        for field in REQUIRED_ENTRY_FIELDS:
            if field not in entry:
                findings.append(
                    _err("CV2", f"обязательное поле отсутствует: {field}", f"{prefix}.{field}")
                )

        # CV4: status — enum.
        status = entry.get("status")
        if "status" in entry and status not in VARIANT_STATUSES:
            findings.append(
                _err(
                    "CV4",
                    f"неизвестный status: {status!r}; ожидается один из "
                    f"{sorted(VARIANT_STATUSES)}",
                    f"{prefix}.status",
                )
            )

        # CV5: файл варианта существует (modes/<variant>.yaml).
        if "variant" in entry:
            _check_mode_file(findings, modes, entry.get("variant"), "variant", prefix, "CV5")

        # CV6: variant_of указывает на существующий базовый режим.
        if "variant_of" in entry:
            _check_mode_file(
                findings, modes, entry.get("variant_of"), "variant_of", prefix, "CV6"
            )

        # CV7: probe_pair{base,variant} обязателен для promoted/rejected.
        if status in DECIDED_STATUSES:
            pair = entry.get("probe_pair")
            if not isinstance(pair, Mapping) or any(k not in pair for k in REQUIRED_PROBE_PAIR):
                findings.append(
                    _err(
                        "CV7",
                        "для status="
                        f"{status!r} обязателен probe_pair{{{', '.join(REQUIRED_PROBE_PAIR)}}}",
                        f"{prefix}.probe_pair",
                    )
                )

    return findings
