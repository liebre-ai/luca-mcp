# Split `server.py` into a tools package (2026-09-17)

## Why

`src/luca_mcp/server.py` holds all 19 tools in one 500-line `create_server()` closure. Every new
tool means editing that one growing function, and the domains (session, businesses, reports,
journal entries, SAT, documents) are only separated by comment banners. Many more tools are
planned, so the layout should make "add a tool" a local change to one small module.

`src/` also still carries two modules from the v1 hosted server that nothing imports:
`settings.py` (pydantic-settings for a remote OAuth proxy) and `errors.py` (`LucaToolError`, with
fields and codes the client never produces). Both describe a design that no longer exists and
mislead the next reader.

## What must not change

- The MCP surface: tool names, descriptions (docstrings are what the agent reads), parameter
  schemas, annotations, `meta`, listing order, the two prompts and the server instructions.
  Proven by diffing the `list_tools` / `list_prompts` JSON before and after.
- `create_server(config)` and `run_stdio()` keep their signatures (`cli.py`, `tests/e2e/harness.py`
  and `tests/e2e/tools_suite.py` call them).
- Behaviour: error envelope, login flow, refresh-and-retry on 401, path-segment validation,
  blocking HTTP calls run off the event loop.
- Dependencies: none added.

## Target layout

```
src/luca_mcp/
├── __init__.py            version
├── cli.py                 `luca-mcp` command line: serve / login / logout / status
├── config.py              Config + load_config (environment)
├── oauth.py               browser login (PKCE), token storage, refresh, revoke
├── errors.py              LucaError and the error envelope (replaces the dead v1 module)
├── api.py                 RaiClient: HTTP to rai with the stored session (sync `get`, async `fetch`)
├── server.py              create_server(): FastMCP + instructions, registers tools and prompts; run_stdio()
├── prompts/
│   ├── __init__.py
│   └── luca.py            luca_guidelines, luca_navigation
└── tools/
    ├── __init__.py        MODULES tuple + register_all(mcp, client)   <- new modules go here
    ├── _common.py         READ_ONLY / SESSION annotations, segment() path validation
    ├── session.py         login, logout, whoami
    ├── businesses.py      list_businesses, get_business, list_periods
    ├── reports.py         get_trial_balance, get_balance_sheet, get_income_statement, get_vat_determination
    ├── journal_entries.py search_journal_entries, list_journal_entries_for_period, get_journal_entry
    ├── sat.py             list_declaraciones, list_sat_archives, get_sat_archive, list_article_69b_matches
    └── documents.py       list_documents, get_document
```

Removed: `settings.py`; the old content of `errors.py`.

This is the shape the v1 plan already prescribed (`tools/{...}.py`, `register(mcp)` per module,
see `docs/plans/2026-09-08-luca-mcp-plan.md` §5) and that the v1 code used before the rewrite
to a thin client folded everything into `server.py`.

## Conventions for new tools

- One module per Liebre domain, named after the resource the tools read (the way rai's
  `/api/v1/mcp/businesses/{id}/...` paths are grouped). A new domain is a new module plus one
  entry in `MODULES` in `tools/__init__.py`; the order of `MODULES` is the order agents see.
- Each module exposes `register(mcp: FastMCP, client: RaiClient) -> None` and defines its tools
  inside it with `@mcp.tool(name=..., annotations=READ_ONLY)`. The tool body forwards to rai with
  `await client.fetch(path, **params)`; `None` parameters are dropped by the client.
- The docstring is the tool description: keep it as specific as the rai endpoint it fronts
  (what is returned, filters, caps, gotchas). Prose lives there, not in comments.
- Every identifier that becomes a URL path segment goes through `segment(value, name)`.
- Read-only tools carry `READ_ONLY`; the session tools carry `SESSION`. A future write tool needs
  its own annotations and an update to the read-only check in `tests/e2e/tools_suite.py`
  (`t_tools`) and `tests/unit/test_tool_registry.py`.
- The README tool table follows the same order as `MODULES`.

## Steps (one commit each)

1. `errors.py`: replace the dead v1 module with `LucaError`, `envelope()` and `NOT_LOGGED_IN`
   moved out of `api.py`; `api.py` and `cli.py` import from it. Delete `settings.py`.
2. `api.py`: expose `RaiClient.config` and add the async `fetch(path, **params)` that
   `create_server()` currently builds as a local closure.
3. `tools/` package: `_common.py`, the six domain modules (bodies and docstrings moved verbatim),
   `__init__.py` with `register_all`. `server.py` keeps only the instructions and the assembly.
4. `tests/unit/test_tool_registry.py`: offline check (no rai, no login) of the ordered tool list,
   descriptions, read-only annotations and the prompts.
5. README (layout, how to add a tool) and this document's evidence section.

## Verification

- `ruff check`, `ruff format --check`, `pytest` (the new offline test).
- Surface diff: `list_tools` / `list_prompts` JSON dumped from the current `main` before the
  change and from the branch after it; the diff must be empty.
- `tests/e2e/tools_suite.py` against the local rai on `:3030` (baseline run on `main` first,
  then on the branch): 36/36, including the real `uv run luca-mcp` stdio process case.

## Evidence (2026-09-17, branch `refactor/src-layout`)

- `ruff check` and `ruff format --check` clean; `uv run pytest` 4 passed
  (`tests/unit/test_tool_registry.py`, no rai needed).
- Surface diff: the `list_tools` / `list_prompts` JSON (19 tools, 2 prompts: names, order,
  descriptions, input schemas, annotations, `meta`, prompt texts) is byte-identical before and
  after; `INSTRUCTIONS` is identical; each tool's decorated source is identical after the two
  renames (`await get(` -> `await client.fetch(`, `_segment(` -> `segment(`).
- `tests/e2e/tools_suite.py` against the local rai on `:3030`: baseline on `main` 28/37, branch
  28/37 with the same nine failures for the same reasons, all environment or data drift on this
  machine, none in the client: this rai now sends fresh `luca-mcp` logins to the real Auth0 tenant,
  so the scripted browser cannot drive the refused-login, session-lifecycle and logout-revoke
  cases (6); bu-2's last open period changed (`get_business`, `list_periods`); `jd@yopmail.com`
  is now also allowed on bu-1385 (restricted identity). The 28 passing cases include the real
  `uv run luca-mcp` stdio process and the not-logged-in stdio case.
- By hand through the real stdio process (`tests/e2e/harness.py call ... --stdio`): `whoami`
  logged in; `get_trial_balance(bu-2, 202608)` 35 rows, total debits equal total credits.

## Follow-up on the same branch

The audit in `docs/plans/2026-09-17-mcp-best-practices-audit.md` added, on top of this layout:
parameter descriptions in every tool schema (shared wording in `tools/_common.py`), the
`LUCA_MCP_TOOLSETS` selection built on `MODULES`, progress notifications in `login`, no fastmcp
banner, README updates and version 0.3.0. The tool surface stayed identical apart from the
descriptions.
