INVENTORY ?= inventory/hosts.yml
PLAYBOOK  ?= playbooks/host-prepare.yml
# vault-pass-file: вместо интерактивного --ask-vault-pass можно
#   make run VAULT_PASS_FILE=~/.vault-pass.txt
# (VAULT_ARGS переопределяется и вручную: make run VAULT_ARGS=...)
VAULT_ARGS ?= --ask-vault-pass
ifneq ($(VAULT_PASS_FILE),)
VAULT_ARGS := --vault-password-file $(VAULT_PASS_FILE)
endif
APB := ansible-playbook -i $(INVENTORY)
.PHONY: deps test lint check run run-tag vault-create vault-edit \
        update update-check update-local update-local-check update-airgap logs events health metrics stats \
        backup backup-verify restore \
        errors errors-report errors-prune errors-cron-cleanup errors-sources
deps:
	# --break-system-packages: Debian 12 PEP 668 (externally-managed); пакеты идут в ~/.local
	python3 -m pip install --user --break-system-packages -r requirements.txt
	ansible-galaxy collection install -r requirements.yml
test:
	$(APB) $(PLAYBOOK) --syntax-check
lint:
	ansible-lint playbooks/
check:
	$(APB) $(PLAYBOOK) --check --diff $(VAULT_ARGS)
run:
	$(APB) $(PLAYBOOK) $(VAULT_ARGS)
# Запуск одного тега (dirs|repos|docker|cron|host_prepare|transfer) на одном хосте:
#   make run-tag PLAYBOOK=playbooks/deploy.yml ROLE=docker HOST=aikb
run-tag:
	$(APB) $(PLAYBOOK) --tags $(ROLE) --limit $(HOST) $(VAULT_ARGS)
vault-create:
	ansible-vault create inventory/group_vars/all/vault.yml
vault-edit:
	ansible-vault edit inventory/group_vars/all/vault.yml

# ─── Ф2 (code-2026-09-22-002): update + read-only ops ───

# Идемпотентный апдейт кода прода (preflight→pull→build→up→health→migrations).
# Операторский гейт: НИКОГДА не запускать без просмотра make update-check.
update:
	$(APB) playbooks/update.yml $(EXTRA_VARS) $(VAULT_ARGS)

# Dry-run апдейта: показать, что изменилось бы (--check --diff).
update-check:
	$(APB) playbooks/update.yml --check --diff $(EXTRA_VARS) $(VAULT_ARGS)

# ─── 038 Ф3: air-gap апдейт из локального пакета (update_source=local) ───
# Пакет собирает scripts/offline-update.sh pack (make update-bundle). В начале
# прогона плейбук печатает дрейф образов (локальный .Id ↔ manifest, P2-5).
# Перед применением ВСЕГДА смотреть update-local-check (dry-run печатает план).
# EXTRA_VARS — любые доп. -e, напр. EXTRA_VARS="-e update_skip_backup=true" (Н10-обход).
update-local:
	$(APB) playbooks/update.yml -e update_source=local -e update_bundle_path=$(BUNDLE) $(EXTRA_VARS) $(VAULT_ARGS)

update-local-check:
	$(APB) playbooks/update.yml --check --diff -e update_source=local -e update_bundle_path=$(BUNDLE) $(EXTRA_VARS) $(VAULT_ARGS)

# ─── 038 Ф3+: air-gap одной командой (O24-proof: playbook берётся ИЗ ПАКЕТА) ───
#   make update-airgap BUNDLE=/var/tmp/update-bundle/mcp-kb-update-<ISO>.tar.gz [SKIP_BACKUP=1] [CHECK=1]
# Сам делает всё, что раньше требовало рук:
#   (1) распаковывает пакет (tar.gz или каталог);
#   (2) достаёт СВЕЖИЕ ansible/ + playbook ИЗ ПАКЕТА (а не из локального клона,
#       который на узле может быть устаревшим — O24) в $(AIRGAP_WORK)/tree;
#   (3) играет playbook, inventory — из ЭТОГО каталога ($(CURDIR)/inventory/);
#   (4) SKIP_BACKUP=1 → -e update_skip_backup=true (пропуск preflight-бэкапа, Н10);
#       CHECK=1 → --check --diff (dry-run).
# Код-клон (deploy_root/repo_name) playbook обновляет САМ (git fetch из пакета + ff-only).
AIRGAP_WORK ?= /var/tmp/airgap-make
# Каталог inventory для update-airgap. По умолчанию — рядом с этим Makefile
# (там hosts.yml и приватный inventory узла); на узле приватный ansible-клон
# задаётся явно: make ... INVENTORY_DIR=/root/mcp-knowledge/ansible/inventory/
INVENTORY_DIR ?= $(CURDIR)/inventory/
update-airgap:
	@set -e; missing=""; for b in git python3 ansible-playbook tar docker; do \
		command -v "$$b" >/dev/null 2>&1 || missing="$$missing $$b"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "✋ air-gap апдейт требует на узле:$$missing (установите и повторите)"; \
		exit 1; \
	fi
	@test -n "$(BUNDLE)" || { echo 'usage: make update-airgap BUNDLE=<пакет.tar.gz|каталог> [SKIP_BACKUP=1] [CHECK=1]'; exit 1; }
	@set -e; \
	rm -rf "$(AIRGAP_WORK)"; mkdir -p "$(AIRGAP_WORK)"; \
	if [ -f "$(BUNDLE)" ]; then tar -xzf "$(BUNDLE)" -C "$(AIRGAP_WORK)"; \
	  PKG="$$(find "$(AIRGAP_WORK)" -mindepth 1 -maxdepth 1 -type d -name 'mcp-kb-update-*' | head -1)"; \
	else PKG="$(BUNDLE)"; fi; \
	[ -f "$$PKG/manifest.json" ] || { echo "нет manifest.json в пакете: $$PKG"; exit 1; }; \
	OLD_BRANCH="$$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["branch"])' "$$PKG/manifest.json")"; \
	echo "~~~ пакет: $$PKG (branch=$$OLD_BRANCH)"; \
	git -C "$(AIRGAP_WORK)/tree" init -q; \
	git -C "$(AIRGAP_WORK)/tree" fetch -q "$$PKG/repo.git" "$$OLD_BRANCH"; \
	git -C "$(AIRGAP_WORK)/tree" checkout -q FETCH_HEAD -- ansible; \
	PB="$(AIRGAP_WORK)/tree/ansible/playbooks/update.yml"; \
	[ -f "$$PB" ] || { echo "в пакете нет ansible/playbooks/update.yml — пересоберите пакет"; exit 1; }; \
	echo "~~~ playbook из пакета: $$PB"; \
	cd "$(AIRGAP_WORK)/tree/ansible" && \
	ansible-playbook -i "$(INVENTORY_DIR)" playbooks/update.yml \
	  -e update_source=local -e update_bundle_path="$(BUNDLE)" \
	  $(if $(filter 1,$(SKIP_BACKUP)),-e update_skip_backup=true,) \
	  $(if $(CHECK),--check --diff,) \
	  $(VAULT_ARGS)

# Read-only операции (ops.yml, БЕЗ vault-промпта — плейбук не использует секретов).
# Примеры:
#   make logs                       # все сервисы, 200 строк
#   make logs S=mcp-server N=500    # один сервис
#   make events M=5000              # события [START|RECONCILE|...] за 24ч
OPS := playbooks/ops.yml
ops-vars = $(if $(S),S=$(S)) $(if $(N),N=$(N)) $(if $(M),M=$(M))
logs:
	$(APB) $(OPS) --tags logs $(if $(ops-vars),-e "$(ops-vars)")
events:
	$(APB) $(OPS) --tags events $(if $(M),-e M=$(M))
health:
	$(APB) $(OPS) --tags health
metrics:
	$(APB) $(OPS) --tags metrics
stats:
	$(APB) $(OPS) --tags stats

# ─── Ф3 (code-2026-09-22-002): бэкап/верификация/восстановление ───

# Полный бэкап (backup.sh: qdrant-snapshots + weekly-4 + SSOT + console + secrets).
backup:
	$(APB) playbooks/backup.yml --tags backup $(VAULT_ARGS)

# Верификация восстановимости существующих бэкапов
# (backup.sh --verify: test-restore + sha256 + console untar-drill).
backup-verify:
	$(APB) playbooks/backup.yml --tags verify $(VAULT_ARGS)

# RESTORE — деструктивный, интерактивный. Всегда сначала backup + backup-verify!
#   make restore SCOPE=console RESTORE_CONFIRM=yes
#   make restore SCOPE=all RESTORE_CONFIRM=yes              # всё последовательно
#   make restore SCOPE=qdrant RESTORE_CONFIRM=yes RESTORE_SNAPSHOT=<name>.snapshot
restore:
	$(APB) playbooks/backup.yml --tags restore $(VAULT_ARGS) $(if $(SCOPE),-e restore_scope=$(SCOPE)) -e restore_confirm=$(RESTORE_CONFIRM) $(if $(RESTORE_SNAPSHOT),-e restore_snapshot=$(RESTORE_SNAPSHOT))

# ─── Ф4 (code-2026-09-22-003): цикл Error→Rule (errors.yml) ───
# Теги view|report|prune|sources vault-переменных НЕ используют (стиль ops-целей,
# БЕЗ VAULT_ARGS). P2-1: inventory vault.yml зашифрован → пароль нужен при
# загрузке инвентаря ВСЕГДА: vault_password_file в ansible.cfg или
#   make errors VAULT_ARGS="--vault-password-file ~/.vault-pass.txt"

# Read-only просмотр топ-сигнатур sink (P0 первыми).
errors:
	$(APB) playbooks/errors.yml --tags view

# Weekly-отчёт (6 секций + alert_state); TG=1 — доставка в Telegram (notify.json).
errors-report:
	$(APB) playbooks/errors.yml --tags report $(if $(TG),-e tg_send=1)

# Ретенция sink. Dry-run по умолчанию; реальное удаление — двойной гейт:
#   make errors-prune CONFIRM=--confirm   (плюс config.prune.enabled=true)
errors-prune:
	$(APB) playbooks/errors.yml --tags prune $(if $(CONFIRM),-e prune_confirm=$(CONFIRM))

# 018: one-shot миграция legacy cron-ключей exit=0 (P2/active → DELETE с
# backup; до деплоя фикса коллектора бессмысленна — legacy-ключи плодятся).
# Dry-run по умолчанию; применение и порядок (deploy → dry-run → confirm →
# срез AC1) — см. scripts/errors_cleanup_cron_legacy.py docstring.
errors-cron-cleanup:
	$(APB) playbooks/errors.yml --tags cron_cleanup $(if $(CONFIRM),-e cron_cleanup_confirm=$(CONFIRM))

# E5: проверка реестра охвата (новый источник без строки → COVERAGE FAIL).
errors-sources:
	$(APB) playbooks/errors.yml --tags sources
