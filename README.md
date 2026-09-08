# Luca MCP

A remote [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude Code,
Codex, Cursor and claude.ai act on behalf of a logged-in Liebre user against the Liebre accounting
API. Read-only today: it reads businesses, periods, journal entries, financial reports, VAT
determination, SAT filings, SAT archives and documents, with the user's own permissions.

Design: `docs/plans/2026-09-08-luca-mcp-plan.md` (reviewed in `docs/plans/2026-09-08-luca-mcp-plan-review.md`).
Progress and verified facts: `docs/plans/2026-09-08-build-log.md`. Research: `docs/research/`.

## How it works

```
MCP client ──OAuth 2.1 (PKCE, CIMD/DCR, consent)──▶ Luca MCP ──user's Auth0 token──▶ Liebre API
                                                       │
                                                       └─ Auth0 (Universal Login) upstream
```

- Luca MCP is the OAuth authorization server the MCP client talks to **and** a confidential
  client of the Liebre Auth0 tenant. The user logs in through the normal Auth0 login. Their
  Auth0 access and refresh tokens stay encrypted on the server; the client only ever holds a
  Luca-issued reference token, which each tool call swaps for the user's real token before
  calling the Liebre API (`Authorization: Bearer` + `Accounting-Firm-ID`).
- Built on `fastmcp` 4.0.3 (`OIDCProxy`) with a few overrides (real revocation, pinned refresh
  scopes, RFC 8707 resource check, root protected-resource metadata). See `src/luca_mcp/auth.py`.
- Streamable HTTP at `/mcp`, stateless, JSON responses; serves both the legacy and the
  2026-07-28 protocol revisions.

## Connect a client

The canonical URL is `https://<host>/mcp` (no trailing slash). Dev (once deployed):
`https://mcp-dev.liebre.ai/mcp`. Locally: `http://localhost:8765/mcp`.

```bash
# Claude Code
claude mcp add --transport http luca https://mcp-dev.liebre.ai/mcp   # -s user to make it global
claude mcp login luca            # or /mcp inside a session; browser -> Liebre login -> consent
claude mcp logout luca           # disconnect (revokes the session server-side)

# Codex CLI
codex mcp add luca --url https://mcp-dev.liebre.ai/mcp
codex mcp login luca
```

Cursor: add `{ "mcpServers": { "luca": { "url": "https://mcp-dev.liebre.ai/mcp" } } }` to
`~/.cursor/mcp.json` and connect. claude.ai / Claude Desktop: Settings → Connectors → Add custom
connector with the URL exactly as above.

Start with the `luca_guidelines` and `luca_navigation` prompts, then `list_businesses`.

## Tools (all read-only)

| Tool | What it returns |
|---|---|
| `whoami` | Login identity, upstream login expiry, MCP client, server build, and the firms/businesses the user can access (`access.error` when they cannot be listed). |
| `list_businesses(query?, limit?, offset?)` | Businesses the user may act on, favourites first; filter by id, name or RFC. |
| `get_business(business_id)` | Profile: names, RFC, fiscal regime, currency, status, last open period. |
| `list_periods(business_id, year?)` | Periods (`YYYYMM`, 13 = annual close) with status and validation status; `last_open_period`. |
| `get_trial_balance(business_id, start_period_id, end_period_id?, levels_deep?, only_detail_accounts?, include_zero_balances?, include_pending_entries?, signed_balances?, max_rows?)` | Balanza de comprobación, rows flattened with `depth`, capped. |
| `get_balance_sheet(business_id, start_period_id, end_period_id?)` | Estado de situación financiera with sections and a `balanced` check. |
| `get_income_statement(business_id, start_period_id, end_period_id?)` | Estado de resultados with sections and line items. |
| `get_vat_determination(business_id, period_id, include_pending_entries?)` | IVA determination from the ledger, with warnings explained in `notes`. |
| `search_journal_entries(business_id, q, limit?, offset?)` | Text search of pólizas across periods (compact rows). |
| `list_journal_entries_for_period(business_id, period_id, status?, journal_entry_type?, date_from?, date_to?, q?, limit?, offset?)` | Period listing with counts by status/type, filters and paging. |
| `get_journal_entry(business_id, journal_entry_id)` | One póliza with lines (account number/name resolved), CFDIs, `balance_check`. |
| `list_declaraciones(business_id, year)` | Monthly SAT filing status, deadlines, folios, acuse links. |
| `list_sat_archives(business_id, extraction_type?, status?, requested_from?, requested_to?, limit?, offset?)` | SAT extraction runs with `available_types`. |
| `get_sat_archive(business_id, sat_archive_id? \| extraction_type?, include_extracted_data?, max_detail_rows?)` | Structured `extracted_data` (e.g. declared DIOT totals and per-supplier details) plus files. |
| `list_documents(business_id, document_type?, year?, month?, limit?, offset?)` | Stored fiscal documents with `available_types`. |
| `get_document(business_id, document_id)` | Metadata and a short-lived signed `download_url`. |

Every business-scoped tool accepts an optional `accounting_firm_id` for users whose business is
linked to more than one firm. `business_id` values are validated against the user's own access
list before any Liebre call.

### Errors

Tools fail with an `isError` result whose text is a JSON envelope
`{"error": {"code", "message", "hint", "platform_url"?, "details"?, "build"?}}`. Codes:
`not_authenticated`, `no_email_claim`, `login_expired` (run `/mcp` and log in again),
`forbidden` (Liebre role lacks the permission), `not_found`, `invalid_input`, `unknown_business`,
`ambiguous_firm`, `conflict`, `upstream_unavailable` (retry later), `upstream_error`,
`not_supported`. Type errors on arguments are rejected by the schema before the tool runs.

### Limits

Lists are capped at 200 rows per page; the trial balance at `max_rows` (default 200, max 1000);
SAT archive `details` at `max_detail_rows` (default 100). Each tool has a 60-second budget with
30-second Liebre calls and three retries on 429/5xx. Reports that answer `status: "processing"`
are polled three times, then returned as-is with a hint.

## Development

```bash
uv sync                                   # Python 3.12, fastmcp 4.0.3 pinned
cp .env.example .env                      # then set real keys (see the file)
./dev/run_local.sh                        # starts the dev mock IdP (:9400) and the server (:8765)
uv run python tests/e2e/harness.py login  # scripted OAuth dance; stores a session in .e2e-artifacts/
uv run python tests/e2e/harness.py tools
uv run python tests/e2e/harness.py call get_trial_balance '{"business_id":"bu-2","start_period_id":"202608"}'
uv run python tests/e2e/harness.py auth-suite   # 17 authentication edge cases
uv run python tests/e2e/tools_suite.py          # 22 tool cases against dev bu-2 (use --only <substring>)
```

`dev/mock_idp.py` is a **dev-only** OpenID provider that stands in for Auth0 until the real
"Luca MCP" Auth0 application exists. It mints Auth0-shaped RS256 tokens for synthetic dev
identities (default `ygreen@company.com`, the account the API repo's fixtures use), which the dev
Liebre API accepts because it resolves users by the `email` claim. It has no real authentication
and must never be deployed. Switching to Auth0 is configuration only (`AUTH0_*` variables).

Logs: `.e2e-artifacts/server.log`, `.e2e-artifacts/mock_idp.log`. Nothing under `.e2e-artifacts/`
is committed.

## Configuration

See `.env.example`. Required: `LUCA_MCP_BASE_URL`, `LIEBRE_API_BASE_URL`, `LIEBRE_APP_BASE_URL`,
`AUTH0_CONFIG_URL`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_AUDIENCE`,
`LUCA_MCP_JWT_SIGNING_KEY` (32+ random bytes), `LUCA_MCP_STORAGE_KEY` (Fernet key). Optional:
`REDIS_URL` (shared OAuth state store; required before running more than one instance),
`LUCA_MCP_ALLOWED_REDIRECT_URIS`, `LUCA_MCP_ADVERTISED_SCOPES`, `LUCA_MCP_CONSENT_MODE`
(`always` | `remember` | `off`, `off` only in local/dev), `LUCA_MCP_ACCESS_TOKEN_TTL_SECONDS`,
`LOG_LEVEL`.

Auth0 application the real deployment needs (dev tenant first): Regular Web Application,
first-party, `client_secret_post`, grants `authorization_code` + `refresh_token`, callback
`https://<host>/auth/callback`, refresh-token rotation and expiration on; on the Liebre API:
"Allow Offline Access" and "Allow Skipping User Consent"; the post-login Action must put `email`
on **access** tokens.

## Deployment

`Dockerfile` + `docker-entrypoint.sh` (uvicorn behind Cloud Run's TLS termination). Tag-driven
GitLab pipeline in `.gitlab-ci.yml` mirrored from the sibling services; `.github/workflows/version.yml`
creates the `vX.Y.Z` tags. Beta topology: one Cloud Run instance (`--min-instances=1
--max-instances=1 --concurrency=20`) with the in-memory state store; set `REDIS_URL` before
scaling out. The server must run with `--allow-unauthenticated` at the Cloud Run layer: OAuth is
enforced in-app.

## Status and known limitations

- Dev-only until the Auth0 application exists and the Liebre API verifies token signatures
  (tracked in the plan's asks).
- Deep links point to the platform root until the frontend routes are confirmed.
- Financial statement exports (XLSX) and write tools are planned, not built.
