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
        update update-check logs events health metrics stats
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
