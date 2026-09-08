# Luca MCP — Implementation Plan

Date: 2026-09-08 · Author: Anand Kamble (drafted with Claude, reviewed by an adversarial pass) · Repo: `github.com/liebre-ai/luca-mcp` (private, empty at time of writing)

Status: **proposal for review.** *Verified* = read in code, observed live, or fetched from vendor docs on 2026-09-08 (details in `docs/research/`). *To confirm* = needs a person or a live test before it is relied on.

---

## 0. TL;DR

- **What**: a standalone remote MCP server ("Luca") through which Claude Code, Codex, Cursor and claude.ai act on behalf of a logged-in Liebre user against the Liebre API. Read-only first, curated writes later, and for everything else the honest answer plus a link into the platform.
- **Auth**: Luca MCP is the OAuth 2.1 authorization server *for MCP clients* (Client ID Metadata Documents and Dynamic Client Registration, PKCE S256, per-client consent, refresh rotation, RFC 9728/8414 metadata) and a *confidential client of the Liebre Auth0 tenant* upstream. Users log in through the normal Auth0 Universal Login; their Auth0 access and refresh tokens stay encrypted on the server; the MCP client only ever holds a Luca-issued reference token. Each tool call swaps that token for the user's real Auth0 token and calls the Liebre API exactly as the web app does (Bearer + `Accounting-Firm-ID`). This is the `OIDCProxy` pattern in **fastmcp 4.0.3** (on `mcp` 2.2.0), the current versions that speak both the legacy protocol and the 2026-07-28 revision.
- **Why this and not the alternatives**: making Auth0 the authorization server for MCP clients directly needs tenant-wide DCR/CIMD changes, turns every connected client into a strict "third-party app" (no `email` scope, unskippable consent, possibly one application slot per client on a Free tenant) and needs an on-behalf-of exchange that is rate-limited to 2 requests/second on the Free tier; reusing ms-rai's OAuth server authenticates against Google, never enforces tokens, keeps clients in process memory and holds no Liebre credential. Details in §3.3.
- **Today (Tue 2026-09-08)**: Auth0 ask first, then a 30-minute curl preflight with a real user token, then **six tools** that cover the demo, auth end to end with Claude Code, one Cloud Run instance. **Tomorrow**: rehearse, then add the next tools in demo order. Week 1 hardens (shared Redis state, multi-instance, CI). Week 2+ adds curated writes.
- **Blocking ask, today (§10 Q1)**: one Auth0 "Luca MCP" Regular Web Application in the dev tenant (first-party, callback `https://<luca-mcp-dev>/auth/callback`, auth code + refresh grants, rotation on), "Allow Offline Access" and "Allow Skipping User Consent" on the Liebre API, and confirmation that the `email` claim lands in the **access** token for this application.

---

## 1. Context, goals, non-goals

Source: AI Daily meeting 2026-09-08 (David Teran, Anand Kamble).

**Goals**
1. Let sophisticated users connect their own agent harness (Claude Code first; Codex, Cursor, claude.ai next) to Luca and build skills on top of it.
2. Expose the *input and output* ends of Liebre processes, not the processes themselves. Somewhere between 10 and 100 tools over time.
3. Step one is **extraction and comparison, no changes**: financial statements, journal entries, chart of accounts, and the data extracted from SAT, so users can compare SAT figures with the ledger.
4. When the MCP cannot do something, say so and point to the exact place in the platform.
5. Files: hand back the same artifacts the API produces, as downloads, rather than re-rendering.
6. Login must be "the standard": a link opens the browser, the user logs in, the token comes back to the client. All of it owned by our server.
7. Beta today, show users tomorrow, collect real use cases, productise the good ones for everyone.

**Non-goals for the first two weeks**
- Automating whole workflows inside the MCP (that stays in Luca/rai).
- Exposing credentials, SAT secrets, period close/open, renumbering, deletes, CFDI stamping, autopilot, or firm/user administration.
- Reporting UI or PDF rendering inside the MCP.
- The "Rick / rifts zero-inbox" review workflow (separate: a Claude session page in the night-shift site).

---

## 2. What we verified

### 2.1 Liebre API (`ant-liebre-api`, main @ d1b55767) — verified in code
- Authentication is an Auth0 RS256 access token in `Authorization: Bearer`, resolved to a user **by the `email` claim** (`src/auth/validate_user.py`). Signature, expiry, issuer and audience are currently **not verified** (the file says "DEVELOPMENT ONLY"). Unknown email → 401. Missing header → 403 "Not authenticated".
- Dev token shape (from expired fixtures committed in that repo): issuer is the dev Auth0 tenant, `aud = ["https://api-dev.liebre.ai/api/v1", "<tenant>/userinfo"]`, `scope = "openid email"`, 24 h lifetime, `email` present. Prod audience presumably `https://api.liebre.ai/api/v1` (*to confirm*).
- Machine-to-machine tokens (`sub` ending in `@clients`) skip the user lookup, `@require_permission` and the business-access check. **They must never carry end-user traffic.**
- Authorization is delegated to an external **ms-permissions** service that receives the caller's own `Authorization` and `Accounting-Firm-ID`; ms-cfdis proxies forward the caller's bearer too. So downstream calls must carry a **genuine Auth0 token with the Liebre API audience**: a token minted by us would pass liebre-api's non-verifying check and then fail every permission and CFDI call.
- 435 of 476 routes need `Accounting-Firm-ID`. Discovery: `GET /api/v1/accounting_firms` (header optional; permission `accounting_firm:settings:view` is checked when the header is present) and `GET /api/v1/businesses` (header required; permission `accounting_firm:businesses:view`; filtered to businesses where the user is `allowed`). **Both discovery calls are permission-gated** — the demo user must hold those slugs (§6.1 preflight). A business can be linked to more than one firm.
- The demo path touches these permissions: `accounting_firm:settings:view`, `accounting_firm:businesses:view`, `business:periods:view`, `business:reports:trial_balance` (also gates `taxes/vat/determination`), `business:reports:profit_and_loss`, `business:reports:balance_sheet`, `business:declaraciones:view`, `business:documents:view` (sat_archives and documents), `business:journal_entries:view_journal_entries`, `business:settings:coa`. `GET .../journal_entries/{id}/review_items` needs the *write* slug `update_journal_entry`.
- Journal entries: `GET /businesses/{b}/journal_entries/search` accepts only `q`, `limit` (default 50), `offset`, `has_cost_centers`, `tag_id` — no date/status/type filters and no declared cap. `GET .../periods/{p}/journal_entries` returns the whole period **unpaginated** (`cfdi_details`, `has_cost_centers`, `tag_id`, `tag_ids` only). There is no date-filtered JE query in the API; filtering by date/status/type must happen inside Luca.
- Reports (JSON): `reports/trial_balance` (`start_period_id*`, `end_period_id`, `levels_deep: str = "all"`, `signed_balances`, `include_zero_balances`, `absolute_values`, `only_detail_accounts`, `hide_order_accounts`, `include_pending_entries`, optional `cache_key`), `reports/balance_sheet` (`start_period_id*`, `end_period_id`, `cache_key`), `reports/income_statement` (`start_period_id*`, `end_period_id`, `cost_center_ids`, `cache_key`), `reports/subledger/{ledger_account_id}`, `reports/ledger_accounts/period_series`. `cache_key` is optional; `status: "processing"` comes from the server-side balance cache rebuilding, independent of it (poll). Exports are **XLSX streams**; there is **no PDF report generation** in the API (only CFDI PDFs).
- SAT-side data: `sat_archives` (structured `extracted_data` + files in GCS; the likely home of "the documents we extracted last week"), `documents` (acuses; `GET /documents/{id}` returns a 10-minute `download_url` whose `token` is a query parameter on an unauthenticated route — Liebre's design, not ours), `declaraciones?year=` (monthly status + acuse links), DIOT facade, `taxes/vat/determination/{period_id}` (`include_pending_entries`).
- Error envelope: `{"detail": {"error": <code>, "message": ..., "errors": [...]}}`. `period_id` is `YYYYMM` (integer in some paths, string in others; 13 = annual close) — *confirm with one real period before writing tools*.
- Deploy: GitLab CI, tag-driven (`vX.Y.Z`), image in `us-central1-docker.pkg.dev/logickernel-fleet/registry`, `gcloud run services update` in `us-central1`; environments development / development2 / staging / production; GitHub `version.yml` mirrors tags to GitLab. Hosts: `api-dev.liebre.ai`, `api.liebre.ai`; app `app-dev.liebre.ai`, `app.liebre.ai`. Frontend URL paths live in `ant-liebre-app` (not inspected).

### 2.2 ms-rai (`ant-rai`, main @ 66773a5c3) — verified in code and live
- Both existing MCP servers use `fastmcp 2.14.5` on `mcp 1.26.0`. The HTTP "luca-management" server has a working OAuth authorization server (DCR, PKCE, refresh, RFC 8414) but **never verifies tokens on the MCP path**, keeps DCR clients in a per-process set, authenticates with Google, and holds no Liebre credential. Live probe: `/.well-known/oauth-authorization-server` works; `/.well-known/oauth-protected-resource` returns the SPA's HTML.
- The stdio "accounting" server (76 tools) reads the Liebre Postgres directly. Its validators, formatters and the `accounting_guidelines` / `accounting_navigation` prompts are reusable as templates; its data access is not.

### 2.3 MCP spec, clients, libraries — verified from vendor docs and live probes (full notes: `docs/research/2026-09-08-mcp-oauth-clients.md`)
- Current spec revision is **2026-07-28**: stateless redesign (no `initialize`, no `Mcp-Session-Id`, mandatory `server/discover`, `Mcp-Method`/`Mcp-Name` headers, multi-round-trip requests instead of server-initiated elicitation). Clients are mid-migration, so the server must serve both eras. `mcp` 2.2.0 (2026-09-07) and `fastmcp` 4.0.3 (2026-09-05) do this per connection.
- **DCR is deprecated** (removal no earlier than 2027-07-28); **CIMD** is the recommended registration. Claude Code (client id `https://claude.ai/oauth/claude-code-client-metadata`, portless loopback redirect URIs), claude.ai connectors, ChatGPT (`private_key_jwt`), Codex and VS Code use CIMD when the server advertises `client_id_metadata_document_supported: true`; **Cursor still needs a `registration_endpoint`**. Every client falls back to DCR.
- Spec MUSTs we implement: PKCE S256; RFC 9728 PRM with `authorization_servers`; a real HTTP 401 with `WWW-Authenticate: Bearer resource_metadata=...`; RFC 8707 `resource` accepted and audience validated; no token passthrough; a per-client consent page for proxies (client name, scopes, redirect URI, CSRF, no iframing); exact redirect matching with the RFC 8252 loopback-port exception; `Origin` validation; RFC 9207 `iss` in authorization responses (SHOULD; ChatGPT requires the advertisement); do **not** publish `offline_access` in `scopes_supported` (SHOULD NOT — see §3.2 for the beta trade-off).
- Client redirect URIs to allow-list: loopback `localhost`/`127.0.0.1` on any port (Claude Code, Codex, Gemini, Cursor desktop `:8787`, VS Code `http://127.0.0.1:33418/`), `https://claude.ai/api/mcp/auth_callback`, `https://chatgpt.com/connector_platform_oauth_redirect`, `https://vscode.dev/redirect`, `https://www.cursor.com/agents/mcp/oauth/callback`, `cursor://anysphere.cursor-mcp/oauth/callback` (legacy), `https://antigravity.google/oauth-callback`. `claude.com/api/mcp/auth_callback` is **not** documented anywhere; do not add it.
- claude.ai connector requirements: real 401; PRM `resource` must equal the URL **exactly as the user typed it**; only the first `authorization_servers` entry is used; discovery/registration/token endpoints must answer within 10 s; ~150k characters per tool result; 300 s timeout; prefer CIMD ("DCR registers a new client on every fresh connection"). Claude Code: default result cap `MAX_MCP_OUTPUT_TOKENS` 25,000; sends `resource` = server URL; appends `offline_access` only when advertised.
- Local clients: Claude Code 2.1.259 (`claude mcp add --transport http`, `--client-id/--client-secret/--callback-port`, `claude mcp login|logout`), Codex 0.153.3 (`codex mcp add <name> --url`, `--oauth-client-registration auto|cimd|dcr`, `--oauth-resource`, `codex mcp login <name> --scopes`).
- Cloud Run: prefer `json_response=True` (no long SSE responses against the 300 s timeout), `stateless_http=True` for legacy sessions (otherwise sessions live in a per-process dict), uvicorn `--proxy-headers`, explicit allowed hosts (the SDK's DNS-rebinding protection rejects unknown hosts with 421), `--allow-unauthenticated` with OAuth enforced in-app, `min-instances ≥ 1` (10-second endpoint budget for claude.ai).

### 2.4 fastmcp 4.0.3 — verified in the installed package (scratch venv), notes in §11.3
- `OIDCProxy(config_url, client_id, client_secret, audience, base_url, valid_scopes, required_scopes, extra_authorize_params, extra_token_params, token_endpoint_auth_method, allowed_client_redirect_uris, client_storage, jwt_signing_key, require_authorization_consent=True|"remember"|"external", forward_resource, enable_cimd=True, fastmcp_access_token_expiry_seconds, token_expiry_threshold_seconds, timeout_seconds, ...)`. `Auth0Provider` is a thin wrapper that **drops** `valid_scopes`, `extra_*_params` and `token_endpoint_auth_method` — we use `OIDCProxy` directly.
- Token model: clients receive a Luca-issued HS256 JWT (`iss` = base URL, `aud` = `<base>/mcp`, `jti`, `token_use`). Upstream Auth0 tokens are stored encrypted (`UpstreamTokenSet`) in `client_storage`; `load_access_token()` verifies the Luca JWT, loads the upstream set, validates the Auth0 token with a `JWTVerifier` (JWKS, `iss`, `aud`, `exp`; **no clock-skew leeway**) and returns it, so `get_access_token().token` in a tool **is the user's Auth0 access token**. Refresh tokens are rejected as access tokens (`expected_token_use="access"`).
- Redirect validation parses the URL (protects against `http://localhost@evil` userinfo tricks, RFC 8252 loopback port flexibility). **`allowed_client_redirect_uris=None` allows every URI** — always pass an explicit list.
- Gaps we must patch (§3.2 "Overrides"): `revoke_token` only deletes the refresh-token metadata and POSTs the *Luca* token to Auth0 (useless upstream); it does not delete the JTI mapping or the upstream token set. Upstream refresh sends the **client's** scopes (hook: `_prepare_scopes_for_upstream_refresh`). The OIDC discovery document is fetched synchronously at construction (`timeout_seconds`, no retry). Only the path-suffixed PRM (`/.well-known/oauth-protected-resource/mcp`) is served. A user-supplied store is **not** auto-encrypted (wrap it in `FernetEncryptionWrapper`). The client's `resource` must string-match one canonical URL (`invalid_target` otherwise).

### 2.5 Auth0 — verified from Auth0 docs and live metadata (full notes: `docs/research/2026-09-08-auth0-for-mcp.md`)
- Proxy pattern needs only: a Regular Web Application (first-party, `client_secret_post`, grants `authorization_code` + `refresh_token`, fixed callback), `offline_access` in the upstream request, "Allow Offline Access" and "Allow Skipping User Consent" on the Liebre API, refresh-token rotation/expiration settings on the app, and the `email` claim added to **access tokens** by a post-login Action (`email` is an allowed un-namespaced claim). Cap: 200 active refresh tokens per user per application. `/oauth/revoke` needs client authentication.
- Auth0-as-AS ("Auth for MCP", GA 2026-05-05) needs: Resource Parameter Compatibility Profile, CIMD toggle and/or DCR (strict third-party apps: no `openid`/`email` scopes, no ID token, unskippable consent, domain-level connections only, exact redirect URIs, possibly one application slot per client), a new "Luca MCP" API, a Custom API client, a user-delegated grant, and On-Behalf-Of exchange (2/8/30 req/s by tier). One access token can never carry two custom API audiences.
- Rate limits relevant to us (Free tier): auth API 300/min, `/oauth/token` 30/s, JWKS 20/s, `/userinfo` 5/min per user.

---

## 3. Architecture

### 3.1 Overview

```
 Claude Code / Codex / Cursor / claude.ai            (MCP client = OAuth public client, PKCE S256)
        │ 1. POST /mcp → 401, WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"
        │ 2. GET PRM → authorization_servers: ["https://mcp-dev.liebre.ai"], resource: "https://mcp-dev.liebre.ai/mcp"
        │ 3. GET /.well-known/oauth-authorization-server → authorize/token/register/revoke, S256, CIMD supported, iss supported
        │ 4. CIMD client_id (Claude, Codex, ChatGPT, VS Code) or POST /register (Cursor, others)
        │ 5. browser → /authorize (consent: client name, scopes, redirect) → Auth0 Universal Login → /auth/callback
        │ 6. POST /token (code + PKCE) → Luca access JWT (1 h) + Luca refresh token
        ▼
 ┌────────────────────────── luca-mcp (Cloud Run, us-central1) ─────────────────────────────┐
 │ fastmcp 4.0.3 · Streamable HTTP, stateless, JSON responses · OIDCProxy (Auth0 upstream)  │
 │  • verify Luca JWT → load encrypted UpstreamTokenSet → validate Auth0 token (JWKS/iss/aud)│
 │  • tool: tenant context (firms/businesses per user, 5-min cache) → Liebre client         │
 │  state: encrypted key-value store  [beta: in-memory, one instance] → [week 1: Redis]      │
 └──────────────┬───────────────────────────────────────────────────┬───────────────────────┘
                │ Authorization: Bearer <user's Auth0 token>        │ code exchange / refresh / revoke
                │ Accounting-Firm-ID: af-N                          ▼
                ▼                                         Auth0 tenant (dev / prod)
        Liebre API (api-dev.liebre.ai/api/v1) → ms-permissions, ms-cfdis, Postgres, GCS
```

Properties: the MCP client never sees an Auth0 token; a leaked Luca token is useless outside `https://<host>/mcp`; the Liebre API sees the *user* with the user's own permissions; nothing about a session lives in the process once the state store is shared.

### 3.2 Auth design

**Roles.** Luca MCP = OAuth 2.1 resource server for `/mcp` and authorization server for MCP clients (issuer `https://<host>`). Auth0 = upstream identity provider; Luca MCP is one first-party Regular Web Application there.

**Flow.**
1. Unauthenticated `POST /mcp` → 401 with `resource_metadata`.
2. PRM → `authorization_servers = ["https://<host>"]`, `resource = "https://<host>/mcp"`, `scopes_supported`.
3. AS metadata → endpoints, `code_challenge_methods_supported: ["S256"]`, `token_endpoint_auth_methods_supported` including `none`, `client_id_metadata_document_supported: true`, `authorization_response_iss_parameter_supported: true`.
4. Client identifies itself by CIMD URL (we fetch, validate `client_id` equality and redirect URIs, cache) or registers via DCR. Redirect URIs are validated against our explicit allow-list either way.
5. `/authorize`: consent page naming the client and the redirect host (mode `"remember"`: once per browser per client + redirect URI), then redirect to Auth0 `/authorize` with `audience=<Liebre API identifier>`, `scope=openid email offline_access`, our own PKCE, our fixed callback `/auth/callback`.
6. Auth0 login (existing accounts, policies, MFA). `/auth/callback` exchanges the code with the client secret, stores the Auth0 access + refresh token encrypted, mints a one-time code for the MCP client.
7. `/token`: Luca access JWT (`fastmcp_access_token_expiry_seconds=3600`) + Luca refresh token (rotated).
8. Tool call: middleware verifies the Luca JWT, loads the upstream set, validates the Auth0 token; the handler gets `AccessToken(token=<Auth0 access token>, claims={email, sub, …})`. With `token_expiry_threshold_seconds=300` the proxy refreshes the upstream token transparently before it expires.
9. Client refresh → `/token` with `grant_type=refresh_token` → new Luca tokens; upstream refreshed when needed with the **pinned** scope set (override below).
10. Logout: Claude Code clears its stored credentials (`claude mcp logout luca`); whether it also calls `/revoke` is *not documented*. Our `revoke_token` override (below) makes `/revoke` actually invalidate the session server-side.

**Scopes.**
- Beta: `valid_scopes = ["openid", "email", "offline_access"]`, `required_scopes = ["openid"]`. Advertised = requested by every client = sent upstream = stored on the refresh token, so authorize and refresh always send the same string and Auth0 issues a refresh token. This deliberately violates the spec's *SHOULD NOT advertise `offline_access`*; the alternative (forcing the upstream scope via `extra_authorize_params` while advertising something else) is the week-1 shape once `luca:read` / `luca:write` exist as permissions on the Liebre API in Auth0. Without `email` in the upstream scope the access-token `email` claim may still depend on the Action (§10 Q1).
- Phase 2 (`luca:write`): step-up via `403 insufficient_scope` (fastmcp `require_scopes`), scopes defined on the Auth0 API so they appear in the upstream token's `scope` claim.

**Overrides we write (small subclass of `OIDCProxy`, all unit-tested):**
1. `revoke_token`: look up the JTI mapping, delete it and the upstream token set, delete the refresh-token record, and POST the *Auth0* refresh token to `https://<tenant>/oauth/revoke` with client authentication. Until this exists, "logout" only expires by TTL.
2. `_prepare_scopes_for_upstream_refresh`: always return the pinned upstream scope list.
3. Startup: bounded retry (3 × 5 s) around `OIDCProxy` construction so a slow Auth0 does not crash-loop the revision; `timeout_seconds=10`.
4. Optional root alias `GET /.well-known/oauth-protected-resource` → same document as the `/mcp` variant (some clients only try the root).
5. Fail closed when the validated upstream token has no `email` claim: return a tool error that says so; never fall back to `/userinfo` (5/min per user, and strict apps cannot call it anyway).

**Tokens and lifetimes.** Auth0 access token 24 h (API setting; unchanged). Auth0 refresh token: rotation on, reuse detection on, absolute 30 days, idle 7 days (*to confirm tenant defaults; Management API default is non-rotating and non-expiring, so set them*). Luca access JWT 1 h. Luca refresh token rotated on use. Immediate server-side revocation only once override 1 is in.

**Keys and secrets.** `LUCA_MCP_JWT_SIGNING_KEY` (32+ random bytes, Secret Manager, never derived from the Auth0 secret), `LUCA_MCP_STORAGE_KEY` (Fernet key; live from day one because we always pass our own store), `AUTH0_CLIENT_SECRET`. Rotating the signing key logs everyone out (acceptable); rotating the storage key requires a forced re-login.

**Redirect URIs (explicit list, never `None`)**: `http://localhost:*`, `http://127.0.0.1:*` (RFC 8252 loopback, any port; consent page shows the hostname), `https://claude.ai/api/mcp/auth_callback`, `https://chatgpt.com/connector_platform_oauth_redirect`, `https://vscode.dev/redirect`, `https://www.cursor.com/agents/mcp/oauth/callback`, `cursor://anysphere.cursor-mcp/oauth/callback`, `https://antigravity.google/oauth-callback`. Additions by config. *Verify during build* that the loopback patterns accept the path variants clients use (`/callback`, `/oauth/callback`, `/`).

**Canonical URL.** Exactly one: `https://<host>/mcp` (no trailing slash). Clients send it as `resource`; anything else fails with `invalid_target`. The README says so, and the host is chosen once (§3.5).

**Identity mapping.** Liebre identifies users by `email`. Auth0 Actions set ID-token and access-token claims through separate calls; our handler reads the **access** token. If the tenant's Action only sets the ID-token claim, every user fails closed — this is the first thing the preflight checks (§6.1). First call by a `pending_activation` user flips them to `active` inside liebre-api (existing side effect).

### 3.3 Alternatives considered

| Option | How | Why not now |
|---|---|---|
| **A. Auth0 is the authorization server ("Auth for MCP")** | PRM points at the tenant; clients register via CIMD (manual admin import) or DCR; Luca MCP validates MCP-audience tokens and exchanges them On-Behalf-Of for a Liebre-audience token | Tenant-wide changes (resource profile, CIMD/DCR toggles, domain-level connections, new API, Custom API client, user-delegated grant, skip-consent); DCR clients are strict third-party apps with no `email` scope and unskippable Auth0 consent, and may each consume an application slot (Free tenant: 10); OBO is 2 req/s on Free, 8/s on Professional, returns no refresh token; `resource` only honoured with the compatibility profile and `audience` still wins. Strong option for enterprise SSO later (fastmcp ships `Auth0MCPProvider` for it); not for a same-day beta. |
| **B. Reuse ms-rai's OAuth server** | Point PRM at `dev-ms-rai…/oauth/*` | Google identity, tokens never enforced today, per-process DCR, no Liebre credential, couples uptime and secrets to rai. |
| **C. Hand-roll the authorization server** | Port rai's `oauth` module, swap Google for Auth0, add PRM/CIMD/consent/redirect validation/persistent clients | Days of security-sensitive code that fastmcp already ships and tests; both SDK vendors discourage it. Escape hatch only. |
| **D. Static tokens** (`--header "Authorization: Bearer …"`) | User pastes a platform token | Not "the standard", 24-hour tokens, no revocation. Internal demo fallback only (§6.1 step 9). |

**Decision: `OIDCProxy` (fastmcp 4.0.3) now; A documented for later.** Fallback if 4.0.3 misbehaves during the build: `fastmcp 2.14.5` `OIDCProxy` (the version rai pins) plus two extra overrides it needs (reject `token_use == "refresh"` in `load_access_token`; replace the fnmatch redirect check with URL parsing).

### 3.4 Multi-tenancy and business scoping
- Tokens carry no firm/business claims. Per request Luca builds a **tenant context** for the user: `GET /accounting_firms` (send the header when known) → per firm `GET /businesses` → `business_id → (firm_id, name, RFC, currency, status)`. Cached 5 minutes per `(sub, email)` per instance — an optimisation only; Liebre re-checks everything.
- Every business-scoped tool takes an explicit `business_id` and an optional `accounting_firm_id`. Ids outside the user's map are rejected before any Liebre call with a message listing what the user *can* access; the firm header always comes from the map or the explicit argument, never from the model's guess. If `GET /accounting_firms` is forbidden for a user, the explicit `accounting_firm_id` path still works.
- A business linked to two firms: prefer the firm where the user is `allowed`; if ambiguous, require `accounting_firm_id`.
- No hidden defaults: a user with exactly one business gets it auto-selected only when `business_id` is omitted, and the response says which business was used. Zero businesses returns a clear explanation (new user, or no `allowed` link yet) with `whoami` output.
- Stateless server: no "current business" session. The instructions and skills tell the model to call `list_businesses` first.

### 3.5 Transport and deployment
- `mcp.http_app(path="/mcp", stateless_http=True, json_response=True)` on its own Starlette app at the root: no nested mounts, so `/.well-known/*` cannot be swallowed by a SPA. Stateless: no session ids, no resumability, no server-initiated messages (none needed). Modern (2026-07-28) clients are served by the same endpoint.
- `base_url` / issuer from config (`LUCA_MCP_BASE_URL`), never derived from the request. uvicorn with `--proxy-headers --forwarded-allow-ips='*'`. Allowed hosts = the public host (+ the `run.app` host while it exists).
- Cloud Run `us-central1`, same registry and tag-driven deploy as the sibling services; `--allow-unauthenticated` (OAuth in-app). Beta: `--min-instances=1 --max-instances=1 --concurrency=20 --timeout=300`, in-memory encrypted store. Do not raise `max-instances` before the shared store exists; do not deploy during the demo window (a revision switch runs two instances and wipes the in-memory store).
- Public hostname: `mcp-dev.liebre.ai` (dev), `mcp.liebre.ai` (prod) via Cloud Run domain mapping. The URL is baked into PRM `resource`, token audience and every user's config; changing it forces everyone to reconnect. `run.app` is acceptable for tomorrow if DNS is not ready.

---

## 4. Tool surface

### 4.1 Design rules
- **Names** `snake_case`, verb-first, stable; server name `luca` (tools appear as `mcp__luca__<tool>` in Claude Code).
- **Inputs**: explicit `business_id` (+ optional `accounting_firm_id`); periods as `YYYYMM` strings; dates ISO; enums as literals; lists take `limit` (default 50, server-side cap 200 enforced by Luca) and `offset`. Signatures come from the OpenAPI document (`docs/research` has the dump), not from memory.
- **Outputs**: `structured_content` (JSON) plus a short text summary. Money as **strings** (Pydantic serialisers), never floats. Never return an unpaginated Liebre list: page or filter inside Luca and say how many rows remain.
- **Annotations** honest: `readOnlyHint=True` on all phase-1 tools; writes carry `destructiveHint`/`idempotentHint` truthfully plus `_meta["anthropic/requiresUserInteraction"]=true`.
- **Errors**: one envelope `{"error": {"code", "message", "hint", "platform_url", "build"}}`; Liebre 401 → "your Luca session expired, run /mcp to log in again", 403 → "your Liebre role lacks <slug>; ask your firm admin or open <url>", 404 → what is missing, 409/422 → the rule in plain words, 5xx/timeouts → "Liebre is busy; retry in a minute". The build id in the hint distinguishes "store wiped by a redeploy" from "expired login".
- **Deep links**: until the frontend paths are confirmed, return the business home only and say the deep link is unavailable; then add per-page links.
- **"I can't do that"**: excluded operations exist as *knowledge* (server instructions + navigation prompt: what is excluded and where to do it in the platform), never as tools.
- **Files**: phase 1 returns JSON and Liebre's own short-lived `download_url`s (stated expiry; never logged). XLSX exports as signed links are a week-2 item, only if David confirms they matter.
- **Limits**: 30 s per Liebre call, 3 retries with jittered backoff on 429/5xx (honour `Retry-After`), 60 s tool deadline; `processing` reports polled 3× (2/4/8 s) then returned with `status: processing` and a retry hint. The Liebre client is the **only** code that reads `get_access_token().token`, and its host is pinned to `LIEBRE_API_BASE_URL` (no tool ever takes a URL argument).
- **Privacy**: log tool, business id, hashed user id, status, latency, upstream status. Never tokens, never `download_url`s.

### 4.2 Today — six demo tools

| Tool | Liebre endpoint(s) | Notes |
|---|---|---|
| `whoami()` | `GET /accounting_firms`, `GET /businesses` | Email, firms, businesses, upstream token expiry (the one that matters), server version/build. First call when anything fails. |
| `list_businesses()` | same | Entry point: firm, business id, legal/commercial name, RFC, currency, status. Clear text when empty. |
| `list_periods(business_id, year?, accounting_firm_id?)` | `GET .../periods?year=` | Status + validation status; flags the last open period. |
| `get_trial_balance(business_id, start_period_id, end_period_id?, levels_deep="1", only_detail_accounts=false, include_zero_balances=false, include_pending_entries=false, signed_balances=false, accounting_firm_id?)` | `GET .../reports/trial_balance` | `levels_deep` is a string (`"all"` or a depth). Totals first, rows after; row cap with a narrowing hint. |
| `get_vat_determination(business_id, period_id, include_pending_entries=false, accounting_firm_id?)` | `GET .../taxes/vat/determination/{p}` | The number to compare with the SAT declaration. |
| `list_declaraciones(business_id, year, accounting_firm_id?)` | `GET .../declaraciones?year=` | Month × declaration status, deadlines, acuse links (expiry stated). |

### 4.3 Tomorrow, in demo order (after the rehearsal)
`get_income_statement(business_id, start_period_id, end_period_id?)`, `get_balance_sheet(business_id, start_period_id, end_period_id?)`, `search_journal_entries(business_id, q, limit, offset)` (1:1 with the endpoint), `list_journal_entries_for_period(business_id, period_id, status?, journal_entry_type?, date_from?, date_to?, limit, offset)` (fetches the period once, filters and pages inside Luca, 60 s per-user cache), `get_journal_entry(business_id, journal_entry_id, include_proposed=false)`, `list_sat_archives(business_id, extraction_type?, status?, limit, offset)`, `get_sat_archive(business_id, sat_archive_id, include_extracted_data=true)` (size-capped; `_meta["anthropic/maxResultSizeChars"]`), `get_business(business_id)`, the `luca_guidelines` and `luca_navigation` prompts.

### 4.4 This week
`get_chart_of_accounts` (compact, depth-limited, filtered), `search_ledger_accounts`, `get_ledger_account`, `get_subledger`, `list_documents`, `get_document`, `list_money_accounts`, `list_statement_lines`, `list_cfdis`, `get_cfdi`, `list_customers`, `list_suppliers`, `get_ar_summary`, `get_ap_summary`, `get_diot_accounting_baseline`, `get_platform_link` (once paths are known).

### 4.5 Phase 2 — curated writes (week 3+, scope `luca:write`, client confirmation)
`create_journal_entry` (draft by default; `save_as_valid` needs the Liebre permission), `update_journal_entry` (full line set + `expected_updated_at`; Liebre deletes omitted lines and 409s on stale versions), `set_journal_entry_status` (transition matrix mirrored for good errors), `add_journal_entry_tags`, `create_ledger_account`, `update_ledger_account`. **Never**: JE delete (hard delete), renumber, period open/close, COA from template (replaces the COA), danger zone, CFDI stamping, SAT credentials, Syncfy, autopilot, backups, firm/user admin — these answer with the platform link.

### 4.6 Skills (repo `skills/`, copied to `~/.claude/skills/` for the demo; plugin later)
- `luca-ask` — "ask Luca for information": `list_businesses` → pick → `list_periods` → the right report; answers with numbers, sources (tool + period) and links. `allowed-tools` lists the tools explicitly (the `mcp__luca__*` wildcard is not documented).
- `luca-compare-sat` — written **after** the demo, from what David says when he sees the first real comparison; not before.

---

## 5. Repository, stack, configuration

```
luca-mcp/
├── pyproject.toml            # uv, python 3.12, fastmcp==4.0.3 (mcp 2.2.0), httpx, pydantic, py-key-value-aio[redis], cryptography
├── src/luca_mcp/
│   ├── main.py               # FastMCP("luca", auth=..., instructions=...); app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
│   ├── settings.py           # pydantic-settings; fails fast on missing secrets; AUTH0_CONFIG_URL is the full openid-configuration URL
│   ├── auth.py               # LucaOIDCProxy(OIDCProxy): the overrides in §3.2; store wiring; redirect allow-list; startup retry
│   ├── tenant.py             # tenant context (firms/businesses per user, cache keyed by sub+email, firm resolution)
│   ├── liebre/client.py      # async httpx: bearer per request, Accounting-Firm-ID, retries, deadline, error mapping, host pinned
│   ├── liebre/models.py      # Pydantic models for consumed shapes (from openapi.json, trimmed); Decimal → str serialisers
│   ├── tools/{business,periods,reports,sat,journal_entries,...}.py   # register(mcp) per module
│   ├── prompts/{guidelines,navigation}.py
│   └── errors.py
├── skills/luca-ask/SKILL.md
├── tests/                    # unit (shaping, tenant, errors, overrides), auth e2e (401 + both metadata documents), contract (dev, opt-in)
├── Dockerfile, docker-entrypoint.sh, .gitlab-ci.yml (mirror of the sibling services), .github/workflows/version.yml
├── docs/plans/, docs/research/, README.md (install commands per client, troubleshooting)
└── .mcp.json                 # project-scoped entry pointing at dev, for dogfooding
```

Configuration names (values per environment in Secret Manager / Cloud Run env): `LUCA_MCP_ENV`, `LUCA_MCP_BASE_URL`, `LIEBRE_API_BASE_URL`, `LIEBRE_APP_BASE_URL`, `AUTH0_CONFIG_URL` (`https://<tenant>/.well-known/openid-configuration`), `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_AUDIENCE` (Liebre API identifier), `LUCA_MCP_JWT_SIGNING_KEY`, `LUCA_MCP_STORAGE_KEY`, `LUCA_MCP_ALLOWED_REDIRECT_URIS`, `REDIS_URL` (week 1), `LOG_LEVEL`. Later: `SENTRY_DSN`, `EXPORTS_BUCKET`.

Conventions: uv, ruff, conventional commits, feature branches + PR (main is release-bound like the sibling repos), additive-only tool schema changes, CHANGELOG per release, server `version` bumped with every tool change.

Beta store: `FernetEncryptionWrapper(MemoryStore(), Fernet(LUCA_MCP_STORAGE_KEY))` — encrypted, per instance, lost on restart (everyone re-logs in). Week 1: swap `MemoryStore()` for `RedisStore(...)` — one line.

---

## 6. Delivery plan

### 6.1 Today (2026-09-08) — beta on dev, in this order
1. **Send the Auth0 ask now** (§10 Q1). It is the only external dependency; everything below except step 2 waits on it.
2. **Preflight with a real token (30 min, before any tool code)**: take the demo user's Auth0 access token from the web app session, decode it and check `email` is in the **access** token, then `curl` `GET /accounting_firms`, `GET /businesses` (with the firm header), `GET .../periods`, `GET .../reports/trial_balance?start_period_id=…&levels_deep=1`, `GET .../taxes/vat/determination/{p}`, `GET .../declaraciones?year=` against `api-dev`. This settles the permission gates (§2.1), the `period_id` shape, and gives a working `--header` fallback for free.
3. Scaffold the repo (§5), pin fastmcp 4.0.3, wire `LucaOIDCProxy` with the in-memory encrypted store, consent `"remember"`, explicit redirect list, the five overrides, `stateless_http=True`, `json_response=True`.
4. `LiebreClient` + tenant context + error mapping.
5. The six tools of §4.2, verified against the preflight responses.
6. Local end to end: MCP Inspector, then Claude Code against `http://localhost:8080/mcp` (the Auth0 app needs `http://localhost:8080/auth/callback` in its callbacks for this). Test **refresh** by setting `fastmcp_access_token_expiry_seconds=60` once.
7. Dockerfile + Cloud Run dev service (`min=max=1`, concurrency 20, secrets mounted, writable `HOME`), domain mapping if DNS is quick.
8. Claude Code on the deployed URL: `claude mcp add --transport http luca https://<host>/mcp` → `/mcp` → login → `whoami`. Then Codex: `codex mcp add luca --url https://<host>/mcp && codex mcp login luca`.
9. README with install + troubleshooting; send David the URL. **Fallback if the Auth0 app is not ready by mid-afternoon**: run the server on Anand's laptop with `auth=None` during a screen-shared demo, Claude Code pointed at `localhost` with `--header "Authorization: Bearer <a dev token>"`. Internal only, never deployed.

Definition of done for today: unauthenticated `POST /mcp` returns 401 with a valid `resource_metadata`; both `.well-known` documents validate; a fresh machine can add the server, log in through the browser (CIMD path with Claude Code, DCR path with Cursor or the Inspector), refresh, and get real numbers from a dev business with the six tools; no token appears in logs.

### 6.2 Tomorrow (2026-09-09) — demo day
- Morning: re-login (whoever logged in today must, if refresh tokens were not enabled in time — rehearse it), run the §7 flow on the demo business, confirm SAT archives / declaraciones exist for the chosen month.
- Then add §4.3 tools in demo order; freeze deploys before the demo window.

### 6.3 Week 1 (→ 2026-09-15) — harden
- Redis (Memorystore) as `client_storage` (Fernet-wrapped); raise `max-instances`; confirm login and refresh across instances.
- Scopes: define `luca:read` / `luca:write` on the Liebre API in Auth0; advertise `luca:read`, force the upstream scope via `extra_authorize_params`, keep the pinned refresh scopes; stop advertising `offline_access`.
- Structured logs with a field allow-list; per-instance semaphore around Liebre calls; `/health`.
- CI: GitLab mirror + tag-driven deploy like the sibling repos; staging environment.
- §4.4 tools; e2e test for the 401 challenge, both metadata documents and the revoke override; contract tests against dev.
- Client verification: Cursor (DCR), claude.ai custom connector (CIMD, exact URL), Codex `--oauth-client-registration cimd` and `dcr`, VS Code. Prune the redirect allow-list to what was observed.
- Deep-link paths from `ant-liebre-app`.

### 6.4 Week 2+ — prod, then writes
- Prod: prod Auth0 application, `mcp.liebre.ai`, prod Redis, deploy gated on staging. Requires the API team's JWKS verification fix (§10 Q11) to be at least scheduled.
- XLSX export links (GCS + signed URLs) only if confirmed useful; Sentry after something breaks twice.
- Phase 2 write tools with confirmations; plugin + marketplace packaging; admin scripts (revoke a user's sessions, rotate keys); per-tool usage analytics.

---

## 7. Demo script (2026-09-09)

1. Platform: Luca running the SAT extraction and its results (David).
2. Terminal: `claude mcp add --transport http luca https://mcp-dev.liebre.ai/mcp` → `/mcp` → browser → Auth0 login → consent → connected.
3. `whoami` → firms and businesses.
4. "Trial balance for <business>, August 2026, top level" → `get_trial_balance`.
5. "Compare the VAT we declared in August with the ledger" → `list_declaraciones` + `get_vat_determination` (+ `get_sat_archive` if shipped) → table with deltas.
6. "Show me the pólizas behind the biggest delta" → `list_journal_entries_for_period` / `search_journal_entries` + `get_journal_entry` (§4.3; if not shipped, step 6 is narrated from the platform).
7. "Create a correcting entry" → the assistant explains it cannot create entries yet and links to the period in the platform — the boundary is the feature.
8. Close: sophisticated users get this today; send us the workflows you build and we make them automatic for everyone.

---

## 8. Edge cases and failure modes

**Authentication and sessions**
- No Auth0 refresh token (offline access not enabled in time): Luca sessions end with the 24 h Auth0 token; users re-login via `/mcp`. Known limitation, announced.
- `email` claim only on the ID token (Action misconfigured): every user fails closed with a support message. Caught by the preflight.
- Auth0 app not first-party or Liebre API without skip-consent: Auth0 shows its own consent screen on top of ours (two prompts). Check `is_first_party` and the API setting.
- Refresh-token rotation is Auth0's per-app setting and may be off; reuse detection revokes the family → user re-logs in; log it.
- Client scopes ≠ upstream scopes → refresh returns a narrowed token that fails `required_scopes` → 401 on day two. Prevented by the pinned-scope override; tested with a 60 s expiry.
- User removed from a firm/business mid-session: Liebre 403/404; tenant cache ≤ 5 min; error suggests `whoami`.
- User in Auth0 but not in Liebre (`user_account` missing): Liebre 401 "Error validating user"; explain that the Liebre account must exist.
- JWKS unavailable: tool error that says "identity provider unavailable", not a 401 that makes clients drop tokens.
- Clock skew: the verifier has no leeway; keep the transparent-refresh threshold at 300 s so tokens are renewed before the edge.
- State store lost (redeploy of the single-instance beta, or a revision switch): everyone re-logs in once; the error hint carries the build id.
- Two instances before Redis: split-brain authorization state → **keep `max-instances=1`** until the shared store lands.
- Consent is a signed browser cookie, not a grant record: clearing cookies re-prompts; a shared browser profile carries approvals; we log approvals ourselves.
- Loopback redirects: Claude Code's CIMD document lists portless `http://localhost/callback`; the validator ignores the loopback port. Never register a fixed port.
- Auth0 `/oauth/token` and JWKS rate limits under bursty refreshes: jittered retry; JWKS cached in-process (verify the verifier's cache during build).
- Dev server with prod credentials or vice versa: issuer/audience mismatch → explicit "this is the dev server" message.
- A pasted Auth0 token in `--header`: rejected (only Luca-issued JWTs pass), hint to use `/mcp`.
- Auth0 caps 200 active refresh tokens per user per application; each client login consumes one; rotation keeps it bounded.

**Tenancy and permissions**
- Discovery gated on `accounting_firm:settings:view` / `accounting_firm:businesses:view`: preflight the demo user; grant beta users; explicit `accounting_firm_id` bypasses firm discovery.
- Business in two firms; sandbox firms; blocked/inactive businesses (shown with status, not hidden).
- Per-user permission differences: 403 mapped to the missing slug and the platform link.
- Cache keyed per `(sub, email)` per instance; a cache hit never changes which firm header is sent.

**Data and shaping**
- Unpaginated Liebre lists (JEs per period, COA tree, statement lines): filter and page inside Luca with a hard cap; the JE search endpoint has only `q`.
- Reports in `processing`: bounded polling, honest status. `levels_deep` is a string.
- `period_id`: `YYYYMM`, 13 = annual close, integer in some paths and string in others; coerce and validate.
- `include_pending_entries` semantics differ between trial balance (excluded by default) and exports: documented in tool descriptions.
- Decimals serialised as strings; negative balances follow `signed_balances`; dual-currency amounts only when enabled.
- Deleted/trashed/canceled entries shown with status; skills filter by default.
- `download_url`s carry the token in the query string and expire in 10 minutes: return as-is with the expiry, never log them.
- SAT archive `extracted_data` is schema-versioned per extraction type: pass through with `schema_version`; cap size.
- Spanish/English field names: keep the API's names in structured output, describe them in English.

**Transport and clients**
- One canonical URL (`/mcp`, no trailing slash): a client configured with `/mcp/` fails `resource` matching with `invalid_target`; README says so.
- JSON responses by default; SSE only if a client demands it. Cloud Run 300 s timeout is irrelevant for sub-minute tools.
- Clients without elicitation (claude.ai/Desktop): never depend on it; explicit parameters only.
- Clients that do not render `resource_link`s: always include a plain URL in the text.
- Tool count and context budgets: six today, ~15 this week, deterministic order; Claude Code (25k-token results, tool search on large sets), claude.ai (~150k chars), VS Code (128 tools), Cursor (~40, community figure).
- Renaming or re-typing a tool breaks users' skills: additive changes only; deprecate with an alias for one release.
- Legacy vs 2026-07-28 clients on the same endpoint: fastmcp negotiates per connection; `stateless_http=True` costs only legacy-era elicitation.
- Cloud Run concurrency: one slow report must not block the OAuth callback for other users → concurrency 20 in the beta, tool deadline 60 s.

**Operations and product**
- Liebre slowness (ms-permissions read timeout upstream is 120 s): 30 s per call, 60 s per tool, explain and suggest retry.
- Liebre contract drift on proxied surfaces: lenient parsing with drift logging.
- Secrets in logs: allow-listed fields; no PII in error tracking.
- Users expect prod data: the beta is dev-only until the prod Auth0 app, the prod deployment and the API's JWKS fix exist; say so in the README.
- Users asking for writes during the demo: the boundary message + link is the intended answer.
- `HOME` not writable in the container: irrelevant with the in-memory store, but set it anyway for anything that uses platformdirs.

---

## 9. Security checklist (MCP authorization spec 2026-07-28 + best practices)

- [ ] OAuth 2.1 only: authorization code + PKCE S256; no implicit or password grants.
- [ ] RFC 9728 PRM at `/.well-known/oauth-protected-resource/mcp` (+ root alias); 401 with `WWW-Authenticate: Bearer resource_metadata=...`; `resource` = canonical URL without trailing slash.
- [ ] RFC 8414 metadata at the issuer root; `issuer` equals `LUCA_MCP_BASE_URL` exactly; advertises S256, `none` in `token_endpoint_auth_methods_supported`, `client_id_metadata_document_supported: true`, `authorization_response_iss_parameter_supported: true`, a `registration_endpoint` for DCR clients.
- [ ] Tokens only in the `Authorization` header; never logged; Luca tokens audience-bound (`aud = <base>/mcp`) with `token_use` enforced.
- [ ] **No token passthrough**: clients never receive Auth0 tokens; the Liebre client is the only reader of the upstream token; outbound host pinned.
- [ ] Per-client consent page (client name, scopes, redirect host, CSRF, no iframing) — `"remember"` mode; approvals logged by us.
- [ ] Explicit redirect allow-list (never `None`); exact match except RFC 8252 loopback ports; CIMD documents fetched over HTTPS with SSRF protections (fastmcp) and validated against the same list.
- [ ] Upstream tokens encrypted at rest (Fernet) in every environment; keys in Secret Manager; TLS to Redis.
- [ ] Upstream token validated on every request (JWKS, `iss`, `aud`, `exp`; required scope `openid`).
- [ ] Revocation actually revokes (override 1): JTI mapping + upstream set deleted, Auth0 refresh token revoked.
- [ ] Stateless transport; `Origin`/host validation configured for the public host.
- [ ] Least-privilege service account: Secret Manager read only (no DB, no Auth0 M2M client, no bucket until exports exist).
- [ ] Pinned dependencies (`fastmcp==4.0.3`, `mcp==2.2.0`) with deliberate upgrades; advisories watched.
- [ ] Recommendation to the API team (P0 before prod exposure): enable JWKS signature/`aud`/`iss`/`exp` verification in `validate_user.py`; remove the committed dev JWT fixtures and the hardcoded Sentry DSN. Until then the beta stays dev-only.

---

## 10. Open questions and asks

| # | Ask | Owner | Needed by |
|---|---|---|---|
| 1 | Auth0 dev tenant: application "Luca MCP" — Regular Web App, **first-party**, OIDC conformant, `client_secret_post`, grants `authorization_code` + `refresh_token`, callbacks `https://<luca-mcp-dev>/auth/callback` and `http://localhost:8080/auth/callback`, refresh-token **rotation on** and **expiration on**; Liebre API: **Allow Offline Access** on and **Allow Skipping User Consent** on; confirm the post-login Action puts `email` on **access tokens** for this application; share client id/secret via Secret Manager | Auth0 owner (the person David referred to for authentication) | today |
| 2 | Prod tenant name, prod API identifier, and the same application for prod | Auth0 owner | week 2 |
| 3 | Hostnames `mcp-dev.liebre.ai` / `mcp.liebre.ai` (Cloud Run domain mapping + DNS) | ops / David | before wider rollout |
| 4 | Redis: reuse the Memorystore ms-rai uses (key prefix) or provision a small instance | ops | week 1 |
| 5 | CI/CD: GitLab mirror like the sibling repos, or GitHub Actions with WIF; Cloud Run service to be created by ops (manual deploy is fine today) | ops | today |
| 6 | Demo user and business: hold the ten permission slugs in §2.1; SAT archives + declaraciones present for the demo month in dev | David | tomorrow morning |
| 7 | Frontend URL patterns for deep links | frontend (`ant-liebre-app`) | week 1 |
| 8 | Scopes `luca:read` / `luca:write` as permissions on the Liebre API in Auth0 | Auth0 owner | week 1 |
| 9 | "PDF" in the meeting: is the XLSX export what users want, or does the web app render PDFs client-side? | David | week 1 |
| 10 | Beta audience (who gets the URL; who grants the two firm-level view permissions) and the support channel | David | tomorrow |
| 11 | JWKS verification fix in liebre-api (P0 before prod exposure) | API owner | before prod |

---

## 11. Appendix

### 11.1 Client setup (goes in the README)

Claude Code
```bash
claude mcp add --transport http luca https://mcp-dev.liebre.ai/mcp    # -s user to make it global
# inside Claude Code:
/mcp                      # select "luca" → Authenticate → browser → Auth0 login → consent
claude mcp logout luca    # disconnect
```
Project `.mcp.json`:
```json
{ "mcpServers": { "luca": { "type": "http", "url": "https://mcp-dev.liebre.ai/mcp" } } }
```

Codex CLI
```bash
codex mcp add luca --url https://mcp-dev.liebre.ai/mcp
codex mcp login luca
```

Cursor: `~/.cursor/mcp.json` → `{ "mcpServers": { "luca": { "url": "https://mcp-dev.liebre.ai/mcp" } } }`, then Connect in Settings → MCP (Cursor registers via DCR).

claude.ai / Claude Desktop: Settings → Connectors → Add custom connector → URL `https://mcp-dev.liebre.ai/mcp` **exactly** (no trailing slash).

Enter the URL exactly as shown everywhere; the trailing-slash variant is a different resource and fails authorization.

### 11.2 Client compatibility (from `docs/research/2026-09-08-mcp-oauth-clients.md`)

| Client | HTTP + OAuth | DCR | CIMD | Pre-registered id | Redirect URI(s) | Sends `resource` | Elicitation |
|---|---|---|---|---|---|---|---|
| Claude Code 2.1.259 | yes | yes | yes | yes | `http://localhost:<port>/callback`, `http://127.0.0.1:<port>/callback` (CIMD doc is portless) | yes | yes |
| claude.ai / Desktop | yes | yes | yes (needs `none` auth method advertised) | yes (advanced settings) | `https://claude.ai/api/mcp/auth_callback` | yes | no |
| Codex 0.153.3 | yes | yes | yes | yes | loopback `http://127.0.0.1:<port>/callback` | yes | yes |
| Cursor | yes | yes | **no** | yes | `http://localhost:8787/callback`, `https://www.cursor.com/agents/mcp/oauth/callback`, legacy `cursor://…` | unverified | yes |
| ChatGPT | yes | yes | yes (`private_key_jwt`) | undocumented | `https://chatgpt.com/connector_platform_oauth_redirect` | yes | unverified |
| VS Code | yes | yes | yes | yes | `http://127.0.0.1:33418/`, `https://vscode.dev/redirect` | unverified | yes |
| Gemini CLI → Antigravity | yes | yes | unverified | yes | `http://localhost:<port>/oauth/callback` / `https://antigravity.google/oauth-callback` | unverified | unverified |

### 11.3 fastmcp 4.0.3 facts used above (verified in the installed package on 2026-09-08)
- `fastmcp/server/auth/oidc_proxy.py`: `OIDCProxy.__init__` signature (§2.4); discovery fetched at construction with `timeout_seconds`.
- `fastmcp/server/auth/oauth_proxy/proxy.py`: `load_access_token` (token swap, upstream validation, `expected_token_use="access"`), `revoke_token` (refresh-record delete + upstream POST of the Luca token; no JTI/upstream cleanup), `_prepare_scopes_for_upstream_refresh` (documented override hook), transparent upstream refresh (`token_expiry_threshold_seconds`), `invalid_target` on `resource` mismatch, default `DiskStore` under the fastmcp home when no store is passed.
- `fastmcp/server/auth/redirect_validation.py`: URL-parsed matching, RFC 8252 loopback handling, `None` = allow all except unsafe schemes, `DEFAULT_LOCALHOST_PATTERNS = ["http://localhost:*", "http://127.0.0.1:*"]`.
- `fastmcp/server/auth/providers/jwt.py`: `exp < time.time()` without leeway; returns `AccessToken(token=<upstream>, client_id, scopes, expires_at, claims)`.
- `fastmcp/server/auth/cimd.py`: CIMD fetch with private-IP blocking and per-document redirect validation.

### 11.4 Research notes in this repo
`docs/research/2026-09-08-mcp-oauth-clients.md` (spec, transport, client matrix, Python options), `docs/research/2026-09-08-auth0-for-mcp.md` (Auth0 DCR/CIMD/OBO/proxy facts, rate limits, settings), `docs/research/2026-09-08-liebre-api-surface-and-auth.md` (auth internals, route tables, SAT side, writes), `docs/research/2026-09-08-ms-rai-mcp-internals.md` (existing MCP servers, OAuth module, tool inventories, lessons), `docs/research/2026-09-08-claude-code-mcp-client-facts.md`.

### 11.5 References
- MCP specification 2026-07-28: authorization, discovery, client registration, security considerations, security best practices, streamable HTTP, versioning (URLs in 11.4's first note).
- RFC 9728, RFC 8414, RFC 7591, RFC 7636, RFC 8707, RFC 9207, RFC 8252 §7.3, RFC 7009.
- fastmcp 4.0.3 docs: `servers/auth/oidc-proxy`, `servers/auth/oauth-proxy`, `servers/authorization`, `deployment/http`.
- ant-rai: `src/mcp_accounting/` (tool and prompt templates), `src/utils/liebre_api/client.py` (resilience settings), `docs/sse-deployment-requirements.md`.
- ant-liebre-api: `src/auth/validate_user.py`, `src/utils/permissions/decorator.py`, `src/utils/header_accounting_firm_validator.py`, `src/endpoints/reports/trial_balance_get.py`, `knowledge-base/ant-liebre-api.md`.
