INVENTORY ?= inventory/hosts.yml
PLAYBOOK  ?= playbooks/host-prepare.yml
VAULT_ARGS ?= --ask-vault-pass
APB := ansible-playbook -i $(INVENTORY)
.PHONY: deps test lint check run run-role vault-create vault-edit
deps:
	# --break-system-packages: Debian 12 PEP 668 (externally-managed); пакеты идут в ~/.local
	python3 -m pip install --user --break-system-packages -r requirements.txt
	ansible-galaxy collection install -r requirements.yml
test:
	$(APB) $(PLAYBOOK) --syntax-check
lint:
	ansible-lint roles/ playbooks/
check:
	$(APB) $(PLAYBOOK) --check --diff $(VAULT_ARGS)
run:
	$(APB) $(PLAYBOOK) $(VAULT_ARGS)
run-role:
	$(APB) $(PLAYBOOK) --tags $(ROLE) --limit $(HOST) $(VAULT_ARGS)
vault-create:
	ansible-vault create inventory/group_vars/all/vault.yml
vault-edit:
	ansible-vault edit inventory/group_vars/all/vault.yml
