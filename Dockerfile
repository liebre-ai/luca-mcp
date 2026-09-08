# Luca MCP server image (Cloud Run). Mirrors the sibling services: uv-managed Python 3.12.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/app/home

WORKDIR /app

# Dependencies first (layer cache), then the source.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev && mkdir -p /app/home

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Cloud Run injects PORT; 8080 is the conventional default.
EXPOSE 8080
CMD ["./docker-entrypoint.sh"]
