> Research snapshot captured 2026-09-08 while planning luca-mcp. Point-in-time; re-verify before relying on a specific version, URL, or setting. Source: Claude Code official docs (code.claude.com), v2.1.259.

# Claude Code Remote MCP OAuth & Plugin Technical Brief

**Version:** Claude Code v2.1.259 | **Source:** https://code.claude.com/docs

---

## 1. Remote MCP Servers Over HTTP: OAuth Authentication

### Authorization Server Discovery

Claude Code discovers OAuth metadata through the following sequence:

1. **RFC 9728 Protected Resource Metadata** at `/.well-known/oauth-protected-resource`
2. **RFC 8414 Authorization Server Metadata** at `/.well-known/oauth-authorization-server` (fallback)
3. **Override:** Specify explicit URL via `.mcp.json` field `oauth.authServerMetadataUrl` (must be HTTPS)

**Metadata URL format:** Supports RFC 8414 and RFC 9728 endpoints. The metadata's `scopes_supported` field overrides server-advertised scopes.

**Sources:** [Remote MCP Servers over HTTP: OAuth Authentication Guide](https://code.claude.com/docs/en/mcp)

### Dynamic Client Registration (RFC 7591)

- **Supported:** Yes, automatically handled by Claude Code
- **No configuration required:** For servers supporting DCR
- **Pre-configured credentials available:** Via CLI flags `--client-id`, `--client-secret`, `--callback-port` or `.mcp.json` `oauth` object

**Flow:** Claude Code attempts DCR first; if server doesn't support it, falls back to pre-configured credentials if provided.

### Client ID Metadata Documents (CIMD)

**Not documented** in Claude Code official documentation. Feature support unclear.

### Redirect URI & Callback Port

- **Format:** `http://localhost:PORT/callback`
- **Port behavior:**
  - Omit `--callback-port` for ephemeral port (random, dynamically allocated)
  - Pass `--callback-port N` for fixed port (register with OAuth provider in advance)
- **Legacy format (v2.1.229):** `http://127.0.0.1:PORT/callback` — upgrade to v2.1.231+ if OAuth sign-in fails with redirect URI mismatch
- **Registration:** Provider must accept redirect URI exactly as configured

**Sources:** [Redirect URI and Callback Port](https://code.claude.com/docs/en/mcp)

### RFC 8707 Resource Parameter

**Not documented** in Claude Code official documentation. Feature support unclear.

### Token Storage & Refresh

- **Storage mechanism:** System keychain (macOS) or credentials file (secure system storage)
- **Automatic refresh:** Access tokens refreshed transparently when expired
- **Rejection handling:** If refresh token is rejected, server flagged in `/mcp` as "! Needs authentication"
- **Clear authentication:** `claude mcp logout <name>` or `/mcp` → **Clear authentication**
- **Re-authentication:** Run `/mcp` → select server → **Re-authenticate** or `claude mcp login <name>`

**Sources:** [Token Storage and Refresh](https://code.claude.com/docs/en/mcp)

### Scope Handling

- **User configuration:** Set `oauth.scopes` field in `.mcp.json` (space-separated, RFC 6749 §3.3 format)
- **Precedence:** User-configured scopes override server-discovered `scopes_supported` catalog
- **Automatic offline scope:** `offline_access` appended if advertised by auth server (v2.1.196+)
- **Scope reduction (v2.1.196+):** Claude Code requests only server-provided scopes, not full `scopes_supported` list

**Example `.mcp.json`:**
```json
{
  "mcpServers": {
    "slack": {
      "type": "http",
      "url": "https://mcp.slack.com/mcp",
      "oauth": {
        "scopes": "channels:read chat:write search:read"
      }
    }
  }
}
```

**Sources:** [Scope Restriction](https://code.claude.com/docs/en/mcp)

---

## 2. MCP Config Scopes & `.mcp.json` Configuration

### Installation Scope Tiers

| Scope | File | Available to | Notes |
|-------|------|--------------|-------|
| `local` | `~/.claude.json` (project-specific entry) | You, this project only | Default; highest precedence |
| `project` | `.mcp.json` (repo root) | Team (when committed to VCS) | Shared with collaborators |
| `user` | `~/.claude.json` (top-level `mcpServers` key) | You, all projects | Lowest precedence |

**Precedence:** Local > Project > User (higher scope overrides lower).

**Sources:** [Where servers are saved](https://code.claude.com/docs/en/mcp-quickstart)

### `.mcp.json` Format & Configuration

#### HTTP Server with OAuth

```json
{
  "mcpServers": {
    "my-server": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "authServerMetadataUrl": "https://auth.example.com/.well-known/openid-configuration",
        "scopes": "channels:read chat:write offline_access",
        "clientId": "your-client-id",
        "callbackPort": 8080
      }
    }
  }
}
```

#### Environment Variable Expansion

- **Format:** `${VARIABLE_NAME}` substitution syntax
- **Scope:** Works in `url`, header values, and other string fields
- **Example:** `"url": "${LUCA_MCP_URL}"`

#### Local Stdio Server

```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"],
      "env": {
        "CUSTOM_VAR": "value"
      }
    }
  }
}
```

**Sources:** [Edit .mcp.json directly](https://code.claude.com/docs/en/mcp-quickstart)

### Custom Authentication (Non-OAuth)

For authentication schemes not covered by OAuth (Kerberos, short-lived tokens, internal SSO):

```json
{
  "mcpServers": {
    "internal-api": {
      "type": "http",
      "url": "https://mcp.internal.example.com",
      "headersHelper": "/opt/bin/get-mcp-auth-headers.sh"
    }
  }
}
```

**Helper script requirements:**
- Outputs JSON object of string key-value pairs to stdout
- Runs fresh on every connection (no caching)
- 10-second execution timeout
- Runs only after folder trust acceptance (project/local servers)
- Receives environment variables: `CLAUDE_CODE_MCP_SERVER_NAME`, `CLAUDE_CODE_MCP_SERVER_URL`, `CLAUDE_PLUGIN_ROOT` (plugins only)
- Auto-retry behavior: On 401/403, Claude Code re-runs helper, reconnects, and retries once

**Sources:** [Custom Authentication section](https://code.claude.com/docs/en/mcp)

### claude.ai Custom Connectors & Redirect URIs

**Not documented in Claude Code docs.** Custom connectors on claude.ai (https://claude.ai/customize/connectors) follow separate documentation at [Claude Connector Building Docs](https://claude.com/docs/connectors/building). Redirect URI formats and OAuth flow for claude.ai connectors require verification against that external documentation; likely differs from Claude Code loopback pattern.

---

## 3. Plugins: Packaging, Distribution & MCP Integration

### Plugin Directory Structure

**Critical Layout Rules:**
- Do NOT put `commands/`, `agents/`, `skills/`, `hooks/` inside `.claude-plugin/`
- Only `plugin.json` goes in `.claude-plugin/`
- All other directories at **plugin root** level

```text
my-plugin/
├── .claude-plugin/
│   └── plugin.json                    # Manifest only
├── skills/
│   ├── skill-1/
│   │   └── SKILL.md
│   └── skill-2/
│       └── SKILL.md
├── agents/
│   └── custom-agent.json
├── hooks/
│   └── hooks.json
├── .mcp.json                          # HTTP & stdio servers
├── .lsp.json                          # Language servers
├── monitors/
│   └── monitors.json
├── settings.json                      # Default plugin settings
├── commands/                          # Legacy; use skills/ for new plugins
└── bin/                               # Executables (restricted in marketplace)
```

**Fallback rule:** Plugin with single skill can place `SKILL.md` directly at root instead of `skills/` directory.

**Sources:** [Plugin structure overview](https://code.claude.com/docs/en/plugins)

### `plugin.json` Manifest Schema

**Minimal example:**
```json
{
  "name": "my-plugin",
  "description": "What this plugin does",
  "version": "1.0.0",
  "author": {
    "name": "Your Name"
  }
}
```

**Field descriptions:**
- **`name`** (required): Unique identifier; becomes skill namespace prefix (`/my-plugin:skill-name`)
- **`description`** (required): Shown in plugin manager; truncated at 1,536 characters
- **`version`** (optional): Semantic version; controls update behavior for users
- **`author`** (optional): Attribution information
- **Supported but undocumented:** `homepage`, `repository`, `license` fields

**Sources:** [Create the plugin manifest](https://code.claude.com/docs/en/plugins)

### HTTP MCP Server in Plugin

- **.mcp.json** location: Plugin root (same as project-scoped servers)
- **OAuth support:** Yes, same discovery and DCR flow applies as project-scoped servers
- **Environment variable expansion:** Supported in `.mcp.json` values
- **`${CLAUDE_PLUGIN_ROOT}`:** Available placeholder for plugin-relative paths in `.mcp.json`

**Example plugin `.mcp.json`:**
```json
{
  "mcpServers": {
    "internal-tool": {
      "type": "http",
      "url": "https://mcp.internal.example.com/mcp",
      "oauth": {
        "clientId": "${PLUGIN_CLIENT_ID}",
        "scopes": "read write"
      }
    }
  }
}
```

**Sources:** [Plugin structure overview](https://code.claude.com/docs/en/plugins)

### Installation & Marketplace Commands

#### Add Marketplace
```shell
/plugin marketplace add anthropics/claude-plugins-official
/plugin marketplace add https://github.com/my-org/plugins.git
/plugin marketplace add ./local/marketplace
```

#### Install Plugin
```shell
/plugin install my-plugin@marketplace-name
claude plugin install my-plugin@marketplace-name --scope user
```

**Scope options:**
- **User scope:** Install for yourself across all projects
- **Project scope:** Install for team (adds to `.claude/settings.json`)
- **Local scope:** Install for yourself in this project only (`.claude/settings.local.json`)

**Marketplace sources supported:**
- GitHub (`owner/repo` shorthand)
- Git URLs (any host, with or without `.git` suffix depending on host)
- Local directories or direct `marketplace.json` file paths
- Remote URLs (direct `marketplace.json` HTTP endpoint)

**Sources:** [Discover and install plugins](https://code.claude.com/docs/en/discover-plugins)

### `marketplace.json` Schema

**Not documented in fetched Claude Code pages.** Refer to reference implementations:
- [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
- [anthropics/claude-plugins-community](https://github.com/anthropics/claude-plugins-community)

---

## 4. SKILL.md Frontmatter Reference

### Complete Frontmatter Schema

```yaml
---
name: my-skill
description: What this skill does; when to auto-invoke it
when_to_use: Additional context for auto-invocation
argument-hint: "[arg1] [arg2]"
disable-model-invocation: false
user-invocable: true
allowed-tools: Read Bash(git *) mcp__slack__post_message
disallowed-tools: WebFetch
arguments: [arg1, arg2]
context: fork
agent: Explore
background: true
model: claude-opus-5
effort: high
shell: bash
paths: "src/**/*.ts"
hooks: { ... }
metadata: { ... }
license: MIT
compatibility: "Node.js 18+"
---
```

### MCP Tool Reference in Skills

Skills reference MCP tools using the naming pattern: **`mcp__<server>__<tool>`**

**Format:** Server name (from MCP config) + tool name (from server's tool list)

**Examples:**
```yaml
allowed-tools: mcp__slack__post_message mcp__slack__list_channels mcp__github__create_issue
```

**Tool search with deferred tools:** When MCP servers expose many tools (threshold not documented), Claude Code defers tool listing using "tool search" mechanism. Individual tools still invoked by `mcp__server__tool` pattern.

**Sources:** [Skills frontmatter reference](https://code.claude.com/docs/en/skills)

### Frontmatter Field Breakdown

#### Core Metadata
- **`name`:** Display name in skill listings; defaults to directory name
- **`description`:** What skill does + when to use; Claude reads for auto-invocation; combined with `when_to_use`; max 1,536 characters in listings
- **`when_to_use`:** Additional context appended to description for auto-invocation triggers

#### Invocation Control
- **`disable-model-invocation`:** Set `true` to prevent Claude auto-invoke; manual `/name` still works (use for deployments)
- **`user-invocable`:** Set `false` to prevent user invocation; only Claude can invoke (background knowledge)
- **`argument-hint`:** Autocomplete hint string, e.g., `"[issue-number]"` or `"[filename] [format]"`

#### Tools & Permissions
- **`allowed-tools`:** Tools available without permission during skill turn; cleared after user's next message
  - Format: space/comma-separated or YAML list
  - Tool patterns: `Bash(git *)`, `Read`, `mcp__server__tool`
- **`disallowed-tools`:** Tools removed from pool while skill active; cleared after user's next message

#### Arguments & Substitution
- **`arguments`:** Named positional args for `$name` substitution in skill content
- **Placeholders available in skill content:**
  - `$ARGUMENTS` — All arguments passed
  - `$ARGUMENTS[N]` or `$N` — Specific argument by index (0-based)
  - `$name` — Named argument (from `arguments` field)
  - `${CLAUDE_SESSION_ID}` — Current session ID
  - `${CLAUDE_EFFORT}` — Current effort level (low/medium/high/xhigh/max)
  - `${CLAUDE_SKILL_DIR}` — Directory containing SKILL.md
  - `${CLAUDE_PROJECT_DIR}` — Project root
  - `${CLAUDE_PLUGIN_ROOT}` — Plugin installation dir (plugin skills only)
  - `${CLAUDE_PLUGIN_DATA}` — Plugin persistent data dir (plugin skills only)

#### Execution Context
- **`context`:** Set to `fork` to run in isolated subagent context (no conversation history access)
- **`agent`:** Subagent type when `context: fork`; options: `Explore`, `Plan`, `general-purpose`, custom agent names
- **`background`:** With `context: fork`, set `false` to wait for result in current turn; default `true` (requires v2.1.218+)
- **`model`:** Model to use when skill active; overrides session model for that turn
- **`effort`:** Effort level when skill active; options: `low`, `medium`, `high`, `xhigh`, `max`
- **`shell`:** Shell for `` `!command` `` blocks; `bash` (default) or `powershell`

#### Advanced Features
- **`paths`:** Glob patterns limiting auto-activation; comma-separated or YAML list
- **`hooks`:** Hooks registered when skill invokes (see Hooks documentation)
- **`metadata`:** Free-form YAML map for custom data; Claude Code ignores, other tools read it
- **`license`:** License covering skill (Agent Skills spec)
- **`compatibility`:** Environment requirements (Agent Skills spec); max 500 characters

### Shell Command Execution in Skills

Skills can run shell commands before sending content to Claude:

```markdown
## Current changes

!`git diff HEAD`

## Instructions

Summarize the changes above...
```

Multi-line commands use fenced blocks:
````markdown
```!
node --version
git status --short
```
````

**Disable:** Set `disableSkillShellExecution: true` in settings to prevent shell execution.

**Synced skills limitation:** Synced skills from claude.ai never run shell commands on local machines.

### Synced Skills from claude.ai

Only these frontmatter fields are supported when syncing to claude.ai:
- `name`
- `description`
- `license`
- `compatibility`
- `metadata`
- `allowed-tools`

**Unsupported fields cause hard errors.**

**Sources:** [Skills frontmatter reference](https://code.claude.com/docs/en/skills)

---

## 5. Elicitation, Resources & Advanced MCP Features

### Elicitation (Mid-task User Input)

MCP servers can request structured input from users mid-task using elicitation.

#### Two Request Modes

**Form Mode:** Claude Code displays dialog with form fields defined by server (e.g., username/password prompt). User fills fields and submits.

**URL Mode:** Claude Code opens browser URL for authentication or approval. User completes flow in browser, then confirms in CLI.

#### URL Length Limitations

Claude Code passes URL as command-line argument to system's URL handler with character cap:
- **No escaping:** ~8,000 character limit
- **Heavy percent-escaping:** ~4,000 character limit

When URL exceeds cap after escaping, user can only decline the request.

#### Auto-responding to Elicitation

To auto-respond without user dialog, use `--permission-prompt-tool` flag with tool that approves/denies request. Agent SDK [`canUseTool` callback](https://code.claude.com/docs/en/agent-sdk) also supports automatic responses.

**Sources:** [Respond to MCP Elicitation Requests](https://code.claude.com/docs/en/mcp)

### MCP Resources & Resource Links

MCP servers expose **resources** — structured data like documents, database records, configuration — distinct from tools.

#### Resource Annotations

Servers can annotate resources with `_meta` fields to guide Claude Code handling:

```json
{
  "uri": "file:///path/to/document",
  "name": "report.md",
  "mimeType": "text/markdown",
  "_meta": {
    "anthropic/maxResultSizeChars": 500000
  }
}
```

#### Referencing Resources in Prompts

Claude references server resources via `@server:resource` syntax in conversation.

**Sources:** [MCP Resources and Resource Links](https://code.claude.com/docs/en/mcp)

### MCP Prompts

MCP servers provide **prompts** — reusable instruction templates Claude can use to structure requests. Prompts expose complex workflows as pre-configured conversation starters.

**Invocation:** Run as commands with `/mcp__server__prompt` pattern (when available in `/` menu).

**Sources:** [MCP Prompts section](https://code.claude.com/docs/en/mcp)

### Tool Annotations & Metadata

#### `anthropic/maxResultSizeChars`

Allow individual tools to return results larger than default threshold:

```json
{
  "name": "get_schema",
  "description": "Returns the full database schema",
  "_meta": {
    "anthropic/maxResultSizeChars": 200000
  }
}
```

- **Hard ceiling:** 500,000 characters per tool result
- **Applies independently** of `MAX_MCP_OUTPUT_TOKENS` for text content

#### `anthropic/requiresUserInteraction`

Mark tool as requiring explicit approval on every call:

```json
{
  "name": "grant_access",
  "description": "Requests access to a protected resource",
  "_meta": {
    "anthropic/requiresUserInteraction": true
  }
}
```

- **Effect:** Permission prompt shown on every call
- **Ignores permission modes:** Even in `acceptEdits`, `auto`, `bypassPermissions` modes
- **No dismissal:** No "don't ask again" option

**Sources:** [Tool Annotations section](https://code.claude.com/docs/en/mcp)

### MCP Blob Content

Some MCP servers return **blob content** — binary data like images, PDFs, archives. Claude Code handles these transparently, passing them to Claude for processing or analysis.

**Sources:** [MCP Blob Content](https://code.claude.com/docs/en/mcp)

### Tool Search & Deferred Tools with Large Tool Sets

When MCP servers expose many tools (threshold not documented), Claude Code defers tool listing using **tool search** mechanism.

**Behavior:**
- Initial tool list abbreviated or deferred
- User can search/filter available tools
- Individual tools still referenced by `mcp__server__tool` pattern
- **`MAX_MCP_OUTPUT_TOKENS`:** Mentioned but no specific numeric limit documented for tool result truncation

**Sources:** [Tool Annotations and Tool Search mention](https://code.claude.com/docs/en/mcp)

---

## Key Gotchas & Documentation Gaps

| Item | Status | Notes |
|------|--------|-------|
| RFC 9728 WWW-Authenticate header parsing | Not documented | Only metadata URL endpoints mentioned |
| Client ID Metadata Documents (CIMD) | Not documented | No mention in OAuth sections |
| RFC 8707 resource parameter | Not documented | Feature support unclear |
| claude.ai custom connector redirect URIs | Not documented | See https://claude.com/docs/connectors/building |
| Plugin OAuth flow end-to-end | Implicitly documented | Uses same `.mcp.json` OAuth as project scope |
| MAX_MCP_OUTPUT_TOKENS numeric limit | Not documented | Referenced but no value given |
| Tool count threshold for deferred tools | Not documented | Referenced as "tool search" without threshold |
| Plugin marketplace.json full schema | Not documented | Refer to GitHub reference implementations |
| Stateless server session handling | Not documented | No guidance on session management |
| Mcp-Session-Id header usage | Not documented | Not mentioned in Claude Code docs |
| Trailing slash normalization in URLs | Not mentioned | May affect endpoint matching |
| Server cache behavior for tool lists | Partially documented | `cached` status mentioned in v2.1.221+ |

---

## Summary for Implementation

1. **OAuth Discovery:** Try RFC 9728 `/.well-known/oauth-protected-resource` first; fallback to RFC 8414; support explicit override URL
2. **Dynamic Registration:** Automatic for DCR-supporting servers; pre-configured credentials fallback available
3. **Callback Port:** Ephemeral by default; fixed via `--callback-port` when pre-registered with provider
4. **Scopes:** Configurable via `oauth.scopes` in `.mcp.json` (space-separated, RFC 6749 format); user config overrides server-discovered
5. **Tokens:** Secure system storage, automatic refresh, rejection handling with re-auth prompt
6. **Plugins:** Ship `.mcp.json` at plugin root; OAuth discovery and DCR same as project scope
7. **Skills:** Reference MCP tools as `mcp__server__tool` in `allowed-tools` field
8. **Elicitation:** Fully supported (form + URL modes); auto-response via `--permission-prompt-tool`
9. **Tool Output:** Supports `anthropic/maxResultSizeChars` (up to 500KB) and `anthropic/requiresUserInteraction` annotations
10. **Deferred Tools:** Large tool sets use tool search mechanism; threshold not documented
11. **Resources:** Support `_meta` annotations; reference via `@server:resource` in prompts
12. **Prompts:** MCP servers expose reusable prompts; invoked as `/mcp__server__prompt` commands

---

**Documentation Source:** https://code.claude.com/docs (v2.1.259) | Fetched 2026-09-08
