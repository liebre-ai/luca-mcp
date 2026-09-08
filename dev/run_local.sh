#!/usr/bin/env bash
# Start (or restart) the dev mock IdP and the Luca MCP server in the background.
# Logs: .e2e-artifacts/mock_idp.log and .e2e-artifacts/server.log
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .e2e-artifacts
pkill -f "dev/mock_idp.py" 2>/dev/null || true
pkill -f "uvicorn luca_mcp.main:create_app" 2>/dev/null || true
sleep 0.5
export MOCK_IDP_CLIENT_SECRET="${MOCK_IDP_CLIENT_SECRET:-dev-secret-not-for-production}"
export MOCK_IDP_ACCESS_TTL="${MOCK_IDP_ACCESS_TTL:-86400}"
nohup .venv/bin/python dev/mock_idp.py > .e2e-artifacts/mock_idp.log 2>&1 &
sleep 1
nohup .venv/bin/uvicorn luca_mcp.main:create_app --factory --host 127.0.0.1 --port "${LUCA_MCP_PORT:-8765}" --proxy-headers --forwarded-allow-ips='*' > .e2e-artifacts/server.log 2>&1 &
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null http://localhost:8765/.well-known/oauth-authorization-server 2>/dev/null; then
    echo "server up (mock idp :9400, luca :${LUCA_MCP_PORT:-8765})"; exit 0
  fi
  sleep 0.5
done
echo "server did not come up; see .e2e-artifacts/server.log" >&2; tail -30 .e2e-artifacts/server.log; exit 1
