FROM python:3.11-slim

# pypi.org недоступен из build-контейнеров на этом хосте (SSL handshake timeout);
# зеркало Tencent проверено 2026-08-06. Переопределение: --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Хостовый volume /app/knowledge принадлежит другому UID → иначе
# "dubious ownership" при git commit (write_knowledge/import_content).
RUN git config --global --add safe.directory /app/knowledge

# Python-зависимости (runtime, БЕЗ torch/transformers — embedder = Ollama;
# sklearn — TF-IDF для keywords.py, без torch)
COPY pyproject.toml .
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} fastmcp fastapi "uvicorn[standard]" "qdrant-client>=1.13.0,<1.15" pydantic pydantic-settings pyyaml prometheus-client gitpython python-dotenv httpx scikit-learn
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} ruff pytest pytest-asyncio pytest-cov
# editable install ПОСЛЕ COPY src/ — setuptools должен видеть src-layout при установке
COPY src/ ./src/
COPY tests/ ./tests/
RUN pip install --no-cache-dir --no-build-isolation -e . --no-deps

EXPOSE 8000

# 1 worker — ИНВАРИАНТ (in-memory состояние)
# Пакет установлен editable как mcp_server (не src.mcp_server) — единый namespace,
# иначе duplicate metrics в prometheus_client
CMD ["uvicorn", "mcp_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
