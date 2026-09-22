#!/usr/bin/env python3
r"""E5-регресс-тест реестра охвата источников ошибок (code-2026-09-22-003, Ф2).

Канон self-improvement-loop.md §3 E5: «охват деградирует со временем» —
тест делает КРАСНЫМ появление нового источника (маркер/сервис/cron/скрипт)
без строки в docs/operations/error-sources.md.

Что сканирует:
  1. Маркеры  — re \[([A-Z][A-Z_]+)\] по mcp_server/src/**/*.py и kb-console/src/**/*.py
  2. Сервисы  — верхнеуровневые ключи services: в docker-compose.yml
  3. Cron     — блоки ansible.builtin.cron (name:+job:, без env) в deploy.yml + errors.yml
  4. Скрипты  — scripts/*.sh + scripts/*.py

Формат реестра: `| <source_id> | ... | covered |` или `| <source_id> | ... | gap: причина |`
(gap не валит тест — реестр = бэклог дыр — но виден в выводе).

Запуск: `make test-errors` / `.venv/bin/python -m pytest tests/ -v`;
прямой (prod-errors-sources): `python3 tests/test_error_sources.py` —
exit 0 «COVERAGE OK», non-zero + список дыр при uncovered>0.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "operations" / "error-sources.md"

MARKER_RE = re.compile(r"\[([A-Z][A-Z_]+)\]")
# уровни логгирования — не маркеры событий (живут в поле level схемы)
NON_MARKERS = {"INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL", "HTTP", "TLS", "SSL", "JSON", "API", "CLI"}


def scan_markers() -> dict:
    """{marker:ID → [файлы]} по src обоих приложений (минус __pycache__/.egg-info)."""
    found = {}
    for base in (ROOT / "mcp_server" / "src", ROOT / "kb-console" / "src"):
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            if "__pycache__" in py.parts or ".egg-info" in py.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in MARKER_RE.finditer(text):
                tag = m.group(1)
                if tag not in NON_MARKERS:
                    found.setdefault(f"marker:{tag}", []).append(str(py.relative_to(ROOT)))
    return found


def scan_services() -> dict:
    """Верхнеуровневые ключи services: из docker-compose.yml (regex по отступу)."""
    compose = ROOT / "docker-compose.yml"
    found = {}
    in_services = False
    for line in compose.read_text(encoding="utf-8").splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services:
            m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$", line)
            if m:
                found[f"service:{m.group(1)}"] = [str(compose.relative_to(ROOT))]
            elif line and not line.startswith((" ", "#")):
                in_services = False  # блок services закончился
    return found


def scan_crons() -> dict:
    """{cron:<name> → [плейбук]} из блоков ansible.builtin.cron (env-записи пропускаем)."""
    found = {}
    for pb in (ROOT / "ansible" / "playbooks" / "deploy.yml", ROOT / "ansible" / "playbooks" / "errors.yml"):
        if not pb.is_file():
            continue
        in_cron, name, has_job = False, None, False
        for line in pb.read_text(encoding="utf-8").splitlines():
            if "ansible.builtin.cron:" in line:
                in_cron, name, has_job = True, None, False
                continue
            if in_cron:
                if re.match(r"^\s*- name:", line):  # следующая задача — блок закрыт
                    if name and has_job:
                        found[f"cron:{name}"] = [str(pb.relative_to(ROOT))]
                    in_cron, name, has_job = False, None, False
                    continue
                m_name = re.match(r'^\s+name:\s*"?(.+?)"?\s*$', line)
                m_job = re.match(r"^\s+job:\s*", line)
                if m_name and name is None:
                    name = m_name.group(1)
                elif m_job:
                    has_job = True
        if in_cron and name and has_job:
            found[f"cron:{name}"] = [str(pb.relative_to(ROOT))]
    return found


def scan_scripts() -> dict:
    """scripts/*.sh + scripts/*.py (минус __pycache__) — по basename."""
    found = {}
    scripts = ROOT / "scripts"
    if not scripts.is_dir():
        return found
    for f in sorted(scripts.iterdir()):
        if f.suffix in (".sh", ".py") and "__pycache__" not in f.parts:
            found[f"script:{f.name}"] = [str(f.relative_to(ROOT))]
    return found


def parse_registry(path: Path = REGISTRY) -> dict:
    """{source_id → (status, raw_line)}; строки таблиц реестра."""
    registry = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) >= 4 and cols[0] and not cols[0].startswith(("<", "-", "source_id")) \
                and not set(cols[0]) <= {"-", ":", " "}:
            sid = cols[0]
            status = cols[-1].lower()
            if status.startswith(("covered", "gap")):
                parts = status.split(":", 1)
                registry[sid] = (parts[0], parts[1] if len(parts) > 1 else "")
    return registry


def coverage_report() -> tuple:
    """→ (uncovered: [(id, где найден)], gaps: [(id, причина)]) — все группы сразу."""
    registry = parse_registry()
    uncovered, gaps = [], []
    for kind, scan in (("marker", scan_markers), ("service", scan_services),
                       ("cron", scan_crons), ("script", scan_scripts)):
        for sid, where in sorted(scan().items()):
            if sid not in registry:
                uncovered.append((sid, ", ".join(where[:2])))
    for sid, (status, reason) in sorted(registry.items()):
        if status == "gap" and not (sid.startswith(("script:errors_", "cron:MCP Knowledge — errors"))):
            # self-строки коллектора — ожидаемые (P2-2), в бэклог-вывод не дублируем
            if sid.startswith(("script:errors_",)):
                continue
            gaps.append((sid, reason or ""))
    return uncovered, gaps


def main() -> int:
    registry = parse_registry()
    uncovered, gaps = coverage_report()
    n_cov = sum(1 for s, _ in registry.values() if s == "covered")
    print(f"Реестр {REGISTRY.relative_to(ROOT)}: {n_cov} covered / {len(gaps)} gap(+self) "
          f"/ всего строк {len(registry)}")
    for group, scan in (("маркеры", scan_markers), ("сервисы", scan_services),
                        ("cron", scan_crons), ("скрипты", scan_scripts)):
        print(f"  скан {group}: {len(scan())} источников")
    if gaps:
        print("\nБэклог gap (не валит проверку):")
        for sid, reason in gaps:
            print(f"  - {sid}: {reason}")
    if uncovered:
        print("\nНЕОХВАЧЕННЫЕ ИСТОЧНИКИ (красный):")
        for sid, where in uncovered:
            print(f"  ✖ {sid} ({where}) — добавь строку в docs/operations/error-sources.md "
                  f"(механизм сбора или gap с причиной)")
        print(f"\nCOVERAGE FAIL: {len(uncovered)} uncovered")
        return 1
    print("\nCOVERAGE OK")
    return 0


# ── pytest-обёртки (тот же движок; прод-режим = __main__) ──

def test_all_markers_covered():
    uncovered, _ = coverage_report()
    assert not uncovered, "Неохваченные источники: " + "; ".join(f"{s} ({w})" for s, w in uncovered)


def test_registry_parse_has_rows():
    registry = parse_registry()
    assert len(registry) >= 40, f"реестр подозрительно мал: {len(registry)} строк"
    assert any(s.startswith("covered") for s, _ in registry.values())
    assert any(s.startswith("gap") for s, _ in registry.values()), "gap-строки — часть честного реестра"


def test_self_reference_gap_present():
    """P2-2: источники самой системы (errors_*.py, errors-collect cron) — gap:self."""
    registry = parse_registry()
    for sid in ("script:errors_collect.py", "script:errors_report.py",
                "script:errors_prune.py", "cron:MCP Knowledge — errors collector"):
        assert sid in registry, f"нет само-референс строки {sid} (P2-2)"
        assert registry[sid][0] == "gap", f"{sid} должен быть gap:self (анти-рекурсия, P2-2)"


if __name__ == "__main__":
    sys.exit(main())
