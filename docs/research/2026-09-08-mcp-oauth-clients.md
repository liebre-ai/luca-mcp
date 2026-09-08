> Research snapshot captured 2026-09-08 while planning luca-mcp. Point-in-time; re-verify before relying on a specific version, URL, or setting. Source: web research (spec pages, vendor docs, SDK docs), URLs inline.

# Remote MCP server: authorization, transport, clients, Python options

Research date: 2026-09-08. Every claim below was checked against a page fetched that day unless marked **UNVERIFIED**. Publication dates are given where the source shows one. Target: a Python Streamable-HTTP MCP server on Google Cloud Run (multiple instances) wrapping a multi-tenant accounting API, used from Claude Code, Codex CLI, Cursor, claude.ai / Claude Desktop custom connectors, ChatGPT, VS Code and Gemini/Antigravity CLI. The platform's identity provider may not support Dynamic Client Registration.

Local CLI facts supplied by the team lead and reconciled below: Codex CLI 0.153.3 (`codex mcp add <name> --url <url> [--oauth-client-id] [--oauth-client-registration auto|cimd|dcr] [--oauth-resource]`, `codex mcp login <name> --scopes a,b`) and Claude Code 2.1.259 (`claude mcp add --transport http` with `--client-id/--client-secret/--callback-port`). Both match the fetched documentation; details in section 3.2.

---

## 0. Headline findings that change the design

1. **The current spec revision is 2026-07-28, not 2025-11-25.** It is a breaking, stateless redesign: `initialize` and `Mcp-Session-Id` are gone, every request carries protocol version and client capabilities in `_meta`, `server/discover` is a mandatory RPC, server-initiated elicitation is replaced by Multi Round-Trip Requests (MRTR), and the GET SSE stream and `Last-Event-ID` resumability are removed. Sources: https://modelcontextprotocol.io/specification/versioning ("The **current** protocol version is **2026-07-28**") and https://modelcontextprotocol.io/specification/2026-07-28/changelog.
2. **Dynamic Client Registration is now Deprecated** (earliest removal: first revision on or after 2027-07-28). Client ID Metadata Documents (CIMD) are the SHOULD. https://modelcontextprotocol.io/specification/2026-07-28/deprecated
3. **Clients are mid-migration.** Anthropic said support "is being rolled out across Claude products soon" (blog, 2026-07-28, no dates); Codex merged its 2026 client support on 2026-07-28; Claude's connector docs still list only the 2025-03-26/06-18/11-25 auth specs. The server must serve both eras. The Python SDK v2 does this on one endpoint with no configuration.
4. **Python versions:** `mcp` 2.2.0 (released 2026-09-07; server class renamed `MCPServer`) and `fastmcp` 4.0.3 (2026-09-05; built on `mcp` 2.x). https://pypi.org/project/mcp/ , https://pypi.org/project/fastmcp/
5. **Every major client can do DCR; CIMD is supported by Claude (hosted and Code), ChatGPT, Codex and VS Code, but not Cursor.** Pre-registered client id/secret is supported by all of them except (officially) ChatGPT, where only third-party writeups describe the field.
6. **The spec-blessed pattern for "our IdP has no DCR" is an OAuth proxy in front of it** (FastMCP `OAuthProxy`), with mandatory per-client consent (confused-deputy mitigation). If the platform has no OAuth authorization endpoint at all, an authorization server (hosted or self-built) is required; see section 7.

---

## 1. MCP Authorization spec, revision 2026-07-28

Main page: https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization. It now splits into sub-pages for discovery, client registration and security considerations.

### 1.1 Roles, OAuth 2.1, PKCE, grants

- "A protected *MCP server* acts as an OAuth 2.1 resource server". "The *authorization server* ... may be hosted with the resource server or a separate entity."
- "Authorization servers **MUST** implement OAuth 2.1 with appropriate security measures for both confidential and public clients." Normative reference is draft-ietf-oauth-v2-1-13.
- PKCE: "MCP clients **MUST** implement PKCE ... and **MUST** verify PKCE support before proceeding". "MCP clients **MUST** use the `S256` code challenge method when technically capable". If `code_challenge_methods_supported` is absent from AS metadata (OAuth or OIDC discovery) "MCP clients **MUST** refuse to proceed"; "Authorization servers providing OpenID Connect Discovery 1.0 **MUST** include `code_challenge_methods_supported`". https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- Grants: OAuth 2.1 "defines the following authorization grant types: authorization code, client credentials, and refresh token" (implicit and password grants are gone); "Clients MUST use `code_challenge` and `code_verifier`"; redirect URIs must "exactly match one that was registered". https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13
- Refresh tokens: "For public clients, authorization servers **MUST** rotate refresh tokens". Servers "**SHOULD NOT** include `offline_access` in `WWW-Authenticate` scope or ... `scopes_supported`".

### 1.2 Protected Resource Metadata (RFC 9728) and the 401

https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery

- "MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata" and the document "**MUST** include the `authorization_servers` field containing at least one authorization server."
- Location: either the `WWW-Authenticate` header on 401 with `resource_metadata`, or the well-known URI, "At the path of the server's MCP endpoint: `https://example.com/public/mcp` could host metadata at `https://example.com/.well-known/oauth-protected-resource/public/mcp`" or "At the root". Clients try the header first, then path-suffixed, then root.
- Canonical 401:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource",
                         scope="files:read"
```

- Multiple AS entries: clients "**MUST** maintain separate registration state (client credentials, tokens) per authorization server". Claude only uses the first entry (section 3).

### 1.3 Authorization Server Metadata discovery order

Clients "**MUST** attempt multiple well-known endpoints". For issuer `https://auth.example.com/tenant1`: (1) `/.well-known/oauth-authorization-server/tenant1`, (2) `/.well-known/openid-configuration/tenant1`, (3) `/tenant1/.well-known/openid-configuration`. Without a path: `/.well-known/oauth-authorization-server` then `/.well-known/openid-configuration`. The returned `issuer` "**MUST** be identical to the issuer identifier used to construct the well-known URL"; otherwise "the client **MUST NOT** use the metadata."

### 1.4 Client registration

https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration

Priority for clients: (1) pre-registered credentials, (2) CIMD "if the Authorization Server indicates that it supports them (via `client_id_metadata_document_supported`)", (3) DCR "as a fallback if the Authorization Server supports it (via `registration_endpoint`)", (4) prompt the user.

**CIMD** (draft-ietf-oauth-client-id-metadata-document-00): the `client_id` is an HTTPS URL with a path that serves a JSON document. Client rules: document "**MUST** include at least ... `client_id`, `client_name`, `redirect_uris`"; `client_id` in the document must equal the URL. Authorization server rules: "**SHOULD** fetch metadata documents when encountering URL-formatted client_ids", "**MUST** validate that the fetched document's `client_id` matches the URL exactly", "**SHOULD** cache metadata respecting HTTP cache headers", "**MUST** validate redirect URIs presented in an authorization request against those in the metadata document". Advertise with:

```json
{ "client_id_metadata_document_supported": true }
```

Clients "**MAY** use `private_key_jwt`" (ChatGPT does). Security: SSRF when fetching, "Localhost Redirect URI Risks" (AS "**MUST** clearly display the redirect URI hostname during authorization", "**SHOULD** display additional warnings for `localhost`-only redirect URIs"), and optional domain trust policies.

**Pre-registration:** clients "**SHOULD** support an option for static client credentials", including "a UI to users that allows them to enter these details".

**DCR:** "**Deprecated**. New implementations should use Client ID Metadata Documents". Still: "MCP clients **MUST** specify an appropriate `application_type` during Dynamic Client Registration" (`native` for CLIs/desktop with loopback redirects; SEP-837). Credentials are issuer-bound: clients "**MUST** associate those credentials with the specific authorization server that issued them" and "**MUST** re-register" if the AS changes (SEP-2352). CIMD ids are portable across authorization servers.

Example CIMD document from the spec:

```json
{
  "client_id": "https://app.example.com/oauth/client-metadata.json",
  "client_name": "Example MCP Client",
  "client_uri": "https://app.example.com",
  "logo_uri": "https://app.example.com/logo.png",
  "redirect_uris": ["http://127.0.0.1:3000/callback", "http://localhost:3000/callback"],
  "grant_types": ["authorization_code"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

### 1.5 Resource Indicators (RFC 8707) and canonical URI

- "The `resource` parameter ... **MUST** be included in both authorization requests and token requests", "**MUST** identify the MCP server", "**MUST** use the canonical URI of the MCP server". "MCP clients **MUST** send this parameter regardless of whether authorization servers support it."
- Valid canonical examples: `https://mcp.example.com/mcp`, `https://mcp.example.com`, `https://mcp.example.com:8443`, `https://mcp.example.com/server/mcp`. Invalid: missing scheme, fragment. "implementations **SHOULD** consistently use the form without the trailing slash".
- Server side: "MCP servers **MUST** validate that access tokens were issued specifically for them as the intended audience", "**MUST** only accept tokens that are valid for use with their own resources", "**MUST NOT** accept or transit any other tokens." "Invalid or expired tokens **MUST** receive a HTTP 401 response."

### 1.6 Scopes and step-up

- Servers "**SHOULD** include a `scope` parameter in the `WWW-Authenticate` header". Client fallback: use `scope` from the 401, else all of `scopes_supported` from PRM, else omit scope. "The `scopes_supported` field is intended to represent the minimal set of scopes necessary for basic functionality".
- Insufficient scope at runtime: "**SHOULD** respond with `HTTP 403 Forbidden`" plus `WWW-Authenticate: Bearer error="insufficient_scope", scope="...", resource_metadata="..."`. New in 2026-07-28: "servers **SHOULD** include all scopes required for the current operation in a single challenge"; scope accumulation is the client's job (union of previously requested and challenged scopes); "Servers **MUST** account for scope hierarchies".
- Error table: 401 "Authorization required or token invalid", 403 "Invalid scopes or insufficient permissions", 400 "Malformed authorization request".

Example step-up response from the spec:

```http
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope",
                         scope="files:write",
                         resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource",
                         error_description="File write permission required for this operation"
```

### 1.7 Token usage and the passthrough ban

- "MCP client **MUST** use the Authorization request header field" (`Authorization: Bearer <access-token>`), on every request; "Access tokens **MUST NOT** be included in the URI query string".
- "MCP clients **MUST NOT** send tokens to the MCP server other than ones issued by the MCP server's authorization server." Security considerations: "If the MCP server makes requests to upstream APIs, it may act as an OAuth client to them. The access token used at the upstream API is a separate token ... The MCP server **MUST NOT** pass through the token it received from the MCP client."

### 1.8 Issuer identification (new, SEP-2468)

Authorization servers "**SHOULD** include the `iss` parameter in authorization responses"; those that do "**MUST** advertise this by setting `authorization_response_iss_parameter_supported` to `true`". Clients record the issuer before redirecting and "**MUST** apply the validation in RFC9207 Section 2.4 before transmitting the authorization code to any token endpoint" (reject if advertised-but-absent; compare without normalization). A future revision "is expected to upgrade ... from **SHOULD** to **MUST**". Gemini CLI and Codex already enforce this (section 3).

### 1.9 Security Best Practices page (2026-07-28)

https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices

- **Confused deputy:** applies exactly to an MCP proxy with a static upstream client id plus DCR/CIMD clients. "MCP proxy servers **MUST** implement per-client consent". The consent page "**MUST**: Clearly identify the requesting MCP client by name; Display the specific third-party API scopes being requested; Show the registered `redirect_uri`; Implement CSRF protection; Prevent iframing". Redirect URI check "**MUST** ... Use exact string matching (not pattern matching or wildcards)". `state` must be stored server-side "**only after** consent has been explicitly approved", single-use, short expiry (e.g. 10 minutes). Consent cookies: `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Lax`, signed, bound to `client_id`.
- **Token passthrough:** "MCP servers **MUST NOT** accept any tokens that were not explicitly issued for the MCP server."
- **SSRF:** applies to clients fetching discovery URLs and to authorization servers fetching CIMD documents; block private ranges, enforce HTTPS, consider egress proxies.
- **State handle hijacking** (replaces the old "session hijacking" section): "MCP servers **MUST NOT** treat possession of a state handle as authentication"; "**SHOULD** bind handles server-side to the authenticated user, for example by keying stored state as `<user_id>:<handle>`". The 2025-11-25 session-hijacking guidance still applies to legacy sessions.
- **Mix-up attacks** (mitigated by `iss`), **localhost redirect URI impersonation** with CIMD, **CIMD trust policies**, **scope minimization** (avoid omnibus scopes, publish a minimal `scopes_supported`, step up incrementally), **OAuth authorization URL validation** (clients must reject `javascript:` etc.).
- **DNS rebinding / Origin:** in the transport page: "Servers **MUST** validate the `Origin` header on all incoming connections"; if present and invalid, "**MUST** respond with HTTP 403 Forbidden"; local servers "**SHOULD** bind only to localhost".

### 1.10 Authorization extensions

Two official extensions live in https://github.com/modelcontextprotocol/ext-auth and https://modelcontextprotocol.io/extensions/auth/overview:

- **Enterprise-Managed Authorization** (stable; SEP-990 is Final; identifier `io.modelcontextprotocol/enterprise-managed-authorization`; spec https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization). Flow: user signs in to the MCP client via the enterprise IdP; the client does an RFC 8693 token exchange at the IdP (`requested_token_type=urn:ietf:params:oauth:token-type:id-jag`, `audience=<your AS issuer>`, `resource=<your MCP server>`); the IdP applies admin policy and mints an ID-JAG; the client presents it to *your* authorization server with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`; your AS validates signature/`iss`/`aud`/`exp`/`jti`, checks `resource`, and issues an access token that "**MUST** be audience-restricted to the MCP Server". Your AS advertises `authorization_grant_profiles_supported: ["urn:ietf:params:oauth:grant-profile:id-jag"]`; Claude additionally reads `grant_types_supported` for `urn:ietf:params:oauth:grant-type:jwt-bearer` and refuses DCR clients for this flow ("The identity provider stamps a fixed `client_id` into every assertion"). Claude Team/Enterprise only. https://claude.com/docs/connectors/building/enterprise-managed-auth
- **OAuth Client Credentials** (draft; M2M). ChatGPT "does **not** support machine-to-machine OAuth grants"; Claude: "A pure machine-to-machine `client_credentials` grant ... is **not supported**."
- Per the official client matrix, only Archestra.AI lists Enterprise Auth support; VS Code's EMA support is claimed in GitHub's changelog (2026-07-23) but not in the matrix. https://modelcontextprotocol.io/extensions/client-matrix

Sources for the SEPs: SEP-990 "Enable enterprise IdP policy controls during MCP OAuth flows" (Final) and SEP-991 "Enable URL-based Client Registration using OAuth Client ID Metadata Documents" (Final), both under https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/seps ; background post https://aaronparecki.com/2025/11/25/1/mcp-authorization-spec-update (2025-11-25).

---

## 2. Streamable HTTP for a stateless, multi-instance deployment

### 2.1 The 2026-07-28 shape

https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http

- Single endpoint, POST only. "The client sends every JSON-RPC request or notification as its own HTTP POST." Server answers each request with "either a single JSON object or a Server-Sent Events (SSE) stream scoped to that request". "The client **MUST** support both."
- Required headers on every POST: `MCP-Protocol-Version` (must equal `_meta["io.modelcontextprotocol/protocolVersion"]`, else `400` + `HeaderMismatch` `-32020`), `Mcp-Method`, and `Mcp-Name` for `tools/call`/`resources/read`/`prompts/get`. Servers "**MUST** reject requests where the values specified in the headers do not match the corresponding values in the request body." Optional `x-mcp-header` mirrors tool parameters into `Mcp-Param-*` headers (useful for routing on a business id at a gateway; never for secrets).
- Unsupported version: `400` + `UnsupportedProtocolVersionError` (`-32022`) listing `supported`. Unknown method: `404` + `-32601`. Requests missing the `MCP-Protocol-Version` header "**MAY**" be treated as 2025-03-26 by servers that still support old clients.
- No sessions, no GET stream, no `DELETE`, no resumability: "Resumable SSE streams via `Last-Event-ID` are not supported." Change notifications use a long-lived `subscriptions/listen` POST response stream. Closing a response stream is the cancellation signal. Servers should send `X-Accel-Buffering: no` and periodic SSE comment keep-alives on long streams.
- Server-to-client requests are gone: elicitation/sampling/roots are returned inline as `InputRequiredResult` (`resultType: "input_required"`, `inputRequests`, opaque `requestState`); the client retries the same call with `inputResponses` and a new `id`. `requestState` "**MUST** be treated as attacker-controlled"; if it influences authorization or business logic servers "**MUST** protect its integrity (e.g. HMAC or AEAD)", and should bind it to principal, TTL, and originating request. https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
- Statelessness: "Servers **MUST NOT** rely on prior requests over the same connection to establish context"; "State that needs to span multiple requests ... **MUST** be referenced by an explicit identifier the client passes on each request." https://modelcontextprotocol.io/specification/2026-07-28/basic/index
- `tools/list` "**MUST NOT** vary per-connection" but "**MAY** vary by the authorization presented on the request". Servers "**SHOULD** return tools in a deterministic order". List results now require `ttlMs` and `cacheScope`. https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- `server/discover`: "Servers **MUST** implement it"; returns `supportedVersions`, `capabilities`, `instructions`, `serverInfo` in `_meta`. https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- Legacy fallback for old clients hitting a modern-only server: GET/DELETE → `405`; ignore `Mcp-Session-Id`; ignore `Last-Event-ID`. Dual-era servers select behavior by how the client opens (an `initialize` request selects legacy semantics). https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning

### 2.2 The legacy (2025-11-25 and earlier) shape you must still serve

https://modelcontextprotocol.io/specification/2025-11-25/basic/transports

- Endpoint "**MUST** ... support both POST and GET". Server "**MAY** assign a session ID at initialization time" via `Mcp-Session-Id`; clients then "**MUST** include it ... on all of their subsequent HTTP requests"; missing → `400`; expired → `404`, and the client "**MUST** start a new session by sending a new `InitializeRequest`". `DELETE` terminates (server may answer `405`). GET opens an optional standalone SSE stream (server may answer `405`). Resumability via SSE event ids + `Last-Event-ID` on GET. `MCP-Protocol-Version` header on subsequent requests; absent → assume 2025-03-26.

### 2.3 What breaks behind Cloud Run's load balancer

- **Legacy sessions are in-process.** Python SDK: "That record is a **plain in-process `dict`**. There is no distributed session store and no way to plug one in." A request carrying `Mcp-Session-Id` that lands on another instance gets `404 Session not found`. https://py.sdk.modelcontextprotocol.io/run/legacy-clients/index.md
- **Fix A: `stateless_http=True`** (legacy-only knob). Each legacy request gets a throwaway session; cost: "no stream for the server to push a request down and no standalone stream for it to push notifications down", so `ctx.elicit()`/`Resolve` on legacy connections raise `NoBackChannelError` and notifications are dropped. Modern (2026-07-28) clients are unaffected either way.
- **Fix B: session affinity.** Cloud Run's is cookie-based and best-effort: "you cannot assume that a client will always reconnect to the same instance, even when session affinity is enabled." MCP clients are not browsers and there is no evidence they honor the affinity cookie. https://docs.cloud.google.com/run/docs/configuring/session-affinity
- **MRTR `requestState` across instances:** the SDK seals it with a per-process key by default; you must configure `RequestStateSecurity(keys=[...])` with the same key *and the same server name* on all instances, or every cross-instance retry is refused with `-32602 "Invalid or expired requestState"`. https://py.sdk.modelcontextprotocol.io/run/deploy/index.md
- **Change notifications across replicas** need a `SubscriptionBus` you implement (Redis/NATS); "The SDK ships no bus that can help you."
- **Long SSE responses** are ordinary requests to Cloud Run and are cut at the request timeout (default 300 s, max 3600 s). https://docs.cloud.google.com/run/docs/configuring/request-timeout
- **Redirects and host allowlists:** the SDK's default `TransportSecuritySettings` rejects non-localhost hosts with `421`; set `allowed_hosts`/`allowed_origins`. Run uvicorn with `--proxy-headers --forwarded-allow-ips` behind TLS termination or "redirects point to `http://` and modern clients refuse the downgrade."

---

## 3. Client behavior in 2026

### 3.1 Compatibility matrix

| Client | HTTP + OAuth | DCR | CIMD | Pre-registered client id/secret | Redirect URI(s) to allowlist | Sends `resource` | Elicitation |
|---|---|---|---|---|---|---|---|
| Claude Code CLI (local 2.1.259) | Yes | Yes (default when no clientId) | Yes; `https://claude.ai/oauth/claude-code-client-metadata` | Yes (`--client-id`, `--client-secret`, `oauth.clientId`) | `http://localhost:<port>/callback` and `http://127.0.0.1:<port>/callback`, ephemeral port (fix with `--callback-port`) | Yes, set to the server URL (issue #76096, v2.1.174, 2026-07) | Yes (form + URL) |
| claude.ai web / Desktop / mobile / Cowork | Yes (from Anthropic's cloud, IPv4, egress `160.79.104.0/21`) | Yes | Yes, only if AS advertises `client_id_metadata_document_supported: true` **and** `"none"` in `token_endpoint_auth_methods_supported` | Yes: "Advanced settings" Client ID + optional Client Secret; also `oauth_anthropic_creds` for directory listings | `https://claude.ai/api/mcp/auth_callback` | Yes, canonical server URL incl. path | No (open issue anthropics/claude-ai-mcp#153, 2026-04) |
| Codex CLI (local 0.153.3) | Yes | Yes | Yes (`--oauth-client-registration auto|cimd|dcr`) | Client id yes (`--oauth-client-id`, `oauth.client_id`); secret **UNVERIFIED** | loopback `http://127.0.0.1:<port>/callback` (Codex prints `http://127.0.0.1/callback`; port random unless `mcp_oauth_callback_port`/`oauth.callback_port`; `mcp_oauth_callback_url` for remote devboxes) | Yes on login (PR #12866); refresh omitted it (issue #33403 open 2026-07); `--oauth-resource` / `oauth_resource` override | Yes (form; MRTR since 2026-07-28) |
| Cursor | Yes | Yes | **No** (forum requests through 2026-08-28, no staff commitment) | Yes (`auth.CLIENT_ID`, `CLIENT_SECRET`, `scopes`) | Desktop `http://localhost:8787/callback` (since 3.10.17, 2026-07-05); web/cloud agents `https://www.cursor.com/agents/mcp/oauth/callback`; legacy `cursor://anysphere.cursor-mcp/oauth/callback` | **UNVERIFIED** | Yes (docs list it) |
| ChatGPT (developer mode / plugins) | Yes (Streamable HTTP at `/mcp`) | Yes ("runs DCR once per MCP server connection") | Yes, preferred; `https://chatgpt.com/oauth/client.json`, auth `none` or `private_key_jwt` (JWKS `https://chatgpt.com/oauth/jwks.json`) | Third-party reports an "Advanced OAuth settings → User-Defined OAuth Client" field; **not in official docs** | `https://chatgpt.com/connector_platform_oauth_redirect` (when AS supports `iss`), else `https://chatgpt.com/connector/oauth/{callback_id}` | Yes, on both requests | **UNVERIFIED** (not documented) |
| VS Code (Copilot) | Yes | Yes (tries DCR first) | Yes; `https://vscode.dev/oauth/client-metadata.json` | Yes (`oauth.clientId` in mcp.json; prompts for id/secret when DCR fails) | `http://127.0.0.1:33418/` and `https://vscode.dev/redirect` | **UNVERIFIED** (supports the 2025-06-18 auth spec, which requires it) | Yes |
| Gemini CLI → Antigravity CLI (replaced 2026-06-18) | Yes | Yes | **UNVERIFIED** (not documented) | Yes (`oauth.clientId`/`clientSecret`) | Gemini: `http://localhost:<random-port>/oauth/callback`; Antigravity: `https://antigravity.google/oauth-callback` (copy/paste code) | **UNVERIFIED** | **UNVERIFIED** |

Live CIMD documents fetched 2026-09-08:

```json
// https://claude.ai/oauth/claude-code-client-metadata
{"client_id":"https://claude.ai/oauth/claude-code-client-metadata","client_name":"Claude Code","client_uri":"https://claude.ai",
 "redirect_uris":["http://localhost/callback","http://127.0.0.1/callback"],
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}

// https://chatgpt.com/oauth/client.json
{"client_id":"https://chatgpt.com/oauth/client.json","client_uri":"https://chatgpt.com/",
 "redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"],
 "token_endpoint_auth_method":"private_key_jwt","token_endpoint_auth_methods_supported":["none","private_key_jwt"],
 "grant_types":["authorization_code","refresh_token"],"response_types":["code"],"client_name":"ChatGPT",
 "logo_uri":"https://persistent.oaistatic.com/sonic/misc/openai-logo.png","token_endpoint_auth_signing_alg":"RS256",
 "jwks_uri":"https://chatgpt.com/oauth/jwks.json"}

// https://vscode.dev/oauth/client-metadata.json
{"client_name":"Visual Studio Code","logo_uri":"https://code.visualstudio.com/assets/branding/code-stable.png",
 "grant_types":["authorization_code","refresh_token","urn:ietf:params:oauth:grant-type:device_code"],
 "response_types":["code"],"token_endpoint_auth_method":"none","application_type":"native",
 "client_id":"https://vscode.dev/oauth/client-metadata.json","client_uri":"https://vscode.dev/product",
 "redirect_uris":["http://127.0.0.1:33418/","https://vscode.dev/redirect"]}
```

### 3.2 Per-client detail

**Claude Code** (https://code.claude.com/docs/en/mcp). Local 2.1.259 matches the docs; the fixes noted below (2.1.186 login command, 2.1.231 loopback host fix) are all older than the local build.

```bash
claude mcp add --transport http luca https://luca.example.com/mcp
# pre-registered client (secret prompted, never written to config; MCP_CLIENT_SECRET for CI)
claude mcp add --transport http --client-id your-client-id --client-secret --callback-port 8080 \
  luca https://luca.example.com/mcp
claude mcp login luca      # v2.1.186+, or run /mcp inside a session
```

```json
{ "mcpServers": { "luca": { "type": "http", "url": "https://luca.example.com/mcp",
    "oauth": { "clientId": "your-client-id", "callbackPort": 8080, "scopes": "ledger:read ledger:write" } } } }
```

Notes: discovers via `/.well-known/oauth-protected-resource` or `/.well-known/oauth-authorization-server`; `oauth.authServerMetadataUrl` overrides discovery; `offline_access` is appended automatically; tokens in macOS keychain or credentials file; `MAX_MCP_OUTPUT_TOKENS` (default 25,000), per-tool `_meta["anthropic/maxResultSizeChars"]`; `MCP_TIMEOUT` startup 30 s; `MCP_TOOL_TIMEOUT`; `headersHelper` for non-OAuth schemes; `--scope local|project|user`; project `.mcp.json` servers require a trust dialog. Gotchas: v2.1.229 briefly sent `127.0.0.1` instead of `localhost` (register both); v2.1.80 sent a portless redirect via CIMD (issue #37747, 2026-03), so the AS must compare loopback redirect URIs with the port ignored (RFC 8252 §7.3; Anthropic asks for the same treatment of `localhost`). Claude Code enforces that PRM `resource` equals the server URL (issue #76096), which breaks Entra-style `api://` audiences. Claude Code CIMD identifies as a public client, so the AS must accept `token_endpoint_auth_method: none`.

**claude.ai / Desktop custom connectors** (https://claude.com/docs/connectors/building/authentication, https://claude.com/docs/connectors/building/troubleshooting, https://claude.com/docs/connectors/building/lazy-authentication, https://claude.com/docs/connectors/building, https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)

- Add via Customize > Connectors > Add custom connector (Free plan: one connector); Team/Enterprise: only Owners add, users connect individually. Supported auth types: `oauth_dcr`, `oauth_cimd`, `oauth_anthropic_creds` (email `mcp-review@anthropic.com`), `static_headers` (beta, admin-entered API key header), `none`. Auth specs supported: 2025-03-26, 2025-06-18, 2025-11-25. Transports: Streamable HTTP and legacy HTTP+SSE (being deprecated).
- Must-haves: "The `401` status is required — Claude does not honor a `WWW-Authenticate` header on a `200` response"; a `200` with `isError: true` never triggers auth; a `403` triggers re-auth only with `error="insufficient_scope"`. PRM `resource` "must match your MCP server URL exactly as the user enters it in Claude, including any path component". Only the first `authorization_servers` entry is used. AS host must serve RFC 8414 or OIDC discovery and be reachable from Anthropic's egress. Token endpoint must accept `application/x-www-form-urlencoded`; return `invalid_grant` for dead refresh tokens; rotate refresh tokens. Discovery/registration/token endpoints must answer within 10 s (refresh 30 s). Redirects to another host drop `Authorization`. Private/CGNAT/IPv6-only hosts are rejected before any request. Discovery docs are cached globally for about 5 minutes; a `403` scope challenge is cached per user for up to 15 minutes; Claude unions the 403 scopes with the discovery-time scopes but "Scopes the user picked up in an earlier step-up aren't reliably carried forward". Limits: ~150,000 chars per tool result, 300 s timeout. "Prefer **CIMD or `oauth_anthropic_creds` over DCR**" ("DCR causes Claude to register a new client on every fresh connection"). Microsoft Entra needs the MCP server URL registered as an Application ID URI (`AADSTS9010010`).
- Protocol features: tools, prompts, resources (text and binary); not resource subscriptions or sampling. Elicitation is not listed and is an open issue (anthropics/claude-ai-mcp#153, 2026-04-06; anthropics/claude-code#41110 closed as not Claude Code). The hosted-surface CIMD `client_id` URL is not named in the docs.

**Codex CLI** (https://learn.chatgpt.com/docs/extend/mcp?surface=cli, https://learn.chatgpt.com/docs/config-file/config-reference). Reconciled with local Codex 0.153.3, whose `codex mcp add` exposes `--oauth-client-id`, `--oauth-client-registration auto|cimd|dcr` and `--oauth-resource`; the fetched docs show the registration flag on `codex mcp login` instead, so treat the local `--help` output as authoritative for 0.153.3.

```bash
codex mcp add luca --url https://luca.example.com/mcp
codex mcp add luca --url https://luca.example.com/mcp \
  --oauth-client-id my-client --oauth-client-registration cimd --oauth-resource https://luca.example.com/mcp
codex mcp login luca --scopes ledger:read,ledger:write     # prints "OAuth callback URL: http://127.0.0.1/callback"
```

```toml
[mcp_servers.luca]
url = "https://luca.example.com/mcp"
oauth_resource = "https://luca.example.com/mcp"   # "Optional RFC 8707 OAuth resource parameter to include during MCP login"
scopes = ["ledger:read"]
bearer_token_env_var = "LUCA_TOKEN"               # alternative to OAuth
http_headers = { "X-Tenant" = "acme" }
env_http_headers = { "X-Api-Key" = "LUCA_API_KEY" }
startup_timeout_sec = 10                          # default 10
tool_timeout_sec = 60                             # default 60
enabled_tools = ["get_balance_sheet"]
[mcp_servers.luca.oauth]
client_id = "my-client"
callback_port = 5555
# global keys: mcp_oauth_callback_port, mcp_oauth_callback_url, mcp_oauth_credentials_store = "auto" | "file" | "keyring"
```

Implementation is the Rust `rmcp` crate (registration strategies AUTO/CIMD/DCR; keyring, or `~/.codex/.credentials.json` in file mode; PKCE mandatory; `iss` validated). `experimental_use_rmcp_client` is deprecated in favor of `[features].rmcp_client` (openai/codex#6995). Codex "prefers ... server-advertised scopes". Helper headers are refreshed once after a same-origin 401/403. `auth = "chatgpt"` is for trusted first-party servers only. Sources: https://deepwiki.com/openai/codex/6.5-oauth-authentication-for-mcp , https://codex.danielvaughan.com/2026/05/06/codex-cli-mcp-oauth-authenticating-remote-tool-servers/ (2026-05-06, updated 2026-09-08), https://github.com/openai/codex/issues/33403 , https://github.com/openai/codex/pull/35725 (2026 client support merged 2026-07-28), https://github.com/openai/codex/issues/6995. Codex's own CIMD document URL was not found. The loopback hostname differs between the official doc (`127.0.0.1`) and the third-party post (`localhost`); register a port-agnostic loopback rule covering both.

**Cursor** (https://cursor.com/docs/context/mcp)

```json
{ "mcpServers": { "luca": { "url": "https://luca.example.com/mcp",
    "auth": { "CLIENT_ID": "your-oauth-client-id", "CLIENT_SECRET": "optional", "scopes": ["ledger:read"] } } } }
```

Without `auth`, Cursor does DCR and reads `scopes_supported` from `/.well-known/oauth-authorization-server`. Docs: "Web and Cursor Agents: https://www.cursor.com/agents/mcp/oauth/callback; Desktop app: http://localhost:8787/callback". Forum guidance (Cursor staff Mohit Jain, 2026-07-08): loopback replaced `cursor://` because "a lot of OAuth providers won't accept a custom-scheme URI"; allowlist all three URIs for DCR, only `http://localhost:8787/callback` for static clients; port 8787 can collide with wrangler/Dask. https://forum.cursor.com/t/oauth-redirect-uri-changed-from-cursor-to-http-localhost-for-streamable-http-mcp/165019 . CIMD: feature request open since 2026-01-06 with +1s through 2026-08-28 and no staff commitment (https://forum.cursor.com/t/mcp-oauth-cimd-support-plans-and-timelines/148096?page=2). Tool limit: the "40 tools" cap is community-reported and users say it no longer warns since Dynamic Context Discovery; **not confirmed by Cursor docs** (https://forum.cursor.com/t/regarding-the-quantity-limit-of-mcp-tools/153432). Remote servers do not support `envFile`; use `${env:VAR}` interpolation.

**ChatGPT** (https://developers.openai.com/plugins/build/auth, https://developers.openai.com/apps-sdk/deploy/connect-chatgpt, https://developers.openai.com/api/docs/mcp)

- Enable via Settings → Security and login → Developer mode ("availability can depend on account and workspace policy"); add the server URL "including the `/mcp` path"; public HTTPS or Secure MCP Tunnel. Some older API docs still say `/sse/` for deep-research connectors; treat as stale. Write actions require confirmation by default.
- Requirements: PRM at `/.well-known/oauth-protected-resource` (`resource`, `authorization_servers`, `scopes_supported`); AS metadata with `issuer`, `authorization_response_iss_parameter_supported: true`, `code_challenge_methods_supported` incl. `S256`, `client_id_metadata_document_supported: true` or `registration_endpoint`, `token_endpoint_auth_methods_supported`. "Expect ChatGPT to append `resource=...` to both the authorization and token requests" and copy it into `aud`. Issuer comparison is exact string ("do not normalize trailing slashes, paths, ports, or casing"). Per-tool `securitySchemes` (`noauth`, `oauth2` with scopes) plus tool errors carrying `_meta["mcp/www_authenticate"]` drive the tool-level login UI (mirrors SEP-1489, still a draft and not in the core spec). ChatGPT presents an mTLS client cert (SAN `mtls.prod.connectors.openai.com`, chain to OpenAI's intermediate CA; do not pin the leaf). Workspace domain restriction needs OIDC `openid email` and a UserInfo endpoint with `email_verified: true`. "Keep the registered OAuth client and any client secret valid while the connector is in use."
- Pre-registered credentials: third-party writeups (https://apxml.com/posts/how-to-setup-oauth-chatgpt-connector, 2025-10-21 updated 2026-05; Jama Software support article) describe "Advanced OAuth settings → User-Defined OAuth Client" with a displayed redirect URI; not in OpenAI's docs. Elicitation: not documented.

**VS Code** (https://code.visualstudio.com/api/extension-guides/ai/mcp, https://code.visualstudio.com/docs/agents/reference/mcp-configuration, https://code.visualstudio.com/docs/copilot/agents/agent-tools)

```json
{ "servers": { "luca": { "type": "http", "url": "https://luca.example.com/mcp",
    "oauth": { "clientId": "example-client-id" } } } }
```

"VS Code first starts with a Dynamic Client Registration (DCR) handshake and then falls back to a client-credentials workflow if the IdP does not support DCR" (prompts for Client ID and Secret); "The redirect URL list must include these URLs: `http://127.0.0.1:33418` and `https://vscode.dev/redirect`". Its live CIMD document lists `application_type: "native"` and grant types incl. device_code; users report VS Code uses CIMD when advertised (microsoft/vscode#282472 duplicate of #279955). `oauth.enterpriseManaged` (preview) routes through enterprise SSO. Hard limit: "A chat request can have a maximum of 128 tools enabled at a time" (virtual tools threshold caps at 128). Supports tools, prompts, resources, elicitation, sampling, roots, MCP Apps, icons. Sensitive values via `${input:...}` variables. GitHub's changelog (2026-07-23) says MCP Apps and Enterprise Managed Auth are "already supported by VS Code" (https://github.blog/changelog/2026-07-23-github-mcp-server-supports-the-next-mcp-specification/).

**Gemini CLI / Antigravity** (https://geminicli.com/docs/tools/mcp-server/, https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/tools/mcp-server.md, https://antigravity.google/docs/cli/mcp/, https://antigravity.google/docs/mcp/)

Gemini CLI: `gemini mcp add --transport http luca https://luca.example.com/mcp` (`-H` for headers); `settings.json` `httpUrl` + `oauth {enabled, clientId, clientSecret, authorizationUrl, tokenUrl, scopes, redirectUri, tokenParamName, audiences}`; auto-discovery and DCR ("Perform dynamic client registration if supported"); `/mcp auth <name>`; tokens in `~/.gemini/mcp-oauth-tokens.json`; enforces RFC 9207 `iss` ("Responses missing `iss` ... are rejected with HTTP 400"); needs a local browser. The docs banner says "Gemini CLI was replaced by Antigravity CLI on June 18th, 2026." Antigravity: `~/.gemini/config/mcp_config.json` (workspace `.agents/mcp_config.json`) with `serverUrl` (not `url`/`httpUrl`), `headers`, `oauth {clientId, clientSecret}`; DCR automatic; callback `https://antigravity.google/oauth-callback` with a copy/paste authorization code; tokens in `~/.gemini/antigravity/mcp_oauth_tokens.json`; `/mcp` panel, no `mcp add` subcommand documented. Antigravity CLI v1.0.0 had a bug sending `initialize` without the bearer token (https://github.com/google-antigravity/antigravity-cli/issues/25).

---

## 4. Python server implementation options

### 4.1 Official `mcp` SDK v2 (2.2.0)

Docs: https://py.sdk.modelcontextprotocol.io/ . `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer, Context`; `mcp.server.fastmcp.*` raises `ModuleNotFoundError`); transport options moved from the constructor to `run()`/`streamable_http_app()`; `mount_path` removed (use ASGI `root_path`); `get_context()` removed (inject `ctx: Context`); `ctx.client_id` removed; sync handlers run on a worker thread; `lifespan` enters once per process. https://py.sdk.modelcontextprotocol.io/migration/index.md , https://py.sdk.modelcontextprotocol.io/whats-new/index.md

**Resource-server auth** (verbatim from https://py.sdk.modelcontextprotocol.io/run/authorization/index.md):

```python
from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

RESOURCE = "http://127.0.0.1:8000/mcp"

KNOWN_TOKENS = {
    "alice-token": AccessToken(token="alice-token",
    client_id="alice", scopes=["notes:read"], resource=RESOURCE),
}

class StaticTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        return KNOWN_TOKENS.get(token)

mcp = MCPServer(
    "Notes",
    token_verifier=StaticTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://auth.example.com"),
        resource_server_url=AnyHttpUrl(RESOURCE),
        required_scopes=["notes:read"],
        validate_token_resource=True,
    ),
)

@mcp.tool()
def whoami() -> str:
    """Report which OAuth client is calling."""
    token = get_access_token()
    if token is None:
        return "anonymous"
    return f"{token.client_id} (scopes: {', '.join(token.scopes)})"
```

- `AccessToken` fields (from https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/src/mcp/server/auth/provider.py): `token: str`, `client_id: str`, `scopes: list[str]`, `expires_at: int | None`, `resource: str | None`, `subject: str | None`, `claims: dict[str, Any] | None`. `TokenVerifier` is a Protocol with one method, `verify_token`.
- The SDK serves PRM at `/.well-known/oauth-protected-resource/mcp` (`resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported`) and answers unauthenticated requests with `401` + `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="..."`. "`token_verifier=` and `auth=` always travel together." `validate_token_resource` unset "warns and behaves as `False`; 3.0 makes `True` the default." Production shape: `examples/servers/simple-auth/IntrospectionTokenVerifier` (RFC 7662; imports `AccessToken, TokenVerifier` from `mcp.server.auth.provider`, validates `aud` against the resource URL, fills `subject` from `sub` and `claims` from the introspection response). Authorization is HTTP-only: "None of this protects `stdio`"; the in-memory test client bypasses it.
- **Embedded authorization server:** `OAuthAuthorizationServerProvider` still exists with `get_client`, `register_client`, `authorize`, `load_authorization_code`, `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token`, `exchange_identity_assertion`, wired via `create_auth_routes(provider, issuer_url=..., identity_assertion_enabled=True)`. The docs call `auth_server_provider=` legacy: "For new implementations using AS/RS separation, consider using the TokenVerifier protocol". Server-side CIMD for this embedded AS is an **open issue** (https://github.com/modelcontextprotocol/python-sdk/issues/1801, 2025-12-18). The SDK "does not give you a login page, a consent screen, or a token."
- **Streamable HTTP / mounting** (https://py.sdk.modelcontextprotocol.io/run/asgi/index.md, https://py.sdk.modelcontextprotocol.io/run/index.md): `mcp.run(transport="streamable-http", host=..., port=..., json_response=True, stateless_http=True, streamable_http_path="/mcp", session_idle_timeout=1800, max_sessions=10000, max_request_body_size=4 MiB)`. When mounting, "a mounted sub-application's lifespan never runs", so:

```python
import contextlib
from starlette.applications import Starlette
from starlette.routing import Mount

@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield

app = Starlette(
    routes=[Mount("/", app=mcp.streamable_http_app(json_response=True))],
    lifespan=lifespan,
)
```

Without it: `RuntimeError: Task group is not initialized. Make sure to use run()`. Multiple servers: enter each `session_manager.run()` via `AsyncExitStack`. CORS for browser clients must allow `Mcp-Method`, `Mcp-Name`, `Mcp-Protocol-Version`, `Mcp-Session-Id` and expose `Mcp-Session-Id`; set matching `TransportSecuritySettings(allowed_origins=[...])`.

- **Deploy** (https://py.sdk.modelcontextprotocol.io/run/deploy/index.md):

```python
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

security = TransportSecuritySettings(
    allowed_hosts=["mcp.example.com", "mcp.example.com:*"],
    allowed_origins=["https://app.example.com"],
)
app = mcp.streamable_http_app(transport_security=security)
```

```python
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
mcp = MCPServer("billing", request_state_security=RequestStateSecurity(keys=[key]))
```

Key rotation: `RequestStateSecurity(keys=[OLD, NEW])` → `keys=[NEW, OLD]` → `keys=[NEW]`; default TTL 600 s per round; state is bound to principal (from the verified token's `subject`), method, tool name and argument digest. Cross-replica notifications: `MCPServer("Notebook", subscriptions=bus)` with your own `SubscriptionBus` (`from mcp.server.subscriptions import SubscriptionBus`). No `EventStore` in v2 (resumability only existed for legacy stateful mode); no built-in health check (`@mcp.custom_route("/health", methods=["GET"])`); scale with `uvicorn server:app --workers 4 --proxy-headers --forwarded-allow-ips='<proxy>'`.

- **Legacy clients** (https://py.sdk.modelcontextprotocol.io/run/legacy-clients/index.md): one endpoint serves both eras, routed by the `MCP-Protocol-Version` header; legacy sessions get `Mcp-Session-Id`; `session_idle_timeout=1800` (client sees `404` and re-initializes), `max_sessions=10000` (`503` beyond); `stateless_http=True` removes stickiness at the cost of the back-channel; notify legacy and modern listeners separately (`ctx.session.send_resource_updated()` vs `ctx.notify_resource_updated()`).
- **Tools / output / media:** `@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=..., idempotent_hint=..., open_world_hint=False))`; return type annotation is the `outputSchema` (Pydantic/TypedDict/dataclass unwrapped; primitives, lists and unions wrapped as `{"result": ...}`; `structured_output=False` to disable; results are validated before sending); `Image(path=...)`/`Audio(...)` become `ImageContent`/`AudioContent`; binary documents via `EmbeddedResource(resource=BlobResourceContents(uri=..., mime_type=..., blob=...))` or `ResourceLink(name=..., uri=...)`; resources returning `bytes` become `BlobResourceContents`; `MCPError` becomes a JSON-RPC error, other exceptions become `isError` results. https://py.sdk.modelcontextprotocol.io/servers/tools/index.md , https://py.sdk.modelcontextprotocol.io/servers/structured-output/index.md , https://py.sdk.modelcontextprotocol.io/servers/media/index.md , https://py.sdk.modelcontextprotocol.io/servers/resources/index.md
- **Elicitation:** on 2026-07-28 connections use `Annotated[ElicitationResult[T], Resolve(fn)]` resolvers returning `Elicit(...)` (era-portable: MRTR on modern, `elicitation/create` on legacy); `ctx.elicit()`/`ctx.elicit_url()` remain for legacy back-channel connections; a tool may alternatively return `InputRequiredResult` itself, but "a tool that uses `Resolve(...)` parameters cannot also return `InputRequiredResult` from its body". Servers validate client capabilities up front and fail with `-32021` if `elicitation` is not declared. https://py.sdk.modelcontextprotocol.io/handlers/elicitation/index.md , https://py.sdk.modelcontextprotocol.io/handlers/dependencies/index.md , https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/index.md
- Recent hardening (v2.2.0, 2026-09-07): HTTP client redirects limited to same origin, legacy sessions idle-expire after 30 min, OAuth client validates `issuer`, `AuthSettings.validate_token_resource`; v2.0.0 (2026-07-28) added RFC 9207 and identity assertion. https://github.com/modelcontextprotocol/python-sdk/releases . Protocol versions supported: handshake era up to 2025-11-25 and 2026-07-28 (https://py.sdk.modelcontextprotocol.io/protocol-versions/index.md). Deprecated list: https://py.sdk.modelcontextprotocol.io/deprecated/index.md . Identity assertion client/server: https://py.sdk.modelcontextprotocol.io/client/identity-assertion/index.md

### 4.2 FastMCP 4.x (4.0.3)

Docs: https://gofastmcp.com/ . 4.0.0 (2026-08-31): built on `mcp` 2.0, negotiates the protocol era per connection, adds identity assertion (SEP-990), `require_roles`, scope step-up challenges (SEP-2350), client-credentials auth, `UserSession`/`SessionId`, `application_type` in DCR, routable headers (SEP-2243); removes server-initiated sampling/roots; `ctx.elicit()` is legacy-protocol-only; background tasks moved to `fastmcp-tasks`. https://gofastmcp.com/changelog.md , https://github.com/jlowin/fastmcp/releases

**Token verification only** (external AS that already supports DCR or CIMD): https://gofastmcp.com/servers/auth/token-verification.md

```python
from fastmcp.server.auth.providers.jwt import JWTVerifier
verifier = JWTVerifier(
    jwks_uri="https://auth.yourcompany.com/.well-known/jwks.json",
    issuer="https://auth.yourcompany.com",
    audience="mcp-production-api",
)
# HS256 with a shared secret: JWTVerifier(public_key="<32+ char secret>", issuer=..., audience=..., algorithm="HS256")
# Opaque tokens: IntrospectionTokenVerifier(introspection_url=..., client_id=..., client_secret=..., required_scopes=[...])
# Dev only: StaticTokenVerifier(tokens={"dev-alice-token": {"client_id": "alice@company.com", "scopes": ["read:data"]}})
```

**RemoteAuthProvider** (serves PRM; requires an IdP with DCR or CIMD): https://gofastmcp.com/servers/auth/remote-oauth.md

```python
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

token_verifier = JWTVerifier(
    jwks_uri="https://auth.yourcompany.com/.well-known/jwks.json",
    issuer="https://auth.yourcompany.com",
    audience="mcp-production-api"
)

auth = RemoteAuthProvider(
    token_verifier=token_verifier,
    authorization_servers=[AnyHttpUrl("https://auth.yourcompany.com")],
    base_url="https://api.yourcompany.com",
)

mcp = FastMCP(name="Company API", auth=auth)
```

`scopes_supported=[...]` overrides advertised scopes; override `get_routes()` to forward `/.well-known/oauth-authorization-server` from the IdP. Built-in RemoteAuthProvider subclasses: `AuthKitProvider` (WorkOS; enable DCR and CIMD in the dashboard and add the resource URL to the resource indicators list, https://gofastmcp.com/integrations/authkit.md), `Auth0MCPProvider` (Auth0 "Auth for MCP" handles DCR and CIMD, https://gofastmcp.com/integrations/auth0.md), `DescopeProvider` (enable DCR on the Descope MCP Server, https://gofastmcp.com/integrations/descope.md), `ScalekitProvider` (DCR, https://gofastmcp.com/integrations/scalekit.md).

**OAuthProxy** (bridges DCR/CIMD clients to an upstream that lacks DCR): https://gofastmcp.com/servers/auth/oauth-proxy.md

```python
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

token_verifier = JWTVerifier(
    jwks_uri="https://your-provider.com/.well-known/jwks.json",
    issuer="https://your-provider.com",
    audience="your-app-id"
)

auth = OAuthProxy(
    upstream_authorization_endpoint="https://provider.com/oauth/authorize",
    upstream_token_endpoint="https://provider.com/oauth/token",
    upstream_client_id="your-client-id",
    upstream_client_secret="your-client-secret",
    token_verifier=token_verifier,
    base_url="https://your-server.com",
)

mcp = FastMCP(name="My Server", auth=auth)
```

Multi-instance production settings (same page):

```python
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from cryptography.fernet import Fernet
import os

auth = OAuthProxy(
    ...,
    jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
    client_storage=FernetEncryptionWrapper(
        key_value=RedisStore(host="redis.example.com", port=6379),
        fernet=Fernet(os.environ["STORAGE_ENCRYPTION_KEY"])
    )
)
```

How it works: `/register` answers DCR clients with the fixed upstream credentials while recording the client's callback; `/authorize` shows a consent page (`require_authorization_consent=True` default = every flow, `"remember"` = once per browser per (client_id, redirect_uri), `"external"`, or `False` for dev) with a signed browser-session cookie, generates its own PKCE and redirects upstream using `redirect_path` (default `/auth/callback`, which must be registered at the upstream); on callback it exchanges the upstream code, stores the upstream token encrypted (Fernet), and issues its **own** HS256 JWT (token factory; JTI references the upstream token; `jwt_issuer` module) so upstream tokens never reach clients. Other params: `allowed_client_redirect_uris` (patterns such as `"http://localhost:*"`, `"https://claude.ai/api/mcp/auth_callback"`; loopback ports may vary), `forward_pkce=True`, `forward_resource=True`, `enable_cimd=True` (CIMD fetcher with HTTPS-only, DNS pinning, private-IP blocking, size/timeout limits; `private_key_jwt` verification with JTI replay cache), `valid_scopes`, `extra_authorize_params`, `extra_token_params`, `issuer_url`, `resource_base_url`, `token_endpoint_auth_method`, `upstream_revocation_endpoint`, `fastmcp_access_token_expiry_seconds` (3.4.0: token lifetime decoupled from upstream `expires_in`), `identity_assertion` (SEP-990 ID-JAG, https://gofastmcp.com/python-sdk/fastmcp-server-auth-identity_assertion.md). `OIDCProxy` is the same thing configured from an OIDC discovery URL (`config_url`, `client_id`, `client_secret`, `audience`, `required_scopes`; https://gofastmcp.com/servers/auth/oidc-proxy.md). Built-ins on top of it: Google (https://gofastmcp.com/integrations/google.md), GitHub, Azure/Entra, Auth0 (`Auth0Provider`), AWS Cognito, Keycloak, Discord, Supabase, PropelAuth, OCI, Hugging Face. `MultiAuth` (3.1.0+) composes an auth server with extra verifiers, for example OAuthProxy for interactive clients plus a JWTVerifier for service tokens (https://gofastmcp.com/servers/auth/multi-auth.md). All FastMCP auth applies only to HTTP transports.

**Full authorization server:** `OAuthProvider` abstract class (get_client, register_client, authorize, load_authorization_code, exchange_authorization_code, load_refresh_token, exchange_refresh_token, load_access_token, verify_token, revoke_token). The docs call it "an extremely advanced pattern that most users should avoid" and say "Use Remote OAuth instead unless you have compelling requirements". No production-grade persistent store or `InMemoryOAuthProvider` is documented. https://gofastmcp.com/servers/auth/full-oauth-server.md

**Inside tools:** `from fastmcp.server.dependencies import get_access_token` (returns `None` when unauthenticated) or `token: AccessToken = CurrentAccessToken()`; `token.claims`, `token.scopes`, `token.client_id`, `token.expires_at`; `get_http_request()`, `get_http_headers()`. Per-component checks: `@mcp.tool(auth=require_scopes("ledger:write"))`, `require_roles("admin", extract=fn)`, custom `AuthContext` callables (AND-combined); server-wide `AuthMiddleware(auth=require_scopes("api"))` raises `InsufficientScopeError` listing only the missing scopes. Component-level failures hide the tool from listing and return not-found. https://gofastmcp.com/servers/authorization.md , https://gofastmcp.com/servers/dependency-injection.md , https://gofastmcp.com/python-sdk/fastmcp-server-auth-auth.md

**Other FastMCP features:** `ToolResult(content=..., structured_content=..., meta=...)`; `output_schema=` override; `Image`/`Audio`/`File` helpers; `ToolError` and `mask_error_details=True`; `@mcp.tool(timeout=30.0)`; `mcp.enable(tags=...)`/`disable`; tool `version=`; `ToolAnnotations` accepted directly; MRTR "guard pattern" via `ctx.input_responses`/`ctx.request_state` plus `request_state_security=RequestStateSecurity(keys=[...])` for multi-instance; `UserSession` (keyed by authenticated user, needs auth) and `SessionId` (explicit handle via `SessionProvider()`), both isolated per user and backed by `session_state_store=` (Redis via the `key-value` library; wrap with `TTLClampWrapper(store, missing_ttl=3600)` for expiry). https://gofastmcp.com/servers/tools.md , https://gofastmcp.com/servers/elicitation.md , https://gofastmcp.com/servers/sessions.md , https://gofastmcp.com/servers/context.md

**HTTP deployment:** `mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp")` or `app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)`; when mounting into Starlette/FastAPI "you **must** pass the lifespan context from the FastMCP app"; `FASTMCP_STATELESS_HTTP=true uvicorn app:app --workers 4`; nginx needs `proxy_buffering off`. https://gofastmcp.com/deployment/http.md

### 4.3 Using the existing platform login as the upstream identity

Three viable shapes. Which one applies depends on what the platform actually exposes.

| Pattern | Requires from the platform | Who implements consent, client registry, refresh, revocation | Pros | Cons |
|---|---|---|---|---|
| **A. OAuth proxy (FastMCP `OAuthProxy`/`OIDCProxy`)** | An OAuth 2.0/OIDC authorization-code endpoint + token endpoint you can register **one** confidential client on (no DCR needed) | FastMCP: DCR + CIMD facade, consent screen, PKCE, own JWTs, refresh; you supply Redis + keys | Spec-blessed pattern (Security Best Practices "Normal OAuth proxy usage"); works with every client in the matrix; the accounting API sees only the upstream token it already understands | You operate an AS surface (authorize/token/register/consent) on Cloud Run; must run per-client consent; upstream token lifetime and refresh semantics leak into the design |
| **B. Hosted authorization server with DCR + CIMD** (Auth0 "Auth for MCP", WorkOS AuthKit, Descope, Scalekit, Stytch Connected Apps) federated to the platform login | Either federate the hosted AS to the existing login (OIDC/SAML connection or custom DB script) or migrate users | The vendor (DCR, CIMD, consent, RFC 8707 audience, refresh rotation, `iss`); you run `RemoteAuthProvider` + `JWTVerifier` | Least code and least attack surface on your side; vendors already track the MCP spec (CIMD, resource indicators) | Vendor cost and dependency; a second identity system unless you federate; Auth0 needs its "Resource Parameter Compatibility Profile" enabled to honor `resource`, and "If both the `resource` and `audience` are available, the `audience` will still be used" (https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile) |
| **C. Build the AS into the MCP server** (FastMCP `OAuthProvider` or SDK `OAuthAuthorizationServerProvider`) | Only the existing session/JWT login page to embed in `/authorize` | You: client store (DCR + CIMD fetch/validate/cache/SSRF), auth codes, PKCE, tokens, refresh rotation, revocation, consent, `iss`, `application_type` handling, step-up | Total control; single deployable | Largest security surface; both SDK vendors discourage it; python-sdk CIMD server support is an open issue; everything must persist in Redis/Firestore across instances |

Firebase Auth, custom JWT or cookie sessions do not expose an OAuth authorization endpoint by themselves, so they cannot be an `OAuthProxy` upstream directly; that leaves B (federate) or C (embed login in your own `/authorize`). **UNVERIFIED for Firebase specifically**; Firebase docs were not fetched. Stytch's CIMD support comes from its own blog (https://stytch.com/blog/stytch-supports-cimd/), not fetched in full.

---

## 5. Production architecture and pitfalls

**Cloud Run + Streamable HTTP**

- "Cloud Run supports hosting MCP servers with streamable HTTP transport, but not MCP servers with stdio transport"; services get HTTPS URLs "with built in support for HTTP response streaming". https://docs.cloud.google.com/run/docs/host-mcp-servers (updated 2026-09-01)
- Request timeout "is set by default to 5 minutes (300 seconds) and can be extended up to 60 minutes (3600 seconds)"; beyond 15 minutes Google recommends retry-tolerant clients (https://docs.cloud.google.com/run/docs/configuring/request-timeout). Streaming responses are still requests: "WebSockets streams are HTTP requests, which are still subject to the request timeout" (https://docs.cloud.google.com/run/docs/triggering/websockets). Concurrency default 80 per instance (https://docs.cloud.google.com/run/docs/configuring/concurrency). Min instances keep containers warm and are billed (https://docs.cloud.google.com/run/docs/configuring/min-instances). Session affinity is best-effort, cookie-based, 30-day TTL (https://docs.cloud.google.com/run/docs/configuring/session-affinity). For multi-instance state Google recommends external systems (Redis Pub/Sub, Firestore).
- Google's own tutorial deploys a FastMCP server with `transport="streamable-http"` and `--no-allow-unauthenticated` behind IAM (`gcloud run services proxy`) (https://docs.cloud.google.com/run/docs/tutorials/deploy-remote-mcp-server, updated 2026-09-01); that IAM model does not fit third-party MCP clients, so run `--allow-unauthenticated` at the Cloud Run layer and enforce OAuth in the app.
- Set uvicorn `--proxy-headers --forwarded-allow-ips` so generated URLs are `https`; set `TransportSecuritySettings.allowed_hosts` to your domain; keep `json_response=True` unless progress notifications are needed (JSON mode avoids long-held SSE responses being cut by the timeout).

**Tokens**

- Validate audience against the canonical resource (Claude Code and claude.ai both send the server URL including path; Claude's troubleshooting page asks servers to "accept the canonical value when checking `aud` rather than doing a strict byte-for-byte comparison"). Do not use a trailing slash in `resource`.
- Short-lived access tokens + rotated refresh tokens are required by the spec for public clients; return `invalid_grant` on refresh failure; token endpoint must accept form-encoding; answer within 10 s for Claude.
- Revocation: `OAuthProxy` supports `upstream_revocation_endpoint`; Claude Code `claude mcp logout` revokes; design JWTs with `jti` so logout can deny-list.

**Multi-tenant business scoping**

- Spec guidance: a state handle "is a name, not a capability"; validate the caller's authorization against it "on every call"; key stored state as `<user_id>:<handle>`; `tools/list` may vary by the token's scopes but not per connection.
- Options: (1) `business_id` tool argument checked against the token subject's memberships on every call (portable to every client; add a `list_businesses` tool; optionally mirror it with `x-mcp-header` for gateway routing); (2) business in a token claim via a scope such as `business:<id>` requested through step-up (`403 insufficient_scope`); Claude and Codex honor step-up, but only Claude documents scope-union behavior, so keep granted scopes in the challenge; (3) elicitation to pick a business: supported by Claude Code, Codex, Cursor, VS Code, not claude.ai/Desktop today, and form mode is limited to flat primitives/enums; (4) per-business server URL (`/b/{id}/mcp`): each URL is a separate canonical resource with its own PRM at `/.well-known/oauth-protected-resource/b/{id}/mcp` and its own audience-bound tokens; Claude requires PRM `resource` to equal the entered URL. Recommendation: (1) with server-side checks, plus a default-business claim if most users have one.

**Returning PDFs**

- Embedded blob resource (`EmbeddedResource` with `BlobResourceContents.blob` base64) travels inline and counts against Claude.ai's ~150,000-char and Claude Code's 25,000-token result caps; `resource_link` needs a client that will call `resources/read` (Claude supports binary resources; ChatGPT resource support **UNVERIFIED**; the spec says tool resource links "are not guaranteed to appear in the results of a `resources/list` request"); a short-lived signed download URL in `structuredContent` works in every client but the URL passes through model context and client logs, so keep TTLs short and audience-bind it. Do not use URL-mode elicitation for downloads: servers "**MUST NOT** provide a URL which is pre-authenticated to access a protected resource" (https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation).

**Tool annotations** (verbatim defaults from https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.ts): `readOnlyHint` "Default: false"; `destructiveHint` "Default: true" ("meaningful only when `readOnlyHint == false`"); `idempotentHint` "Default: false"; `openWorldHint` "Default: true". "Clients should never make tool use decisions based on `ToolAnnotations` received from untrusted servers." Annotate honestly (`readOnlyHint=True` on every reporting tool; `destructiveHint=False, idempotentHint=True` on safe writes) but never rely on them for safety.

**Structured content:** "If an output schema is provided: Servers **MUST** provide structured results that conform to this schema." Also return the JSON as text for backward compatibility (both SDKs do this automatically). `structuredContent` may be any JSON value in 2026-07-28.

**Pagination:** only `tools/list`, `resources/list`, `resources/templates/list`, `prompts/list` paginate at protocol level (opaque `cursor`, server-chosen page size, invalid cursor → `-32602`). Tool results paginate only via your own tool arguments (e.g. `cursor` in/out of `structuredContent`). https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination

**Tool count / context budgets:** VS Code 128 tools per request (hard); Cursor ~40 (community; unconfirmed in current docs); Claude Code 25,000-token output cap; Claude.ai ~150k chars per result and 300 s timeout; Claude API MCP connector supports `defer_loading` with the tool search tool (https://platform.claude.com/docs/en/agents-and-tools/mcp-connector). Keep the exposed tool set small and stable-ordered.

**MCP Registry (optional):** `server.json` with `"remotes": [{"type": "streamable-http", "url": "https://.../mcp"}]` (SSE is deprecated); "A remote server **MUST** be publicly accessible at its specified URL"; URL template variables (`{tenant_id}`) and `headers` are supported; namespace `com.example/...` requires DNS TXT `v=MCPv1; k=ed25519; p=<pubkey>` or `/.well-known/mcp-registry-auth`; publish with `mcp-publisher login dns --domain ... --private-key ...` then `mcp-publisher publish`. https://modelcontextprotocol.io/registry/remote-servers , https://modelcontextprotocol.io/registry/authentication , https://modelcontextprotocol.io/registry/quickstart

**Server versioning:** report `io.modelcontextprotocol/serverInfo` (`name`, `version`) in every result and `server/discover` (self-reported, not for security decisions); set `ttlMs`/`cacheScope` on list results; changing the URL path changes the canonical resource and invalidates all tokens, so version tools (FastMCP `version=`) rather than the endpoint.

---

## 6. What changed 2025–2026

| Revision | Highlights that affect this design | Source |
|---|---|---|
| 2025-03-26 | Streamable HTTP replaces HTTP+SSE; first OAuth 2.1 authorization spec | https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http (history note) |
| 2025-06-18 | MCP server as resource server; PRM (RFC 9728) mandatory; `resource` (RFC 8707) mandatory; `MCP-Protocol-Version` header; structured output; elicitation | referenced throughout the 2025-11-25 changelog |
| 2025-11-25 | CIMD added as recommended registration (SEP-991); OIDC discovery fallback; incremental scope consent via `WWW-Authenticate` (SEP-835); PRM well-known fallback aligned with RFC 9728 (SEP-985); URL-mode elicitation (SEP-1036); experimental tasks (SEP-1686); tool name rules; icons; JSON Schema 2020-12 default; Origin 403 clarification; Enterprise-Managed Authorization (SEP-990) as an extension | https://modelcontextprotocol.io/specification/2025-11-25/changelog , https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization |
| 2026-07-28 (current, published 2026-07-28) | Sessions and `Mcp-Session-Id` removed (SEP-2567); `initialize` removed, per-request `_meta` (SEP-2575); `server/discover`; `subscriptions/listen` replaces GET stream; MRTR (SEP-2322) with `resultType`; resumability removed; `Mcp-Method`/`Mcp-Name` headers (SEP-2243); `ttlMs`/`cacheScope` (SEP-2549); `iss` validation (SEP-2468); `application_type` in DCR (SEP-837); issuer-bound credentials (SEP-2352); **DCR deprecated** (PR #2858); Roots, Sampling, Logging deprecated (SEP-2577); HTTP+SSE reclassified Deprecated (SEP-2596); tasks moved to an extension (SEP-2663); error code range `-32020..-32099` reserved; 12-month deprecation policy | https://modelcontextprotocol.io/specification/2026-07-28/changelog , https://blog.modelcontextprotocol.io/posts/2026-07-28/ , https://www.theregister.com/devops/2026/07/23/model-context-protocol-prepares-to-break-with-its-stateful-past/5276722 |
| Rejected/draft SEPs worth knowing | SEP-1880 (tool-level scope requirements) closed as not planned; SEP-1489 (`_meta["mcp/www_authenticate"]` tool-error auth trigger) still draft but implemented by ChatGPT | https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1880 , https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1489 |

SDK timeline: `mcp` v2.0.0 on 2026-07-28 (dual-era), v2.1.0 2026-08-24, v2.2.0 2026-09-07, v1.30.0 maintenance backport 2026-09-07; FastMCP 4.0.0 2026-08-31, 4.0.3 2026-09-05. Client timeline: Claude "rolling out soon" (2026-07-28, https://claude.com/blog/bringing-mcp-2026-07-28-to-claude); Codex 2026 client support merged 2026-07-28; GitHub MCP Server ahead of release (2026-07-23).

---

## 7. Decision guidance

1. **Architecture:** MCP server = pure resource server (`JWTVerifier`/`TokenVerifier`, PRM, 401 with `resource_metadata` and `scope`). Put the authorization-server role behind the same domain (`/authorize`, `/token`, `/register`, `/.well-known/oauth-authorization-server`) so Claude's single-origin discovery, IP allowlisting and 10-second timeouts are easy to satisfy.
2. **If the platform has an OAuth authorize/token endpoint (even without DCR): use FastMCP `OAuthProxy`/`OIDCProxy`.** Configure `client_storage` on Redis with `FernetEncryptionWrapper`, an explicit `jwt_signing_key`, `require_authorization_consent=True` (or `"remember"`), `allowed_client_redirect_uris` covering the table in section 3, `forward_resource=True`, and `enable_cimd=True`. Advertise `client_id_metadata_document_supported: true`, `token_endpoint_auth_methods_supported: ["none", ...]` (needed for Claude and ChatGPT CIMD), `code_challenge_methods_supported: ["S256"]`, `authorization_response_iss_parameter_supported: true`, and keep a `registration_endpoint` for Cursor and Antigravity.
3. **If the platform has no OAuth AS at all:** prefer a hosted DCR+CIMD AS federated to the login (WorkOS AuthKit or Auth0 with the resource-parameter profile) and `RemoteAuthProvider`. Build your own AS (FastMCP `OAuthProvider`) only if identity must stay in-house; budget for a persistent client/code/token store, consent UI, CIMD fetching with SSRF controls, refresh rotation, and revocation.
4. **Transport:** Python `mcp` 2.x (directly or via FastMCP 4) on Cloud Run with `json_response=True`; for legacy clients either accept that each request is its own session (`stateless_http=True`, losing legacy-era elicitation) or rely on the 2026-07-28 MRTR path plus a shared `RequestStateSecurity` key. Do not depend on Cloud Run session affinity. Set the request timeout to 60 minutes only if you stream; otherwise keep tools under a few minutes and paginate.
5. **Scopes:** publish a minimal `scopes_supported` (e.g. `ledger:read`), require `ledger:write` via `403 insufficient_scope` step-up, include already-granted scopes in each challenge, and never publish `offline_access` in PRM.
6. **Tenancy:** explicit `business_id` argument validated per call against the token subject; keep `tools/list` stable; use `UserSession`/handles only for convenience state.
7. **Files:** return short-lived signed URLs in `structuredContent` plus a text summary; offer embedded blobs only for small documents.
8. **Do not** implement the HTTP+SSE transport, sampling, roots, or protocol logging in a new server.

---

## 8. Things that could not be verified

- `https://claude.com/api/mcp/auth_callback` does not appear in any Anthropic page fetched; only `https://claude.ai/api/mcp/auth_callback` is documented.
- The CIMD `client_id` URL used by claude.ai/Desktop (hosted surfaces) is not named in Anthropic's docs; only Claude Code's is.
- Whether claude.ai/Desktop already speak 2026-07-28 (blog says "rolling out soon", no dates) and whether MRTR will bring elicitation to those surfaces.
- Cursor: `resource` parameter support, token storage location, current tool-count limit, and 2026-07-28 support.
- VS Code: `resource` parameter behavior and core 2026-07-28 support (only extension support is claimed by GitHub's changelog); the exact VS Code version that added CIMD (den.dev post returned 403).
- ChatGPT: elicitation support, tool-count limits, and the "User-Defined OAuth Client" client id/secret UI (third-party sources only; official plugin docs do not mention it; OpenAI help-center article returned 403).
- Codex: its CIMD document URL, client-secret support for pre-registered clients, whether `experimental_use_rmcp_client` has been removed entirely (an issue says deprecated in favor of `[features].rmcp_client`), and whether the login-vs-add placement of `--oauth-client-registration` differs by version (docs say `login`, local 0.153.3 shows it on `add`).
- Gemini CLI / Antigravity: CIMD, `resource` parameter, elicitation.
- Firebase Authentication's suitability as an `OAuthProxy` upstream (not fetched).
- Whether Cloud Run needs `X-Accel-Buffering: no` for SSE (Google documents streaming support but not buffering behavior).
- The Medium CIMD write-up on Claude products and the OpenAI help-center developer-mode article returned 403; claims attributed to them come from search snippets only.

---

## Sources

**Spec and governance**
- https://modelcontextprotocol.io/specification/versioning
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- https://modelcontextprotocol.io/specification/2026-07-28/basic/index
- https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
- https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination
- https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://modelcontextprotocol.io/specification/2026-07-28/deprecated
- https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.ts
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- https://modelcontextprotocol.io/specification/2025-11-25/changelog
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/seps (SEP-990, SEP-991 files)
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1489
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1880
- https://github.com/modelcontextprotocol/ext-auth
- https://modelcontextprotocol.io/extensions/auth/overview
- https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization
- https://modelcontextprotocol.io/extensions/client-matrix
- https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-remote-servers
- https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13
- https://aaronparecki.com/2025/11/25/1/mcp-authorization-spec-update
- https://stackoverflow.blog/2026/01/21/is-that-allowed-authentication-and-authorization-in-model-context-protocol/
- https://www.theregister.com/devops/2026/07/23/model-context-protocol-prepares-to-break-with-its-stateful-past/5276722

**Clients**
- https://code.claude.com/docs/en/mcp
- https://claude.com/docs/connectors/building
- https://claude.com/docs/connectors/building/authentication
- https://claude.com/docs/connectors/building/lazy-authentication
- https://claude.com/docs/connectors/building/troubleshooting
- https://claude.com/docs/connectors/building/enterprise-managed-auth
- https://claude.ai/oauth/claude-code-client-metadata
- https://claude.com/blog/bringing-mcp-2026-07-28-to-claude
- https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
- https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- https://github.com/anthropics/claude-code/issues/37747
- https://github.com/anthropics/claude-code/issues/10572
- https://github.com/anthropics/claude-code/issues/76096
- https://github.com/anthropics/claude-code/issues/41110
- https://github.com/anthropics/claude-ai-mcp/issues/153
- https://sunpeak.ai/blogs/claude-connector-oauth-authentication/ (third-party, 2026-07-30)
- https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- https://learn.chatgpt.com/docs/extend/mcp?surface=chatgpt
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://codex.danielvaughan.com/2026/05/06/codex-cli-mcp-oauth-authenticating-remote-tool-servers/ (third-party)
- https://deepwiki.com/openai/codex/6.5-oauth-authentication-for-mcp (code-derived)
- https://github.com/openai/codex/issues/33403
- https://github.com/openai/codex/pull/35725
- https://github.com/openai/codex/issues/6995
- https://developers.openai.com/plugins/build/auth
- https://developers.openai.com/api/docs/mcp
- https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- https://chatgpt.com/oauth/client.json
- https://apxml.com/posts/how-to-setup-oauth-chatgpt-connector (third-party)
- https://cursor.com/docs/context/mcp
- https://forum.cursor.com/t/oauth-redirect-uri-changed-from-cursor-to-http-localhost-for-streamable-http-mcp/165019
- https://forum.cursor.com/t/mcp-oauth-cimd-support-plans-and-timelines/148096?page=2
- https://forum.cursor.com/t/regarding-the-quantity-limit-of-mcp-tools/153432
- https://code.visualstudio.com/api/extension-guides/ai/mcp
- https://code.visualstudio.com/docs/agents/reference/mcp-configuration
- https://code.visualstudio.com/docs/copilot/agents/agent-tools
- https://vscode.dev/oauth/client-metadata.json
- https://github.com/microsoft/vscode/issues/282472
- https://github.blog/changelog/2026-07-23-github-mcp-server-supports-the-next-mcp-specification/
- https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/tools/mcp-server.md
- https://geminicli.com/docs/tools/mcp-server/
- https://antigravity.google/docs/cli/mcp/
- https://antigravity.google/docs/mcp/
- https://github.com/google-antigravity/antigravity-cli/issues/25

**Python**
- https://pypi.org/project/mcp/
- https://github.com/modelcontextprotocol/python-sdk/releases
- https://py.sdk.modelcontextprotocol.io/run/authorization/index.md
- https://py.sdk.modelcontextprotocol.io/run/asgi/index.md
- https://py.sdk.modelcontextprotocol.io/run/deploy/index.md
- https://py.sdk.modelcontextprotocol.io/run/index.md
- https://py.sdk.modelcontextprotocol.io/run/legacy-clients/index.md
- https://py.sdk.modelcontextprotocol.io/migration/index.md
- https://py.sdk.modelcontextprotocol.io/whats-new/index.md
- https://py.sdk.modelcontextprotocol.io/protocol-versions/index.md
- https://py.sdk.modelcontextprotocol.io/deprecated/index.md
- https://py.sdk.modelcontextprotocol.io/servers/tools/index.md
- https://py.sdk.modelcontextprotocol.io/servers/structured-output/index.md
- https://py.sdk.modelcontextprotocol.io/servers/media/index.md
- https://py.sdk.modelcontextprotocol.io/servers/resources/index.md
- https://py.sdk.modelcontextprotocol.io/handlers/elicitation/index.md
- https://py.sdk.modelcontextprotocol.io/handlers/dependencies/index.md
- https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/index.md
- https://py.sdk.modelcontextprotocol.io/handlers/context/index.md
- https://py.sdk.modelcontextprotocol.io/client/identity-assertion/index.md
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/src/mcp/server/auth/provider.py
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/examples/servers/simple-auth/README.md
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/examples/servers/simple-auth/mcp_simple_auth/server.py
- https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/examples/servers/simple-auth/mcp_simple_auth/token_verifier.py
- https://github.com/modelcontextprotocol/python-sdk/issues/1801
- https://pypi.org/project/fastmcp/
- https://github.com/jlowin/fastmcp/releases
- https://gofastmcp.com/changelog.md
- https://gofastmcp.com/servers/auth/authentication
- https://gofastmcp.com/servers/auth/oauth-proxy.md
- https://gofastmcp.com/servers/auth/oidc-proxy.md
- https://gofastmcp.com/servers/auth/remote-oauth.md
- https://gofastmcp.com/servers/auth/token-verification.md
- https://gofastmcp.com/servers/auth/full-oauth-server.md
- https://gofastmcp.com/servers/auth/multi-auth.md
- https://gofastmcp.com/servers/authorization.md
- https://gofastmcp.com/servers/dependency-injection.md
- https://gofastmcp.com/servers/sessions.md
- https://gofastmcp.com/servers/tools.md
- https://gofastmcp.com/servers/elicitation.md
- https://gofastmcp.com/servers/context.md
- https://gofastmcp.com/deployment/http.md
- https://gofastmcp.com/python-sdk/fastmcp-server-auth-auth.md
- https://gofastmcp.com/python-sdk/fastmcp-server-auth-cimd.md
- https://gofastmcp.com/python-sdk/fastmcp-server-auth-identity_assertion.md
- https://gofastmcp.com/python-sdk/fastmcp-server-auth-jwt_issuer.md
- https://gofastmcp.com/integrations/auth0.md
- https://gofastmcp.com/integrations/authkit.md
- https://gofastmcp.com/integrations/descope.md
- https://gofastmcp.com/integrations/scalekit.md
- https://gofastmcp.com/integrations/google.md
- https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile

**Cloud Run and registry**
- https://docs.cloud.google.com/run/docs/host-mcp-servers
- https://docs.cloud.google.com/run/docs/configuring/request-timeout
- https://docs.cloud.google.com/run/docs/configuring/concurrency
- https://docs.cloud.google.com/run/docs/configuring/min-instances
- https://docs.cloud.google.com/run/docs/configuring/session-affinity
- https://docs.cloud.google.com/run/docs/triggering/websockets
- https://docs.cloud.google.com/run/docs/tutorials/deploy-remote-mcp-server
- https://modelcontextprotocol.io/registry/quickstart
- https://modelcontextprotocol.io/registry/remote-servers
- https://modelcontextprotocol.io/registry/authentication
