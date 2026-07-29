#!/usr/bin/env bash
# offline-deploy.sh — Air-gap развёртывание MCP Knowledge Server
# Использование:
#   ./offline-deploy.sh prepare   # на машине с интернетом
#   ./offline-deploy.sh deploy    # на изолированном хосте
#   ./offline-deploy.sh verify    # дымовой тест

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ARTIFACTS_DIR="$PROJECT_DIR/artifacts"
BUNDLE="mcp-kb-airgap-bundle.tar.gz"

# --- prepare: сборка артефактов на машине с интернетом ---
prepare() {
    echo "=== Preparing air-gap artifacts ==="
    mkdir -p "$ARTIFACTS_DIR"/{images,wheelhouse,models/bge-m3}

    # 1. Docker образы
    echo "[1/5] Pulling Docker images..."
    docker pull qdrant/qdrant:v1.13.4
    docker pull python:3.11-slim
    docker save qdrant/qdrant:v1.13.4 python:3.11-slim \
        -o "$ARTIFACTS_DIR/images/images.tar"

    # 2. Python зависимости
    echo "[2/5] Downloading Python wheels..."
    pip download -r "$PROJECT_DIR/mcp_server/requirements.txt" \
        -d "$ARTIFACTS_DIR/wheelhouse/" 2>/dev/null || \
    pip download \
        fastmcp fastapi uvicorn qdrant-client pydantic pydantic-settings \
        pyyaml sentence-transformers onnxruntime prometheus-client gitpython \
        ruff mypy pytest pytest-asyncio \
        -d "$ARTIFACTS_DIR/wheelhouse/"

    # 3. Модель BGE-M3 (предзагрузка)
    echo "[3/5] Pre-downloading BGE-M3 model..."
    python3 -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('BAAI/bge-m3', cache_folder='$ARTIFACTS_DIR/models')
" 2>&1

    # 4. Контрольные суммы
    echo "[4/5] Generating checksums..."
    cd "$ARTIFACTS_DIR"
    find . -type f -exec sha256sum {} \; > CHECKSUMS.sha256

    # 5. Упаковка
    echo "[5/5] Creating bundle..."
    cd "$PROJECT_DIR"
    tar -czf "$BUNDLE" \
        -C "$(dirname "$ARTIFACTS_DIR")" "$(basename "$ARTIFACTS_DIR")" \
        docker-compose.yml .env.example Makefile scripts/

    echo "✅ Bundle created: $PROJECT_DIR/$BUNDLE"
    echo "   Transfer to isolated host and run: ./offline-deploy.sh deploy"
}

# --- deploy: развёртывание на изолированном хосте ---
deploy() {
    echo "=== Deploying from air-gap bundle ==="
    
    # Распаковка
    [ -f "$BUNDLE" ] || { echo "ERROR: $BUNDLE not found. Run 'prepare' first."; exit 1; }
    tar -xzf "$BUNDLE"

    # Проверка целостности
    echo "[1/6] Verifying checksums..."
    cd "$ARTIFACTS_DIR"
    sha256sum -c CHECKSUMS.sha256

    # Загрузка образов
    echo "[2/6] Loading Docker images..."
    docker load -i images/images.tar

    # Установка Python-зависимостей
    echo "[3/6] Installing Python deps..."
    pip install --no-index --find-links wheelhouse/ \
        fastmcp fastapi uvicorn qdrant-client pydantic pydantic-settings \
        pyyaml sentence-transformers onnxruntime prometheus-client gitpython

    # Модели
    echo "[4/6] Placing BGE-M3 model..."
    mkdir -p /opt/mcp-knowledge/models_cache/bge-m3
    cp -r models/bge-m3/* /opt/mcp-knowledge/models_cache/bge-m3/

    # Env
    echo "[5/6] Configuring environment..."
    cd "$PROJECT_DIR"
    [ -f .env ] || cp .env.example .env
    export HF_HUB_OFFLINE=1
    export TRANSFORMERS_OFFLINE=1

    # Запуск
    echo "[6/6] Starting services..."
    docker compose up -d --wait

    echo "✅ Deployment complete. Run './offline-deploy.sh verify' to test."
}

# --- verify: дымовой тест ---
verify() {
    echo "=== Smoke test ==="
    echo -n "mcp-server /health: "
    curl -sf http://localhost:8000/health && echo "✅" || echo "❌"
    echo -n "qdrant /healthz:   "
    curl -sf http://localhost:6333/healthz && echo "✅" || echo "❌"
    echo "Done."
}

# --- Main ---
case "${1:-}" in
    prepare) prepare ;;
    deploy)  deploy ;;
    verify)  verify ;;
    *)
        echo "Usage: $0 {prepare|deploy|verify}"
        exit 1
        ;;
esac
