DOCKER_COMPOSE ?= docker compose
DATA_DIR := ../data
KNOWLEDGE_DIR := ../knowledge
MODELS_DIR := ../models_cache

.PHONY: dev down logs test lint clean dlq-replay reindex backup prereq-dirs

# Проверка и создание необходимых директорий перед запуском
prereq-dirs:
	@mkdir -p $(DATA_DIR)/{qdrant/snapshots,dlq,quality,backups} $(MODELS_DIR)
	@test -d $(KNOWLEDGE_DIR)/.git || (echo "❌ knowledge/ должен быть git-репозиторием (git init)" && exit 1)

dev: prereq-dirs
	$(DOCKER_COMPOSE) up -d --wait

down:
	$(DOCKER_COMPOSE) down

logs:
	$(DOCKER_COMPOSE) logs -f

test:
	$(DOCKER_COMPOSE) exec mcp-server pytest tests/ -v

lint:
	$(DOCKER_COMPOSE) exec mcp-server ruff check src/ tests/

clean:
	$(DOCKER_COMPOSE) down -v

# 🆕 v2.2: возврат задач из DLQ в очередь индексации
dlq-replay:
	$(DOCKER_COMPOSE) exec mcp-server python -m mcp_server.cli dlq-replay

# Полный переиндекс из Markdown SSOT
reindex:
	$(DOCKER_COMPOSE) exec mcp-server python -m mcp_server.cli reindex

# Бэкап Qdrant + SSOT
backup:
	bash scripts/backup.sh

# ═══════════════════════════════════════════════════════════════
# Torch GPU/CPU установка (Фаза 8.1)
# ═══════════════════════════════════════════════════════════════

.PHONY: install-gpu install-cpu
install-gpu:  ## Установить CUDA-12 torch (cu121) для GPU-инференса (driver 535 / CUDA 12.2)
	.venv/bin/pip install --upgrade --force-reinstall torch \
	    --index-url https://download.pytorch.org/whl/cu121

install-cpu:  ## Альтернатива: CPU-only torch (air-gap / dev, без CUDA-deps)
	.venv/bin/pip install --upgrade --force-reinstall torch \
	    --index-url https://download.pytorch.org/whl/cpu
