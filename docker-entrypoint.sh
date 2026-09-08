#!/bin/bash
# Serve Luca MCP. Cloud Run terminates TLS at the load balancer, so trust forwarded headers.
set -euo pipefail
exec uv run --no-dev uvicorn luca_mcp.main:create_app --factory \
  --host 0.0.0.0 --port "${PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips='*' \
  --log-level "${UVICORN_LOG_LEVEL:-info}"
