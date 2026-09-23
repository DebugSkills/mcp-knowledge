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

# ═══════════════════════════════════════════════════════════════
# PROD-обвязка (code-2026-09-22-002 Ф2) — passthrough в ansible/
# ═══════════════════════════════════════════════════════════════
# Апдейт кода прода (preflight→pull→build→up→health→migrations) и read-only
# диагностика. Перед prod-update ВСЕГДА смотреть prod-update-check.
# Переменные пробрасываются: S=<сервис> N=<строк логов> M=<строк событий>;
#   make prod-logs S=mcp-server N=500

.PHONY: prod-update prod-update-check prod-logs prod-events prod-health prod-metrics prod-stats \
        prod-backup prod-backup-verify prod-restore \
        prod-errors prod-errors-report prod-errors-prune prod-errors-sources test-errors

prod-update:  ## Прод: идемпотентный апдейт кода (гейты preflight + migrations pause)
	$(MAKE) -C ansible update

prod-update-check:  ## Прод: dry-run апдейта (--check --diff) — смотреть ПЕРЕД prod-update
	$(MAKE) -C ansible update-check

prod-logs:  ## Прод: логи сервисов (S=<сервис> N=<строк>; дефолт: все/200)
	$(MAKE) -C ansible logs $(if $(S),S=$(S)) $(if $(N),N=$(N))

prod-events:  ## Прод: события [START|RECONCILE|...] за 24ч (M=<строк>, дефолт 2000)
	$(MAKE) -C ansible events $(if $(M),M=$(M))

prod-health:  ## Прод: health-пробы mcp-server/qdrant/ollama/kb-console
	$(MAKE) -C ansible health

prod-metrics:  ## Прод: Prometheus-метрики :8000/metrics
	$(MAKE) -C ansible metrics

prod-stats:  ## Прод: docker stats --no-stream
	$(MAKE) -C ansible stats

prod-backup:  ## Прод: полный бэкап (qdrant+weekly+ssot+console+secrets)
	$(MAKE) -C ansible backup

prod-backup-verify:  ## Прод: проверка восстановимости бэкапов (test-restore+sha256+drill)
	$(MAKE) -C ansible backup-verify

prod-restore:  ## Прод: ВОССТАНОВЛЕНИЕ (деструктивно; SCOPE=… RESTORE_CONFIRM=yes)
	$(MAKE) -C ansible restore $(if $(SCOPE),SCOPE=$(SCOPE)) $(if $(RESTORE_SNAPSHOT),RESTORE_SNAPSHOT=$(RESTORE_SNAPSHOT)) RESTORE_CONFIRM=$(RESTORE_CONFIRM)

# ─── Error→Rule (code-2026-09-22-003 Ф4): наблюдаемость ошибок ───
# Sink: {{ data_root }}/logs/errors (вне клона/контейнеров). P2-1: требуется
# vault_password_file в ansible/ansible.cfg (vault.yml шифрован даже для RO-тегов).

prod-errors:  ## Прод: топ-сигнатур sink, P0 первыми (read-only)
	$(MAKE) -C ansible errors

prod-errors-report:  ## Прод: weekly-отчёт 6 секций (+TG при TG=1)
	$(MAKE) -C ansible errors-report $(if $(TG),TG=$(TG))

prod-errors-prune:  ## Прод: ретенция sink (dry-run; реальное удаление CONFIRM=--confirm)
	$(MAKE) -C ansible errors-prune $(if $(CONFIRM),CONFIRM=$(CONFIRM))

prod-errors-sources:  ## Прод: E5-проверка реестра охвата источников ошибок
	$(MAKE) -C ansible errors-sources

test-errors:  ## Error→Rule: юнит-тесты коллектора + E5-реестр (P2-9)
	.venv/bin/python -m pytest tests/test_error_sources.py tests/test_errors_lib.py -v

# ═══════════════════════════════════════════════════════════════
# Preflight (code-2026-09-22-004) — pre-push гейт вместо CI/CD
# ═══════════════════════════════════════════════════════════════

.PHONY: preflight preflight-quick preflight-full hooks-install hooks-uninstall

preflight:  ## Pre-push гейт: G1-G10 (lint/unit×2/E5/compose/ansible/shell/smoke/make)
	bash scripts/preflight.sh

preflight-quick:  ## Быстрый гейт ≤60с: G1 lint + G4 root + G5 E5 + G10 make
	bash scripts/preflight.sh --quick

preflight-full:  ## Полный: default + e2e и контейнерные test/lint (нужен стек)
	bash scripts/preflight.sh --full

hooks-install:  ## Установить .git/hooks/pre-push → preflight (escape: --no-verify)
	@printf '#!/usr/bin/env bash\nexec "$$(git rev-parse --show-toplevel)/scripts/preflight.sh"\n' \
		> .git/hooks/pre-push && chmod +x .git/hooks/pre-push
	@echo "✔ pre-push hook установлен (preflight). Обход при необходимости: git push --no-verify"

hooks-uninstall:  ## Удалить pre-push hook (в .trash/, обратимо)
	@mkdir -p .trash
	@mv .git/hooks/pre-push ".trash/pre-push-hook-$$(date +%Y%m%d-%H%M%S)" 2>/dev/null \
		&& echo "✔ pre-push hook удалён (копия в .trash/)" \
		|| echo "— hook не был установлен"
