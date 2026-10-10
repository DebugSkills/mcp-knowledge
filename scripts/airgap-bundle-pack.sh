#!/usr/bin/env bash
# =============================================================================
# airgap-bundle-pack.sh — ПОЛНЫЙ офлайн-бандл (038) на интернет-машине.
# Обёртка над offline-update.sh pack: [N/7] баннеры + тайминги, tee-лог в --out,
# pv-прогресс. При грязном дереве пакуем из ЧИСТОЙ ЛОКАЛЬНОЙ КОПИИ
# (scripts/airgap-clean-src.sh → git clone --local) — клон сохраняет ветку
# (manifest.branch != HEAD) и чистое дерево, offline-update.sh собирает образы
# из дерева и требует --commit == HEAD (ловушка 038, устранена).
# Состав: mcp-kb-update-<ISO>.tar.gz + mcp-kb-models-<ISO>.tar.gz (carrier) +
# python-3.11-slim.tar.gz. --no-* — отказники частей.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR=""; COMMIT=""; LOG_FILE=""
NO_MODELS=0; NO_MODELS_IMAGE=0; NO_PYTHON_BASE=0; NO_CLEAN_SRC=0; DRY_RUN=0
PY_BASE_IMAGE="python:3.11-slim"
CARRIER_BASE="busybox:1.36"
FREE_MIN_GB=30

usage() { cat <<'EOF'
Usage: airgap-bundle-pack.sh [--out DIR] [--commit SHA] [--no-models]
      [--no-models-image] [--no-python-base] [--no-clean-src] [--dry-run]
      [--log FILE] [--help]
Собрать ПОЛНЫЙ офлайн-бандл (образы + код + модели + carrier + python-база).
--no-worktree — deprecated-алиас --no-clean-src.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out) shift; OUT_DIR="${1:-}" ;;
        --commit) shift; COMMIT="${1:-}" ;;
        --no-models) NO_MODELS=1 ;;
        --no-models-image) NO_MODELS_IMAGE=1 ;;
        --no-python-base) NO_PYTHON_BASE=1 ;;
        --no-clean-src) NO_CLEAN_SRC=1 ;;
        --no-worktree) NO_CLEAN_SRC=1 ;;   # deprecated-алиас
        --dry-run) DRY_RUN=1 ;;
        --log) shift; LOG_FILE="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ОШИБКА: неизвестный флаг $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

[ -z "$OUT_DIR" ] && OUT_DIR="$GIT_ROOT/artifacts"
TARGET="${COMMIT:-HEAD}"

have_pv() { command -v pv >/dev/null 2>&1; }
pv_pipe() { if have_pv; then pv -f -N "$1" -b -e -r; else cat; fi; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

STEP_T0=0
step() { STEP_T0="$(date +%s)"; echo ""; echo "═══ [$1/7] $2 ═══"; }
elapsed() { echo "   ⏱ $1: $(( $(date +%s) - STEP_T0 ))s"; }

if [ "$DRY_RUN" -eq 1 ]; then
    echo "ПЛАН (--dry-run, ничего не выполняется):"
    echo "  [1/7] preflight      — docker/git/gzip + свободно ≥ ${FREE_MIN_GB} ГБ на --out"
    echo "  [2/7] чистая копия  — при грязном дереве: git clone --local → $OUT_DIR/.pack-src (существующий ПЕРЕЗАПИСЫВАЕТСЯ)"
    echo "  [3/7] сборка пакета  — offline-update.sh pack$([ $NO_MODELS -eq 0 ] && echo ' --with-models') --out $OUT_DIR"
    echo "  [4/7] carrier-образ  — mcp-kb-models:<ISO> → mcp-kb-models-<ISO>.tar.gz"
    echo "  [5/7] база python    — docker save $PY_BASE_IMAGE → python-3.11-slim.tar.gz"
    echo "  [6/7] верификация    — offline-update.sh inspect --check <pkg>"
    echo "  [7/7] сводка         — таблица файл→размер→sha256 + команда переноса"
    exit 0
fi

# ранний гейт: чистое дерево? (ДО docker-preflight — не требует демона)
USE_CLEAN_SRC=0
dirty="$(git -C "$GIT_ROOT" status --porcelain 2>/dev/null || true)"
if [ -n "$dirty" ]; then
    if [ "$NO_CLEAN_SRC" -eq 1 ]; then
        echo "ОШИБКА: рабочее дерево ГРЯЗНОЕ, а --no-clean-src запрещает временную копию." >&2
        echo "  Закоммитьте правки ИЛИ запустите без --no-clean-src (git clone --local при $TARGET)." >&2
        exit 2
    fi
    USE_CLEAN_SRC=1
fi

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
[ -n "$LOG_FILE" ] || LOG_FILE="$OUT_DIR/bundle-pack-$TS.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "лог: $LOG_FILE"

CLEAN_SRC=""; CLEAN_TMP=""
cleanup() {
    [ -n "$CLEAN_TMP" ] && rm -rf "$CLEAN_TMP" 2>/dev/null || true
    # только собственный каталог копии внутри OUT_DIR
    if [ -n "$CLEAN_SRC" ] && [ "$CLEAN_SRC" = "$OUT_DIR/.pack-src" ] && [ -d "$CLEAN_SRC" ]; then
        rm -rf "$CLEAN_SRC" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ─── [1/7] preflight ───
step 1 "preflight (docker/git/gzip, место)"
for tool in docker git gzip; do command -v "$tool" >/dev/null 2>&1 || die "нет $tool в PATH"; done
fs_free_kb() { local d="$1"; while [ ! -d "$d" ]; do d="$(dirname "$d")"; done; df -P -k "$d" | awk 'NR==2 {print $4}'; }
free_kb="$(fs_free_kb "$OUT_DIR")"
[ -z "$free_kb" ] || [ "$free_kb" -ge $(( FREE_MIN_GB * 1024 * 1024 )) ] \
    || die "свободно $(( free_kb / 1024 / 1024 )) ГБ на --out, нужно ≥ ${FREE_MIN_GB} ГБ"
echo "   docker: $(docker --version 2>/dev/null || echo '?')"
echo "   git:    $(git --version 2>/dev/null || echo '?')"
echo "   pv:     $(have_pv && echo 'есть' || echo 'нет')"
echo "   свободно на --out: $(( free_kb / 1024 / 1024 )) ГБ"
elapsed preflight

# ─── [2/7] чистая копия (git clone --local) ───
step 2 "чистая копия (airgap-clean-src)"
REPO_RUN="$GIT_ROOT"
if [ "$USE_CLEAN_SRC" -eq 1 ]; then
    CLEAN_SRC="$OUT_DIR/.pack-src"
    echo "   дерево грязное → git clone --local → $CLEAN_SRC (существующий перезаписывается --force)"
    "$GIT_ROOT/scripts/airgap-clean-src.sh" --force --from "$GIT_ROOT" --to "$CLEAN_SRC" ${COMMIT:+--commit "$COMMIT"}
    REPO_RUN="$CLEAN_SRC"
else
    echo "   дерево чистое — работаем из $GIT_ROOT"
fi
elapsed clean-src

# ─── [3/7] сборка пакета ───
step 3 "сборка пакета (offline-update.sh pack)"
# ollama — 6-й элемент BASE_IMAGES (arch-2026-10-10-ai-ws-p2-1 R2):
# легаси-ollama-флаг удалён, базовый набор уже полный.
PACK_FLAGS=()
[ "$NO_MODELS" -eq 0 ] && PACK_FLAGS+=(--with-models)
[ -z "$COMMIT" ] || PACK_FLAGS+=(--commit "$COMMIT")
MODELS_SRC="${OLLAMA_MODELS:-$GIT_ROOT/data/ollama/models}"
[ "$NO_MODELS" -ne 0 ] || [ -d "$MODELS_SRC" ] || echo "   WARN: нет каталога моделей $MODELS_SRC — модели пропущены"
echo "   OLLAMA_MODELS=$MODELS_SRC"
OLLAMA_MODELS="$MODELS_SRC" "$REPO_RUN/scripts/offline-update.sh" pack "${PACK_FLAGS[@]}" --out "$OUT_DIR"
elapsed pack

pkg_dir="$(ls -dt "$OUT_DIR"/mcp-kb-update-*/ 2>/dev/null | head -1 | sed 's|/$||')"
[ -n "$pkg_dir" ] || die "пакет mcp-kb-update-* не появился в $OUT_DIR"
iso="$(basename "$pkg_dir")"; iso="${iso#mcp-kb-update-}"

# копия больше не нужна (pack завершён) — удаляем только свой каталог
if [ -n "$CLEAN_SRC" ] && [ "$CLEAN_SRC" = "$OUT_DIR/.pack-src" ] && [ -d "$CLEAN_SRC" ]; then
    rm -rf "$CLEAN_SRC"
    CLEAN_SRC=""
fi

# ─── [4/7] carrier-образ моделей ───
step 4 "carrier-образ моделей (mcp-kb-models:$iso)"
if [ "$NO_MODELS" -eq 1 ] || [ "$NO_MODELS_IMAGE" -eq 1 ]; then
    echo "   пропуск (--no-models / --no-models-image)"
elif [ -d "$pkg_dir/models" ] && [ -n "$(ls -A "$pkg_dir/models" 2>/dev/null)" ]; then
    CLEAN_TMP="$(mktemp -d /tmp/kilo/airgap-carrier.XXXXXX)"
    printf 'FROM %s\nCOPY models /models\n' "$CARRIER_BASE" > "$CLEAN_TMP/Dockerfile"
    docker build -t "mcp-kb-models:$iso" -f "$CLEAN_TMP/Dockerfile" "$pkg_dir"
    docker save "mcp-kb-models:$iso" | pv_pipe "save mcp-kb-models" | gzip -1 > "$OUT_DIR/mcp-kb-models-$iso.tar.gz"
    rm -rf "$CLEAN_TMP"; CLEAN_TMP=""
else
    echo "   WARN: в пакете нет models/ — carrier-образ не собран"
fi
elapsed carrier

# ─── [5/7] база python ───
step 5 "база python ($PY_BASE_IMAGE)"
if [ "$NO_PYTHON_BASE" -eq 1 ]; then
    echo "   пропуск (--no-python-base)"
else
    docker image inspect "$PY_BASE_IMAGE" >/dev/null 2>&1 || { echo "   docker pull $PY_BASE_IMAGE …"; docker pull "$PY_BASE_IMAGE"; }
    docker save "$PY_BASE_IMAGE" | pv_pipe "save python-base" | gzip -1 > "$OUT_DIR/python-3.11-slim.tar.gz"
fi
elapsed python-base

# ─── [6/7] верификация ───
step 6 "верификация пакета (inspect --check)"
if ! "$GIT_ROOT/scripts/offline-update.sh" inspect --check "$pkg_dir"; then
    echo "ОШИБКА: верификация FAILED — частичный результат сохранён в $OUT_DIR (НЕ удаляю)."
    exit 1
fi
elapsed verify

# ─── [7/7] сводка ───
step 7 "сводка"
echo "   артефакты в $OUT_DIR:"
for f in "$OUT_DIR"/*.tar.gz; do
    [ -f "$f" ] || continue
    printf '   %-40s %8s  %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)" "$(sha256sum "$f" | awk '{print $1}')"
done
echo ""
echo "   Перенос: rsync -avP $OUT_DIR/*.tar.gz <user>@<aikb>:/opt/mcp-knowledge/bundle/"
echo "   На узле: sha256sum /opt/mcp-knowledge/bundle/*.tar.gz (сверка с таблицей выше)"
echo "   Нужны: mcp-kb-update-*.tar.gz + mcp-kb-models-*.tar.gz + python-3.11-slim.tar.gz"
elapsed summary
echo ""
echo "✅ bundle-pack завершён. Лог: $LOG_FILE"
