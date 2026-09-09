#!/usr/bin/env bash
# Start (or restart) the dev stand-in for the Liebre app page and the Liebre API token check
# (dev/mock_idp.py), which completes Luca logins against a local rai.
#
# rai (started separately) must be configured with:
#   MCP_LUCA_LOGIN_UI_URL=http://localhost:9400/luca/connect
#   MCP_LIEBRE_TOKEN_CHECK_URL=http://localhost:9400/accounting_firms
# and this script must know where rai is: MOCK_RAI_URL (default http://localhost:3030).
# The client uses LUCA_RAI_URL=<that rai>.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .e2e-artifacts
pkill -f "dev/mock_idp.py" 2>/dev/null || true
sleep 0.3
export MOCK_IDP_CLIENT_ID="${MOCK_IDP_CLIENT_ID:-luca-rai-dev}"
export MOCK_IDP_CLIENT_SECRET="${MOCK_IDP_CLIENT_SECRET:-dev-secret-not-for-production}"
export MOCK_IDP_ALLOWED_REDIRECTS="${MOCK_IDP_ALLOWED_REDIRECTS:-http://localhost:3030/oauth/callback,http://127.0.0.1:3030/oauth/callback}"
export MOCK_RAI_URL="${MOCK_RAI_URL:-http://localhost:3030}"
nohup .venv/bin/python dev/mock_idp.py > .e2e-artifacts/mock_idp.log 2>&1 &
for i in $(seq 1 20); do
  curl -fsS -o /dev/null http://localhost:9400/.well-known/openid-configuration 2>/dev/null && { echo "mock idp up on :9400 (client ${MOCK_IDP_CLIENT_ID}, rai ${MOCK_RAI_URL})"; exit 0; }
  sleep 0.3
done
echo "mock idp did not start; see .e2e-artifacts/mock_idp.log" >&2; exit 1
