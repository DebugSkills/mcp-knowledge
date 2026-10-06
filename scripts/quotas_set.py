#!/usr/bin/env bash
''''exec python3 -- "$0" "$@" # '''
"""Хост-редактор квот participant-ролей (Ф4.5c-1): безопасная правка quotas.yaml.

trace_id: arch-2026-10-05-ai-workspace. Оператор правит SSOT
``ai_workspace/registry/quotas.yaml`` ВРУЧНУЮ с хоста: в контейнере консоли
``ai_workspace`` нет и реестр не смонтирован (осознанное решение). Этот CLI —
безопасная обёртка ручной правки: dry-run по умолчанию, точечная правка
значений с сохранением комментариев/порядка (yaml.safe_dump уничтожил бы
документацию D1-D8/PLACEHOLDER; ruamel.yaml в окружении нет), бэкап в
``.trash/`` + атомарная запись (temp + os.replace) + пост-валидация
``validate_quotas`` с откатом из бэкапа (fail-closed).

Рантайм подхватывает правку САМ по mtime (``Registry.reload_if_changed()``) —
рестарт консоли НЕ нужен. git-коммит не делается: правку коммитит оператор.

Запуск:
    make quotas-show                                  # роли + бюджеты
    make quotas-show ARGS="--json"                    # то же, JSON
    make quotas-set ARGS="set --role guest --priority low"  # dry-run (файл цел)
    make quotas-set ARGS="set --role member --priority high --tokens 300000 --apply"

Exit-коды: 0 — ок (dry-run с изменениями тоже 0); 2 — usage/IO; 3 — валидация
отказала (неверный --priority/--tokens/--conc/--grants/--budget-ext, роль не
найдена, битый патч-анкор, пост-проверка после записи — с откатом из бэкапа);
4 — изменений нет (идемпотентно: файл не пишется, бэкап не создаётся).
"""

import argparse
import copy
import dataclasses
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # ai_workspace импортируется из репо

from ai_workspace.registry import Registry, RegistryError
from ai_workspace.registry.quotas import PRIORITIES, validate_quotas

EXIT_OK = 0
EXIT_USAGE_IO = 2
EXIT_VALIDATION = 3
EXIT_NO_CHANGES = 4

_UNSET = object()  # «флаг не передан» (отличать от None-значения --tokens none)


class PatchAnchorError(Exception):
    """Анкор точечной правки не найден — патч считается битым (exit 3)."""


def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _emit(args: argparse.Namespace, payload: dict) -> None:
    """JSON-first вывод; в человеко-режиме — тот же смысл, но читаемо."""
    if getattr(args, "json", False):
        _out(payload)
        return
    text = _humanize(payload)
    print(text, end="" if text.endswith("\n") else "\n")


def _findings_json(findings: list) -> list[dict]:
    return [dataclasses.asdict(f) for f in findings]


# ---------------------------------------------------------------- точечная правка

def _split_value_comment(rest: str) -> tuple[str, str]:
    """Разделить хвост после ``field:`` на значение и комментарий ``# ...``.

    Значения quotas.yaml (enum/int/flow-список) не содержат `` #`` — первый
    `` #`` и есть начало комментария.
    """
    idx = rest.find(" #")
    if idx != -1:
        return rest[:idx], rest[idx:]
    stripped = rest.lstrip()
    if stripped.startswith("#"):
        return "", rest
    return rest, ""


def _find_line(lines: list[str], pattern: re.Pattern[str], start: int) -> int | None:
    for i in range(start, len(lines)):
        if pattern.match(lines[i]):
            return i
    return None


def _patch_field(
    lines: list[str], section: str, key: str, field: str, new_repr: str
) -> list[str] | None:
    """Точечно заменить значение ``{section}.{key}.{field}``.

    Анкоры строгие: ``^section:$`` → ``^  key:$`` → ``^    field:``; всё вне
    заменённой строки — байт-в-байт (комментарии, порядок, отступы). Вернуть
    новую копию строк или None, если анкор не найден.
    """
    sec_re = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    key_re = re.compile(rf"^  {re.escape(key)}:\s*(#.*)?$")
    field_re = re.compile(rf"^    {re.escape(field)}:(?P<rest>.*)$")

    i = _find_line(lines, sec_re, 0)
    if i is None:
        return None
    j = _find_line(lines, key_re, i + 1)
    if j is None:
        return None
    for k in range(i + 1, j):
        if lines[k][:1] not in ("", " ", "#"):  # вышли из секции — анкор битый
            return None
    k = j + 1
    while k < len(lines):
        line = lines[k]
        if line.strip() and (len(line) - len(line.lstrip(" "))) <= 2:
            break  # конец блока key (следующий ключ с отступом <= 2)
        m = field_re.match(line)
        if m:
            _, comment = _split_value_comment(m.group("rest"))
            new_line = f"    {field}: {new_repr}" + comment
            out = lines.copy()
            out[k] = new_line
            return out
        k += 1
    return None


def build_new_text(orig_text: str, patches: list[tuple[str, str, str, str]]) -> str:
    """Применить точечные патчи ``(section, key, field, repr)`` к тексту.

    Трейлинг-newline сохраняется; вне заменённых значений — байт-в-байт.
    """
    lines = orig_text.splitlines()
    for section, key, field, new_repr in patches:
        new_lines = _patch_field(lines, section, key, field, new_repr)
        if new_lines is None:
            raise PatchAnchorError(
                f"анкор не найден: {section}.{key}.{field} "
                f"(структура файла отличается от ожидаемой — прави вручную)"
            )
        lines = new_lines
    new_text = "\n".join(lines)
    if orig_text.endswith("\n"):
        new_text += "\n"
    return new_text


def _atomic_write(path: Path, text: str) -> None:
    """Атомарная запись: temp в том же каталоге + os.replace."""
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _fmt_scalar(value: Any) -> str:
    """YAML-представление скаляра (int/float/None) в стиле quotas.yaml."""
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------- парсинг аргументов

def _parse_nullable_int(raw: str, minimum: int, what: str) -> int | None:
    """``N|none|null`` → int | None; диапазон и формат — сразу fail (exit 3)."""
    if raw.strip().lower() in ("none", "null"):
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{what}: ожидается целое или none, получено {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{what}: должно быть >= {minimum}, получено {value}")
    return value


def _parse_grants(raw: str) -> list[str]:
    """Comma-список классов моделей; ref-целостность проверит validate_quotas."""
    items = [g.strip() for g in raw.split(",")]
    if not items or any(not g for g in items):
        raise ValueError("--grants: непустой список классов через запятую")
    return list(dict.fromkeys(items))  # dedupe с сохранением порядка


def _parse_number(raw: str, what: str) -> int | float:
    """Число > 0 для budgets.ext.limit (int если целое, иначе float)."""
    try:
        value: int | float = int(raw.strip())
    except ValueError:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{what}: ожидается число, получено {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{what}: должно быть > 0, получено {value}")
    return value


# ---------------------------------------------------------------- show / set

def _load(registry_dir: Path) -> tuple[str, dict, dict]:
    """Прочитать quotas.yaml + классы моделей (загрузчик — registry.Registry)."""
    text = (registry_dir / "quotas.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    model_classes = Registry(registry_dir).get("model_classes")
    return text, doc, model_classes


def cmd_show(args: argparse.Namespace) -> int:
    try:
        _, doc, model_classes = _load(Path(args.registry_dir))
    except (OSError, yaml.YAMLError, RegistryError) as exc:
        _emit(args, {"ok": False, "error": f"реестр недоступен/битый: {exc}"})
        return EXIT_USAGE_IO
    findings = validate_quotas(doc, model_classes)
    if findings:
        _emit(
            args,
            {"ok": False, "error": "квоты невалидны", "findings": _findings_json(findings)},
        )
        return EXIT_VALIDATION
    payload = {
        "ok": True,
        "registry_dir": str(Path(args.registry_dir)),
        "defaults": doc["defaults"],
        "participants": doc["participants"],
        "budgets": doc["budgets"],
        "note": (
            "эффективное значение null = без личного лимита (только общий "
            "бюджет K); правка — make quotas-set (dry-run по умолчанию)"
        ),
    }
    _emit(args, payload)
    return EXIT_OK


def _validate_args(args: argparse.Namespace) -> dict[str, Any]:
    """Разобрать и проверить --priority/--tokens/--conc/--grants/--budget-ext."""
    values: dict[str, Any] = {}
    if args.priority not in PRIORITIES:
        raise ValueError(
            f"--priority: ожидается один из {sorted(PRIORITIES)}, "
            f"получено {args.priority!r}"
        )
    values["priority"] = args.priority
    if args.tokens is not None:
        values["tokens_per_day"] = _parse_nullable_int(args.tokens, 0, "--tokens")
    if args.conc is not None:
        values["conc"] = _parse_nullable_int(args.conc, 1, "--conc")
    if args.grants is not None:
        values["grants"] = _parse_grants(args.grants)
    if args.budget_ext is not None:
        values["budget_ext"] = _parse_number(args.budget_ext, "--budget-ext")
    return values


def _git_diff_stat(path: Path) -> str:
    """Подсказка оператору: git diff --stat по файлу (если репо под git)."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return ""
        res = subprocess.run(
            ["git", "-C", str(path.parent), "diff", "--stat", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


HOT_RELOAD_NOTE = (
    "рантайм подхватит правку по mtime (Registry.reload_if_changed()) — "
    "рестарт не нужен; git-коммит делает оператор"
)


def cmd_set(args: argparse.Namespace) -> int:
    registry_dir = Path(args.registry_dir)
    try:
        values = _validate_args(args)
    except ValueError as exc:
        _emit(args, {"ok": False, "error": str(exc), "findings": []})
        return EXIT_VALIDATION

    try:
        orig_text, doc, model_classes = _load(registry_dir)
    except (OSError, yaml.YAMLError, RegistryError) as exc:
        _emit(args, {"ok": False, "error": f"реестр недоступен/битый: {exc}"})
        return EXIT_USAGE_IO

    role = args.role
    participants = doc.get("participants") if isinstance(doc, dict) else None
    if not isinstance(participants, dict) or role not in participants:
        _emit(
            args,
            {
                "ok": False,
                "error": f"роль {role!r} не найдена в participants",
                "findings": [],
            },
        )
        return EXIT_VALIDATION

    # Валидация входа ДО записи — авторитет validate_quotas на новом документе.
    new_doc = copy.deepcopy(doc)
    spec = new_doc["participants"][role]
    spec["priority"] = values["priority"]
    if "tokens_per_day" in values:
        spec["tokens_per_day"] = values["tokens_per_day"]
    if "conc" in values:
        spec["conc"] = values["conc"]
    if "grants" in values:
        spec["grants"] = values["grants"]
    if "budget_ext" in values:
        new_doc["budgets"]["ext"]["limit"] = values["budget_ext"]

    findings = validate_quotas(new_doc, model_classes)
    if findings:
        _emit(
            args,
            {"ok": False, "error": "патч отклонён валидацией", "findings": _findings_json(findings)},
        )
        return EXIT_VALIDATION

    # Точечный текстовый патч (комментарии/порядок — байт-в-байт вне правок).
    patches: list[tuple[str, str, str, str]] = [
        ("participants", role, "priority", _fmt_scalar(values["priority"])),
    ]
    if "tokens_per_day" in values:
        patches.append(
            ("participants", role, "tokens_per_day", _fmt_scalar(values["tokens_per_day"]))
        )
    if "conc" in values:
        patches.append(("participants", role, "conc", _fmt_scalar(values["conc"])))
    if "grants" in values:
        grants_repr = "[" + ", ".join(values["grants"]) + "]"
        patches.append(("participants", role, "grants", grants_repr))
    if "budget_ext" in values:
        patches.append(("budgets", "ext", "limit", _fmt_scalar(values["budget_ext"])))

    try:
        new_text = build_new_text(orig_text, patches)
    except PatchAnchorError as exc:
        _emit(args, {"ok": False, "error": f"битый патч: {exc}", "findings": []})
        return EXIT_VALIDATION
    if yaml.safe_load(new_text) != new_doc:
        _emit(
            args,
            {"ok": False, "error": "битый патч: текст разошёлся с моделью", "findings": []},
        )
        return EXIT_VALIDATION

    quotas_path = registry_dir / "quotas.yaml"
    diff = "\n".join(
        difflib.unified_diff(
            orig_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{quotas_path}",
            tofile=f"b/{quotas_path}",
            lineterm="",
        )
    )

    if new_text == orig_text:
        payload = {
            "ok": True,
            "applied": False,
            "changed": False,
            "diff": "",
            "findings": [],
            "note": "идемпотентно: значения уже такие — файл не пишется, бэкапа нет",
        }
        _emit(args, payload)
        return EXIT_NO_CHANGES

    if not args.apply:
        _emit(
            args,
            {
                "ok": True,
                "applied": False,
                "changed": True,
                "diff": diff,
                "findings": [],
                "note": f"DRY-RUN: файл не изменён; повторите с --apply. {HOT_RELOAD_NOTE}",
            },
        )
        return EXIT_OK

    # --apply: бэкап → атомарная запись → пост-валидация (fail-closed откат).
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        f"quotas-pre-set-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.yaml"
    )
    backup_path.write_text(orig_text, encoding="utf-8")
    _atomic_write(quotas_path, new_text)

    restore_note = ""
    try:
        reread = quotas_path.read_text(encoding="utf-8")
        reread_doc = yaml.safe_load(reread)
        post = validate_quotas(reread_doc, model_classes)
        if post:
            raise RegistryError(
                "; ".join(f"[{f.code}] {f.path}: {f.message}" for f in post)
            )
    except Exception as exc:  # noqa: BLE001 — любой сбой = откат из бэкапа
        _atomic_write(quotas_path, orig_text)
        restore_note = f"; восстановлено из бэкапа {backup_path}"
        _emit(
            args,
            {
                "ok": False,
                "error": f"пост-валидация отказала: {exc}{restore_note}",
                "findings": [],
            },
        )
        return EXIT_VALIDATION

    git_stat = _git_diff_stat(quotas_path)
    _emit(
        args,
        {
            "ok": True,
            "applied": True,
            "changed": True,
            "diff": diff,
            "findings": [],
            "backup": str(backup_path),
            "git_diff_stat": git_stat,
            "note": HOT_RELOAD_NOTE,
        },
    )
    return EXIT_OK


# ---------------------------------------------------------------- вывод

def _humanize(payload: dict) -> str:
    """Человеко-читаемый вид JSON-пейлоада (тот же смысл)."""
    if not payload.get("ok", True):
        lines = [f"ОШИБКА: {payload.get('error', '')}"]
        for f in payload.get("findings", []):
            lines.append(f"  [{f['code']}] {f['path']}: {f['message']}")
        return "\n".join(lines) + "\n"
    if "participants" in payload:  # show
        lines = [f"quotas.yaml ({payload.get('registry_dir', '')})"]
        lines.append("participant-роли:")
        for role, spec in payload["participants"].items():
            tokens = spec["tokens_per_day"]
            conc = spec["conc"]
            tokens_h = "null (только общий K)" if tokens is None else str(tokens)
            conc_h = "null (только общий K)" if conc is None else str(conc)
            grants = ", ".join(spec["grants"])
            lines.append(
                f"  {role}: priority={spec['priority']} tokens/day={tokens_h} "
                f"conc={conc_h} grants={grants}"
            )
        lines.append("бюджеты:")
        for name, spec in payload.get("budgets", {}).items():
            lines.append(
                f"  {name}: {spec['limit']} {spec['currency']}/{spec['period']} "
                f"(per_user_mirror={spec['per_user_mirror']}, "
                f"reconcile={spec['reconcile']})"
            )
        defaults = payload.get("defaults", {})
        lines.append(f"defaults.role: {defaults.get('role', '')}")
        note = payload.get("note")
        if note:
            lines.append(f"note: {note}")
        return "\n".join(lines) + "\n"
    # set
    lines = []
    if payload.get("applied"):
        lines.append(f"APPLIED (бэкап: {payload.get('backup', '')})")
    elif payload.get("changed"):
        lines.append("DRY-RUN (файл не изменён; --apply для записи)")
    else:
        lines.append("НЕТ ИЗМЕНЕНИЙ (идемпотентно)")
    if payload.get("diff"):
        lines.append("diff:")
        lines.append(payload["diff"])
    if payload.get("git_diff_stat"):
        lines.append(f"git diff --stat:\n{payload['git_diff_stat']}")
    if payload.get("note"):
        lines.append(f"note: {payload['note']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    epilog = (
        "Примечания:\n"
        "  • dry-run по умолчанию: без --apply файл не изменяется.\n"
        "  • none|null в --tokens/--conc → YAML null (без личного лимита).\n"
        "  • --apply: бэкап .trash/quotas-pre-set-<ts>.yaml → атомарная запись\n"
        "    → пост-валидация validate_quotas; при findings — откат, exit 3.\n"
        f"  • {HOT_RELOAD_NOTE}.\n"
        "  • Комментарии и порядок ключей сохраняются (точечная правка значений;\n"
        "    ruamel.yaml в окружении нет — yaml.safe_dump НЕ используется)."
    )
    parser = argparse.ArgumentParser(
        prog="quotas_set",
        description="Хост-редактор quotas.yaml (Ф4.5c-1): dry-run/show + безопасный set",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="participant-роли + бюджеты (эффективные)")
    p_show.add_argument("--json", action="store_true", help="JSON-вывод")
    p_show.add_argument(
        "--registry-dir",
        default=str(REPO_ROOT / "ai_workspace" / "registry"),
        help="каталог YAML-реестров (по умолчанию — ai_workspace/registry репо)",
    )

    p_set = sub.add_parser("set", help="точечно изменить квоты роли/бюджета")
    p_set.add_argument("--role", required=True, help="participant-роль (admin|member|guest|…)")
    p_set.add_argument(
        "--priority",
        required=True,
        help="high|med|low (маппинг MULT-очереди, D2; проверка — exit 3)",
    )
    p_set.add_argument(
        "--tokens", help="tokens_per_day: целое >= 0 или none (null = только общий K)",
    )
    p_set.add_argument("--conc", help="conc: целое >= 1 или none (null = только общий K)")
    p_set.add_argument(
        "--grants", help="классы моделей через запятую (heavy,fast,local-only)"
    )
    p_set.add_argument(
        "--budget-ext", help="budgets.ext.limit, ₽ (число > 0, D4 глобальный hard)"
    )
    p_set.add_argument(
        "--apply",
        action="store_true",
        help="записать (бэкап + атомарно + пост-валидация); без флага — dry-run",
    )
    p_set.add_argument("--json", action="store_true", help="JSON-вывод")
    p_set.add_argument(
        "--registry-dir",
        default=str(REPO_ROOT / "ai_workspace" / "registry"),
        help="каталог YAML-реестров (по умолчанию — ai_workspace/registry репо)",
    )
    p_set.add_argument(
        "--backup-dir",
        default=str(REPO_ROOT / ".trash"),
        help="куда класть бэкап (по умолчанию — .trash репо)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "show":
        return cmd_show(args)
    return cmd_set(args)


if __name__ == "__main__":
    raise SystemExit(main())
