# Luca MCP

The installable [Model Context Protocol](https://modelcontextprotocol.io) client for Luca, the
accounting assistant of the Liebre platform. It runs on your machine over stdio, logs you in with
your Liebre account through the browser, and gives Claude Code, Codex, Cursor and other MCP
clients read-only tools over your businesses: periods, journal entries, financial reports, VAT
determination, SAT filings, SAT archives, IMSS emisiones and documents, always with your own
permissions.

Design: `docs/plans/2026-09-08-v2-rai-auth-and-thin-client.md` and, for the login through the
Liebre app, `docs/plans/2026-09-08-liebre-app-login-plan.md`. The server side lives in `ant-rai`
(`src/endpoints/oauth` is the login server, `src/endpoints/mcp` the tool endpoints); the login
page lives in the Liebre app (`/luca/connect`).

## How it works

```
agent ──stdio──▶ luca-mcp (this repo) ──bearer──▶ rai /api/v1/mcp/* ──machine token + User-Id──▶ Liebre API
                     │
                     └── login: browser → rai /oauth/authorize → Liebre app /luca/connect (your normal
                         Liebre login if needed, then one click) → app approves at rai with your
                         Liebre session → loopback callback
```

- **Login** is OAuth 2.1 authorization code with PKCE against rai's own authorization server.
  The client is the pre-registered public client `luca-mcp`; it listens once on
  `http://127.0.0.1:<random port>/callback` for the round trip (RFC 8252). rai hands the request
  to the Liebre app: you log in there exactly as always (nothing to do if you already are), see
  who is about to be connected, and click **Conectar**. The app tells rai, rai asks the Liebre
  API whether your session is good, and only an active Liebre account gets through. Cancelling,
  or a refusal, reaches the agent at once with the reason.
- **Credentials**: rai issues a 30-minute access token and a rotating refresh token. They are kept
  in the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service) with a file
  fallback at `~/.config/luca-mcp/credentials.json` (mode 0600). Your Liebre session never
  reaches your machine's agent. Tokens refresh automatically; `logout` revokes them.
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

# Codex CLI (then, in ~/.codex/config.toml under [mcp_servers.luca], add
# `tool_timeout_sec = 300` so `login` can wait for the browser, and
# `startup_timeout_sec = 60` so the first run, which installs the package, is not cut off at 10 s)
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
In Claude Code they appear as `/mcp__luca__luca_guidelines` and `/mcp__luca__luca_navigation`.

### Updating and pinning

`uvx` caches the resolved commit of the repository, so a new version does not reach an installed
client until it runs `uvx --refresh --from git+https://github.com/liebre-ai/luca-mcp luca-mcp --help`
and reconnects the server in the agent. To stay on a known release instead, pin a tag:
`git+https://github.com/liebre-ai/luca-mcp@v0.3.0`. Tags follow the version in `pyproject.toml`
(`vX.Y.Z`, created when a version is released); `whoami` reports the running version as
`client_version`.

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
| `list_article_69b_matches(business_id, start_period_id, end_period_id?, statuses?, limit?, offset?)` | Customers/suppliers on the SAT 69-B list (EFOS) for the period's CFDIs: summary, MXN amounts, exposure, one row per matched RFC with status history. `statuses` defaults to `presunto, definitivo`. |
| `list_imss_emisiones(business_id, year, registro_patronal?, with_amounts?)` | Calendar of the archived IMSS cédulas (EMA monthly, EBA bimonthly) per registro patronal: archived / missing / future / not_due per month, archive and document ids, summaries; `with_amounts` adds the figures. |
| `get_imss_emision(business_id, sat_archive_id? \| tipo+year+month(+registro_patronal), include_workers?, max_workers?)` | One cédula: employer, period, `fecha_limite_pago`, `conceptos` (patronal / obrera / suma), `saldos`, warnings, files, `workers_total`; worker rows with full NSS only when `include_workers` is true, capped. |
| `list_documents(business_id, document_type?, year?, month?, limit?, offset?)` | Stored fiscal documents with `available_types`. |
| `get_document(business_id, document_id)` | Metadata and a short-lived signed `download_url`. |

Every business-scoped tool accepts an optional `accounting_firm_id` for users whose business is
linked to more than one firm. Identifiers are validated locally (one URL segment, no slashes or
whitespace) and then against your own access list in rai before any Liebre call. Every parameter
carries a description in the tool schema; the tool description says when to use the tool and what
it returns.

### Toolsets

Agents that cap the number of active tools (Cursor allows about 40 across all servers) can load a
subset: `LUCA_MCP_TOOLSETS=reports,sat` in the server's `env` exposes only those modules. The
toolsets are the modules of `src/luca_mcp/tools/`: `businesses`, `reports`, `journal_entries`,
`sat`, `imss` and `documents`; `login`, `logout` and `whoami` are always available. An unknown name stops
the server at startup with the list of known ones.

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
MOCK_RAI_URL=http://localhost:3030 ./dev/run_local.sh   # dev stand-in (:9400) for the Liebre app page + Liebre API token check
# ant-rai: `uv run fastapi dev src/main.py --port 3030` with
#          MCP_LUCA_LOGIN_UI_URL=http://localhost:9400/luca/connect
#          MCP_LIEBRE_TOKEN_CHECK_URL=http://localhost:9400/accounting_firms
export LUCA_RAI_URL=http://localhost:3030 LUCA_MCP_CREDENTIAL_STORE=file LUCA_MCP_CONFIG_DIR=.e2e-artifacts/config
uv run python tests/e2e/harness.py login   # scripted browser through rai and the mock app page
uv run python tests/e2e/harness.py tools
uv run python tests/e2e/harness.py call get_trial_balance '{"business_id":"bu-2","start_period_id":"202608"}'
uv run python tests/e2e/harness.py call whoami '{}' --stdio   # same, through `uv run luca-mcp`
uv run python tests/e2e/tools_suite.py     # 36 cases against dev bu-2 (use --only <substring>)
```

Layout of `src/luca_mcp/`: `server.py` assembles the FastMCP server; the tools live in `tools/`,
one module per Liebre domain (`session`, `businesses`, `reports`, `journal_entries`, `sat`,
`imss`, `documents`), each exposing `register(mcp, client)`; `tools/_common.py` holds the annotations and
the path-segment check; `api.py` is the HTTP client for rai (`fetch` for tools); `oauth.py` the
browser login and token storage; `errors.py` the error envelope; `prompts/` the two prompts.

To add a tool: write it in the module of its domain (or in a new module added to `MODULES` in
`tools/__init__.py`, whose order is the order agents see), decorate it with
`@mcp.tool(name=..., annotations=READ_ONLY)`, forward with `await client.fetch(path, **params)`
after passing every path identifier through `segment()`, and keep the docstring as specific as
the rai endpoint (it is the description the agent reads). Then add it to `EXPECTED_TOOLS` in
`tests/unit/test_tool_registry.py`, to `t_tools` in `tests/e2e/tools_suite.py` with a case against
bu-2, and to the table above. `uv run pytest` runs the offline registry checks.

`.mcp.json` registers this checkout as `luca-dev` for Claude Code (`uv run luca-mcp` against the
local rai). `dev/mock_idp.py` is **dev-only**: it plays the Liebre app's `/luca/connect` page
(pick an identity, it approves at rai with a token it mints) and the Liebre API's token check
(`/accounting_firms`: 200 for its own tokens, 401 for `unknown.user@yopmail.com`, 403 for
`nofirm@yopmail.com`). It has no real authentication and must never be deployed. The seeded
bu-2 fixtures the suite checks are described in `tests/e2e/fixtures/bu-2-seed-manifest.json`.

Nothing under `.e2e-artifacts/` is committed.

## Configuration

All optional, see `.env.example`: `LUCA_RAI_URL`, `LUCA_MCP_CLIENT_ID`, `LUCA_MCP_TIMEOUT`,
`LUCA_MCP_LOGIN_TIMEOUT`, `LUCA_MCP_CONFIG_DIR`, `LUCA_MCP_CREDENTIAL_STORE` (`auto` | `file`;
`file` skips the OS keychain for CI and headless machines), `LUCA_MCP_TOOLSETS` (comma-separated
toolsets to expose; default all).

Server-side settings (ant-rai), all with working defaults: `MCP_LUCA_LOGIN_UI_URL` (the app's
connect page; derived from `LIEBRE_APP_BASE_URL` / the environment as `<app>/luca/connect`),
`MCP_LIEBRE_TOKEN_CHECK_URL` (the Liebre API call rai makes to check a session token; derived
from the environment's Liebre API as `<api>/accounting_firms`), `MCP_OAUTH_ALLOWED_CLIENT_IDS`
(default `claude-code,luca-mcp`). No Auth0 application is needed for Luca: the Liebre app's own
login is the login.

## Status and known limitations

- Login needs the Liebre app's `/luca/connect` page deployed in the same environment as rai
  (ant-liebre-app); until then rai sends the browser to a page that does not exist yet. Only
  people with a Liebre account (active, member of a firm) can use Luca.
- Read-only; write tools are planned, not built. Excluded operations answer with a link to the
  platform.
- Deep links point to the platform root until the frontend routes are confirmed.
