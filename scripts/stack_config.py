#!/usr/bin/env python3
"""Редактор stack.settings.yaml — единой НЕсекретной точки конфигурации стека (Ф5).

trace_id: arch-2026-10-10-ws-airgap-layers. Паттерн (бэкап .trash/ + temp+os.replace +
пост-валидация с откатом + regex-патч с сохранением комментариев) скопирован по образцу
scripts/quotas_set.py; общее ядро выносить при 3-м потребителе (R7).

Команды: show · get KEY [--quiet] · set KEY=VALUE [--apply] · validate
Exit: 0 ok · 2 usage/IO · 3 валидация · 4 нет изменений · 5 файл битый YAML
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML не установлен (нужен python3-yaml)", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
SETTINGS, DOTENV, TRASH = ROOT / "stack.settings.yaml", ROOT / ".env", ROOT / ".trash"
# dotted-key -> section, leaf, env-var, .env-key, default ("" = авто-детект)
KEYS: dict[str, tuple[str, str, str, str, str]] = {
    "ws.local_model": ("ws", "local_model", "WS_LOCAL_MODEL", "WS_LOCAL_MODEL",
                       "qwen3:30b-a3b-instruct-2507-q4_K_M"),
    "ws.local_ollama_base": ("ws", "local_ollama_base", "WS_LOCAL_OLLAMA_BASE",
                             "WS_LOCAL_OLLAMA_BASE", "mcp-knowledge-ollama:11434"),
    "gateway.max_parallel": ("gateway", "max_parallel", "LITELLM_MAX_PARALLEL",
                             "LITELLM_MAX_PARALLEL", ""),
}


def die(code: int, msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def read_text() -> str | None:
    return SETTINGS.read_text(encoding="utf-8") if SETTINGS.exists() else None


def parse(text: str, *, strict: bool) -> dict:
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        if strict:
            print(f"WARN: stack.settings.yaml — YAML parse error ({exc})", file=sys.stderr)
            sys.exit(5)
        raise


def file_get(data: dict, key: str) -> str | None:
    sec_name, leaf, *_ = KEYS[key]
    sec = data.get(sec_name)
    val = sec.get(leaf) if isinstance(sec, dict) else None
    return None if val is None else str(val)


def dotenv_get(key: str) -> str | None:
    dotkey = KEYS[key][3]
    if not DOTENV.exists():
        return None
    for line in DOTENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{dotkey}="):
            return line.split("=", 1)[1].strip() or None
    return None


def resolve(key: str, data: dict | None) -> tuple[str, str]:
    env = os.environ.get(KEYS[key][2])
    if env:
        return env, "env"
    if data is not None and (fv := file_get(data, key)) is not None:
        return fv, "file"
    if (dv := dotenv_get(key)) is not None:
        return dv, ".env"
    return KEYS[key][4], "default"


def validate_value(key: str, value: str) -> None:
    if key not in KEYS:
        die(3, f"ключ вне whitelist: {key!r} (допустимо: {', '.join(sorted(KEYS))})")
    if key == "gateway.max_parallel" and value and not (re.fullmatch(r"\d+", value) and int(value) >= 1):
        die(3, f"gateway.max_parallel: требуется int ≥ 1 (I13), получено {value!r}")


def patch_text(text: str, section: str, leaf: str, value: str) -> tuple[str, bool]:
    """Точечная правка leaf внутри section; комментарии/порядок сохраняются."""
    lines = text.splitlines(keepends=True)
    sec_re = re.compile(rf"^{re.escape(section)}\s*:\s*(#.*)?\n?$")
    leaf_re = re.compile(rf"^(\s+)(#\s*)?{re.escape(leaf)}\s*:\s*(.*?)(\n?)$")
    sec_idx = next((i for i, ln in enumerate(lines) if sec_re.match(ln)), None)
    if sec_idx is None:
        return text, False
    for j in range(sec_idx + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace():
            break
        if (m := leaf_re.match(lines[j])):
            indent, _c, rest, nl = m.groups()
            inline = (re.match(r"^(.*?)(\s+#.*)$", rest) or [None, None, ""])[2] \
                if re.match(r"^(.*?)(\s+#.*)$", rest) else ""
            new = f"{indent}{leaf}: {value}{inline}{nl or chr(10)}"
            if new == lines[j]:
                return text, False
            lines[j] = new
            return "".join(lines), True
    lines.insert(sec_idx + 1, f"  {leaf}: {value}\n")
    return "".join(lines), True


def cmd_show(_a) -> int:
    text = read_text()
    data = None
    if text is not None:
        try:
            data = parse(text, strict=False)
        except yaml.YAMLError as exc:
            print(f"ERROR: stack.settings.yaml — YAML parse error ({exc})", file=sys.stderr)
            return 2
    print(f"stack.settings.yaml ({SETTINGS}) — эффективный конфиг (источник каждого ключа):")
    for key in KEYS:
        val, src = resolve(key, data)
        print(f"  {key:<24} = {(val or '(авто-детект)'):<46} [{src}]")
    return 0


def cmd_get(a) -> int:
    if a.key not in KEYS:
        die(2, f"ключ вне whitelist: {a.key!r}")
    text = read_text()
    val = None if text is None else file_get(parse(text, strict=True), a.key)
    print(val or "" if a.quiet else f"{a.key}={val or ''}")
    return 0


def cmd_validate(_a) -> int:
    text = read_text()
    if text is None:
        print("validate: файла нет (каскад упадёт на .env/default)", file=sys.stderr)
        return 0
    data = parse(text, strict=True)
    for key in KEYS:
        if (val := file_get(data, key)) is not None:
            validate_value(key, val)
    print("validate: OK")
    return 0


def cmd_set(a) -> int:
    if "=" not in a.assignment:
        die(2, "формат: set KEY=VALUE")
    key, _, value = a.assignment.partition("=")
    key, value = key.strip(), value.strip()
    validate_value(key, value)
    text = read_text()
    if text is None:
        die(2, f"нет файла {SETTINGS}")
    new_text, changed = patch_text(text, KEYS[key][0], KEYS[key][1], value)
    if not changed:
        print(f"без изменений: {key}={value} (идемпотентно)")
        return 4
    if not a.apply:
        sys.stdout.writelines(difflib.unified_diff(
            text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile="stack.settings.yaml (текущий)", tofile="stack.settings.yaml (после)", n=1))
        print(f"\n[dry-run] {key} = {value}  (применить: добавь --apply)")
        return 0
    TRASH.mkdir(exist_ok=True)
    bak = TRASH / f"stack.settings.yaml.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(SETTINGS, bak)
    tmp = SETTINGS.with_name("stack.settings.yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, SETTINGS)
    try:
        parse(SETTINGS.read_text(encoding="utf-8"), strict=False)
    except yaml.YAMLError as exc:
        shutil.copy2(bak, SETTINGS)
        die(3, f"пост-валидация отказала ({exc}); откат из {bak}")
    print(f"OK: {key} = {value}  (бэкап: {bak.relative_to(ROOT)})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="stack_config.py", description="Редактор stack.settings.yaml (Ф5)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    g = sub.add_parser("get"); g.add_argument("key"); g.add_argument("--quiet", action="store_true")
    s = sub.add_parser("set"); s.add_argument("assignment"); s.add_argument("--apply", action="store_true")
    sub.add_parser("validate")
    a = p.parse_args()
    return {"show": cmd_show, "get": cmd_get, "set": cmd_set, "validate": cmd_validate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
