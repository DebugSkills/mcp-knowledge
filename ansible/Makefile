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
        update update-check logs events health metrics stats \
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
	$(APB) playbooks/update.yml $(VAULT_ARGS)

# Dry-run апдейта: показать, что изменилось бы (--check --diff).
update-check:
	$(APB) playbooks/update.yml --check --diff $(VAULT_ARGS)

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
