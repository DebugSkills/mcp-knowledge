FROM python:3.11-slim

# pypi.org недоступен из build-контейнеров на этом хосте (SSL handshake timeout);
# зеркало Tencent проверено 2026-08-06. Переопределение: --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --no-build-isolation --index-url ${PIP_INDEX_URL} .

ENV CONSOLE_PORT=8085
ENV MCP_SERVER_URL=http://localhost:8000
# MCP_API_KEY НЕ задаётся в ENV-инструкции (секрет) — передаётся при запуске:
#   docker run -e MCP_API_KEY=<key> ... / compose: environment: MCP_API_KEY=${MCP_API_KEY:-}

EXPOSE 8085

CMD ["python", "-m", "kb_console.app"]
