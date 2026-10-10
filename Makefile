DOCKER_COMPOSE ?= docker compose
DATA_DIR := ../data
KNOWLEDGE_DIR := ../knowledge
MODELS_DIR := ../models_cache

# Маркер air-gap узла: файл есть → deploy/push запрещены (сборка из
# исходников требует интернета; обновление узла — только пакетом).
AIRGAP_NODE_MARKER ?= /etc/mcp-knowledge/airgap-node

.PHONY: dev deploy down deploy-latest-log deploy-follow logs test lint clean dlq-replay reindex backup prereq-dirs errors-view errors-report errors-alert errors-notify-import errors-cron-install errors-cron-remove errors-cron-status errors-cron-cleanup prod-errors-cron-cleanup help

# help: self-documenting список команд (docstring через `##` попадает сюда)
help:  ## Список команд (docstring через ##)
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | \
		awk -F ':.*## ' '{printf "  %-24s %s\n", $$1, $$2}'

# Проверка и создание необходимых директорий перед запуском
prereq-dirs:
	@mkdir -p $(DATA_DIR)/{qdrant/snapshots,dlq,quality,backups} $(MODELS_DIR) .trash
	@test -d $(KNOWLEDGE_DIR)/.git || (echo "❌ knowledge/ должен быть git-репозиторием (git init)" && exit 1)

# dev: сервер + kb-console с ЖИВЫМИ логами (foreground, Ctrl+C — стоп).
# Логи ДУБЛИРУЮТСЯ в .trash/dev-<timestamp>.log — агент/разбор инцидентов
# читают файл, а не консоль. Требуются собранные образы: `make deploy` один раз.
DEV_LOG := .trash/dev-$(shell date +%Y%m%d-%H%M%S).log

# ── Уровни вывода deploy (verbosity) ──────────────────────────────────────
# V=0 (по умолчанию): в терминал — только фазы + компактный статус контейнеров;
#                     полный вывод docker build → лог-файл (как dev-*.log).
# V=1: полный поток в терминал (и дубль в лог).
# Провал всегда печатает хвост лога (тихий режим не скрывает ошибку).
# Просмотр: make deploy-latest-log / make deploy-follow
V ?= 0
DEPLOY_LOG := .trash/deploy-$(shell date +%Y%m%d-%H%M%S).log
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

# deploy-latest-log: путь к последнему лог-файлу make deploy (для агента/диагностики)
deploy-latest-log:
	@ls -t .trash/deploy-*.log 2>/dev/null | head -1 || echo "нет логов .trash/deploy-*.log"

# deploy-follow: следить за свежим логом deploy в реальном времени (Ctrl+C — выход)
deploy-follow:
	@LOG=$$(ls -t .trash/deploy-*.log 2>/dev/null | head -1); \
	if [ -z "$$LOG" ]; then echo "нет логов .trash/deploy-*.log — запустите make deploy"; exit 1; fi; \
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

# deploy: сборка образов (mcp-server + kb-console) и запуск стека в фоне.
# На air-gap узле (маркер AIRGAP_NODE_MARKER) запрещено — обновление пакетом.
deploy: prereq-dirs  ## Сборка+запуск стека в фоне (НЕ для air-gap узла — маркер-гвард)
	@if [ -f "$(AIRGAP_NODE_MARKER)" ]; then \
		echo "✋ Это air-gap узел (маркер $(AIRGAP_NODE_MARKER)): обновление — только пакетом (make bundle-pack → перенос → на узле make airgap-update BUNDLE=…), сборка из исходников на узле недопустима"; \
		exit 1; \
	fi
	@mkdir -p .trash
	@if [ "$(V)" = "1" ]; then \
		echo "🏗  deploy: V=1 — полный вывод (дубль в $(DEPLOY_LOG))"; \
		$(DOCKER_COMPOSE) up -d --build --force-recreate 2>&1 | tee $(DEPLOY_LOG); \
	else \
		echo "🏗  deploy: сборка/перезапуск (тихий режим; детали → $(DEPLOY_LOG); полный вывод: make deploy V=1)"; \
		if $(DOCKER_COMPOSE) --progress quiet up -d --build --force-recreate >$(DEPLOY_LOG) 2>&1; then \
			echo "✔ deploy ok"; \
			$(DOCKER_COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}'; \
		else \
			code=$$?; \
			echo "✖ deploy FAILED (exit $$code) — хвост $(DEPLOY_LOG):"; \
			tail -n 40 $(DEPLOY_LOG); \
			exit $$code; \
		fi; \
	fi

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

# Ф6b: green-field cutover-драйвер (идемпотентный; dry-run по умолчанию).
#   make cutover                                      — dry-run (план, ничего не меняет)
#   make cutover APPLY=1 CONFIRM_DESTRUCTIVE=<token>  — реальный прогон (снос шага 4 — только по confirm)
cutover:  ## Ф6b: green-field cutover-драйвер (dry-run; APPLY=1 + CONFIRM_DESTRUCTIVE=TOKEN)
	.venv/bin/python scripts/cutover.py $(if $(APPLY),--apply,) \
	    $(if $(CONFIRM_DESTRUCTIVE),--confirm-destructive $(CONFIRM_DESTRUCTIVE),) \
	    $(ARGS)

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

airgap-verify:  ## Air-gap: проверка изолированного контура — smoke + E2E S1-S19 (offline-deploy.sh)
	./scripts/offline-deploy.sh verify

# Deprecated-alias (038 Ф3): оставить 1 релиз, чтобы не ломать привычку.
prod-verify:
	@echo "⚠️  prod-verify переименован в airgap-verify (038) — путаница с verify-deploy (post-deploy)."
	@$(MAKE) airgap-verify

# ─── Offline-update (038 Ф3): полный пакет + идемпотентное применение ───
# Поток: (интернет-машина) make update-bundle [ARGS="--with-models"]
#        → (носитель)  make update-bundle-verify DIR=/media/…/mcp-kb-update-….tar.gz
#        → (aikb)      make prod-update-local BUNDLE=/media/…/mcp-kb-update-….tar.gz

.PHONY: update-bundle update-bundle-verify prod-update-local bundle-pack bundle-unpack bundle-ship-usb bundle-ship-net airgap-runbook airgap-pack airgap-update

update-bundle:  ## 038: собрать пакет offline-обновления (интернет-машина; ARGS="--with-models")
	./scripts/offline-update.sh pack $(ARGS)

update-bundle-verify:  ## 038: проверка пакета на носителе (sha256 + bundle + manifest)
	./scripts/offline-update.sh inspect --check $(DIR)

prod-update-local:  ## 038: air-gap апдейт прода из пакета (BUNDLE=… [EXTRA_VARS="-e …"]; перед применением — update-local-check)
	$(MAKE) -C ansible update-local BUNDLE=$(BUNDLE) $(EXTRA_VARS)

# ─── Air-gap подмножество (038 Ф3+): пакет кода + ТОЛЬКО нужных образов ───
# Тот же поток, что update-bundle, но без пересборки своих образов и без внешних
# (qdrant/caddy) и моделей — для узла, где они уже стоят. Manifest несёт и id
# (config-digest), и digest OCI-манифеста → сверка на узле store-агностична (Н11).
#   make airgap-pack ARGS="--image kb-console:prod --out /media/usb"
airgap-pack:  ## Air-gap: пакет-подмножество (код + локальные образы) — ARGS="--image IMG --out DIR"
	./scripts/airgap-pack-subset.sh $(ARGS)

# ─── Air-gap апдейт узла ОДНОЙ командой: playbook берётся ИЗ ПАКЕТА (O24-proof) ───
#   make airgap-update BUNDLE=/var/tmp/update-bundle/mcp-kb-update-<ISO>.tar.gz SKIP_BACKUP=1
# Сам распаковывает пакет, тянет СВЕЖИЕ ansible/ + playbook из пакета, играет от
# inventory узла; SKIP_BACKUP=1 → -e update_skip_backup=true; CHECK=1 → --check --diff;
# VERIFY=1 → пост-апдейтный verify (scripts/verify-deploy.sh из каталога узла).
airgap-inventory ?= /root/mcp-knowledge/ansible/inventory/
airgap-update:  ## Air-gap: апдейт узла одной командой (BUNDLE=… [SKIP_BACKUP=1] [CHECK=1] [VERIFY=1])
	@test -n "$(BUNDLE)" || { echo 'usage: make airgap-update BUNDLE=<пакет.tar.gz|каталог> [SKIP_BACKUP=1] [CHECK=1] [VERIFY=1]'; exit 1; }
	$(MAKE) -C ansible update-airgap BUNDLE="$(BUNDLE)" INVENTORY_DIR="$(airgap-inventory)" \
	  SKIP_BACKUP=$(SKIP_BACKUP) CHECK=$(CHECK)
	@if [ "$(VERIFY)" = "1" ]; then \
		echo "~~~ post-update verify…"; \
		bash scripts/verify-deploy.sh || exit $$?; \
	fi

bundle-pack:  ## 038: полный офлайн-бандл (образы+код+модели+carrier+python-база) — прогресс и лог (ARGS=…)
	./scripts/airgap-bundle-pack.sh $(ARGS)

bundle-unpack:  ## 038: распаковка бандла на узле (docker load + установка моделей) — прогресс и лог (BUNDLE=…)
	./scripts/airgap-bundle-unpack.sh --bundle "$(BUNDLE)" $(ARGS)

bundle-ship-usb:  ## 038: бандл → USB (USB=/media/…) — прогресс, sha256-сверка, чек-лист узла (--checklist-only)
	./scripts/airgap-bundle-ship.sh --usb "$(USB)" $(ARGS)

bundle-ship-net:  ## 038: бандл → узел по сети (ARGS="--host JUMP" | ARGS="--pipe-via JUMP") — resumable (--checklist-only)
	./scripts/airgap-bundle-ship.sh $(ARGS)

airgap-runbook:  ## 038: напечатать полный ранбук air-gap (сборка→перенос→установка→приёмка)
	@cat docs/operations/airgap-first-install.md

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
	.venv/bin/python -m pytest tests/test_error_sources.py tests/test_errors_lib.py tests/test_errors_svyazi_pull.py tests/test_errors_guard.py tests/test_errors_notify.py tests/test_errors_alert.py tests/test_errors_shell.py tests/test_errors_cron_cleanup.py tests/test_errors_report.py tests/test_errors_digest.py tests/test_errors_tick.py tests/test_errors_watchdog.py -v

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

errors-digest:  ## T1 (A1): короткая утренняя сводка хостов (dry-run, печатает текст; FULL=1 → подробная)
	.venv/bin/python scripts/errors_report.py --digest $(if $(FULL),--full)

errors-tick:  ## T1 (A2): единый оповещатель tick (dry-run по умолчанию; TG=1 → отправка)
	.venv/bin/python scripts/errors_tick.py $(if $(TG),--send-tg)

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
#
# Квотинг (code-2026-10-03-f1-quoting): SIG/REASON/UNTIL передаются скрипту
# ЧЕРЕЗ ОКРУЖЕНИЕ (--from-env), а не интерполяцией "$(SIG)" в shell-рецепт —
# сигнатуры с кавычками/пайпами/пробелами не искажаются молча и не роняют shell.

export SIG REASON UNTIL

errors-guard-add:  ## Гвард: заглушить ТОЧНУЮ сигнатуру (SIG=… REASON=… [UNTIL=YYYY-MM-DD]; кавычки в SIG ок)
	.venv/bin/python scripts/errors_guard.py add --from-env

errors-guard-remove:  ## Гвард: снять глушение (SIG=…; кавычки в SIG ок)
	.venv/bin/python scripts/errors_guard.py remove --from-env

errors-resolve:  ## 028-B: пометить сигнатуру исправленной (SIG=… [REASON=…] [ACTOR=…] [DRY=1])
	.venv/bin/python scripts/errors_alert.py --resolve "$(SIG)" $(if $(REASON),--reason "$(REASON)") $(if $(ACTOR),--actor "$(ACTOR)") $(if $(DRY),--dry-run)

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

.PHONY: verify-deploy push push-fast

verify-deploy:  ## Post-deploy проверки стека: /health + логи + MCP tools + консоль (auth-aware)
	bash scripts/verify-deploy.sh

# push: preflight уже прогнан зависимостью (шаг 1), поэтому git push идёт с
# --no-verify — иначе pre-push hook (.git/hooks/pre-push → preflight.sh)
# погнал бы гейт ВТОРОЙ раз (~4-5 мин). Штатный escape задокументирован
# в README («git push --no-verify — escape-hatch»).
push: preflight  ## Пуш+деплой стека: preflight → git push --no-verify → deploy → verify-deploy (НЕ для air-gap узла)
	@if [ -f "$(AIRGAP_NODE_MARKER)" ]; then \
		echo "✋ Это air-gap узел (маркер $(AIRGAP_NODE_MARKER)): обновление — только пакетом (make bundle-pack → перенос → на узле make airgap-update BUNDLE=…), сборка из исходников на узле недопустима"; \
		exit 1; \
	fi
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
		echo "    Диагностика: make logs / make deploy-latest-log / make dev-latest-log / skill mcp-knowledge-prod-ops."; \
		exit 1; \
	fi

# push-fast: fail-safe быстрый режим пуша для config/docs-правок (ansible/**,
# docs/**, *.md, .knowledge/**, plans/**, README, AGENTS.md, .gitignore).
# Сначала classify-changes.sh решает, можно ли без код-гейтов: при ЛЮБОМ
# код-пути (scripts/, mcp_server/, kb-console/, tests/, Makefile, docker-compose*,
# pyproject, requirements*) он отказывает → используйте make push. Прогоняется
# ТОЛЬКО preflight --config-only (G6+G7+G8+G10), сводка помечается
# mode=config-only (это НЕ полный preflight — частичный прогон за полный не выдаётся).
push-fast:  ## Пуш+деплой config/docs-правок (fail-safe: код-пути → отказ; полный preflight НЕ гоняется)
	@cls="$$(bash scripts/classify-changes.sh)"; \
	case "$$cls" in \
	  config-only) : ;; \
	  code) echo "❌ push-fast · config-only: изменения затрагивают код → используйте make push (полный preflight обязателен)."; exit 1 ;; \
	  none) echo "ℹ️  push-fast · config-only: нечего пушить (изменений нет)."; exit 1 ;; \
	  *) echo "❌ push-fast: неожиданный результат классификации '$$cls' → используйте make push."; exit 1 ;; \
	esac; \
	echo ""; \
	echo "push-fast · config-only ($$cls): прогон preflight --config-only — только G6/G7/G8/G10, это НЕ полный preflight…"; \
	bash scripts/preflight.sh --config-only || exit 1; \
	echo ""; \
	echo "✔ preflight --config-only пройден → git push --no-verify (гейт уже прогнан выше)"; \
	git push --no-verify || exit 1; \
	echo ""; \
	echo "✔ код отправлен → deploy…"; \
	$(MAKE) deploy || exit 1; \
	echo ""; \
	echo "deploy завершён → post-deploy проверки (verify-deploy)…"; \
	if $(MAKE) verify-deploy; then \
	  echo ""; \
	  echo "✅ push-fast complete: config-правки запушены, стек задеплоен и проверен (mode=config-only)."; \
	else \
	  echo ""; \
	  echo "⚠️  ВНИМАНИЕ: код УЖЕ запушен, но стек требует разбора — verify-deploy нашёл проблемы."; \
	  echo "    Диагностика: make logs / make deploy-latest-log / make dev-latest-log / skill mcp-knowledge-prod-ops."; \
	  exit 1; \
	fi

# ── AI-workspace Ф1 (arch-2026-10-05-ai-workspace): LLM-шлюз LiteLLM ──────────
# Порт 4000 НЕ публикуется (I1/I6 — только внутренняя сеть mcp-knowledge_default).
# Конфиг по умолчанию — обе полки (local+ext); local_only: GATEWAY_CONFIG=litellm.local_only.config.yaml
GATEWAY_COMPOSE := compose.gateway.yml
GATEWAY_CONTAINER := mcp-knowledge-litellm
GATEWAY_CONFIG ?= litellm.config.yaml
GATEWAY_K ?= 1

.PHONY: gateway-up gateway-down gateway-logs gateway-health gateway-canary
gateway-up: ## Ф1: поднять LLM-шлюз (start_period до 120s → проверь make gateway-health)
	docker compose -f $(GATEWAY_COMPOSE) up -d

gateway-down: ## Ф1: остановить LLM-шлюз
	docker compose -f $(GATEWAY_COMPOSE) down

gateway-logs: ## Ф1: логи LLM-шлюза (follow)
	docker compose -f $(GATEWAY_COMPOSE) logs -f --tail=100

gateway-health: ## Ф1: liveliness + per-deployment /health (изнутри контейнера; ext без ключа → partial, допустимо)
	@docker exec $(GATEWAY_CONTAINER) python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness', timeout=10); print('liveliness: OK')" \
	  && echo "liveliness: OK" || { echo "❌ liveliness: FAIL"; exit 1; }
	@key=$$(grep -m1 '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-); \
	docker exec -e LITELLM_MASTER_KEY="$$key" $(GATEWAY_CONTAINER) python3 -c \
	  "import os,urllib.request; req=urllib.request.Request('http://localhost:4000/health',headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}); print(urllib.request.urlopen(req,timeout=120).read().decode()[:800])" \
	  || echo "⚠️  /health partial (ext unhealthy без DEEPSEEK_API_KEY — допустимо, Ф1.E-i)"

gateway-canary: ## Ф1: K+1-проба → 429 throttling_error (fail-closed W==1). K: make gateway-canary K=2
	docker exec -i $(GATEWAY_CONTAINER) python3 - --k $(GATEWAY_K) --model local \
	  --base-url http://127.0.0.1:4000 < scripts/gateway_canary.py

# ── AI-workspace Ф3.1 (arch-2026-10-05-ai-workspace): каркас + job-store ──────
# Job-store ws:job:{id} (статус-машина, CAS по version, epoch-fencing) —
# см. ai_workspace/README.md. Порт 6390 — ТОЛЬКО test-only overlay (I6).
WS_COMPOSE := compose.workspace.yml
WS_COMPOSE_TEST := compose.workspace.test.yml
WS_TEST_REDIS_URL := redis://127.0.0.1:6390/0

.PHONY: ws-up ws-down ws-up-test ws-test ws-test-integration ws-budget-reconcile ws-quota-sweep ws-prio ws-redis-check quotas-set quotas-show modes-validate golden-run f47-run
ws-up: ## Ф3.1: поднять ws-redis (порт НЕ публикуется — I6, internal-only)
	docker compose -f $(WS_COMPOSE) up -d ws-redis

ws-down: ## Ф3.1: остановить ws-redis (только его, не workspace-консоль)
	docker compose -f $(WS_COMPOSE) stop ws-redis

ws-up-test: ## Ф3.1: ws-redis с test-only overlay (127.0.0.1:6390, loopback)
	docker compose -f $(WS_COMPOSE) -f $(WS_COMPOSE_TEST) up -d ws-redis

ws-test: ## Ф3.1: unit-тесты ai_workspace (без Redis; integration авто-skip)
	.venv/bin/python -m pytest ai_workspace/tests -q

ws-test-integration: ## Ф3.1: integration-тесты (сначала make ws-up-test)
	WS_REDIS_URL=$(WS_TEST_REDIS_URL) .venv/bin/python -m pytest ai_workspace/tests -q -m integration

ws-budget-reconcile: ## Ф4-rev P0-1: ночная сверка ws:budget:* с журналом (WS_REDIS_URL — ПРОД ws-redis, дефолта НЕТ; владелец — оператор, nightly)
	.venv/bin/python scripts/ws_budget_reconcile.py

ws-quota-sweep: ## Ф4.2e P1-3: свип истёкших conc-резервов (WS_REDIS_URL — ПРОД ws-redis, дефолта НЕТ; владелец — оператор, ≤60с до wiring-tick Ф4.7)
	.venv/bin/python scripts/ws_quota_sweep.py

ws-prio: ## Ф4.5a D8: per-job приоритет ws:prio:{job} (WS_REDIS_URL — ПРОД ws-redis, дефолта НЕТ): make ws-prio ARGS="set --job J --prio high [--ttl S] [--actor A] [--reason R]" | clear --job J | show --job J
	.venv/bin/python scripts/ws_prio.py $(ARGS)

ws-redis-check: ## Ф6 TODO 8 (I12/D2): память ПРОД ws-redis (read-only): used/max/% из INFO memory; при used>=80% maxmemory (160mb при 200mb) — make errors-alert (сообщение генерится ошибками sink, цель не принимает произвольный текст; dry-run по умолчанию, TG=1 -> отправка)
	@.venv/bin/python -c "import subprocess,sys;r=subprocess.run(['docker','exec','mcp-knowledge-ws-redis','redis-cli','INFO','memory'],capture_output=True,text=True);r.returncode and (sys.stderr.write('ws-redis недоступен: '+(r.stderr.strip() or ('exit '+str(r.returncode)))+'\n'),sys.exit(r.returncode));d={k:v for k,_,v in (l.strip().partition(':') for l in r.stdout.splitlines()) if _};u=int(d.get('used_memory') or 0);m=int(d.get('maxmemory') or 0);print(f'ws-redis memory: used={u/1048576:.1f}MB / max='+(f'{m/1048576:.1f}MB ({100*u/m:.0f}%)' if m else 'unlimited (0)'));(m and u*10>=m*8) and (print(f'ALERT: ws-redis used>=80% maxmemory ({u/1048576:.1f}MB из {m/1048576:.1f}MB) — вызываю make errors-alert (сообщение генерится ошибками sink; TG=1 — реальная отправка)',file=sys.stderr),sys.exit(subprocess.run(['$(MAKE)','errors-alert']).returncode))"

quotas-set: ## Ф4.5c-1: правка quotas.yaml с хоста (dry-run по умолчанию; --apply = бэкап .trash + атомарная запись + пост-валидация; рантайм подхватит по mtime, рестарт не нужен): make quotas-set ARGS="set --role member --priority high [--tokens N|none] [--conc N|none] [--grants heavy,fast,local-only] [--budget-ext RUB] [--apply]"
	.venv/bin/python scripts/quotas_set.py $(ARGS)

quotas-show: ## Ф4.5c-1: показать participant-роли и бюджеты quotas.yaml: make quotas-show [ARGS="--json"]
	.venv/bin/python scripts/quotas_set.py show $(ARGS)

modes-validate: ## Ф3.5: валидация режимов ai_workspace/modes/*.yaml
	.venv/bin/python -m ai_workspace.tools.modes_validate

golden-run: ## Ф3.10: golden-run «статья» (T/I/Q) → отчёт в plans/_provenance (нужен ws-up-test или ws-up)
	WS_REDIS_URL=$(WS_TEST_REDIS_URL) .venv/bin/python -m ai_workspace.tools.golden_run --out .trash/golden-artifacts

f47-run: ## Ф4.7: приёмочный контурный прогон (нужен make ws-up-test)
	WS_REDIS_URL=$(WS_TEST_REDIS_URL) .venv/bin/python -m ai_workspace.tools.f47_acceptance
