#!/usr/bin/env bash
# =============================================================================
# offline-deploy.sh — Air-gap развёртывание MCP Knowledge Server (Фаза 13.5)
#
# Принцип: ОДИН архивный bundle переносится на продовую машину (USB/диск),
# без сети. В bundle: docker-образы (mcp-server + qdrant), Ollama-модели
# (mxbai-embed-large + nomic-embed-text), compose-файл, конфиги, скрипты.
# Bundle САМОДОСТАТОЧЕН — распаковывается в любом месте и содержит этот скрипт.
#
# ── На машине с интернетом (сборка): ──
#   ./scripts/offline-deploy.sh prepare          # → mcp-kb-airgap-bundle.tar.gz
#
# ── На изолированном хосте (перенос архива): ──
#   tar -xzf mcp-kb-airgap-bundle.tar.gz         # → каталог staging/
#   cd staging
#   ./scripts/offline-deploy.sh deploy           # загрузка образов + модели + запуск
#   ./scripts/offline-deploy.sh verify           # smoke + ВЫСОКОУРОВНЕВЫЕ тесты (E2E)
#   ./scripts/offline-deploy.sh import --src /path/to/md/   # импорт знаний + reindex
#
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"     # корень развёртывания (где лежат images/, compose)
GIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"   # git-корень (при prepare)
STAGING_DIR="$GIT_ROOT/artifacts/staging"  # каталог, который уходит в bundle (prepare)
BUNDLE="$GIT_ROOT/mcp-kb-airgap-bundle.tar.gz"

MCP_IMAGE="mcp-knowledge-server:prod"
QDRANT_IMAGE="qdrant/qdrant:v1.13.4"
KB_CONSOLE_IMAGE="kb-console:prod"
COMPOSE_PROD="docker-compose.prod.yml"

# Ollama-модели, которые нужны embedder'у (основная + fallback)
OLLAMA_MODELS_NEEDED=("mxbai-embed-large" "nomic-embed-text")

# ─── ollama_models_dir: где лежат модели Ollama ────────────────
ollama_models_dir() {
    if [ -n "${OLLAMA_MODELS:-}" ]; then echo "$OLLAMA_MODELS"; return; fi
    if command -v systemctl >/dev/null 2>&1; then
        local user
        user=$(systemctl show ollama -p User --value 2>/dev/null || true)
        if [ -n "$user" ] && [ "$user" != "root" ]; then
            local home
            home=$(getent passwd "$user" | cut -d: -f6 2>/dev/null || true)
            if [ -n "$home" ] && [ -d "$home/.ollama/models" ]; then
                echo "$home/.ollama/models"; return
            fi
        fi
    fi
    if [ -d "$HOME/.ollama/models" ]; then echo "$HOME/.ollama/models"; return; fi
    if [ -d /usr/share/ollama/.ollama/models ]; then echo "/usr/share/ollama/.ollama/models"; return; fi
    echo ""
}

# ─── export_ollama_models: выборочный экспорт embedder-моделей ─
# Копирует только манифесты + blobs нужных моделей (по digest из манифеста).
export_ollama_models() {
    local dest="$1"
    local src
    src=$(ollama_models_dir)
    if [ -z "$src" ]; then
        echo "WARN: Ollama models dir не найден — модели не будут включены в bundle."
        echo "      Загрузите их на этой машине: ollama pull mxbai-embed-large nomic-embed-text"
        return 0
    fi
    echo "Exporting Ollama models from $src"
    local manifest_dir="$src/manifests/registry.ollama.ai/library"
    local blobs_src="$src/blobs"
    local blobs_dest="$dest/blobs"
    mkdir -p "$blobs_dest"

    for model in "${OLLAMA_MODELS_NEEDED[@]}"; do
        local manifest="$manifest_dir/$model/latest"
        if [ ! -f "$manifest" ]; then
            echo "WARN: модель '$model' не найдена ($manifest) — пропуск"
            continue
        fi
        echo "  ✔ $model"
        mkdir -p "$dest/manifests/registry.ollama.ai/library/$model"
        cp "$manifest" "$dest/manifests/registry.ollama.ai/library/$model/latest"
        python3 -c "
import json
with open('$manifest') as f:
    m = json.load(f)
for l in m['layers']:
    d = l['digest'].removeprefix('sha256:')
    print('$blobs_src/sha256-' + d, '$blobs_dest/sha256-' + d)
" | while read -r s d; do
            if [ -f "$s" ]; then cp "$s" "$d"; else echo "WARN: blob не найден: $s"; fi
        done
    done
    echo "  Ollama models exported: $(du -sh "$dest" | cut -f1)"
}

# ─── install_ollama_models: размещение моделей на проде ────────
install_ollama_models() {
    local src="$1"
    if [ ! -d "$src" ]; then
        echo "SKIP: в bundle нет Ollama-моделей."
        return 0
    fi
    local dest
    dest=$(ollama_models_dir)
    if [ -z "$dest" ]; then
        echo "ERROR: не найден каталог моделей Ollama. Установите Ollama или"
        echo "       укажите OLLAMA_MODELS=/path/to/models"
        return 1
    fi
    echo "Installing Ollama models → $dest"
    mkdir -p "$dest"
    cp -r "$src/manifests" "$dest/"
    cp -r "$src/blobs" "$dest/"
    local user
    user=$(systemctl show ollama -p User --value 2>/dev/null || true)
    if [ -n "$user" ] && [ "$user" != "$(id -un)" ]; then
        chown -R "$user:$user" "$dest" 2>/dev/null || true
    fi
    if systemctl list-unit-files ollama.service >/dev/null 2>&1; then
        systemctl restart ollama 2>/dev/null && echo "  Ollama restarted." || echo "  WARN: не удалось перезапустить ollama (нужны права?)"
    fi
    sleep 3
    curl -sf http://localhost:11434/api/tags >/dev/null \
        && echo "  Ollama API: OK (модели: $(curl -s http://localhost:11434/api/tags | python3 -c 'import json,sys; print(", ".join(m["name"] for m in json.load(sys.stdin).get("models", [])))' 2>/dev/null || echo '?')" \
        || echo "  WARN: Ollama API недоступен на localhost:11434 — проверьте сервис ollama"
}

# ─── prepare: сборка bundle на машине с интернетом ─────────────
prepare() {
    echo "=== [prepare] Сборка air-gap bundle (машина с интернетом) ==="
    rm -rf "$GIT_ROOT/artifacts" && mkdir -p "$STAGING_DIR"/{images,ollama,scripts}

    echo "[1/5] Building mcp-server image (без torch)..."
    docker build -t "$MCP_IMAGE" "$GIT_ROOT/mcp_server"

    echo "[2/6] Building kb-console image (NiceGUI-клиент)..."
    docker build -t "$KB_CONSOLE_IMAGE" "$GIT_ROOT/kb-console"

    echo "[3/6] Saving Docker images (mcp-server + qdrant + kb-console)..."
    docker save "$MCP_IMAGE" "$QDRANT_IMAGE" "$KB_CONSOLE_IMAGE" -o "$STAGING_DIR/images/images.tar"
    echo "  images.tar: $(du -sh "$STAGING_DIR/images/images.tar" | cut -f1)"

    echo "[4/6] Exporting Ollama models..."
    export_ollama_models "$STAGING_DIR/ollama/models"

    echo "[5/6] Copying configs, scripts, docs..."
    cp "$GIT_ROOT/$COMPOSE_PROD" "$STAGING_DIR/"
    cp "$GIT_ROOT/.env.prod.example" "$STAGING_DIR/.env.example"
    cp "$GIT_ROOT/scripts/seed_knowledge.py" "$STAGING_DIR/scripts/"
    cp "$GIT_ROOT/scripts/backup.sh" "$STAGING_DIR/scripts/"
    cp "$GIT_ROOT/scripts/offline-deploy.sh" "$STAGING_DIR/scripts/"
    cp "$GIT_ROOT/docs/air-gap-validation.md" "$STAGING_DIR/DEPLOYMENT.md" 2>/dev/null || true
    cp "$GIT_ROOT/kb-console/USER_GUIDE.md" "$STAGING_DIR/USER_GUIDE.md" 2>/dev/null || true

    echo "[6/6] Checksums + pack..."
    ( cd "$STAGING_DIR" && find . -type f ! -name CHECKSUMS.sha256 -exec sha256sum {} \; > CHECKSUMS.sha256 )
    tar -C "$GIT_ROOT/artifacts" -czf "$BUNDLE" "$(basename "$STAGING_DIR")"

    echo ""
    echo "=== Artifact summary ==="
    ( cd "$STAGING_DIR" && find . -type f | sed 's|^\./|  |' | sort )
    echo "  TOTAL: $(du -sh "$BUNDLE" | cut -f1) → $BUNDLE"
    echo ""
    echo "✅ Перенесите архив на продовую машину и выполните:"
    echo "   tar -xzf mcp-kb-airgap-bundle.tar.gz"
    echo "   cd staging && ./scripts/offline-deploy.sh deploy"
}

# ─── deploy: развёртывание на изолированном хосте ──────────────
deploy() {
    echo "=== [deploy] Установка из air-gap bundle (корень: $PROJECT_DIR) ==="
    [ -f "$PROJECT_DIR/images/images.tar" ] || { echo "ERROR: не найдены артефакты. Распакуйте bundle: tar -xzf mcp-kb-airgap-bundle.tar.gz"; exit 1; }

    echo "[1/6] Verifying checksums..."
    ( cd "$PROJECT_DIR" && sha256sum -c CHECKSUMS.sha256 >/dev/null ) && echo "  OK"

    echo "[2/6] Loading Docker images (mcp-server + qdrant)..."
    docker load -i "$PROJECT_DIR/images/images.tar"

    echo "[3/6] Installing Ollama models..."
    install_ollama_models "$PROJECT_DIR/ollama/models"

    echo "[4/6] Configuring environment..."
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        echo "  .env создан из .env.example — ЗАМЕНИТЕ КЛЮЧИ ДОСТУПА:"
        grep -E "MCP_(READ|WRITE)_KEYS" "$PROJECT_DIR/.env" | sed 's/^/    /'
    else
        echo "  .env уже существует — не тронут."
    fi

    echo "[5/6] Preparing data dirs..."
    mkdir -p "$PROJECT_DIR/knowledge" "$PROJECT_DIR/data/qdrant" "$PROJECT_DIR/data/quality" "$PROJECT_DIR/data/dlq"

    echo "[6/6] Starting services (qdrant + mcp-server)..."
    docker compose -f "$PROJECT_DIR/$COMPOSE_PROD" up -d
    for i in $(seq 1 30); do
        curl -sf http://localhost:8000/health/live >/dev/null 2>&1 && break
        sleep 5
    done
    curl -sf http://localhost:8000/health/live >/dev/null \
        && echo "  mcp-server: LIVE ✅" \
        || { echo "ERROR: mcp-server не поднялся. Логи: docker compose -f $COMPOSE_PROD logs mcp-server"; exit 1; }

    echo ""
    echo "✅ Deployment complete."
    echo "   Проверка всей системы: ./scripts/offline-deploy.sh verify"
    echo "   Импорт знаний:         ./scripts/offline-deploy.sh import --src /path/to/md/"
}

# ─── verify: smoke + высокоуровневые тесты всей системы ────────
verify() {
    echo "=== [verify] Проверка системы (корень: $PROJECT_DIR) ==="
    local ok=0 fail=0

    echo "--- Smoke tests ---"
    local -a probes=(
        "mcp-server /health/live|http://localhost:8000/health/live"
        "mcp-server /health     |http://localhost:8000/health"
        "qdrant /healthz        |http://localhost:6333/healthz"
        "Ollama /api/tags       |http://localhost:11434/api/tags"
        "kb-console /           |http://localhost:8085/"
    )
    for p in "${probes[@]}"; do
        local label url
        label="${p%%|*}"; url="${p##*|}"
        echo -n "  $label: "
        if curl -sf "$url" >/dev/null; then echo "✅"; ok=$((ok+1)); else echo "❌"; fail=$((fail+1)); fi
    done

    echo ""
    echo "--- Высокоуровневые тесты (E2E S1-S19: реальные Qdrant+Ollama, in-process HTTP) ---"
    echo "    Коллекция knowledge_e2e изолирована от прод-данных (создаётся и удаляется)."
    if docker compose -f "$PROJECT_DIR/$COMPOSE_PROD" exec -T mcp-server \
        pytest tests/e2e -q --tb=line -m "e2e and not e2e_slow" -p no:cacheprovider 2>&1 | tail -3; then
        echo "  E2E suite: PASS ✅"
    else
        echo "  E2E suite: FAILED ❌"
        fail=$((fail+1))
    fi

    echo ""
    if [ "$fail" -eq 0 ]; then
        echo "✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ (smoke $ok/5 + E2E). Система работоспособна."
    else
        echo "❌ $fail проверок не прошли (smoke ok=$ok/5)."
        exit 1
    fi
}

# ─── import: импорт знаний из Markdown-каталога + reindex ──────
import_knowledge() {
    local src="${1:-}"
    if [ -z "$src" ] || [ ! -d "$src" ]; then
        echo "Usage: ./offline-deploy.sh import --src /path/to/markdown_dir"
        echo "       Копирует все *.md (с сохранением структуры) в knowledge/ и перестраивает индекс."
        exit 1
    fi
    local dest="$PROJECT_DIR/knowledge"
    mkdir -p "$dest"

    echo "=== [import] Импорт знаний: $src → $dest ==="
    local count=0
    while IFS= read -r -d '' f; do
        local rel target
        rel="${f#"$src"/}"
        target="$dest/$rel"
        mkdir -p "$(dirname "$target")"
        cp "$src/$f" "$target"
        count=$((count+1))
    done < <(find "$src" -name "*.md" -type f -print0)
    echo "  Скопировано .md файлов: $count"

    echo "  Перестроение индекса (reindex через CLI в контейнере)..."
    docker compose -f "$PROJECT_DIR/$COMPOSE_PROD" exec -T mcp-server python -m mcp_server.cli reindex

    echo "✅ Импорт завершён. Проверка: curl -s http://localhost:8000/health"
}

# ─── Main ─────────────────────────────────────────────────────
case "${1:-}" in
    prepare) prepare ;;
    deploy)  deploy ;;
    verify)  verify ;;
    import)  import_knowledge "${2:-}" ;;
    *)
        echo "Usage: $0 {prepare|deploy|verify|import --src <dir>}"
        echo ""
        echo "  prepare — собрать bundle на машине с интернетом"
        echo "  deploy  — установить из bundle на изолированном хосте"
        echo "  verify  — smoke + высокоуровневые E2E-тесты всей системы"
        echo "  import  — импорт знаний из Markdown-каталога + reindex"
        exit 1
        ;;
esac
