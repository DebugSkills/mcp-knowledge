#!/usr/bin/env bash
# preflight.sh — единый pre-push гейт mcp-knowledge (code-2026-09-22-004).
#
# ЗАЧЕМ: CI/CD в проекте нет — все проверки гоняются руками. Этот скрипт
# запускается ПЕРЕД пушем (make preflight / git hook pre-push) и покрывает
# все гейты проекта host-side (через .venv, БЕЗ зависимости от поднятого
# стека; compose/контейнерные проверки — SKIP, если стека нет).
#
# Гейты:
#   G1  lint        ruff: src+tests обоих пакетов + tests/ + errors-скрипты
#   G2  unit mcp    pytest mcp_server/tests (~1023; e2e-маркеры — по addopts)
#   G3  unit console pytest kb-console/tests (~258)
#   G4  root tests  pytest tests/ (~50: errors_lib + E5-скан)
#   G5  E5-отчёт    test_error_sources.py → COVERAGE OK (gap>0 → FAIL)
#   G6  compose     docker compose config -q (dev+prod; нет docker → SKIP)
#   G7  ansible     ansible-lint playbooks/ + --syntax-check по всем плейбукам
#   G8  shell       bash -n scripts/*.sh (+ shellcheck, если есть в PATH)
#   G9  smoke E→R   errors_collect/report/prune на свежем temp-sink (нет docker → SKIP)
#   G10 make         make -n prod-errors* (проброс не сломан)
#
# Флаги: --quick (G1+G4+G5+G10) · --full (+e2e и контейнерные test/lint при
#        поднятом стеке) · --no-smoke (без G9) · --fail-fast · --help
# Выход: exit = число упавших гейтов (0 = зелёно). Никаких rm в репо:
# temp-sink живёт в mktemp -d (системный /tmp, вне репозитория).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
QUICK=0; FULL=0; NO_SMOKE=0; FAIL_FAST=0
PASSED=0; FAILED=0; SKIPPED=0; FAILED_IDS=()

# Цвет — только при TTY (в CI/пайпе вывод чистый)
if [ -t 1 ]; then
    C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
    C_G=""; C_R=""; C_Y=""; C_B=""; C_0=""
fi

say_pass() { printf '%s[PASS]%s G%s %s · %s · %ss\n' "$C_G" "$C_0" "$1" "$2" "$3" "$4"; }
say_fail() { printf '%s[FAIL]%s G%s %s · %s · %ss\n' "$C_R" "$C_0" "$1" "$2" "$3" "$4"; }
say_skip() { printf '%s[SKIP]%s G%s %s · %s · %ss\n' "$C_Y" "$C_0" "$1" "$2" "$3" "$4"; }

# run_gate <id> <name> <skip:0|1> <cmd...>: выполняет команду, считает секунды,
# пишет одну строку результата. Вывод команды глотается (tail при FAIL — в лог).
run_gate() {
    local id="$1" name="$2" skip="$3"; shift 3
    local t0=$SECONDS log
    log="$(mktemp)"  # системный /tmp, вне репо
    if [ "$skip" = "1" ]; then
        SKIPPED=$((SKIPPED + 1))
        say_skip "$id" "$name" "предусловие не выполнено" 0
        rm -f "$log"
        return 0
    fi
    if "$@" >"$log" 2>&1; then
        PASSED=$((PASSED + 1))
        say_pass "$id" "$name" "ok" "$((SECONDS - t0))"
    else
        FAILED=$((FAILED + 1)); FAILED_IDS+=("G$id")
        say_fail "$id" "$name" "вывод ниже" "$((SECONDS - t0))"
        tail -25 "$log" | sed 's/^/    | /'
        if [ "$FAIL_FAST" = "1" ]; then
            rm -f "$log"
            echo "fail-fast: остановка после первого провала" >&2
            finish
        fi
    fi
    rm -f "$log"
}

finish() {
    echo ""
    echo "${C_B}═══ Preflight: ${C_G}${PASSED} passed${C_0} / ${C_R}${FAILED} failed${C_0} (${FAILED_IDS[*]:-}) / ${C_Y}${SKIPPED} skipped${C_0} ═══${C_0}"
    exit "$FAILED"
}

help() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --quick)     QUICK=1 ;;
        --full)      FULL=1 ;;
        --no-smoke)  NO_SMOKE=1 ;;
        --fail-fast) FAIL_FAST=1 ;;
        --help|-h)   help ;;
        *) echo "Неизвестный флаг: $arg (см. --help)" >&2; exit 64 ;;
    esac
done

cd "$ROOT"
echo "${C_B}═══ mcp-knowledge preflight · $(date -Iseconds) · mode=$( \
    [ "$FULL" = 1 ] && echo full || { [ "$QUICK" = 1 ] && echo quick || echo default; }) ═══${C_0}"

HAS_PY=1; [ -x "$PY" ] || HAS_PY=0
HAS_DOCKER=1; command -v docker >/dev/null 2>&1 || HAS_DOCKER=0
STACK_UP=0
if [ "$HAS_DOCKER" = "1" ] && docker compose ps -q mcp-server >/dev/null 2>&1 \
   && [ -n "$(docker compose ps -q mcp-server 2>/dev/null)" ]; then
    STACK_UP=1
fi

# ── G1 lint (quick/full/default) ──
run_gate 1 "lint ruff" "$([ "$HAS_PY" = 1 ] && echo 0 || echo 1)" \
    "$PY" -m ruff check mcp_server/src mcp_server/tests kb-console/src kb-console/tests \
    tests scripts/errors_collect.py scripts/errors_report.py scripts/errors_prune.py

# ── G2 unit mcp_server (default/full) ──
if [ "$QUICK" = 0 ]; then
    run_gate 2 "unit mcp_server" "$([ "$HAS_PY" = 1 ] && echo 0 || echo 1)" \
        "$PY" -m pytest mcp_server/tests -q
fi

# ── G3 unit kb-console (default/full) ──
if [ "$QUICK" = 0 ]; then
    run_gate 3 "unit kb-console" "$([ "$HAS_PY" = 1 ] && echo 0 || echo 1)" \
        "$PY" -m pytest kb-console/tests -q
fi

# ── G4 root tests (+ E5-скан внутри) ──
run_gate 4 "root tests (incl E5)" "$([ "$HAS_PY" = 1 ] && echo 0 || echo 1)" \
    "$PY" -m pytest tests/ -q

# ── G5 E5-отчёт (наглядный COVERAGE OK / список gap) ──
run_gate 5 "E5 error-sources" "$([ "$HAS_PY" = 1 ] && echo 0 || echo 1)" \
    "$PY" tests/test_error_sources.py

# ── G6 compose config (default/full; нет docker → SKIP) ──
if [ "$QUICK" = 0 ]; then
    compose_check() {
        docker compose config -q && docker compose -f docker-compose.prod.yml config -q
    }
    run_gate 6 "compose config (dev+prod)" "$([ "$HAS_DOCKER" = 1 ] && echo 0 || echo 1)" compose_check
fi

# ── G7 ansible lint + syntax-check (default/full) ──
if [ "$QUICK" = 0 ]; then
    ansible_gates() {
        make -C ansible lint || return 1
        local pb rc=0
        for pb in ansible/playbooks/*.yml; do
            ansible-playbook -i ansible/inventory/hosts.yml "$pb" --syntax-check >/dev/null 2>&1 || {
                echo "syntax-check FAIL: $pb" >&2; rc=1
            }
        done
        return "$rc"
    }
    run_gate 7 "ansible lint+syntax(9)" 0 ansible_gates
fi

# ── G8 shell: bash -n (+ shellcheck при наличии) ──
if [ "$QUICK" = 0 ]; then
    shell_gates() {
        local sh rc=0
        for sh in scripts/*.sh; do
            bash -n "$sh" || { echo "bash -n FAIL: $sh" >&2; rc=1; }
        done
        if command -v shellcheck >/dev/null 2>&1; then
            shellcheck scripts/*.sh || rc=1
        else
            echo "shellcheck не установлен — тихий SKIP (только bash -n)"
        fi
        return "$rc"
    }
    run_gate 8 "shell bash -n(+shellcheck)" 0 shell_gates
fi

# ── G9 smoke Error→Rule на свежем temp-sink (default/full; --no-smoke/SKIP) ──
if [ "$QUICK" = 0 ] && [ "$NO_SMOKE" = 0 ]; then
    smoke_gate() {
        local tmp; tmp="$(mktemp -d)"   # системный /tmp, вне репо
        # ВАЖНО: без trap RETURN — bash наследует его во вложенные функции
        # (run_gate) и ломается на unset $tmp; убираем temp явно в обеих ветках
        if DATA_ROOT="$ROOT/data" "$PY" scripts/errors_collect.py --sink "$tmp" \
           && "$PY" scripts/errors_report.py --sink "$tmp" --view >/dev/null \
           && "$PY" scripts/errors_prune.py --sink "$tmp"; then
            rm -rf "$tmp"
            echo "smoke ok: collect→view→prune(dry-run) на temp-sink"
            return 0
        fi
        rm -rf "$tmp"
        return 1
    }
    run_gate 9 "smoke Error→Rule" "$([ "$HAS_DOCKER" = 1 ] && echo 0 || echo 1)" smoke_gate
fi

# ── G10 make-проброс prod-errors* ──
run_gate 10 "make prod-errors*" 0 \
    make -n prod-errors prod-errors-report prod-errors-prune prod-errors-sources

# ── --full: доп. проверки на Поднятом стеке (нет стека → SKIP) ──
if [ "$FULL" = 1 ]; then
    full_extra() {
        make e2e && docker compose exec -T mcp-server pytest tests/ -q \
            && docker compose exec -T mcp-server ruff check src/ tests/
    }
    run_gate 11 "full: e2e + контейнерные test/lint" "$([ "$STACK_UP" = 1 ] && echo 0 || echo 1)" full_extra
fi

finish
