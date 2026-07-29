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
