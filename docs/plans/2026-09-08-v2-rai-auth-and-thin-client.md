# Luca MCP v2 — auth and tools in rai, thin installable client

> **Status (2026-09-08 evening):** implemented on `ant-rai` branch `luca-mcp/oauth-auth0-and-mcp-endpoints`
> and this repository's `feat/thin-client-over-rai`; E2E-proven against the local rai and dev
> Liebre (bu-2). Evidence and defects found: `2026-09-08-v2-build-log.md`.


Date: 2026-09-08 (evening). Supersedes the remote-server design of `2026-09-08-luca-mcp-plan.md` for the
auth and hosting model; the tool surface, shaping rules and error envelope carry over unchanged.

Decisions (Anand, 2026-09-08 evening):
1. The authentication page and the token issuer are **ant-rai** (its existing OAuth 2.1 server at
   `/oauth/*`), and the login identity is the **Liebre platform account via Auth0**.
2. rai calls Liebre with its **machine token plus the `User-Id` header** (rai's existing pattern).
   Because Liebre skips per-user checks for machine tokens, **rai enforces access itself** from the
   Liebre tables it can read: the user must exist and be `active` in `user_account`, and the business
   must be linked to the user with `business_user_account_status = allowed`.
3. The **tool logic lives in rai** as MCP-facing endpoints under `/api/v1/mcp/...`; this repository
   becomes a **thin stdio client** installed with `uvx` from GitHub that logs in against rai and
   forwards tool calls.

## Architecture

```
Claude Code / Codex (stdio) ──▶ luca-mcp (local process)
                                  │  1. login: browser -> rai /oauth/authorize (PKCE) -> Auth0 login
                                  │     -> rai /oauth/callback -> loopback callback -> rai /oauth/token
                                  │  2. tools: GET https://<rai>/api/v1/mcp/... Authorization: Bearer <rai mcp_access JWT>
                                  ▼
                                ant-rai ── machine token + Accounting-Firm-ID + User-Id ──▶ Liebre API
                                  │  access check: liebre_dev.user_account + accounting_firm_business_user_account
                                  └─ Auth0 (Universal Login) upstream for /oauth/authorize
```

## ant-rai changes (branch `luca-mcp/oauth-auth0-and-mcp-endpoints`, PR to main)

Auth (`src/endpoints/oauth/`, `src/auth/`):
- `/oauth/authorize` and `/oauth/callback` gain an **Auth0 identity provider** next to Google, selected
  by `MCP_OAUTH_IDP=auth0|google` (default `auth0` when `MCP_AUTH0_DOMAIN` is set, else `google`, so
  existing deployments keep working). Auth0 registration via Authlib with
  `server_metadata_url=<MCP_AUTH0_DOMAIN>/.well-known/openid-configuration`, scope `openid email profile`.
  No `audience`/`offline_access`: rai only needs the identity (decision 2).
- Callback: e-mail from the ID token; **fail closed** unless the e-mail exists in Liebre's
  `user_account` with status `active`; upsert the rai `users` row. The `users.google_id` column is
  `NOT NULL UNIQUE` and migrations must not be run against the dev DB, so Auth0 users are stored with
  `google_id = "auth0|<sub>"` (documented follow-up: an `idp`/`idp_subject` migration).
- Client registry: default allow-list becomes `claude-code,luca-mcp`; for `luca-mcp` the redirect URI
  must be a loopback `http://127.0.0.1:<port>/callback` or `http://localhost:<port>/callback`.
- Token verification dependency `require_mcp_user()` (bearer -> `decode_mcp_access_jwt` -> user id +
  e-mail), used **only** by the new router. Nothing else in rai changes its auth.

MCP-facing endpoints (`src/endpoints/mcp/router.py` + `service.py`, prefix `/api/v1/mcp`):
| Endpoint | Tool |
|---|---|
| `GET /whoami` | whoami |
| `GET /businesses?query&limit&offset` | list_businesses |
| `GET /businesses/{b}` | get_business |
| `GET /businesses/{b}/periods?year` | list_periods |
| `GET /businesses/{b}/reports/trial_balance?...` | get_trial_balance |
| `GET /businesses/{b}/reports/balance_sheet?...` | get_balance_sheet |
| `GET /businesses/{b}/reports/income_statement?...` | get_income_statement |
| `GET /businesses/{b}/taxes/vat/{period}?include_pending_entries` | get_vat_determination |
| `GET /businesses/{b}/journal_entries/search?q&limit&offset` | search_journal_entries |
| `GET /businesses/{b}/periods/{p}/journal_entries?filters` | list_journal_entries_for_period |
| `GET /businesses/{b}/journal_entries/{id}` | get_journal_entry |
| `GET /businesses/{b}/declaraciones?year` | list_declaraciones |
| `GET /businesses/{b}/sat_archives?...` and `/sat_archives/{id_or_latest}?extraction_type` | list_sat_archives, get_sat_archive |
| `GET /businesses/{b}/documents?...` and `/documents/{id}` | list_documents, get_document |

- Responses are exactly the shapes the v1 tools produced (ported from `src/luca_mcp/tools/*`); errors
  are the same envelope `{"error": {"code", "message", "hint", ...}}` as the HTTP body with a matching
  status (401 login_expired/not_authenticated, 403 forbidden, 404 not_found/unknown_business, 400
  invalid_input, 409 conflict, 502/503 upstream_*).
- Access: `TenantResolver` (new read-only DAO over `user_account` + `accounting_firm_business_user_account`
  + `accounting_firm_business`) resolves the caller's businesses; every business route rejects ids
  outside that set before any Liebre call. `Accounting-Firm-ID` is derived from the link.
- Liebre calls: rai's `LiebreAPI` (sync `requests`) via `asyncio.to_thread`, with a new small
  `get_json(path, *, accounting_firm_id, params, user_id)` method for endpoints the client lacks
  (balance sheet, income statement, VAT, declaraciones, documents, JE search, periods list). Sends
  `User-Id` for audit.
- Journal entry lines are enriched from the Liebre `ledger_account` table (one query) instead of a
  450 KB chart-of-accounts fetch. No module-level caches (rai statelessness rule); the one precedent
  (`firm_resolution`) is reused for firm lookups.
- The auto-published OpenAPI MCP at rai's `/mcp` excludes `^/api/v1/mcp/` (these routes need the Luca
  token, and the thin client is the intended surface).
- Tests: `tests/endpoints/mcp/` (router + service with mocked Liebre/DAOs), `tests/endpoints/oauth/`
  additions (Auth0 branch, redirect rule, disabled/unknown Liebre user), `tests/auth/` for the new
  dependency. Baseline discipline per `run-tests`. Live proof on the local dev server (`localhost:3030`)
  with bu-2 only.

## luca-mcp (this repository) becomes the client

- Package `luca_mcp` with console script `luca-mcp`: `luca-mcp` (stdio MCP server), `luca-mcp login`,
  `luca-mcp logout`, `luca-mcp status`. Installed with
  `uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp`.
- Login: OAuth 2.1 authorization code + PKCE against rai (`/.well-known/oauth-authorization-server`),
  client id `luca-mcp`, loopback callback on a random port, browser opened automatically (URL printed
  as fallback), tokens refreshed proactively, stored in the OS keychain (`keyring`) with a `0600` file
  fallback under `~/.config/luca-mcp/`, keyed by rai URL. `LUCA_RAI_URL` selects the environment.
- Tools: the same 16 names, signatures and docstrings; each forwards to rai and returns rai's JSON;
  HTTP errors map to the same envelope; 401 triggers one refresh then a clear "run `luca-mcp login`"
  message. A `login` tool starts the browser flow from inside the agent.
- Removed from this repo: the OIDC proxy server, the tool implementations, the Dockerfile/CI for a
  hosted service. Kept: `dev/mock_idp.py` (now used as rai's dev Auth0 stand-in), the E2E harness
  rewritten for the client, the seeded fixtures.

## Security notes
- rai's `mcp_access` JWT is HS256 with rai's `JWT_SECRET_KEY`, 30 min, refresh rotated (30 days);
  the client never sees Auth0 tokens; rai never needs them.
- Enforcement moves from Liebre to rai for MCP traffic: existence + `active` status + business link.
  Role-level permissions (e.g. `business:reports:*`) are **not** enforced on this path; called out in
  the PR and the client's guidelines prompt.
- Loopback redirect rule for `luca-mcp`; PKCE S256 only; the mock IdP stays dev-only.

## E2E plan
1. Local mock IdP with rai's callback allowed; rai dev server (`localhost:3030`) with
   `MCP_OAUTH_IDP=auth0`, `MCP_AUTH0_DOMAIN=http://localhost:9400`, client id/secret of the mock.
2. rai suites: `rai-pytest tests/endpoints/mcp tests/endpoints/oauth tests/auth` + full suite vs baseline.
3. Client: `luca-mcp login` driven by the harness (rai authorize -> mock login -> callback), then
   `tests/e2e/tools_suite.py` over stdio (same 27 cases, same bu-2 facts and seeded fixtures).
4. Real Claude Code: `claude mcp add luca -- uvx --from <repo> luca-mcp` (locally `uv run luca-mcp`),
   `claude -p` tool calls; Codex the same way.
5. PRs: rai (auth + endpoints + tests + evidence) and luca-mcp (client + suites + docs).
