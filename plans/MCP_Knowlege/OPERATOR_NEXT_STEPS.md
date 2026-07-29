# 🎯 ОПЕРАТОРУ: Пошаговые действия — MCP Knowledge Server

> **Статус:** Анализ завершён ✅ | Critic Gate: **PASS (0.84)** ✅ | Реализация: готова к старту
> **План:** [`00-implementation-plan.md`](00-implementation-plan.md) (**v3.1** post-Critic Gate, 26 решений, 4 фазы, ~122 ч Ф0–Ф3; Hybrid Search Architecture)
> **Дата:** 2026-07-29
>
> **Critic Gate v3.0→v3.1:** 6 P1-замечаний интегрированы (cache-invalidation, bounded search, INDEX size enforcement, suggested_tags алгоритм, structural change recovery, 4 новых риска R14–R17). Покрытие: §13.7. **План готов к передаче Code Implementer.**

---

## 📍 Где мы сейчас

Архитектура спроектирована, план утверждён, критика отработана (2 внешние рецензии + Critic Gate). Следующий шаг — **Фаза 0: Scaffolding** (создание скелета проекта).

---

## 🖥️ ШАГ 0: Выбор сервера

Тебе нужен Debian-сервер с:
- **Локальным диском** (НЕ SMB!) — минимум 50 GB свободно (Qdrant + модели + Markdown)
- Docker 24+ и `docker compose` плагин
- Опционально: nvidia-container-toolkit (если GPU доступен)
- Доступ по SSH с ключом

> **💡 Фактическое размещение (частный случай):** `/kvm/mcp-knowledge/` (диск 1.8T, 1.4T свободно).
> **Для публичной документации и production-плана указывай стандартный путь `/opt/mcp-knowledge/`.**

Если сервера ещё нет — можно начать на рабочей станции (dev), а потом развернуть на сервере через Ansible (шаг 4).

```bash
# Проверь на целевом хосте:
ssh root@<server-ip> '
  echo "=== OS ===" && cat /etc/debian_version && \
  echo "=== Disk ===" && df -h /kvm && \
  echo "=== Docker ===" && docker --version && docker compose version && \
  echo "=== GPU ===" && (nvidia-smi 2>/dev/null || echo "NO GPU")'
```

---

## 📁 ШАГ 1: Создать структуру каталогов на сервере

```bash
ssh root@<server-ip> 'bash -s' << 'ENDSSH'
set -e

# Рабочая директория (локальный диск!)
# 💡 Фактический путь: /kvm/mcp-knowledge (частный случай)
#    В публичных доках: /opt/mcp-knowledge (стандарт)
# ⚠️ mcp-knowledge/ и knowledge/ создадутся через git clone (Шаг 2)
mkdir -p /kvm/mcp-knowledge/{data,models_cache,scripts}
mkdir -p /kvm/mcp-knowledge/data/{qdrant/snapshots,dlq,quality,backups}

# Bare-репо для git-push backup SSOT
mkdir -p /kvm/mcp-knowledge-bare

# SMB-шара (только backup-target) — если доступна
# mkdir -p /mnt/smb-backup

# Права
chown -R 1000:1000 /kvm/mcp-knowledge/data  # для Docker bind mounts

echo "✅ Структура создана:"
find /kvm/mcp-knowledge -maxdepth 3 -type d | sort
ENDSSH
```

---

## 📦 ШАГ 2: Клонировать два git-репозитория с GitHub

> **⚠️ Предварительно:** создай репозитории на GitHub:
> - `mcp-knowledge` — **PUBLIC** (код сервера)
> - `knowledge` — **PRIVATE** (Markdown SSOT)
>
> Замени `<github-user>` на свой GitHub username/org.

### 2.1 Репо №1: код сервера (`mcp-knowledge/`) — PUBLIC

```bash
cd /kvm/mcp-knowledge
git clone git@github.com:<github-user>/mcp-knowledge.git
cd mcp-knowledge
```

Убедись, что `.gitignore` есть в репо (если нет — создай и запушь):

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/.gitignore << 'EOF'
# Docker volumes (runtime)
data/

# Модели (тяжёлые)
models_cache/

# Python
__pycache__/
*.pyc
.venv/
*.egg-info/

# Env (секреты)
.env
!.env.example

# IDE
.vscode/
.idea/
EOF

git add .gitignore && git commit -m "chore: add .gitignore" && git push
```

### 2.2 Репо №2: Markdown SSOT (`knowledge/`) — PRIVATE

```bash
cd /kvm/mcp-knowledge
git clone git@github.com:<github-user>/knowledge.git
cd knowledge

# Если репо пустой — создай начальную структуру
mkdir -p engineering/python/backend
touch engineering/python/backend/.gitkeep
git add -A && git commit -m "init: knowledge SSOT repo" && git push
```

### 2.3 Bare-репо для бэкапа SSOT

```bash
git init --bare /kvm/mcp-knowledge-bare/knowledge.git

# Добавь remote в knowledge/
cd /kvm/mcp-knowledge/knowledge
git remote add backup /kvm/mcp-knowledge-bare/knowledge.git
git push backup main
```

---

## 🐳 ШАГ 3: Создать docker-compose.yml и Dockerfile

Эти файлы создаются **на рабочей станции** в репе `mcp-knowledge/`.

### 3.1 `docker-compose.yml`

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/docker-compose.yml << 'COMPOSE'
version: "3.8"

services:
  qdrant:
    image: qdrant/qdrant:v1.13.4
    container_name: mcp-qdrant
    ports:
      - "6333:6333"   # REST
      - "6334:6334"   # gRPC
    volumes:
      - ../data/qdrant:/qdrant/storage     # bind mount — локальный диск (#27)
      - ../data/qdrant/snapshots:/qdrant/snapshots
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:6333/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 15s
    restart: unless-stopped

  mcp-server:
    build:
      context: ./mcp_server
      dockerfile: Dockerfile
    container_name: mcp-knowledge-server
    ports:
      - "8000:8000"
    volumes:
      - ../knowledge:/app/knowledge:ro        # read-only mount SSOT
      - ../data/dlq:/app/data/dlq
      - ../data/quality:/app/data/quality
      - ../models_cache:/app/models_cache
    env_file:
      - .env
    depends_on:
      qdrant:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
    # GPU (опционально):
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: 1
    #           capabilities: [gpu]
COMPOSE
```

### 3.2 `mcp_server/Dockerfile`

```bash
mkdir -p /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server

cat > /kvm/mcp-knowledge/mcp-knowledge/mcp_server/Dockerfile << 'DOCKERFILE'
FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir \
    fastmcp fastapi uvicorn qdrant-client pydantic pydantic-settings \
    pyyaml sentence-transformers onnxruntime prometheus-client gitpython

COPY src/ ./src/

EXPOSE 8000

# 1 worker — ИНВАРИАНТ (in-memory состояние)
CMD ["uvicorn", "src.mcp_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
DOCKERFILE
```

### 3.3 `pyproject.toml` (минимальный)

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/mcp_server/pyproject.toml << 'PYPROJECT'
[project]
name = "mcp-knowledge-server"
version = "0.1.0"
description = "MCP Knowledge Server — семантическая база знаний для AI-агентов"
requires-python = ">=3.11"
dependencies = [
    "fastmcp",
    "fastapi>=0.115.0",
    "uvicorn[standard]",
    "qdrant-client>=1.13.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "pyyaml>=6.0",
    "sentence-transformers>=3.0",
    "onnxruntime>=1.18",
    "prometheus-client>=0.20",
    "gitpython>=3.1",
]

[project.optional-dependencies]
dev = [
    "ruff",
    "mypy",
    "pytest",
    "pytest-asyncio",
]
PYPROJECT
```

### 3.4 `config.py` (заготовка)

```bash
mkdir -p /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server

cat > /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server/config.py << 'CONFIG'
"""Настройки MCP Knowledge Server (pydantic-settings)."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Qdrant
    QDRANT_URL: str = "http://qdrant:6334"
    QDRANT_COLLECTION: str = "knowledge"

    # Embedding
    EMBEDDING_BACKEND: str = "auto"  # auto | cpu | gpu
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024
    MODELS_CACHE_DIR: str = "/app/models_cache"

    # MCP Auth (мульти-ключи #18)
    MCP_READ_KEYS: List[str] = []
    MCP_WRITE_KEYS: List[str] = []

    # Git audit (#21)
    GIT_AUDIT: bool = True
    KNOWLEDGE_DIR: str = "/app/knowledge"

    # Chunking (#13, #20)
    CHUNK_MAX_TOKENS: int = 512
    CHUNK_OVERLAP: int = 80

    # Pipeline
    WORKERS: int = 1  # ИНВАРИАНТ — не менять!

    # DLQ (#14)
    DLQ_DIR: str = "/app/data/dlq"
    DLQ_MAX_RETRIES: int = 3

    # Quality (Фаза 4)
    QUALITY_DIR: str = "/app/data/quality"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.WORKERS != 1:
            raise ValueError(
                f"WORKERS={self.WORKERS}, ожидается 1. "
                "In-memory состояние (asyncio.Queue, sync_barrier) не переживает >1 worker."
            )


settings = Settings()
CONFIG
```

### 3.5 `__init__.py` и `main.py` (заготовки)

```bash
touch /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server/__init__.py

cat > /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server/main.py << 'MAIN'
"""MCP Knowledge Server — точка входа."""
from fastapi import FastAPI
from .config import settings
from .health import router as health_router

app = FastAPI(
    title="MCP Knowledge Server",
    version="0.1.0",
    description="Семантическая база знаний для AI-агентов (MCP-протокол)",
)
app.include_router(health_router)


@app.on_event("startup")
async def startup():
    print(f"🚀 MCP Knowledge Server v0.1.0")
    print(f"   EMBEDDING_BACKEND: {settings.EMBEDDING_BACKEND}")
    print(f"   QDRANT_URL: {settings.QDRANT_URL}")
    print(f"   GIT_AUDIT: {settings.GIT_AUDIT}")
    print(f"   WORKERS: {settings.WORKERS} (инвариант)")
MAIN
```

### 3.6 `health.py`

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/mcp_server/src/mcp_server/health.py << 'HEALTH'
"""Health-check эндпоинты (#10)."""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "0.1.0",
        "backend": "placeholder",  # заменится в задаче 1.7
    }
HEALTH
```

### 3.7 `.env.example`

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/.env.example << 'ENV'
# Qdrant
QDRANT_URL=http://qdrant:6334
QDRANT_COLLECTION=knowledge

# Embedding
EMBEDDING_BACKEND=auto
EMBEDDING_MODEL=BAAI/bge-m3
MODELS_CACHE_DIR=/app/models_cache

# MCP Auth (мульти-ключи через запятую, #18)
MCP_READ_KEYS=changeme-read-key
MCP_WRITE_KEYS=changeme-write-key

# Git audit (#21)
GIT_AUDIT=true
KNOWLEDGE_DIR=/app/knowledge

# Pipeline
WORKERS=1
ENV
```

### 3.8 `Makefile`

```bash
cat > /kvm/mcp-knowledge/mcp-knowledge/Makefile << 'MAKEFILE'
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
MAKEFILE
```

## 🔧 ШАГ 4: Ansible playbook (опционально для начала)

Если хочешь сразу автоматизировать развёртывание — создай структуру:

```bash
mkdir -p /kvm/mcp-knowledge/mcp-knowledge/ansible/roles/{mcp_kb_dirs,mcp_kb_repos,mcp_kb_docker,mcp_kb_cron}
mkdir -p /kvm/mcp-knowledge/mcp-knowledge/ansible/templates

cat > /kvm/mcp-knowledge/mcp-knowledge/ansible/inventory.yml << 'INVENTORY'
all:
  hosts:
    mcp-knowledge-server:
      ansible_host: <server-ip>
      ansible_user: root
INVENTORY

cat > /kvm/mcp-knowledge/mcp-knowledge/ansible/playbook.yml << 'PLAYBOOK'
---
- name: Deploy MCP Knowledge Server
  hosts: all
  gather_facts: yes

  roles:
    - mcp_kb_dirs
    - mcp_kb_repos
    - mcp_kb_docker
    - mcp_kb_cron
PLAYBOOK
```

> **Примечание:** Ansible roles будут наполнены в задаче 0.9. Пока создай скелет — это займёт 5 минут.

---

## ✅ ШАГ 5: Первый запуск и проверка

```bash
cd /kvm/mcp-knowledge/mcp-knowledge

# 1. Скопируй .env.example → .env и замени ключи
cp .env.example .env
# Отредактируй .env: замени changeme-*-key на реальные ключи

# 2. Запусти
make dev

# 3. Проверь (должно быть 2 healthy контейнера):
docker compose ps

# 4. Health-check:
curl http://localhost:8000/health
# → {"status":"healthy","version":"0.1.0","backend":"placeholder"}

curl http://localhost:6333/healthz
# → 200 OK

# 5. Проверь, что knowledge/ — отдельный репо:
cd /kvm/mcp-knowledge/knowledge && git rev-parse --git-dir
# → /kvm/mcp-knowledge/knowledge/.git  (не родительский!)

# 6. Проверь, что volumes — локальный диск (не сетевая ФС):
mount | grep data/qdrant
```

---

## 📋 ЧТО ДАЛЬШЕ

| Шаг | Что | Где в плане | ~Время |
|-----|-----|-------------|--------|
| ✅ | Структура + docker-compose + health | §4 Фаза 0 (задачи 0.1–0.8) | 8 ч |
| 🔜 | Ядро: Markdown SSOT + Qdrant + BGE-M3 + INDEX.gen.yaml (+cache, truncation, suggested_tags, structural change) | §5 Фаза 1 (задачи 1.1–1.11) | 31 ч |
| 🔜 | MCP-сервер: 9 Tools + auth + reconcile (+read-after-write, bounded search) | §6 Фаза 2 (задачи 2.1–2.15) | 38 ч |
| 🔜 | Production: backup + метрики + air-gap | §7 Фаза 3 (задачи 3.1–3.10) | 27 ч |
| 🔜 | Ansible playbook (наполнение) | §4 задача 0.9 | 4 ч |

---

## ⚠️ КРИТИЧНЫЕ ПРАВИЛА

1. **`WORKERS=1` — инвариант.** Не меняй, иначе сломается in-memory состояние.
2. **Локальный диск для runtime.** Никаких NFS/SMB для `data/qdrant/` и `knowledge/.git/`.
3. **Два отдельных репо.** `mcp-knowledge/` (код) ≠ `knowledge/` (SSOT). Не смешивай.
4. **Git-коммит после каждого write.** Включён флагом `GIT_AUDIT=true` (#21).
5. **GPU — опционально.** `EMBEDDING_BACKEND=auto` сам выберет CPU при отсутствии GPU.

---

> **Ссылки:** [План реализации](00-implementation-plan.md) | [Фаза 4 (quality)](04-phase4-knowledge-quality.md) | [.board.md](../../.board.md)
