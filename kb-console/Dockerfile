FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir .

ENV CONSOLE_PORT=8085
ENV MCP_SERVER_URL=http://localhost:8000
# MCP_API_KEY НЕ задаётся в ENV-инструкции (секрет) — передаётся при запуске:
#   docker run -e MCP_API_KEY=<key> ... / compose: environment: MCP_API_KEY=${MCP_API_KEY:-}

EXPOSE 8085

CMD ["python", "-m", "kb_console.app"]
