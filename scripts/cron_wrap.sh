#!/usr/bin/env bash
# cron_wrap.sh — обёртка cron-задач для цикла Error→Rule (code-2026-09-22-003, Ф1).
#
# Использование:
#   cron_wrap.sh <job-name> <logfile> -- <cmd...>
#
# Что делает:
#   1) выполняет <cmd...>;
#   2) дописывает в <logfile> строку: [CRON] job=<name> exit=<N> dur=<s> ts=<iso8601>
#   3) возвращает exit-код команды (cron видит реальный код).
#
# Формат существующих логов (backup.sh/quality_scan.sh) НЕ меняется —
# добавляется только одна [CRON]-строка на запуск. errors_collect.py читает
# эти строки (source=cron_log): exit≠0 → P0-признак cron_nonzero (E3).
#
# set -uo pipefail БЕЗ -e: wrapper обязан записать exit-код даже при падении команды.
set -uo pipefail

if [ "$#" -lt 3 ] || [ "$3" != "--" ]; then
    echo "Usage: $0 <job-name> <logfile> -- <cmd...>" >&2
    exit 64
fi

JOB_NAME="$1"
LOGFILE="$2"
shift 3

START=$(date +%s)
"$@"
EXIT=$?
DUR=$(( $(date +%s) - START ))

# [CRON]-строка собирается без подстановок вывода команды (секреты из env/args
# не логируем — P2-10/E7; сама команда в лог НЕ пишется).
echo "[CRON] job=${JOB_NAME} exit=${EXIT} dur=${DUR}s ts=$(date -Iseconds)" >> "${LOGFILE}"

exit "${EXIT}"
