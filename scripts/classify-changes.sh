#!/usr/bin/env bash
# classify-changes.sh — классификатор набора изменений, уходящих пушем
# (fail-safe быстрый режим `make push-fast`).
#
# ЗАЧЕМ: `make push` гоняет полный preflight (G1-G10, ~7 мин) даже для правок
# конфигов/доков, где код-гейты бесполезны. Этот скрипт отвечает на вопрос
# «можно ли пушить без код-гейтов?» и печатает в stdout РОВНО один токен
# (exit 0):
#   config-only — все изменённые пути в allow-list (ansible/, docs/, *.md, …);
#   code        — есть хотя бы один путь вне allow-list (scripts/, mcp_server/,
#                 kb-console/, tests/, Makefile, docker-compose, pyproject, …);
#   none        — изменений нет (пушить нечего).
# Правило fail-safe: неизвестное/неоднозначное → `code` (лишний полный preflight
# безопаснее пропущенного код-гейта). Untracked-файлы не уходят пушем и
# игнорируются; если кроме untracked ничего нет — `none`.
#
# Набор путей: git diff --name-only <base>...HEAD (коммиты, что уйдут пушем;
# три точки = merge-base, НЕ прямой diff) + git diff --name-only HEAD
# (staged+unstaged правки рабочего дерева). Сетевых операций нет (fetch/pull
# не делается).
#
# Запуск: bash scripts/classify-changes.sh [BASE_REF]   (по умолчанию origin/main)

set -euo pipefail

BASE="${1:-origin/main}"

# Fail-safe: BASE_REF не существует/не разрешается → code (никогда config-only),
# чтобы отсутствующий origin/main не проглатывал код-изменения.
if ! git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1; then
    echo "code"
    echo "classify-changes: BASE_REF '${BASE}' не разрешается — fail-safe: code" >&2
    exit 0
fi

# allow-list (config-only), ERE по путям. Всё остальное (scripts/, mcp_server/,
# kb-console/, tests/, Makefile, docker-compose*.yml, **/Dockerfile*, pyproject,
# requirements*.txt) → code.
ALLOW='^ansible/|^docs/|^\.knowledge/|^plans/|^README|\.md$|^AGENTS\.md$|^\.gitignore$'

PATHS="$(
    {
        git diff --name-only "${BASE}...HEAD"
        git diff --name-only HEAD
    } | sed '/^$/d' | sort -u
)"

if [ -z "$PATHS" ]; then
    echo "none"
    exit 0
fi

if printf '%s\n' "$PATHS" | grep -E -v "$ALLOW" | grep -q .; then
    echo "code"
else
    echo "config-only"
fi
