DOCKER_COMPOSE ?= docker compose
DATA_DIR := ../data
KNOWLEDGE_DIR := ../knowledge
MODELS_DIR := ../models_cache

.PHONY: dev deploy down logs test lint clean dlq-replay reindex backup prereq-dirs errors-view errors-report errors-alert errors-notify-import errors-cron-install errors-cron-remove errors-cron-status errors-cron-cleanup prod-errors-cron-cleanup

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
        prod-errors prod-errors-report prod-errors-prune prod-errors-sources test-errors \
        errors-guard-add errors-guard-remove errors-guard-list

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

prod-errors-cron-cleanup:  ## Прод: 018 миграция legacy cron-ключей exit=0 из P2 (dry-run; CONFIRM=--confirm)
	$(MAKE) -C ansible errors-cron-cleanup $(if $(CONFIRM),CONFIRM=$(CONFIRM))

prod-errors-sources:  ## Прод: E5-проверка реестра охвата источников ошибок
	$(MAKE) -C ansible errors-sources

test-errors:  ## Error→Rule: юнит-тесты коллектора + гварда + sender/алертов + shell + E5-реестр (P2-9 008; P3-e 016; T1-T8 018)
	.venv/bin/python -m pytest tests/test_error_sources.py tests/test_errors_lib.py tests/test_errors_guard.py tests/test_errors_notify.py tests/test_errors_alert.py tests/test_errors_shell.py tests/test_errors_cron_cleanup.py -v

# ─── TG-оповещения Error→Rule (code-2026-09-24-016) ───
# Каналы: weekly-отчёт (Пн 10:02) + немедленные алерты new-P0/burst (*/5).
# notify.json: прод рендерит ansible (errors-notify.json.j2); локально —
# sudo-хелпер из /etc/backup-status.env (0600). Д1: запуск БЕЗ внешнего
# sudo — sudo уже в рецепте (двойной sudo → SUDO_USER=root → root-владелец,
# cron-юзер не читает). Живая отправка после: 1) make errors-notify-import;
# 2) make errors-cron-install.

errors-view:  ## 016: сводка sink (агрегаты/флаги) — что уйдёт в TG
	.venv/bin/python scripts/errors_report.py

errors-report:  ## 016: weekly-отчёт 6 секций, --weekly обязателен (TG=1 → отправка; по умолчанию stdout)
	.venv/bin/python scripts/errors_report.py --weekly $(if $(TG),--send-tg)

errors-alert:  ## 016: немедленные алерты new-P0/burst (TG=1 → отправка; dry-run по умолчанию)
	.venv/bin/python scripts/errors_alert.py $(if $(TG),--send-tg)

errors-notify-import:  ## 016: sudo-хелпер notify.json 0600 из /etc/backup-status.env (БЕЗ внешнего sudo!; ENV=… OUT=… HOST=…)
	sudo bash scripts/errors_notify_import.sh $(if $(ENV),--env $(ENV)) $(if $(OUT),--out $(OUT)) $(if $(HOST),--host $(HOST))

errors-cron-install:  ## 016+028: установить 4 cron-джобы (collector/alerts */5, weekly Пн 10:02, prune Пн 10:33); FILE=… модель
	bash scripts/errors_cron.sh --install $(if $(FILE),--file $(FILE))

errors-cron-remove:  ## 016: снять cron-джобы 016 (обратимо; FILE=… модель)
	bash scripts/errors_cron.sh --remove $(if $(FILE),--file $(FILE))

errors-cron-status:  ## 016+028: статус cron-джоб (4) + config-оверлея (FILE=… модель)
	bash scripts/errors_cron.sh --status $(if $(FILE),--file $(FILE))

errors-cron-cleanup:  ## 018: миграция legacy cron-ключей exit=0 из P2-рейтинга (dry-run; CONFIRM=--confirm; SINK=…)
	.venv/bin/python scripts/errors_cleanup_cron_legacy.py $(if $(CONFIRM),$(CONFIRM)) $(if $(SINK),--sink $(SINK))

# ─── Storm-guard (code-2026-09-23-008): suppression-лист known-noise ───
# Файл: $DATA_ROOT/logs/errors/suppression.json (в sink — вне клона, НЕ
# рендерится ansible, переживает деплои). Ключ — ТОЛЬКО точная сигнатура
# (диапазоны кодов/regex запрещены архитектурно). Прод: запуск на хосте aikb
# с DATA_ROOT из prod .env. Каждый add/remove пишет audit.jsonl.

errors-guard-add:  ## Гвард: заглушить ТОЧНУЮ сигнатуру (SIG=… REASON=… [UNTIL=YYYY-MM-DD])
	.venv/bin/python scripts/errors_guard.py add "$(SIG)" --reason "$(REASON)" $(if $(UNTIL),--until $(UNTIL))

errors-guard-remove:  ## Гвард: снять глушение (SIG=…)
	.venv/bin/python scripts/errors_guard.py remove "$(SIG)"

errors-guard-list:  ## Гвард: показать suppression-лист (+истёкшие)
	.venv/bin/python scripts/errors_guard.py list

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

# ═══════════════════════════════════════════════════════════════
# Push ⇒ Deploy ⇒ Verify (2026-09-24) — «пуш без деплоя = незавершённый
# пуш» одной командой. Канон: AGENTS.md «🚀 Пуш ⇒ деплой», skill
# mcp-knowledge-prod-ops, docs/observability/self-improvement-loop.md §13.9
# ═══════════════════════════════════════════════════════════════

.PHONY: verify-deploy push

verify-deploy:  ## Post-deploy проверки стека: /health + логи + MCP tools + консоль (auth-aware)
	bash scripts/verify-deploy.sh

# push: preflight уже прогнан зависимостью (шаг 1), поэтому git push идёт с
# --no-verify — иначе pre-push hook (.git/hooks/pre-push → preflight.sh)
# погнал бы гейт ВТОРОЙ раз (~4-5 мин). Штатный escape задокументирован
# в README («git push --no-verify — escape-hatch»).
push: preflight  ## Пуш+деплой стека: preflight → git push --no-verify → deploy → verify-deploy
	@echo ""
	@echo "1/4 ✔ preflight пройден → git push --no-verify (гейт уже прогнан выше)"
	@git push --no-verify
	@echo ""
	@echo "2/4 ✔ код отправлен → 3/4 деплой работающего стека…"
	@$(MAKE) deploy
	@echo ""
	@echo "4/4 деплой завершён → post-deploy проверки…"
	@if $(MAKE) verify-deploy; then \
		echo ""; \
		echo "✅ push complete: код запушен, стек задеплоен и проверен."; \
	else \
		echo ""; \
		echo "⚠️  ВНИМАНИЕ: код УЖЕ запушен, но стек требует разбора — verify-deploy нашёл проблемы."; \
		echo "    Диагностика: make logs / make dev-latest-log / skill mcp-knowledge-prod-ops."; \
		exit 1; \
	fi
