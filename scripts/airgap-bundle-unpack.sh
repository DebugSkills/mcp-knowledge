#!/usr/bin/env bash
# =============================================================================
# airgap-bundle-unpack.sh — РАСПАКОВКА офлайн-бандла 038 на целевом хосте.
# [N/6] баннеры + тайминги, tee-лог, pv-прогресс. Идемпотентна: docker load
# только расходящихся .Id (повтор = skip), модели → DATA_ROOT/ollama/models.
# Контейнеры НЕ поднимаются/не рестартятся — только подготовка узла.
#
# ═══ ИНВАРИАНТ КОНТУРНОЙ ИЗОЛЯЦИИ (аудит 2026-10-02) ═══
# Скрипт пишет ТОЛЬКО в: (1) docker (load образов), (2) DATA_ROOT/ollama/models
# (модели), (3) PKG_TMP/staging (распаковка бандла). НИКОГДА в corpus (репозиторий
# `…/knowledge`) и data/qdrant (индекс) — документы и индексы у dev и prod СВОИ,
# между контурами едут только код и модели.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUNDLE=""; DATA_ROOT_ARG=""; MODELS_IMAGE=""; PYTHON_BASE=""; LOG_FILE=""
REPO="$GIT_ROOT"; NO_MODELS=0; DRY_RUN=0

usage() { cat <<'EOF'
Usage: airgap-bundle-unpack.sh --bundle <DIR|TAR.GZ> [--data-root PATH]
      [--models-image FILE] [--python-base FILE] [--repo PATH]
      [--no-models] [--dry-run] [--log FILE] [--help]
Распаковать бандл: docker load образов (идемпотентно) + модели в DATA_ROOT.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bundle) shift; BUNDLE="${1:-}" ;;
        --data-root) shift; DATA_ROOT_ARG="${1:-}" ;;
        --models-image) shift; MODELS_IMAGE="${1:-}" ;;
        --python-base) shift; PYTHON_BASE="${1:-}" ;;
        --repo) shift; REPO="${1:-}" ;;
        --no-models) NO_MODELS=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --log) shift; LOG_FILE="${1:-}" ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ОШИБКА: неизвестный флаг $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

[ -n "$BUNDLE" ] || { echo "ОШИБКА: обязателен --bundle <DIR|TAR.GZ>" >&2; usage >&2; exit 2; }

# читаемость --bundle ДО мутаций
if [ -d "$BUNDLE" ]; then
    [ -f "$BUNDLE/manifest.json" ] || { echo "ОШИБКА: $BUNDLE без manifest.json" >&2; exit 2; }
elif [ ! -f "$BUNDLE" ]; then
    echo "ОШИБКА: --bundle нечитаем: $BUNDLE (нужен каталог или *.tar.gz)" >&2
    exit 2
fi

have_pv() { command -v pv >/dev/null 2>&1; }
pv_pipe() { if have_pv; then pv -f -N "$1" -b -e -r; else cat; fi; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

STEP_T0=0
step() { STEP_T0="$(date +%s)"; echo ""; echo "═══ [$1/6] $2 ═══"; }
elapsed() { echo "   ⏱ $1: $(( $(date +%s) - STEP_T0 ))s"; }

data_root() {
    [ -n "$DATA_ROOT_ARG" ] && { echo "$DATA_ROOT_ARG"; return; }
    local envf="" v=""
    [ -f "$REPO/.env" ] && envf="$REPO/.env"
    [ -z "$envf" ] && [ -f ".env" ] && envf=".env"
    if [ -n "$envf" ]; then
        v="$(grep '^DATA_ROOT=' "$envf" 2>/dev/null | head -1 | cut -d= -f2-)"
        [ -n "$v" ] && { echo "$v"; return; }
    fi
    echo "/opt/mcp-knowledge/data"
}
DATA_ROOT="$(data_root)"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "ПЛАН (--dry-run, ничего не выполняется):"
    echo "  [1/6] preflight — docker + место ≥ бандл×2.2; DATA_ROOT=$DATA_ROOT"
    echo "  [2/6] подготовка — развернуть/использовать каталог бандла, manifest.json"
    echo "  [3/6] образы      — gunzip -c | docker load (идемпотентно, skip по .Id)"
    echo "  [4/6] модели      — установка в $DATA_ROOT/ollama/models (root:root)"
    echo "  [5/6] доп. образы — docker load --python-base / --models-image"
    echo "  [6/6] сводка      — загруженные образы + следующие шаги"
    exit 0
fi

TS="$(date +%Y%m%d-%H%M%S)"
if [ -z "$LOG_FILE" ]; then
    [ -d "$BUNDLE" ] && LOG_FILE="$BUNDLE/bundle-unpack-$TS.log" || LOG_FILE="$(pwd)/bundle-unpack-$TS.log"
fi
exec > >(tee -a "$LOG_FILE") 2>&1
echo "лог: $LOG_FILE"

PKG_TMP=""
cleanup() { [ -n "$PKG_TMP" ] && rm -rf "$PKG_TMP" 2>/dev/null || true; }
trap cleanup EXIT

# ─── [1/6] preflight ───
step 1 "preflight (docker, место, DATA_ROOT)"
command -v docker >/dev/null 2>&1 || die "нет docker — установите docker / группу docker"
docker info >/dev/null 2>&1 || echo "   WARN: docker-демон недоступен — возможно нужен root/группа docker"
[ -d "$BUNDLE" ] && bsize="$(du -s -b "$BUNDLE" 2>/dev/null | cut -f1)" || bsize="$(stat -c%s "$BUNDLE" 2>/dev/null || echo 0)"
fs_free_kb() { local d="$1"; while [ ! -d "$d" ]; do d="$(dirname "$d")"; done; df -P -k "$d" | awk 'NR==2 {print $4}'; }
free_kb="$(fs_free_kb "$(dirname "$DATA_ROOT")")"
need_b=$(( bsize * 22 / 10 ))
[ -z "$free_kb" ] || [ "$free_kb" -eq 0 ] || [ "$(( free_kb * 1024 ))" -ge "$need_b" ] \
    || echo "   WARN: свободно $(( free_kb / 1024 / 1024 )) ГБ, бандл $(( bsize / 1024 / 1024 )) МБ — может не хватить"
echo "   DATA_ROOT=$DATA_ROOT"
elapsed preflight

# ─── [2/6] подготовка ───
step 2 "подготовка (каталог бандла)"
if [ -f "$BUNDLE" ]; then
    PKG_TMP="$(mktemp -d /tmp/kilo/airgap-unpack.XXXXXX)"
    echo "   распаковка $BUNDLE → $PKG_TMP"
    tar -xzf "$BUNDLE" -C "$PKG_TMP"
    inner="$(find "$PKG_TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [ -n "$inner" ] || die "в архиве нет каталога пакета"
    PKG="$inner"
else
    PKG="$BUNDLE"
fi
[ -f "$PKG/manifest.json" ] || die "нет manifest.json в $PKG"
elapsed prepare

manifest_images_tsv() { python3 -c 'import json,sys
m=json.load(open(sys.argv[1]))
for img in m.get("images",[]):
    print("\t".join([str(img.get("name","")),str(img.get("id","")),str(img.get("file",""))]))' "$1"; }

# ─── [3/6] образы (идемпотентно по .Id) ───
step 3 "образы (docker load, идемпотентно)"
loaded=0; skipped=0
while IFS=$'\t' read -r name id file; do
    [ -n "$name" ] || continue
    local_id="$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null || true)"
    if [ "$local_id" = "$id" ]; then
        echo "   skip (уже загружен): $name"
        skipped=$((skipped+1)); continue
    fi
    echo "   docker load $name ← $file"
    gunzip -c "$PKG/$file" | pv_pipe "load $name" | docker load
    echo "     тег=$name Id=$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null || echo '?')"
    loaded=$((loaded+1))
done < <(manifest_images_tsv "$PKG/manifest.json")
echo "   образы: загружено $loaded, пропущено $skipped"
elapsed images

# ─── [4/6] модели ───
step 4 "модели → $DATA_ROOT/ollama/models"
MDEST="$DATA_ROOT/ollama/models"
MODELS_IMG_TAG=""
if [ "$NO_MODELS" -eq 1 ]; then
    echo "   пропуск (--no-models)"
elif [ -n "$MODELS_IMAGE" ]; then
    echo "   загрузка carrier-образа $MODELS_IMAGE …"
    MODELS_IMG_TAG="$(docker load -i "$MODELS_IMAGE" 2>/dev/null | LC_ALL=C sed -n 's/^Loaded image: //p' | head -1)"
    [ -n "$MODELS_IMG_TAG" ] || die "не удалось определить тег образа из $MODELS_IMAGE"
    docker run --rm -v "$MDEST":/dest "$MODELS_IMG_TAG" sh -c 'mkdir -p /dest && cp -a /models/. /dest/'
elif [ -d "$PKG/models" ]; then
    echo "   копирование $PKG/models → $MDEST"
    mkdir -p "$MDEST"
    cp -a "$PKG/models/." "$MDEST/"
else
    echo "   WARN: в пакете нет моделей — пропуск"
fi
if [ "$NO_MODELS" -eq 0 ] && [ -d "$MDEST" ]; then
    chown -R root:root "$MDEST" 2>/dev/null || echo "   WARN: chown root:root не удался (нужен root?)"
    echo "   манифестов в сторе: $(ls "$MDEST"/manifests/registry.ollama.ai/library 2>/dev/null | wc -l)"
fi
elapsed models

# ─── [5/6] доп. образы ───
step 5 "доп. образы (python-base / models-image)"
[ -z "$PYTHON_BASE" ] || gunzip -c "$PYTHON_BASE" | pv_pipe "load python-base" | docker load
if [ -n "$MODELS_IMAGE" ] && [ -z "$MODELS_IMG_TAG" ]; then
    gunzip -c "$MODELS_IMAGE" | pv_pipe "load models-image" | docker load
fi
elapsed extra-images

# ─── [6/6] сводка ───
step 6 "сводка"
echo "   загруженные образы:"
docker images --format '   {{.Repository}}:{{.Tag}}  {{.ID}}' | sort
echo ""
echo "   store моделей: $MDEST"
[ -d "$MDEST/manifests/registry.ollama.ai/library" ] \
    && ls "$MDEST/manifests/registry.ollama.ai/library" 2>/dev/null | sed 's/^/     • /' || echo "     (нет моделей)"
echo ""
echo "   Следующие шаги (НЕ выполняются):"
echo "     • docker compose up -d --build --force-recreate"
echo "     • reindex / проверка /health (:8000) и консоли (:8085)"
echo "     • первое обновление — offline-update.sh apply-stage"
elapsed summary
echo ""
echo "✅ bundle-unpack завершён. Лог: $LOG_FILE"
