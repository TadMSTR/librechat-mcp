FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY librechat_mcp/ librechat_mcp/

RUN pip install --no-cache-dir .

ENV MCP_PORT=8496
EXPOSE 8496

CMD ["librechat-mcp"]
