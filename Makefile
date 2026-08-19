DOCKER_COMPOSE ?= docker compose
DATA_DIR := ../data
KNOWLEDGE_DIR := ../knowledge
MODELS_DIR := ../models_cache

.PHONY: dev deploy down logs test lint clean dlq-replay reindex backup prereq-dirs

# Проверка и создание необходимых директорий перед запуском
prereq-dirs:
	@mkdir -p $(DATA_DIR)/{qdrant/snapshots,dlq,quality,backups} $(MODELS_DIR) .trash
	@test -d $(KNOWLEDGE_DIR)/.git || (echo "❌ knowledge/ должен быть git-репозиторием (git init)" && exit 1)

# dev: сервер + kb-console с ЖИВЫМИ логами (foreground, Ctrl+C — стоп).
# Логи ДУБЛИРУЮТСЯ в .trash/dev-<timestamp>.log — агент/разбор инцидентов
# читают файл, а не консоль. Требуются собранные образы: `make deploy` один раз.
DEV_LOG := .trash/dev-$(shell date +%Y%m%d-%H%M%S).log
dev: prereq-dirs
	@echo ""
	@echo "🚀 Поднимается стек (живые логи, Ctrl+C — стоп):"
	@echo "   • kb-console (клиент):  http://localhost:8085"
	@echo "   • mcp-server (API):     http://localhost:8000/health"
	@echo "   • Если в консоли «Authentication failed» — задайте MCP_API_KEY в .env"
	@echo "     (равным ключу из MCP_READ_KEYS сервера)"
	@echo "   • Копия логов: $(DEV_LOG)"
	@echo ""
	@$(DOCKER_COMPOSE) up mcp-server kb-console 2>&1 | tee $(DEV_LOG)

# dev-latest-log: путь к последнему лог-файлу make dev (для агента/диагностики)
dev-latest-log:
	@ls -t .trash/dev-*.log 2>/dev/null | head -1 || echo "нет логов .trash/dev-*.log"

# dev-follow: следить за свежим логом dev-стека в реальном времени (Ctrl+C — выход)
dev-follow:
	@LOG=$$(ls -t .trash/dev-*.log 2>/dev/null | head -1); \
	if [ -z "$$LOG" ]; then echo "нет логов .trash/dev-*.log — запустите make dev"; exit 1; fi; \
	echo "📡 follow: $$LOG (Ctrl+C — выход)"; tail -f "$$LOG"

# dev-follow-server: live-логи контейнера mcp-knowledge-server (docker logs -f)
dev-follow-server:
	@docker logs -f --tail 100 mcp-knowledge-server

# dev-follow-console: live-логи контейнера kb-console (docker logs -f)
dev-follow-console:
	@docker logs -f --tail 100 kb-console

# dev-resources: live-мониторинг ресурсов контейнеров (2 сек)
dev-resources:
	@docker stats --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}"

# deploy: сборка образов (mcp-server + kb-console) и запуск стека в фоне
deploy: prereq-dirs
	$(DOCKER_COMPOSE) up -d --build --force-recreate

down:
	$(DOCKER_COMPOSE) down

logs:
	$(DOCKER_COMPOSE) logs -f

test:
	$(DOCKER_COMPOSE) exec mcp-server pytest tests/ -v

lint:
	$(DOCKER_COMPOSE) exec mcp-server ruff check src/ tests/

# V3 (13.26): статическая проверка типов — контент-пайплайн (гейт 0 ошибок).
# Расширение scope на весь src — P2 (77 ошибок легаси в 17 файлах).
.PHONY: typecheck
typecheck:
	$(DOCKER_COMPOSE) exec mcp-server mypy src/mcp_server/content src/mcp_server/tools/content.py

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
# E2E-тесты (Фаза 9) — реальный Qdrant (REST localhost:6333) + Ollama
# ═══════════════════════════════════════════════════════════════

.PHONY: e2e e2e-slow
e2e:  ## E2E-тесты ключевых решений (нужен запущенный Qdrant + Ollama)
	.venv/bin/python -m pytest mcp_server/tests/e2e -m "e2e and not e2e_slow" -v

e2e-slow:  ## E2E + медленные сценарии (blue-green, GPU)
	.venv/bin/python -m pytest mcp_server/tests/e2e -m "e2e or e2e_slow" -v

bundle:  ## Собрать air-gap bundle для изолированного контура (машина с интернетом)
	./scripts/offline-deploy.sh prepare

prod-verify:  ## Высокоуровневая проверка прода: smoke + E2E S1-S19 (из docker-compose.prod.yml)
	./scripts/offline-deploy.sh verify

# ═══════════════════════════════════════════════════════════════
# kb-console (Фаза 13.7) — NiceGUI-клиент (диагностика + импорт + поиск)
# ═══════════════════════════════════════════════════════════════

.PHONY: console-build console-test
console-build:  ## Собрать образ kb-console:prod
	docker build -t kb-console:prod ./kb-console

console-test:  ## Юнит + smoke тесты kb-console (нужен установленный пакет)
	.venv/bin/python -m pytest kb-console/tests -v

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
