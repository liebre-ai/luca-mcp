# MCP best-practices audit (2026-09-17)

Question asked: does this client follow the current best practices for building an MCP server
for Claude Code, Codex, Cursor and the other agents? Sources were read directly on this date:
the MCP specification at its current revision 2026-07-28 (tools, authorization, security best
practices, stdio transport, versioning, deprecations), Anthropic's "Writing tools for agents" and
tool-definition docs, the Claude Code, Codex and Cursor documentation, the MCP Bundle manifest and
MCP Registry guides, RFC 8252, and the installed `mcp` / `fastmcp` sources.

## What already conformed

- **Protocol revision.** `mcp` 2.2.0 under `fastmcp` 4.0.3 speaks revision 2026-07-28 and every
  handshake revision back to 2024-11-05, so modern and legacy clients both work. Nothing this
  client uses is deprecated (server-to-client logging is, and it is not used).
- **Names and annotations.** Tool names use the spec's character set; the longest name with
  Claude Code's `mcp__luca__` prefix is 42 characters, under the 64-character limit Codex and
  Bedrock gateways enforce. Data tools carry `readOnlyHint`; `login` and `logout` are declared
  non-destructive and idempotent, which matches the annotation semantics in the schema.
- **Errors.** Failures are `isError` results carrying `{"error": {code, message, hint}}`, the
  spec's "tool execution errors with actionable feedback".
- **Inputs.** `additionalProperties: false` everywhere, the recommended empty-object schema for
  tools without parameters, local path-segment validation, full validation in rai.
- **stdio hygiene.** Only JSON-RPC on stdout; logs on stderr.
- **Result size.** `_meta.anthropic/maxResultSizeChars` on `get_sat_archive` is a documented
  Claude Code mechanism (500k-character ceiling); every other tool stays under the 25k-token
  default through server-side caps.
- **Deterministic tool order**, which the spec recommends for client caching.
- **Auth.** The spec says stdio servers should take credentials from the environment rather than
  run the HTTP OAuth flow; this client deliberately runs a browser login instead, the pattern
  GitHub's official local MCP server also uses. The flow follows RFC 8252: `127.0.0.1` literal
  instead of `localhost`, ephemeral port opened only during login, PKCE S256, `state` check,
  system browser, public client without a secret. Tokens live in the OS keychain with a 0600
  file fallback, as CLIs such as IBM's mcp-cli do. rai issues 30-minute access tokens and
  30-day refresh tokens rotated on every refresh (the old one is revoked at once).
- **Prompts and instructions.** Claude Code CLI exposes the prompts as
  `/mcp__luca__luca_guidelines` and `/mcp__luca__luca_navigation` and injects the server
  instructions; Cursor supports prompts; no evidence that Codex surfaces MCP prompts.

## Gaps found and what was done

| Gap | Change |
|---|---|
| 82 of 82 parameters had no JSON-schema `description`; their meaning lived only in docstring prose. Anthropic asks for the meaning of every parameter, and Claude Code now selects MCP tools through tool search over the schemas. | Every parameter is `Annotated[<same type>, Field(description=...)]`; shared wording lives in `tools/_common.py` (`BusinessId`, `AccountingFirmId`, `PeriodId`, ...). Guarded by `tests/unit/test_tool_registry.py`. |
| fastmcp printed a banner and called pypi.org for a version check on every server start. | `run(transport="stdio", show_banner=False)`. |
| Codex kills servers that take more than 10 s to start; a cold `uvx` install can exceed that. | README recommends `startup_timeout_sec = 60` next to `tool_timeout_sec = 300`. |
| Cursor caps active tools at about 40 across all servers; the catalogue will grow. | `LUCA_MCP_TOOLSETS` exposes a subset of modules (session tools always on). Consolidating tools (for example one `get_report` with a `report` enum) is a breaking change left for a product decision. |
| `uvx --from git+https://...` pins the resolved commit; users only get updates with `--refresh` or a tag. The MCP Registry needs PyPI; Claude Desktop needs a `.mcpb` bundle. | README "Updating and pinning" section; version bumped to 0.3.0 so the next tag is `v0.3.0`. Registry and bundle publication are options for later. |
| `login` blocked silently for up to 5 minutes; Claude Code backgrounds calls after 2 minutes and Codex times out at 60 s by default. | `login` sends MCP progress notifications every 5 s with the elapsed time and the authorize URL; the error still carries the URL. Unit-tested offline. |

Two facts worth keeping in mind, not changed here: one keychain entry per rai URL serves every
client on the machine (one login for Claude Code, Codex and Cursor), and refresh-token rotation
without a grace period means two client processes refreshing at the same instant could
invalidate each other; the loser sees `not_authenticated` and must log in again.

## Sources

- MCP spec: versioning https://modelcontextprotocol.io/specification/versioning, tools
  https://modelcontextprotocol.io/specification/2026-07-28/server/tools, authorization
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization, security
  https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices,
  stdio https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio,
  deprecations https://modelcontextprotocol.io/specification/2026-07-28/deprecated
- Anthropic: https://www.anthropic.com/engineering/writing-tools-for-agents,
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools
- Claude Code: https://code.claude.com/docs/en/mcp
- Codex: https://learn.chatgpt.com/docs/extend/mcp?surface=cli,
  https://learn.chatgpt.com/docs/config-file/config-reference,
  https://github.com/openai/codex/issues/46188 (64-character tool names)
- Cursor: https://cursor.com/docs/mcp, https://forum.cursor.com/t/tools-limited-to-40-total/67976
- Distribution: https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md,
  https://github.com/modelcontextprotocol/registry/blob/main/docs/guides/publishing/publish-server.md,
  https://docs.astral.sh/uv/guides/tools/
- Auth: https://www.rfc-editor.org/rfc/rfc8252.html, https://github.com/github/github-mcp-server

## Evidence (2026-09-17, branch `refactor/src-layout`, version 0.3.0)

- `ruff check` and `ruff format --check` clean; `uv run pytest` 11 passed (registry, toolsets and
  login-progress tests, all offline).
- `list_tools` / `list_prompts` JSON identical to the pre-change snapshot except for the added
  parameter descriptions; a per-module schema-diff script enforced that while the descriptions
  were written.
- `tests/e2e/tools_suite.py` against the local rai on `:3030`: 28/37, the same nine failures for
  the same reasons as the `main` baseline (this rai sends fresh logins to the real Auth0 tenant,
  bu-2's last open period changed, jd@yopmail.com gained a business); the tool-surface,
  robustness, prompts, concurrency and both real-stdio cases pass.
- Through the real `uv run luca-mcp` process: 19 tools with 82 of 82 parameters described,
  `whoami` reports `client_version` 0.3.0, `LUCA_MCP_TOOLSETS=reports,documents` lists 9 tools,
  `LUCA_MCP_TOOLSETS=typo` stops the server naming the known toolsets, stderr carries no banner.
