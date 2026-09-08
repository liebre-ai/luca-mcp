#!/usr/bin/env bash
# Start (or restart) the dev mock identity provider used as rai's Auth0 stand-in.
#
# rai (localhost:3030, started separately with its own dev.sh) must be configured with:
#   MCP_OAUTH_IDP=auth0
#   MCP_AUTH0_DOMAIN=http://localhost:9400
#   MCP_AUTH0_CLIENT_ID=luca-rai-dev  MCP_AUTH0_CLIENT_SECRET=dev-secret-not-for-production
# and the client with LUCA_RAI_URL=http://localhost:3030.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .e2e-artifacts
pkill -f "dev/mock_idp.py" 2>/dev/null || true
sleep 0.3
export MOCK_IDP_CLIENT_ID="${MOCK_IDP_CLIENT_ID:-luca-rai-dev}"
export MOCK_IDP_CLIENT_SECRET="${MOCK_IDP_CLIENT_SECRET:-dev-secret-not-for-production}"
export MOCK_IDP_ALLOWED_REDIRECTS="${MOCK_IDP_ALLOWED_REDIRECTS:-http://localhost:3030/oauth/callback,http://127.0.0.1:3030/oauth/callback}"
nohup .venv/bin/python dev/mock_idp.py > .e2e-artifacts/mock_idp.log 2>&1 &
for i in $(seq 1 20); do
  curl -fsS -o /dev/null http://localhost:9400/.well-known/openid-configuration 2>/dev/null && { echo "mock idp up on :9400 (client ${MOCK_IDP_CLIENT_ID})"; exit 0; }
  sleep 0.3
done
echo "mock idp did not start; see .e2e-artifacts/mock_idp.log" >&2; exit 1
