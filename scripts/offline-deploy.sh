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

# ─── verify_model_cache: проверка целостности кэша моделей ──────
# Проверяет, что все необходимые артефакты BGE-M3 присутствуют
# и модель имеет валидный размер. Используется как в prepare()
# (после загрузки), так и в deploy() (перед установкой).
#
# Аргументы:
#   $1 — путь к директории с кэшем моделей (по умолчанию $ARTIFACTS_DIR/models)
verify_model_cache() {
    local cache_dir="${1:-$ARTIFACTS_DIR/models}"
    local errors=0

    echo "=== Verifying model cache: $cache_dir ==="

    # Проверка существования директории
    if [ ! -d "$cache_dir" ]; then
        echo "ERROR: Model cache directory not found: $cache_dir"
        exit 1
    fi

    # Поиск директории модели (может быть bge-m3/ или models--BAAI--bge-m3/)
    local model_root
    model_root=$(find "$cache_dir" -type d -name "*bge-m3*" -print -quit 2>/dev/null || true)
    if [ -z "$model_root" ]; then
        echo "ERROR: No BGE-M3 model directory found in $cache_dir"
        echo "  Expected: bge-m3/ or models--BAAI--bge-m3/ or similar"
        exit 1
    fi
    echo "  Model directory: $model_root"

    # Список обязательных артефактов
    local required_files=(
        "config.json"
        "tokenizer.json"
        "tokenizer_config.json"
        "vocab.txt"
        "special_tokens_map.json"
    )

    local missing_count=0
    for fname in "${required_files[@]}"; do
        local found
        found=$(find "$model_root" -name "$fname" -print -quit 2>/dev/null || true)
        if [ -n "$found" ]; then
            local fsize
            fsize=$(stat -c%s "$found" 2>/dev/null || echo "0")
            echo "  ✅ $fname ($fsize bytes)"
        else
            echo "  ❌ MISSING: $fname"
            missing_count=$((missing_count + 1))
            errors=1
        fi
    done

    # Проверка весов модели (> 100 MB)
    local model_file
    model_file=$(find "$model_root" \( -name "*.safetensors" -o -name "pytorch_model.bin" -o -name "pytorch_model-*.bin" \) -print -quit 2>/dev/null || true)
    if [ -z "$model_file" ]; then
        echo "  ❌ MISSING: model weights file (*.safetensors or pytorch_model.bin)"
        errors=1
    else
        local model_size
        model_size=$(stat -c%s "$model_file" 2>/dev/null || echo "0")
        local model_size_mb=$((model_size / 1048576))
        if [ "$model_size" -lt 104857600 ]; then
            echo "  ❌ Model weights too small: $model_size_mb MB (need > 100 MB)"
            echo "     File: $model_file"
            errors=1
        else
            echo "  ✅ Model weights: $(basename "$model_file") ($model_size_mb MB)"
        fi
    fi

    # Подсчёт общего числа артефактов и размера
    local artifact_count
    artifact_count=$(find "$model_root" -type f | wc -l)
    local total_size
    total_size=$(du -sh "$model_root" 2>/dev/null | cut -f1 || echo "unknown")

    echo ""
    echo "  Model artifacts: $artifact_count files, $total_size total"

    if [ "$errors" -eq 1 ]; then
        echo ""
        echo "❌ Model cache verification FAILED: $missing_count missing, or weights invalid."
        echo "   Re-run 'prepare' on an internet-connected machine to re-download."
        exit 1
    fi

    echo "✅ All BGE-M3 artifacts present and valid."
}

# ─── prepare: сборка артефактов на машине с интернетом ─────────
prepare() {
    echo "=== Preparing air-gap artifacts ==="
    mkdir -p "$ARTIFACTS_DIR"/{images,wheelhouse,models}

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
print(f'BGE-M3 loaded: dim={model.get_sentence_embedding_dimension()}')
" 2>&1

    # 4. Контрольные суммы
    echo "[4/5] Generating checksums..."
    cd "$ARTIFACTS_DIR"
    find . -type f -exec sha256sum {} \; > CHECKSUMS.sha256

    # 5. Верификация модели + упаковка
    echo "[5/5] Verifying model cache and creating bundle..."

    # Вызываем verify_model_cache ДО упаковки
    verify_model_cache "$ARTIFACTS_DIR/models"

    # Сводка по артефактам
    echo ""
    echo "=== Artifact Summary ==="
    echo "  Docker images:"
    ls -lh "$ARTIFACTS_DIR/images/images.tar" | awk '{print "    images.tar: " $5}'
    echo "  Python wheels: $(find "$ARTIFACTS_DIR/wheelhouse" -name '*.whl' | wc -l) files"
    du -sh "$ARTIFACTS_DIR/wheelhouse" | awk '{print "    total: " $1}'
    echo "  Model cache:"
    local model_dir_for_summary
    model_dir_for_summary=$(find "$ARTIFACTS_DIR/models" -type d -name "*bge-m3*" -print -quit 2>/dev/null || echo "$ARTIFACTS_DIR/models")
    echo "    artifacts: $(find "$model_dir_for_summary" -type f | wc -l) files"
    du -sh "$model_dir_for_summary" | awk '{print "    total: " $1}'
    echo ""

    # Упаковка bundle
    cd "$PROJECT_DIR"
    tar -czf "$BUNDLE" \
        -C "$(dirname "$ARTIFACTS_DIR")" "$(basename "$ARTIFACTS_DIR")" \
        docker-compose.yml .env.example Makefile scripts/

    BUNDLE_SIZE=$(du -sh "$BUNDLE" | cut -f1)
    echo "✅ Bundle created: $PROJECT_DIR/$BUNDLE ($BUNDLE_SIZE)"
    echo "   Transfer to isolated host and run: ./offline-deploy.sh deploy"
}

# ─── deploy: развёртывание на изолированном хосте ─────────────
deploy() {
    echo "=== Deploying from air-gap bundle ==="

    # Распаковка
    [ -f "$BUNDLE" ] || { echo "ERROR: $BUNDLE not found. Run 'prepare' first."; exit 1; }
    tar -xzf "$BUNDLE"

    # Предварительная проверка модели (до установки)
    echo "[0/6] Pre-flight model cache verification..."
    verify_model_cache "$ARTIFACTS_DIR/models"

    # Проверка целостности (checksums)
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
    mkdir -p /opt/mcp-knowledge/models_cache

    # Копируем всю структуру модели (а не только bge-m3/)
    # find нужную директорию и копируем содержимое
    local model_src
    model_src=$(find "$ARTIFACTS_DIR/models" -type d -name "*bge-m3*" -print -quit 2>/dev/null || echo "")
    if [ -n "$model_src" ] && [ -d "$model_src" ]; then
        cp -r "$model_src"/* /opt/mcp-knowledge/models_cache/
    else
        echo "WARNING: No model directory found in artifacts. Copying all models/ contents."
        cp -r "$ARTIFACTS_DIR/models"/* /opt/mcp-knowledge/models_cache/
    fi

    # Финальная проверка размещённой модели
    verify_model_cache "/opt/mcp-knowledge/models_cache"

    # Env
    echo "[5/6] Configuring environment..."
    cd "$PROJECT_DIR"
    [ -f .env ] || cp .env.example .env

    # Проверяем, что air-gap переменные установлены
    if ! grep -q "HF_HUB_OFFLINE=1" .env 2>/dev/null; then
        echo "" >> .env
        echo "# Air-gap / Offline mode" >> .env
        echo "HF_HUB_OFFLINE=1" >> .env
        echo "TRANSFORMERS_OFFLINE=1" >> .env
        echo "SENTENCE_TRANSFORMERS_HOME=/app/models_cache" >> .env
    fi

    export HF_HUB_OFFLINE=1
    export TRANSFORMERS_OFFLINE=1

    # Запуск
    echo "[6/6] Starting services..."
    docker compose up -d --wait

    echo "✅ Deployment complete. Run './offline-deploy.sh verify' to test."
}

# ─── verify: дымовой тест ─────────────────────────────────────
verify() {
    echo "=== Smoke test ==="
    echo -n "mcp-server /health/live: "
    curl -sf http://localhost:8000/health/live && echo "✅" || echo "❌"
    echo -n "mcp-server /health:      "
    curl -sf http://localhost:8000/health && echo "✅" || echo "❌"
    echo -n "qdrant /healthz:         "
    curl -sf http://localhost:6333/healthz && echo "✅" || echo "❌"
    echo "Done."
}

# ─── Main ─────────────────────────────────────────────────────
case "${1:-}" in
    prepare) prepare ;;
    deploy)  deploy ;;
    verify)  verify ;;
    *)
        echo "Usage: $0 {prepare|deploy|verify}"
        exit 1
        ;;
esac
