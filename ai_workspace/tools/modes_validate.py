"""CLI схем-валидации режимов (Ф3.5a-2, контур (а) спеки mode-engine §4).

Запуск: ``python -m ai_workspace.tools.modes_validate [--file PATH | --dir DIR]``.
Каталог по умолчанию — ``ai_workspace/modes``; реестр — ``ai_workspace/registry``.

Выход: ``0`` — ошибок нет (или режимов нет); ``1`` — есть хотя бы один error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ai_workspace.orchestrator.mode_schema import Finding, validate_schema
from ai_workspace.registry import Registry

AI_WORKSPACE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODES_DIR = AI_WORKSPACE_DIR / "modes"
DEFAULT_REGISTRY_DIR = AI_WORKSPACE_DIR / "registry"


def _validate_file(path: Path, registry: Registry) -> bool:
    """Валидировать один YAML-файл режима; True — есть error."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"❌ {path}: не читается или битый YAML: {exc}")
        return True
    findings: list[Finding] = validate_schema(doc, registry)
    if not findings:
        print(f"✅ {path}: ok")
        return False
    for f in findings:
        print(f"{f.code}: {f.message} ({f.path})")
    return any(f.severity == "error" for f in findings)


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код выхода (0/1)."""
    parser = argparse.ArgumentParser(
        prog="modes-validate",
        description="Схема-валидация YAML-режимов AI-верстака (Ф3.5a-2)",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--file", type=Path, help="один YAML-файл режима")
    target.add_argument("--dir", type=Path, help="каталог режимов (по умолчанию ai_workspace/modes)")
    args = parser.parse_args(argv)

    registry = Registry(DEFAULT_REGISTRY_DIR)
    if args.file is not None:
        return 1 if _validate_file(args.file, registry) else 0

    modes_dir = args.dir if args.dir is not None else DEFAULT_MODES_DIR
    files = sorted({*modes_dir.glob("*.yaml"), *modes_dir.glob("*.yml")})
    if not files:
        print("ℹ️ режимов нет")
        return 0
    has_error = False
    for path in files:
        has_error = _validate_file(path, registry) or has_error
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
