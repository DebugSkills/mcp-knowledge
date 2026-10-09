"""В1-2 (1d) Ф7 (arch-2026-10-08-f7-calibration): CLI approve профиля калибровки.

Утверждение профиля (``draft`` → ``calibrated``) — решение ТОЛЬКО оператора
(P5, §6.3): этот CLI НЕ узурпирует решение — он лишь ИСПОЛНИЕТ его атомарно
и fail-closed. «Применим» = два носителя согласованы (F3): профиль
``status: calibrated, version+1`` И реестр ``model_classes.yaml`` →
``calibration_status/calibrated_for/active_profile`` — ОДНОЙ операцией
паттерном ``t1_writeback`` (``calibration/drift.py:184-223``): сначала
готовятся ОБА документа, затем запись профиля → запись реестра; сбой записи
реестра откатывает профиль к исходным байтам и падает дальше (fail-loud) —
половинчатого состояния не остаётся.

Fail-closed отказы (exit 2, НЕ пишется ничего — включая аудит):
- профиля нет на носителе / файл не отображение / не проходит схему
  ``calibration-profile/1``;
- ``evidence.flags`` содержит ``ceiling`` (В2-A 2b): замер на ceiling-сете
  (``golden_median_score >= 0.999 ∧ golden_dispersion == 0``) НЕ различает
  конфигурации — approve без явного решения оператора запрещён; обход —
  ``--ceiling-ok`` (или ``--force``) с ``--reason`` и записью решения в
  аудит ``profiles_dir/approve_audit.jsonl`` (append-only, ДО носителей);
- ``calibrated_for`` пуст или неполон: обязательны ``model_id`` И ``digest``
  (F-2а: approve без применимости легитимировал бы ложный «зелёный»);
- ``--force`` (осознанный обход пустого факта) — только с ``--reason`` и
  записью решения в аудит ``profiles_dir/approve_audit.jsonl`` (append-only,
  ДО касания носителей);
- класса нет в реестре, либо это rule-класс (``local-only``: ``rule: zone``) —
  калибровке не подлежит (комментарий реестра).

``--stale`` меняет операцию на понижение в ``stale`` обоих носителей —
делегирует ``drift.t1_writeback`` (факт полки для понижения не нужен).
``--confirm`` — Operator Gate: без него только план (dry-run по умолчанию).
Прод-``registry/model_classes.yaml`` правит ТОЛЬКО этот CLI (оператор через
``--confirm``); тесты работают на tmp-копиях.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ai_workspace.calibration import profiles
from ai_workspace.calibration.drift import (
    _load_yaml_mapping,
    _write_yaml_atomic,
    t1_writeback,
)
from ai_workspace.tools import golden_run

__all__ = [
    "AUDIT_FILENAME",
    "CEILING_FLAG",
    "DEFAULT_PROFILES_DIR",
    "DEFAULT_REGISTRY_PATH",
    "append_force_audit",
    "approve_writeback",
    "main",
]

#: Каталог профилей по умолчанию (как probe_run: ``calibration/profiles``)
DEFAULT_PROFILES_DIR: Path = golden_run.AI_WORKSPACE_DIR / "calibration" / "profiles"

#: Реестр классов по умолчанию (двухносительная операция, F3)
DEFAULT_REGISTRY_PATH: Path = (
    golden_run.AI_WORKSPACE_DIR / "registry" / "model_classes.yaml"
)

#: Аудит-журнал решений ``--force``/``--ceiling-ok`` (append-only JSONL)
AUDIT_FILENAME = "approve_audit.jsonl"

#: 2b (В2-A «Достоверность»): флаг ceiling-сета в ``evidence.flags`` профиля
CEILING_FLAG = "ceiling"

_REFUSED_EXIT = 2        # fail-closed отказ: предусловия не выполнены, ничего не писалось
_WRITE_FAILED_EXIT = 1   # сбой записи: носители откачены (атомарность), fail-loud


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="profile-approve",
        description="Утверждение профиля калибровки: оба носителя атомарно "
                    "(решение — оператор P5; CLI только исполняет fail-closed)",
    )
    parser.add_argument(
        "--profile", required=True,
        help="profile_id утверждаемого профиля",
    )
    parser.add_argument(
        "--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR,
        help="каталог профилей (по умолчанию calibration/profiles)",
    )
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY_PATH,
        help="путь YAML реестра классов (по умолчанию registry/model_classes.yaml)",
    )
    parser.add_argument(
        "--stale", action="store_true",
        help="понизить ОБА носителя в stale (делегирует drift.t1_writeback)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="escape-хатч F-2а: применить approve с неполным calibrated_for; "
             "требует --reason и протоколируется в аудит",
    )
    parser.add_argument(
        "--reason", default=None,
        help="обоснование решения оператора (обязательно с --force и --ceiling-ok)",
    )
    parser.add_argument(
        "--ceiling-ok", action="store_true",
        help="escape-хатч ceiling (В2-A 2b): применить профиль, замеренный на "
             "ceiling-сете (score>=0.999/disp=0 не различает конфигурации); "
             "требует --reason и протоколируется в аудит",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Operator Gate: применить запись (без флага — dry-run план)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только план (поведение по умолчанию; ничего не пишется)",
    )
    return parser


def _refuse(message: str) -> int:
    """Отказ fail-closed: сообщение в stderr, exit 2, никаких записей."""
    print(f"ОТКАЗ: {message}", file=sys.stderr)
    return _REFUSED_EXIT


def approve_writeback(
    profiles_dir: Path | str,
    profile_id: str,
    registry_path: Path | str,
    *,
    now: datetime | None = None,
) -> int:
    """Атомарно применить approve к ОБЕИМ носителям (паттерн t1_writeback, F3).

    Профиль ``<profile_id>.yaml`` → ``status: calibrated, version+1,
    updated_at``; реестр ``model_classes.<model_class>`` →
    ``calibration_status: calibrated`` + ``calibrated_for`` (из профиля) +
    ``active_profile``. Оба документа готовятся заранее, затем запись
    профиля → запись реестра; сбой второй записи откатывает профиль к
    исходным байтам и падает дальше (половинчатого состояния не остаётся).
    Возвращает новую version профиля.
    """
    profile_path = Path(profiles_dir) / f"{profile_id}.yaml"
    reg_path = Path(registry_path)
    profile_doc = _load_yaml_mapping(profile_path)
    registry_doc = _load_yaml_mapping(reg_path)

    model_class = profile_doc.get("model_class")
    classes = registry_doc.get("model_classes")
    if not isinstance(classes, Mapping) or model_class not in classes:
        raise ValueError(f"класс модели отсутствует в реестре: {model_class!r}")

    cal = profile_doc.get("calibrated_for")
    cal = cal if isinstance(cal, Mapping) else {}
    try:
        new_version = int(profile_doc.get("version", 1)) + 1
    except (TypeError, ValueError):
        raise ValueError(
            f"некорректная version профиля: {profile_doc.get('version')!r}"
        ) from None

    stamp = profiles._utc_iso(now if now is not None else datetime.now(timezone.utc))
    new_profile = dict(profile_doc)
    new_profile["status"] = "calibrated"
    new_profile["version"] = new_version
    new_profile["updated_at"] = stamp

    new_registry = dict(registry_doc)
    new_classes = dict(classes)
    new_class = dict(new_classes[model_class])
    new_class["calibration_status"] = "calibrated"
    new_class["calibrated_for"] = {
        "model_id": cal.get("model_id"),
        "digest": cal.get("digest"),
    }
    new_class["active_profile"] = profile_id
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
    return new_version


def append_force_audit(
    profiles_dir: Path | str,
    profile_id: str,
    model_class: str,
    reason: str,
    *,
    now: datetime | None = None,
    action: str = "force_approve",
) -> Path:
    """Протоколировать решение оператора в аудит (append-only JSONL).

    ``action``: ``force_approve`` (F-2а, обход пустого факта) или
    ``ceiling_approve`` (В2-A 2b, обход ceiling-гейта). Запись решения
    ПЕРВИЧНА — ДО касания носителей: escape легитимен только с протоколом;
    исход применения виден по носителям и коду возврата CLI, аудит хранит
    само решение оператора.
    """
    path = Path(profiles_dir) / AUDIT_FILENAME
    record = {
        "ts": profiles._utc_iso(
            now if now is not None else datetime.now(timezone.utc)
        ),
        "action": action,
        "profile_id": profile_id,
        "model_class": model_class,
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _profile_flags(doc: Mapping) -> list[str]:
    """Флаги замера из ``evidence.flags`` (2b); нет/не список → пусто."""
    evidence = doc.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    raw = evidence.get("flags")
    if not isinstance(raw, list):
        return []
    return [str(f) for f in raw]


def _print_plan(
    args: argparse.Namespace, doc: Mapping, model_class: str, missing: list[str],
) -> None:
    """Dry-run: план двухносительной операции без единой записи."""
    profile_path = args.profiles_dir / f"{args.profile}.yaml"
    if args.stale:
        print("== profile-approve --stale: ПЛАН (dry-run; ничего не пишется) ==")
        print(f"  профиль:  {profile_path} → status: stale")
        print(f"  реестр:   {args.registry} → {model_class}.calibration_status: stale")
        print("  операция: drift.t1_writeback — оба носителя атомарно")
    else:
        cal = doc.get("calibrated_for")
        cal = cal if isinstance(cal, Mapping) else {}
        print("== profile-approve: ПЛАН (dry-run; ничего не пишется) ==")
        print(f"  профиль:  {profile_path}")
        print(f"  статус:   {doc.get('status')!r} → calibrated "
              f"(version {doc.get('version')} → {int(doc.get('version', 1)) + 1})")
        print(f"  реестр:   {args.registry}")
        print(f"    {model_class}.calibration_status: calibrated")
        print(f"    {model_class}.calibrated_for: "
              f"{{model_id: {cal.get('model_id')!r}, digest: {cal.get('digest')!r}}}")
        print(f"    {model_class}.active_profile: {args.profile}")
        if missing:
            print(f"  ⚠ calibrated_for неполон ({', '.join(missing)}) — путь --force: "
                  f"запись решения в аудит {args.profiles_dir / AUDIT_FILENAME}")
        if CEILING_FLAG in _profile_flags(doc):
            print(f"  ⚠ замер на ceiling-сете ({CEILING_FLAG}: score>=0.999/disp=0 — "
                  "не различает конфигурации) — путь --ceiling-ok: "
                  f"запись решения в аудит {args.profiles_dir / AUDIT_FILENAME}")
    print("применить: --confirm (Operator Gate: решение оператора P5)")


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    """CLI profile-approve. 0 — план/успех; 2 — fail-closed отказ (ничего не
    написано); 1 — сбой записи (носители откачены, fail-loud)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ── fail-closed предусловия (и для dry-run: невыполнимый approve не
    #    планируется вовсе; ничего не пишется, включая аудит) ──
    doc = profiles.load_profile(args.profiles_dir, args.profile)
    if doc is None:
        return _refuse(f"профиль {args.profile!r} не найден в {args.profiles_dir}")
    if not isinstance(doc, Mapping):
        return _refuse(f"файл профиля {args.profile!r} — не YAML-отображение")
    findings = profiles.validate_profile(dict(doc))
    if findings:
        details = "; ".join(f"{f.path}: {f.message}" for f in findings)
        return _refuse(f"профиль не проходит схему: {details}")
    model_class = str(doc["model_class"])
    try:
        int(doc.get("version", 1))
    except (TypeError, ValueError):
        return _refuse(f"некорректная version профиля: {doc.get('version')!r}")

    # реестр: читаемость + класс существует + калибруем ли (rule-класс — нет)
    try:
        registry_doc = _load_yaml_mapping(args.registry)
    except Exception as exc:  # noqa: BLE001 — носитель недоступен: применять не к чему
        return _refuse(f"реестр не читается ({args.registry}): {exc}")
    classes = registry_doc.get("model_classes")
    spec = classes.get(model_class) if isinstance(classes, Mapping) else None
    if not isinstance(spec, Mapping):
        return _refuse(f"класс модели отсутствует в реестре: {model_class!r}")
    if "rule" in spec and "shelf" not in spec:
        return _refuse(
            f"класс {model_class!r} не подлежит калибровке (rule-класс; "
            "реестр: local-only)"
        )

    # факт полки (F-2а): approve заявляет применимость — model_id+digest обязательны
    cal = doc.get("calibrated_for")
    cal = cal if isinstance(cal, Mapping) else {}
    missing = [
        field for field in ("model_id", "digest")
        if not str(cal.get(field) or "").strip()
    ]
    forced = False
    if missing and not args.stale:
        if not args.force:
            return _refuse(
                f"calibrated_for неполон (пусты: {', '.join(missing)}) — approve "
                "запрещён (fail-closed, F-2а); осознанный обход: --force --reason "
                "… (решение протоколируется в аудит)"
            )
        reason = str(args.reason or "").strip()
        if not reason:
            return _refuse(
                "--force требует --reason (решение оператора протоколируется "
                "в аудит)"
            )
        forced = True

    # ── 2b (В2-A «Достоверность»): ceiling-сет не различает конфигурации —
    #    approve без явного решения оператора запрещён (dry-run тоже: не
    #    планируем невыполнимый approve; ничего не пишется, включая аудит) ──
    ceiling = CEILING_FLAG in _profile_flags(doc) and not args.stale
    ceiling_ok = False
    if ceiling:
        if not (args.ceiling_ok or args.force):
            return _refuse(
                f"профиль замерен на ceiling-сете (флаг {CEILING_FLAG!r}: "
                "golden_median_score>=0.999, golden_dispersion=0 — метрика не "
                "различает конфигурации): approve запрещён (В2-A 2b); осознанное "
                "решение оператора: --ceiling-ok --reason … (протоколируется "
                "в аудит)"
            )
        reason = str(args.reason or "").strip()
        if not reason:
            return _refuse(
                "--ceiling-ok/--force при ceiling требует --reason (решение "
                "оператора протоколируется в аудит)"
            )
        ceiling_ok = True

    if not args.confirm:
        _print_plan(args, doc, model_class, missing)
        return 0

    # ── --confirm: оператор применил решение (P5) — исполняем атомарно ──
    try:
        if args.stale:
            t1_writeback(args.profiles_dir, args.profile, args.registry, model_class)
            print("== profile-approve --stale: ПРИМЕНЕНО (drift.t1_writeback) ==")
            print(f"  профиль: {args.profiles_dir / (args.profile + '.yaml')} "
                  "→ status: stale")
            print(f"  реестр:  {args.registry} → "
                  f"{model_class}.calibration_status: stale")
            return 0
        if forced:
            audit_path = append_force_audit(
                args.profiles_dir, args.profile, model_class, reason, now=now,
            )
            print(f"аудит --force: {audit_path} (решение записано до носителей)")
        if ceiling_ok:
            audit_path = append_force_audit(
                args.profiles_dir, args.profile, model_class, reason, now=now,
                action="ceiling_approve",
            )
            print(f"аудит --ceiling-ok: {audit_path} (решение записано до носителей)")
        new_version = approve_writeback(
            args.profiles_dir, args.profile, args.registry, now=now,
        )
    except Exception as exc:  # noqa: BLE001 — откат выполнен внутри; fail-loud наружу
        print(
            f"ОТКАЗ: операция не применена, носители откачены ({exc})",
            file=sys.stderr,
        )
        return _WRITE_FAILED_EXIT
    print("== profile-approve: ПРИМЕНЕНО ==")
    print(f"  профиль: {args.profiles_dir / (args.profile + '.yaml')} → "
          f"status: calibrated, version: {new_version - 1} → {new_version}")
    print(f"  реестр:  {args.registry} → {model_class}: calibration_status="
          f"calibrated, active_profile={args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
