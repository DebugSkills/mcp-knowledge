#!/usr/bin/env bash
# airgap-clean-src.sh — чистая ЛОКАЛЬНАЯ копия репо для сборки бандла 038.
# git clone --local наследует ТЕКУЩУЮ ветку (manifest.branch != HEAD) и даёт
# чистое дерево; --force перезаписывает существующий --to (только безопасные пути).
set -euo pipefail

FROM=""; TO=""; COMMIT=""; DRY_RUN=0; FORCE=0

usage() { cat <<'EOF'
Usage: airgap-clean-src.sh --from <repo> --to <dir> [--commit SHA] [--dry-run] [--force]
Создать чистую локальную копию (git clone --local) с сохранением ветки источника.
--force — перезаписать существующий --to (только безопасные пути).
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --from) shift; FROM="${1:-}" ;;
        --to) shift; TO="${1:-}" ;;
        --commit) shift; COMMIT="${1:-}" ;;
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ОШИБКА: неизвестный флаг $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

[ -n "$FROM" ] || { echo "ОШИБКА: обязателен --from <repo>" >&2; usage >&2; exit 2; }
[ -n "$TO" ] || { echo "ОШИБКА: обязателен --to <dir>" >&2; usage >&2; exit 2; }
[ -d "$FROM/.git" ] || { echo "ОШИБКА: --from ($FROM) не git-репозиторий" >&2; exit 2; }

if [ "$DRY_RUN" -eq 1 ]; then
    echo "ПЛАН (--dry-run, ничего не выполняется):"
    echo "  git clone --quiet --local $FROM $TO"
    echo "  ветка клона наследуется от источника; дерево чистое"
    exit 0
fi

if [ -e "$TO" ]; then
    if [ "$FORCE" -ne 1 ]; then
        echo "ОШИБКА: --to ($TO) уже существует (передайте --force для перезаписи)" >&2
        exit 2
    fi
    # безопасность --force: удаляем только валидный путь, не пересекающийся с --from
    [ -n "$TO" ] || { echo "ОШИБКА: --to пуст" >&2; exit 2; }
    [ "$TO" != "/" ] || { echo "ОШИБКА: отказываюсь удалять /" >&2; exit 2; }
    [ "$TO" != "$FROM" ] || { echo "ОШИБКА: --to совпадает с --from" >&2; exit 2; }
    case "$FROM" in
        "$TO"/*) echo "ОШИБКА: --to ($TO) является родителем --from ($FROM)" >&2; exit 2 ;;
    esac
    rm -rf "$TO"
fi

git clone --quiet --local "$FROM" "$TO" || { echo "ОШИБКА: git clone --local FAILED" >&2; exit 1; }

head="$(git -C "$TO" rev-parse HEAD)"
branch="$(git -C "$TO" rev-parse --abbrev-ref HEAD)"

if [ -n "$COMMIT" ] && [ "$COMMIT" != "$head" ]; then
    echo "ОШИБКА: --commit ($COMMIT) != HEAD клона ($head): pack собирает образы из дерева" >&2
    exit 2
fi

if [ -n "$(git -C "$TO" status --porcelain)" ]; then
    echo "ОШИБКА: клон $TO не чистый (git status --porcelain непуст)" >&2
    exit 2
fi

echo "branch=$branch head=$head"
