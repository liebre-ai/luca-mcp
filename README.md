# Luca MCP

The installable [Model Context Protocol](https://modelcontextprotocol.io) client for Luca, the
accounting assistant of the Liebre platform. It runs on your machine over stdio, logs you in with
your Liebre account through the browser, and gives Claude Code, Codex, Cursor and other MCP
clients read-only tools over your businesses: periods, journal entries, financial reports, VAT
determination, SAT filings, SAT archives and documents, always with your own permissions.

Design: `docs/plans/2026-09-08-v2-rai-auth-and-thin-client.md`. The server side lives in
`ant-rai` (`src/endpoints/oauth` is the login server, `src/endpoints/mcp` the tool endpoints).

## How it works

```
agent ──stdio──▶ luca-mcp (this repo) ──bearer──▶ rai /api/v1/mcp/* ──machine token + User-Id──▶ Liebre API
                     │
                     └── login: browser → rai /oauth/authorize → Auth0 (Liebre login) → loopback callback
```

- **Login** is OAuth 2.1 authorization code with PKCE against rai's own authorization server.
  The client is the pre-registered public client `luca-mcp`; it listens once on
  `http://127.0.0.1:<random port>/callback` for the round trip (RFC 8252). rai authenticates you
  through the Liebre Auth0 tenant and only accepts an e-mail that is an active Liebre account.
- **Credentials**: rai issues a 30-minute access token and a rotating refresh token. They are kept
  in the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service) with a file
  fallback at `~/.config/luca-mcp/credentials.json` (mode 0600). Nothing from Auth0 ever reaches
  your machine. Tokens refresh automatically; `logout` revokes them.
- **Access control** happens in rai on every call: your e-mail is resolved to your Liebre account,
  the account must be active, and the business must be linked to you with `allowed` status. rai
  then calls the Liebre API with its machine token and your `User-Id`. Deleted businesses are
  hidden.
- **Tool logic** (shapes, validation, hints) lives in rai; this client is thin on purpose so
  everyone gets fixes without reinstalling.

## Install and connect

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is fetched automatically) and access to
the repository.

```bash
# Claude Code (add -s user to make it available in every project)
claude mcp add luca -- uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp

# Codex CLI (then raise its 60 s per-tool timeout so `login` can wait for the browser:
# in ~/.codex/config.toml under [mcp_servers.luca] add `tool_timeout_sec = 300`)
codex mcp add luca -- uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp
```

Cursor and other clients: add to your MCP settings

```json
{ "mcpServers": { "luca": { "command": "uvx",
    "args": ["--from", "git+https://github.com/liebre-ai/luca-mcp", "luca-mcp"] } } }
```

Then either ask the agent to call the `login` tool, or run it yourself:

```bash
uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp login    # opens the browser
uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp status   # who am I, which server
uvx --from git+https://github.com/liebre-ai/luca-mcp luca-mcp logout   # revoke + forget
```

The client talks to the dev Luca server by default. Point it elsewhere with `LUCA_RAI_URL`
(for example `http://localhost:3030` for a local rai), passed as an env entry in the MCP config.

Start a session with the `luca_guidelines` and `luca_navigation` prompts, then `list_businesses`.

## Tools

Session: `login` (opens the browser, blocks until done), `logout`, `whoami` (never fails: says
whether you are logged in, who you are, which firms and businesses you can use, and which server
the client talks to).

Data tools, all read-only and annotated as such:

| Tool | What it returns |
|---|---|
| `list_businesses(query?, limit?, offset?)` | Businesses you may act on, favourites first; filter by id, name or RFC. |
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
linked to more than one firm. Identifiers are validated locally (one URL segment, no slashes or
whitespace) and then against your own access list in rai before any Liebre call.

### Errors

Tools fail with an `isError` result whose text is a JSON envelope
`{"error": {"code", "message", "hint"?, "platform_url"?, "details"?}}`, produced by rai and
passed through verbatim. Codes: `not_authenticated` (call `login`), `login_expired` (call
`login` again), `forbidden` (inactive account), `not_found`, `invalid_input`, `unknown_business`
(the id is not among your businesses; the hint lists them), `ambiguous_firm`, `conflict`,
`upstream_unavailable` (rai unreachable; retry later), `upstream_error`. Argument type errors are
rejected by the tool schema before anything runs.

### Limits

Lists are capped at 200 rows per page; the trial balance at `max_rows` (default 200, max 1000);
SAT archive `details` at `max_detail_rows` (default 100). Reports that answer
`status: "processing"` are polled by rai a few times, then returned as-is with a hint.

## Development

```bash
uv sync                                    # Python 3.12, fastmcp 4.0.3 pinned
./dev/run_local.sh                         # dev mock IdP (:9400) standing in for Auth0
# ant-rai: `uv run fastapi dev src/main.py --port 3030` with MCP_OAUTH_IDP=auth0,
#          MCP_AUTH0_DOMAIN=http://localhost:9400, MCP_AUTH0_CLIENT_ID=luca-rai-dev, MCP_AUTH0_CLIENT_SECRET=...
export LUCA_RAI_URL=http://localhost:3030 LUCA_MCP_CREDENTIAL_STORE=file LUCA_MCP_CONFIG_DIR=.e2e-artifacts/config
uv run python tests/e2e/harness.py login   # scripted browser through rai and the mock IdP
uv run python tests/e2e/harness.py tools
uv run python tests/e2e/harness.py call get_trial_balance '{"business_id":"bu-2","start_period_id":"202608"}'
uv run python tests/e2e/harness.py call whoami '{}' --stdio   # same, through `uv run luca-mcp`
uv run python tests/e2e/tools_suite.py     # 36 cases against dev bu-2 (use --only <substring>)
```

`.mcp.json` registers this checkout as `luca-dev` for Claude Code (`uv run luca-mcp` against the
local rai). `dev/mock_idp.py` is a **dev-only** OpenID provider that stands in for Auth0 in
rai's `MCP_AUTH0_DOMAIN`; it has no real authentication and must never be deployed. The seeded
bu-2 fixtures the suite checks are described in `tests/e2e/fixtures/bu-2-seed-manifest.json`.

Nothing under `.e2e-artifacts/` is committed.

## Configuration

All optional, see `.env.example`: `LUCA_RAI_URL`, `LUCA_MCP_CLIENT_ID`, `LUCA_MCP_TIMEOUT`,
`LUCA_MCP_LOGIN_TIMEOUT`, `LUCA_MCP_CONFIG_DIR`, `LUCA_MCP_CREDENTIAL_STORE` (`auto` | `file`;
`file` skips the OS keychain for CI and headless machines).

Server-side settings (ant-rai): `MCP_OAUTH_IDP=auth0`, `MCP_AUTH0_DOMAIN`, `MCP_AUTH0_CLIENT_ID`,
`MCP_AUTH0_CLIENT_SECRET`, `MCP_OAUTH_ALLOWED_CLIENT_IDS` (default `claude-code,luca-mcp`). The
Auth0 application rai needs: Regular Web Application, callback `https://<rai>/oauth/callback`,
scopes `openid email profile`.

## Status and known limitations

- Dev: works against the local rai and dev Liebre. The dev rai deployment needs the Auth0
  application and the `MCP_*` settings before the default `LUCA_RAI_URL` works for everyone.
- Read-only; write tools are planned, not built. Excluded operations answer with a link to the
  platform.
- Deep links point to the platform root until the frontend routes are confirmed.
