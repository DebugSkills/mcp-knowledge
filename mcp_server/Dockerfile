FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости (runtime, БЕЗ torch/transformers — embedder = Ollama;
# sklearn — TF-IDF для keywords.py, без torch)
COPY pyproject.toml .
RUN pip install --no-cache-dir fastmcp fastapi "uvicorn[standard]" qdrant-client pydantic pydantic-settings pyyaml prometheus-client gitpython python-dotenv httpx scikit-learn
RUN pip install --no-cache-dir ruff pytest pytest-asyncio pytest-cov
# editable install ПОСЛЕ COPY src/ — setuptools должен видеть src-layout при установке
COPY src/ ./src/
COPY tests/ ./tests/
RUN pip install --no-cache-dir -e . --no-deps

EXPOSE 8000

# 1 worker — ИНВАРИАНТ (in-memory состояние)
# Пакет установлен editable как mcp_server (не src.mcp_server) — единый namespace,
# иначе duplicate metrics в prometheus_client
CMD ["uvicorn", "mcp_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
