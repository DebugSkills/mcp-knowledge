DOCKER_COMPOSE ?= docker compose

.PHONY: dev down logs test lint clean

dev:
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
