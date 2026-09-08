> Research snapshot captured 2026-09-08 while planning luca-mcp. Point-in-time; re-verify before relying on a specific version, URL, or setting. Source: Auth0 documentation and live metadata probes, URLs inline.

# Auth0 specifics for the Luca remote MCP server — research brief

Researched 2026-09-08. Every claim below was read on the cited page that day (or verified live with curl where noted). Items I could not confirm are marked **[could not verify]**. Quotes are verbatim.

## 0. Headline findings (read this first)

1. **Auth0 now has a first-class "Auth for MCP" product (GA 2026-05-05).** The recommended pattern is: register the MCP server as an Auth0 **API** whose identifier is the MCP server URL; Auth0 is the authorization server; the MCP server publishes RFC 9728 metadata itself; MCP clients register via **CIMD** (recommended), a pre-registered app, or **DCR**; the MCP server calls your downstream API with an **On-Behalf-Of (OBO) token exchange** (RFC 8693), which needs a new application type, the **Custom API client**. Python/FastMCP sample code exists. (Sources in §1.6, §3.1.)
2. **RFC 8414 metadata is live** at `https://<tenant>/.well-known/oauth-authorization-server` and is byte-for-byte the same document as `/.well-known/openid-configuration` (verified with curl on two Auth0 tenants). Auth0 does **not** serve `/.well-known/oauth-protected-resource` (404); that is the MCP server's job.
3. **RFC 8707 `resource` is supported** only after turning on the tenant setting **Resource Parameter Compatibility Profile** (`resource_parameter_profile: "compatibility"`). `audience` still wins if both are present. GA 2026-05-05.
4. **DCR works but every DCR client is a third-party app in "strict" mode**: consent cannot be skipped, only domain-level connections, explicit client grants (configure "default permissions for third-party apps" first), **no OIDC scopes / no ID token / no `/userinfo`** ("Planned for a future release"), exact-match redirect URIs, refresh tokens must expire, `/oidc/register` is open (no token) at 5 req/s per tenant. Tenant ACL for the `dcr` scope is Enterprise-only.
5. **One access token cannot carry two custom API audiences.** The only multi-audience case is `<custom API> + /userinfo`. So an MCP-audience token can never also be a Liebre-API token; you need OBO (or Custom Token Exchange) or the proxy pattern.
6. **OBO exchange is GA and appears available on every public tier** (it has a rate-limit row on Free: 2 req/s; Essentials/Professional: 8 req/s; Enterprise: 30 req/s). Custom Token Exchange is B2C/B2B Professional and Enterprise only (GA 2026-08-28).
7. **Two frictions for the "Auth0-as-AS" path with your existing setup**: (a) OBO requires the downstream Liebre API to allow "skip user consent" for the first-party Custom API client and a user-delegated client grant; (b) the `email` claim in the Liebre token must come from a post-login Action (Actions do run for OBO with `event.transaction.protocol = "oauth2-token-exchange"`), and `email` is one of the OIDC profile claims Auth0 permits as a non-namespaced access-token claim.

---

## 1. Auth0 as the MCP authorization server (direct)

### 1.1 RFC 8414 / OIDC discovery — supported (verified live)

Curl on 2026-09-08:

| URL | Result |
|---|---|
| `https://auth0.auth0.com/.well-known/oauth-authorization-server` | HTTP 200 |
| `https://auth0.auth0.com/.well-known/openid-configuration` | HTTP 200, **identical key set and values** to the line above |
| `https://samples.auth0.com/.well-known/oauth-authorization-server` | HTTP 200, same shape |
| `https://auth0.auth0.com/.well-known/oauth-protected-resource` | HTTP 404 |

Fields observed (both endpoints): `issuer` = `https://auth0.auth0.com/` (trailing slash), `authorization_endpoint` `/authorize`, `token_endpoint` `/oauth/token`, `registration_endpoint` `/oidc/register`, `revocation_endpoint` `/oauth/revoke`, `userinfo_endpoint`, `jwks_uri` `/.well-known/jwks.json`, `code_challenge_methods_supported: ["S256","plain"]`, `token_endpoint_auth_methods_supported: ["client_secret_basic","client_secret_post","private_key_jwt","none"]`, `grant_types_supported` includes `urn:ietf:params:oauth:grant-type:token-exchange`, `dpop_signing_alg_values_supported: ["ES256"]`, `scopes_supported` includes `offline_access` and `email`, plus `global_token_revocation_endpoint`, `backchannel_logout_supported`. **Not present** on that tenant: `client_id_metadata_document_supported`, `authorization_response_iss_parameter_supported`, `resource_parameter_supported`, `pushed_authorization_request_endpoint`, `introspection_endpoint`.
**[could not verify]** whether `registration_endpoint` is advertised only when DCR is enabled, and whether the CIMD / iss flags appear in metadata once the corresponding tenant toggles are on (Auth0's CIMD doc says the toggle "indicate[s] CIMD support in the Auth0 Authorization Server metadata").

History: community request 2025-06-03; a user reported it working 2025-08-05 ("That must have gone live recently!"); no Auth0 staff reply in that thread. https://community.auth0.com/t/provide-a-well-known-oauth-authorization-server-for-mcp-authorization/187508

### 1.2 Dynamic Client Registration (RFC 7591 / OIDC DCR)

Sources: https://auth0.com/docs/get-started/applications/dynamic-client-registration , https://auth0.com/docs/api/authentication/dynamic-application-client-registration/dynamic-application-registration , https://auth0.com/docs/get-started/tenant-settings , https://auth0.com/docs/api/management/v2/tenants/patch-settings , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/dynamic-client-registration

- **Enable**: Dashboard > Settings > Advanced > "Dynamic Client Registration (DCR)"; or `PATCH /api/v2/tenants/settings` with `flags.enable_dynamic_client_registration: true` (scope `update:tenant_settings`). Tenant-settings doc: "When enabled, third-party developers will be able to dynamically register applications for your APIs. By default, this feature is disabled." Management API description: "Whether third-party developers can dynamically register applications for your APIs (true) or not (false)."
- **Open registration**: `POST /oidc/register` "accepts requests without access token". Inspector guide warning: "if you enable this feature, anyone will be able to create applications in your tenant without a token."
- **Request**: `client_name`, `redirect_uris` (required: "An array of URLs that Auth0 will accept as valid callback URLs at the end of an authentication flow."), `token_endpoint_auth_method` ("Use `none` for public clients (SPA, Native) or `client_secret_post` (default) for confidential clients."), `grant_types` ("Filtered down in response ... Applications created through DCR support `authorization_code` and `refresh_token`." "The `client_credentials` grant type is not available via DCR.").
- **Response** (doc sample): `client_id: "tpc_8SXWY6j3afl2CP5ntwEOpMdPxxy49Gt2"`, `client_secret: "Q5O...33P"` ("Not returned when `token_endpoint_auth_method` is `none`."), `client_secret_expires_at: 0` ("Always 0 (never expires) for Auth0."), `grant_types`, `token_endpoint_auth_method`. So: `none` => public client, no secret; `client_secret_post` => confidential.
- **What you get**: "All applications created through Dynamic Client Registration are third-party applications with enhanced security controls." (`tpc_` prefix, PKCE mandatory, domain-level connections only, explicit client grants). "Third-party developers cannot modify application settings after registration."
- **Prerequisite**: "Before enabling DCR, configure default permissions for third-party applications on the APIs that dynamically registered clients should access. Without default permissions, DCR clients will not be able to access any API." Default permissions = `POST /api/v2/client-grants` with `"default_for": "third_party_clients"` (Dashboard: Applications > APIs > [API] > Settings > "Default Permissions for Third Party Apps"). https://auth0.com/docs/get-started/applications/third-party-applications/configure-third-party-applications
- **Rate limit**: "The /oidc/register endpoint is rate-limited to 5 requests per second per tenant." (Rate tables: 5 burst / 5 per sec on Free, Essentials/Professional and Enterprise.)
- **Security mode**: tenant field `dynamic_client_registration_security_mode`: "`strict` applies enhanced security controls. `permissive` preserves pre-existing behavior and is only available to tenants with prior third-party client usage." Permissive is only for "customers who have at least one tenant with third-party applications before April 2026". https://auth0.com/docs/get-started/applications/third-party-applications/permissive-mode
- **Tenant ACL** (`dcr` scope, IP/CIDR/country/user-agent rules): "Enterprise plan customers: 1 Tenant ACL"; up to 10 with the Attack Protection add-on. https://auth0.com/docs/secure/tenant-access-control-list
- **Auth0's own recommendation for MCP**: "For production MCP deployments, we recommend using manual CIMD registration ... instead of DCR."
- **Plan gating of DCR**: not listed on https://auth0.com/pricing (checked). A third-party post (API Evangelist, 2026-07-26) claims "at minimum a Professional or Enterprise plan"; **[could not verify]** in any Auth0 page.

### 1.3 Consequences of being a third-party app (strict mode) — this is the big list

Sources: https://auth0.com/docs/get-started/applications/third-party-applications , https://auth0.com/docs/get-started/applications/third-party-applications/security-controls , https://auth0.com/docs/get-started/applications/confidential-and-public-applications/user-consent-and-third-party-applications , https://auth0.com/docs/get-started/applications/third-party-applications/troubleshooting , https://auth0.com/docs/troubleshoot/product-lifecycle/deprecations-and-migrations/migrate-to-enhanced-security-third-party-applications

- **Consent**: "Auth0 always requires user consent when a third-party application requests access to APIs. Consent cannot be skipped." "Once consent has been given, the user does not see the consent dialog during subsequent logins until consent is revoked explicitly." `prompt=consent` forces it again. Consent is scoped per Organization.
- **Consent dialog content**: "The consent dialog shows API scopes only." Use `flags.use_scope_descriptions_for_consent` to show scope descriptions.
- **No OIDC for strict third-party apps**: "Third-party applications with enhanced security controls do not return ID tokens or process OIDC scopes (`openid`, `profile`, `email`) in this release. The token endpoint will return an access token but no `id_token`." Features table: "OIDC scopes and ID tokens | Not supported. Planned for a future release." and "`/userinfo` endpoint | Not supported." Workaround per Auth0: "Use API-scoped access tokens to retrieve the information your application needs."
- **Connections**: "Third-party applications can only authenticate users through connections promoted to the domain level." Promote via `PATCH /api/v2/connections/{id}` `{"is_domain_connection": true}` (Dashboard: Authentication > connection > "Promote Connection to Domain Level"). The troubleshooting page lists the failure: "Connection not available for a third-party application".
- **Grants / API access**: "Third-party applications always require a client grant to access any API, regardless of the API's access policy." (`unauthorized_client` otherwise.) Grant types: `authorization_code` (PKCE mandatory), `refresh_token`, `client_credentials` (confidential only). No implicit, password, device code, PAR, CIBA. Organizations: org must set `third_party_client_access: allow`.
- **`/authorize` parameter allow-list for third-party apps**: allowed include `audience`, `resource`, `scope`, `state`, `prompt`, `login_hint`, `max_age`, `nonce`, `code_challenge(_method)`, `connection`, `ui_locales`, `dpop_jkt`, `ext-*`. **Not supported** (=> `invalid_request`): `screen_hint`, `invitation`, `id_token_hint`, `claims`, `request`, `request_uri`, `login_ticket`.
- **`prompt=none`**: silent-auth errors are `login_required`, `consent_required` ("The user was logged in at Auth0, but needs to give consent to authorize the application."), `interaction_required`. Since consent can't be skipped, first silent attempt returns `consent_required`; after a stored user grant the doc says the dialog is not shown again. **[could not verify]** an explicit statement that `prompt=none` succeeds for strict third-party apps after consent. https://auth0.com/docs/authenticate/login/configure-silent-authentication
- **Redirect URIs**: "Wildcard subdomains in URLs | Not supported. Callback URLs, allowed origins, and web origins must use exact URLs." Default `redirection_policy: open_redirect_protection` (no redirect to callback on auth errors). Nothing in the DCR or third-party docs states loopback/`localhost` rules or RFC 8252 port-agnostic matching. The CIMD doc says redirect URIs must be HTTPS and "loopback URIs allowed for native apps only" (summary of https://auth0.com/docs/get-started/auth0-overview/create-applications/register-applications-with-cimd). **[could not verify]** whether Auth0 ignores the port for `http://127.0.0.1:<port>` or accepts `http://localhost:<port>`. Practical note: with DCR each client instance registers its actual `http://localhost:<port>/callback`, so exact matching works per registration; with a static CIMD document the port problem is real (Claude Code issues https://github.com/anthropics/claude-code/issues/37747 and https://github.com/anthropics/claude-code/issues/42765 describe Claude Code using `http://localhost:<ephemeral-port>/callback`).
- **Refresh tokens for third-party apps**: `refresh_token` grant is allowed; "Expiration required: Non-expiring refresh tokens are not available. Infinite idle lifetime is not available." "SPA and Native third-party applications have refresh token rotation enabled by default, aligned with the OAuth 2.1 and MCP requirements." The API must have "Allow Offline Access" and the client must request `offline_access`. **[could not verify]** how `offline_access` is rendered on the consent screen for strict apps (the dialog "shows API scopes only").
- **Other strict-mode gaps**: Rules not supported (Actions are), `jwt_configuration.alg` must be RS256, "Logout endpoints | Not supported. Use POST /oauth/revoke", third-party apps "cannot access system APIs" (Management API, My Account).
- **Permanence**: `third_party_security_mode cannot be changed after creation`; "Third-party applications cannot be converted to first-party applications, and vice versa."
- **Auth0 compatibility claim**: migration guide scenario 3: "MCP clients (Claude Code, VS Code) are compatible with enhanced security controls."
- **Timeline**: Enhanced Security Controls GA 2026-04-30 (changelog). From 2026-10-23 the Management API default for new third-party apps flips from `permissive` to `strict` for the legacy-eligible tenants.

### 1.4 CIMD (Client ID Metadata Documents) — Auth0's preferred MCP registration

Sources: https://auth0.com/docs/get-started/auth0-overview/create-applications/register-applications-with-cimd , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/manual-cimd-registration , https://auth0.com/blog/cimd-vs-dcr-mcp-registration/ (2025-11-24), changelog "Client ID Metadata (CIMD) Registration (GA)" 2026-05-05.

- "The CIMD URL is the application's client ID and proves domain ownership". Registration is **manual**: an admin imports the URL (Dashboard: Applications > Create Application > Import from URL > Preview > Create; or `POST /api/v2/clients/cimd/preview` and `/api/v2/clients/cimd/register` with `external_client_id`). Auth0 does **not** auto-register unknown CIMD `client_id`s at `/authorize`.
- Tenant toggle: Settings > Advanced > "Client ID Metadata Document Registration" (`client_id_metadata_document_supported` in the Auth0 CLI example).
- CIMD clients "always register with `third_party_security_mode: "strict"`" (so all of §1.3 applies), app type native or regular web, public (`token_endpoint_auth_method: none` + PKCE) or confidential with `private_key_jwt` only ("available only for Enterprise customers"). Grant types allowed: `authorization_code`, `refresh_token`. Document <= 5 KB, no HTTP redirects. Rate limits per CIMD client "will be introduced in a future release".
- Auth0 names no specific MCP hosts as CIMD-capable in these pages. **[could not verify]** which of Claude Code / Codex / Cursor / claude.ai / ChatGPT currently send CIMD `client_id`s (out of scope for this brief; the client-side researcher should confirm).

### 1.5 RFC 8707 `resource` vs `audience`, and Default Audience

Sources: https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile , https://auth0.com/docs/api/management/v2/tenants/patch-settings , https://auth0.com/docs/get-started/tenant-settings , https://community.auth0.com/t/rfc-8707-implementation-audience-vs-resource/188990 , https://github.com/microsoft/vscode/issues/274226 , https://auth0.com/docs/secure/tokens/access-tokens/get-access-tokens , https://support.auth0.com/center/s/article/How-to-stop-getting-JWEs-when-JWT-is-required

- Historically Auth0 only honoured `audience` (since 2017) and "does not examine or consider the `resource` parameter" (VS Code issue, 2025-10-30). Auth0's Aaron Parecki, 2025-10-31: "Auth0 support for the `resource` parameter is in "closed EA" ... going to "limited EA" on Monday". Docs existed by 2025-11-21; changelog "Resource Parameter Compatibility Mode (GA)" 2026-05-05.
- **Setting**: Dashboard > Settings > Advanced > "Resource Parameter Compatibility Profile" (Auth0 also tells you to enable "Include Issuer in Authorization Responses" = `authorization_response_iss_parameter_supported`, RFC 9207). Management API field `resource_parameter_profile`: "When set to `audience` (default), the audience parameter is used to specify the resource server. When set to `compatibility`, the audience parameter is still checked first, but if it not provided, then the resource parameter can be used to specify the resource server."
- Guide: "If both the `resource` and `audience` are available, the `audience` will still be used." `resource` must be "an absolute URI" (RFC 8707) and must equal an API identifier. Applies to `/authorize`, PAR, JAR, CIBA, refresh-token grant.
- **Conflict to check on your tenant**: the tenant-settings page says the profile is "enabled (default for new tenants)" while the Management API doc says `audience` is the default. Read `resource_parameter_profile` from `GET /api/v2/tenants/settings` rather than assuming. **[could not verify]** which is right for tenants created in 2026.
- **[could not verify]** behaviour with multiple `resource` values in one request (not documented).
- **Default Audience**: "API identifier to use for Authorization Flows. If you enter a value, all access tokens issued by Auth0 will specify this API identifier as an audience." Side effect: "Setting the Default Audience is equivalent to appending this audience to every authorization request made to your tenant for every application. This will cause new behavior that might result in breaking changes for some of your applications." Auth0 staff (2025-08-04) called it a workaround with "limitations (you only get one)". Without any audience Auth0 issues an opaque token "only intended for use with the tenant's `/userinfo` endpoint" (the VS Code issue calls it "an encrypted JWT"; Auth0's support article says opaque; JWE only if the API's "JSON Web Encryption" setting is on). With the compatibility profile you do not need Default Audience for MCP.

### 1.6 RFC 9728 and Auth0's recommended MCP architecture (with Python)

Sources: https://auth0.com/ai/docs/mcp/intro/overview , https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server , https://auth0.com/ai/docs/mcp/get-started/call-your-apis-on-users-behalf , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application , https://auth0.com/blog/auth0-auth-for-mcp-servers-generally-available/ (2026-05-06), https://github.com/auth0-samples/auth0-ai-samples/tree/main/auth-for-mcp , https://github.com/auth0/auth0-api-python

- Auth0 tenants do **not** host protected-resource metadata (404 verified). Your MCP server must serve `/.well-known/oauth-protected-resource` and return `WWW-Authenticate: Bearer ..., resource_metadata="..."` on 401. Auth0's Python sample does exactly that with the official MCP SDK helper.
- **Tenant setup Auth0 prescribes** (from the sample README, mirrored by the quickstart):
  1. Enable "Resource Parameter Compatibility Profile" (and iss-in-response).
  2. `auth0 tenant-settings update set client_id_metadata_document_supported flags.enable_dynamic_client_registration flags.use_scope_descriptions_for_consent`
  3. Promote the connections third-party clients may use: `auth0 api patch connections/<id> --data '{"is_domain_connection": true}'`
  4. Create the MCP server **API**: identifier = MCP server URL (sample uses `http://localhost:3001/`), `"signing_alg": "RS256"`, `"token_dialect": "rfc9068_profile_authz"`, `"enforce_policies": true`, scopes = tool scopes (`tool:whoami`, `tool:greet`).
  5. RBAC roles/permissions on that API; default permissions for third-party apps.
  6. Register clients: manual CIMD (recommended) > manual > DCR.
- **Validation code** (sample `src/auth0/middleware.py`, uses `auth0-api-python`):

```python
from auth0_api_python import ApiClient, ApiClientOptions
from auth0_api_python.errors import VerifyAccessTokenError

self.client = ApiClient(ApiClientOptions(domain=domain, audience=audience))
...
decoded = await self.client.verify_access_token(token, required_claims=["sub"])
```

- **PRM + WWW-Authenticate** (sample `src/auth0/__init__.py`):

```python
from mcp.server.auth.routes import create_protected_resource_routes

routes = create_protected_resource_routes(
    resource_url=self.audience,                       # == the Auth0 API identifier == MCP server URL
    authorization_servers=[f"https://{self.domain}"], # Auth0 tenant (or custom domain)
    scopes_supported=list(self._scopes_supported),
    resource_name=self.name,
)
# 401 handler:
www_auth_params = [f'error="{error_code}"', f'error_description="{description}"']
if include_resource_metadata and self.mcp_server_url:
    metadata_url = self.mcp_server_url.rstrip("/") + "/.well-known/oauth-protected-resource"
    www_auth_params.append(f'resource_metadata="{metadata_url}"')
return f"Bearer {', '.join(www_auth_params)}"
```

- **Calling the downstream API on the user's behalf** (sample `fastmcp-mcp-on-behalf-of-tokenexchange-python/src/tools.py`):

```python
result = await api_client.get_token_on_behalf_of(
    access_token=access_token,                 # the incoming MCP-audience token, raw (no "Bearer ")
    audience=config.api_auth0_audience,        # e.g. https://api-dev.liebre.ai/api/v1
    scope=config.mcp_auth0_exchange_scope or None,
)
# result["access_token"], result.get("scope"), result["expires_in"]
```
  The `ApiClient` for OBO is constructed with `client_id`/`client_secret` of the **Custom API client** (see §3.1). The GA blog frames this as the MCP server's "dual role": resource server toward the MCP client, OAuth client toward Auth0 for the downstream call.
- Sample repo paths: `auth-for-mcp/fastmcp-mcp-python` (deps: `mcp ^1.14.1`, `auth0-api-python ^1.0.0b6`, `starlette`, `uvicorn`), `auth-for-mcp/fastmcp-mcp-on-behalf-of-tokenexchange-python`, `auth-for-mcp/fastmcp-mcp-customtokenexchange-python`, `auth-for-mcp/xmcp-mcp-tokenvault-js`.
- `auth0-api-python` current PyPI release is **1.0.0b10 (2026-08-03)**, still a beta version number; it provides OIDC discovery + JWKS caching, `verify_access_token`, `verify_request` (Bearer + DPoP), `get_token_on_behalf_of`, `get_token_by_exchange_profile`, and `act`-claim helpers.
- Not to be confused: **`auth0/auth0-mcp-server`** is Auth0's MCP server for *administering* a tenant through the Management API (create apps, deploy Actions, query logs), not a library for protecting your MCP server. https://github.com/auth0/auth0-mcp-server , https://auth0.com/docs/get-started/auth0-mcp-server
- Pricing: "Auth0 for AI Agents" is an add-on ("Adds 50% to the base price"); the OBO docs say the add-on raises the OBO rate limit to the tier's Authentication API ceiling. Nothing on the pricing page gates Auth for MCP itself. **[could not verify]** whether any MCP feature is unavailable on Free beyond the rate limits.

---

## 2. OAuth-proxy pattern: MCP server is the AS for MCP clients, Auth0 upstream

Everything here is the ordinary confidential-client Authorization Code flow against Auth0 with one pre-registered Regular Web Application and a fixed callback.

### 2.1 Refresh tokens
- Requirements: "you must include the `offline_access` scope when you initiate an authentication request through the `/authorize` endpoint" and enable "Allow Offline Access" on the API ("Enable this setting to allow applications to ask for refresh tokens for the API."; Management API `allow_offline_access`); the app needs the `refresh_token` grant. https://auth0.com/docs/secure/tokens/refresh-tokens/get-refresh-tokens , https://auth0.com/docs/get-started/apis/api-settings
- Cap: "Auth0 limits the amount of active refresh tokens to 200 tokens per user per application" (relevant if the proxy stores one Auth0 refresh token per MCP client installation per user). https://auth0.com/docs/secure/tokens/refresh-tokens
- Refresh call: `POST /oauth/token` `grant_type=refresh_token`, `client_id`, `client_secret` (confidential), `refresh_token`. Response includes a new `access_token`, `expires_in`, `scope`, and "If the scope of the initial token included `openid`, then a new ID token will be in the response as well". https://auth0.com/docs/secure/tokens/refresh-tokens/use-refresh-tokens **[could not verify]** on that page whether `audience` may be changed on refresh (the resource-param guide lists the refresh grant as supporting `resource`).
- **Rotation**: Dashboard > Applications > app > Settings > "Allow Refresh Token Rotation"; Management API `refresh_token.rotation_type: "rotating" | "non-rotating"` (default `non-rotating`), `refresh_token.leeway` (default 0; "Allow the same refresh token to be used within the time period to account for potential network concurrency issues"). Reuse detection: "the entire set of refresh tokens issued since that invalidated token was issued will immediately be revoked along with the grant, requiring the user to re-authenticate." "Refresh Token Rotation is only supported for OIDC-conformant applications with the Refresh Token grant type enabled." https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation , https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation
- **Expiration**: `refresh_token.expiration_type: "expiring" | "non-expiring"` (Management API default `non-expiring`), `token_lifetime` (absolute), `idle_token_lifetime`, `infinite_token_lifetime`, `infinite_idle_token_lifetime` (default false). Dashboard doc: Idle lifetime "Default | 2,592,000 seconds (30 days)", "Maximum | 31,557,600 seconds (1 year)"; Maximum (absolute) lifetime max 1 year, no default stated (doc's JSON sample uses `"token_lifetime": 2592000`); "The Idle Refresh Token Lifetime can not exceed the Maximum Refresh Token Lifetime"; "SPAs will default into rotation and will not support non-expiring refresh tokens." https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-expiration , https://auth0.com/docs/api/management/v2/clients/post-clients
- Revocation: `POST /oauth/revoke` with `client_id`, `token` (the refresh token), `client_secret` for confidential clients (or `client_assertion`); 200 with empty body. The API-level toggle "Refresh Token Revocation Deletes Grant" decides whether only that token or the whole grant (all refresh tokens for user+app+audience) is revoked. https://auth0.com/docs/api/authentication/revoke-refresh-token/revoke-refresh-token

### 2.2 Access token lifetime
- Per API: "The default value is 86400 seconds (24 hours). The maximum value you can set is 2592000 seconds (30 days)." Management API `token_lifetime` (0–2592000) and `token_lifetime_for_web`. https://auth0.com/docs/get-started/apis/api-settings , https://auth0.com/docs/api/management/v2/resource-servers/post-resource-servers

### 2.3 Putting `email` in the access token (Action)
- Post-login Action API: `api.accessToken.setCustomClaim(key, value)` where `key` is the "Name of the claim (note that this may need to be a fully-qualified url)". https://auth0.com/docs/customize/actions/triggers/post-login/api-object
- Rules: "Access tokens with an Auth0 API audience, excluding the /userinfo endpoint, cannot have private, non-namespaced custom claims" and "Only specified OIDC user profile claims can be added to access tokens". `email`, `email_verified`, `name`, `given_name`, `family_name`, `nickname`, `preferred_username` are in the permitted OIDC profile list, so `api.accessToken.setCustomClaim('email', event.user.email)` is allowed; any other custom claim must be namespaced with an HTTP(S) URL you control ("Auth0 domains cannot be used as namespace identifiers"; `urn:auth0` reserved). Payload max 100 KB. https://auth0.com/docs/secure/tokens/json-web-tokens/create-custom-claims
- The same post-login trigger runs for OBO exchanges: "OBO token exchanges trigger the post-login Action trigger, where: The `event.transaction.protocol` is set to `oauth2-token-exchange`." So an existing "add email" Action will also stamp the Liebre token produced by OBO unless it filters on protocol. **[could not verify]** on the fetched pages that the post-login trigger also runs on `refresh_token` grants (Auth0 documents refresh-time MFA in Actions, which implies it, but I did not read a page saying so).

### 2.4 `/authorize` parameters, `state`, token-endpoint auth, `/userinfo`
- `state`: "(recommended) An opaque arbitrary alphanumeric string your app adds to the initial request"; Auth0's guidance: generate, store, compare on callback; "If you get the error `414 Request-URI Too Large`, try a smaller value." https://auth0.com/docs/secure/attack-protection/state-parameters
- `prompt=none` semantics and errors: §1.3. `prompt=login` is "a UX hint" the RP cannot verify; `max_age` forces an `auth_time` claim and "`max_age=0` effectively forces a login prompt"; verify `auth_time` yourself. `prompt=consent` forces the consent dialog. `login_hint` "(optional) Populates the username/email field for the login or signup page." https://auth0.com/docs/authenticate/login/max-age-reauthentication , https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow/add-login-auth-code-flow
- PKCE: "`S256` ... is the only one supported by Auth0" (metadata still lists `plain`). https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce/call-your-api-using-the-authorization-code-flow-with-pkce
- Token endpoint auth for confidential apps: Client Secret Post, Client Secret Basic, Private Key JWT (Dashboard: Credentials tab > "Authentication Method"; Management API `token_endpoint_auth_method`, whose **API default is `none`**, so set it explicitly when scripting). https://auth0.com/docs/get-started/applications/confidential-and-public-applications , https://auth0.com/docs/api/management/v2/clients/post-clients
- `/userinfo`: needs `openid` scope and an RS256 token whose `aud` includes `https://<tenant>/userinfo`; returns `X-RateLimit-*` headers; limit "10 burst, 5/minute, to a unique User ID" on Free, Essentials/Professional and Enterprise. Not available to strict third-party apps. https://auth0.com/docs/api/authentication/user-profile/get-user-info

### 2.5 Rate limits (public cloud; verified from the tier tables)

| Bucket | Free | Essentials / Professional | Enterprise |
|---|---|---|---|
| Authentication API global | 300 burst, 300/minute | 25 burst, 25/second | Production 100/second (2x–4x "Public Performance Burst" for 48 h/month); Non-production 100/second |
| `/oauth/token` | 30 burst, 30 per/sec (explicit row) | no separate row (global 25/s applies) | no separate row (global applies) |
| `/oidc/register` | 5 / 5 per sec | 5 / 5 per sec | 5 / 5 per sec |
| `/.well-known/jwks.json` | 20 / 20 per sec | no row | no row |
| `/userinfo` | 10 / 5 per min per user | same | same |
| On-Behalf-Of exchange | 2 / 2 per sec | 8 / 8 per sec | 30 / 30 per sec |
| Custom Token Exchange | no row | 4 / 4 per sec | 15 / 15 per sec |
| Management API global | 2 / 2 per sec | (see page) | Production 50 burst, 16/sec; Non-production 10 / 2 per sec |

Sources: https://auth0.com/docs/troubleshoot/customer-support/operational-policies/rate-limit-policy/rate-limit-configurations/free-public , .../essentials-professional-b2b , .../enterprise-public . "Production rate limits only apply to tenants tagged as `Production`." https://auth0.com/docs/get-started/auth0-overview/create-tenants/set-up-multiple-environments

---

## 3. Token exchange options (MCP-audience token -> Liebre-API-audience token)

### 3.1 On-Behalf-Of (OBO) Token Exchange — the fit for this problem
Source: https://auth0.com/docs/secure/call-apis-on-users-behalf/on-behalf-of-token-exchange ; changelog "On-Behalf-Of Token Exchange" GA 2026-05-05; community error thread https://community.auth0.com/t/unable-to-get-a-token-on-behalf-of-a-user-client-must-be-a-custom-api-client-with-resource-server-id/200896 (June 2026).

- Purpose: "enables middle-tier services to preserve user identity and permissions when calling downstream APIs." Use case #1 in the doc: "MCP servers that need to call first-party APIs on the user's behalf".
- **Who may call it**: "Only Custom API clients associated with a resource server can use the OBO token exchange. A Custom API client is linked to a resource server when they share the same identifier." Create with `POST /api/v2/clients` `{"name": "...", "app_type": "resource_server", "resource_server_identifier": "<MCP API identifier>"}` (Dashboard shows Application Type "Custom API Client"). "Because Custom API clients are first-party clients, make sure you skip user consent for the APIs your first-party client needs to access." (i.e. "Allow Skipping User Consent" on the Liebre API; note the localhost caveat: consent can't be skipped for apps with `localhost` callbacks, but a Custom API client has no callbacks.)
- **Grant**: user-delegated client grant from the Custom API client to the downstream API: `POST /api/v2/client-grants` `{"client_id": "<custom api client>", "audience": "<Liebre API>", "scope": [...], "subject_type": "user"}` (Dashboard: app > API Access > Edit > "User-Delegated Access" > Grant Access).
- **Enable**: `PATCH /api/v2/clients/{id}` `{"token_exchange": {"allow_any_profile_of_type": ["on_behalf_of_token_exchange"]}}` (Dashboard: Token Exchange > "On-Behalf-Of Token Exchange"). The community fix also added `urn:ietf:params:oauth:grant-type:token-exchange` to the client's `grant_types`. Using an M2M app instead yields "Client must be a Custom API Client with resource_server_id".
- **Request** `POST /oauth/token`: `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `client_id`, `client_secret` ("You can use any client authentication method; however, you cannot set `token_endpoint_auth_method` to `none`."), `subject_token=<incoming Auth0 access token>`, `subject_token_type=urn:ietf:params:oauth:token-type:access_token`, `requested_token_type=urn:ietf:params:oauth:token-type:access_token`, `audience=<downstream API>`, optional `scope`. Response: `access_token`, `expires_in` ("depending on the configuration of the downstream API"), `token_type` (Bearer or DPoP), `issued_token_type`, `scope`.
- **Claims**: "Token B has the same `sub` (user ID) as Token A, but the `aud` (audience) is now the first-party API." Adds an `act` claim chain (outermost `act.sub` = current actor = Custom API client id; `azp` matches it). "The delegation chain is limited to five nested levels"; exchange fails (400) if the subject already has four. Org context (`org_id`) is preserved and re-validated.
- **Actions / RBAC**: post-login trigger runs with `event.transaction.protocol = "oauth2-token-exchange"`; "the scopes returned for downstream API calls are based on the user's Role-Based Access Control (RBAC) policies" and must be within the user-delegated grant.
- **No refresh token** comes back (SDK: "does not expose `id_token` or `refresh_token`"). Auth0: "Cache access tokens for the lifetime of the token instead of requesting a new token for each API call."
- **Token binding**: Auth0 does not re-verify DPoP/mTLS binding of the subject token; if you don't present binding, "Auth0 issues an unbound bearer token".
- **Availability/tier**: no plan gate stated in the doc; rate-limit rows exist on Free (2/s), Essentials/Professional (8/s), Enterprise (30/s); the "Auth0 for AI Agents" add-on lets OBO use the tier's full Authentication API limit. Treat "available on Free" as an inference from the rate table. **[could not verify]** a sentence that says so explicitly.
- Python: `ApiClient(ApiClientOptions(domain, audience=<MCP API>, client_id, client_secret)).get_token_on_behalf_of(access_token=..., audience=..., scope=...)` (see §1.6 snippet). JS equivalent `apiClient.getTokenOnBehalfOf(...)`.

### 3.2 Custom Token Exchange (CTE)
Source: https://auth0.com/docs/authenticate/custom-token-exchange ; changelog GA 2026-08-28 ("available to Enterprise, B2B Professional, and B2C Professional customers"); blog 2026-05-05 https://auth0.com/blog/developers-guide-flexible-token-exchange-auth0/ .
- "This feature is available for B2C Professional, B2B Professional, and Enterprise plans." (The `auth0-api-python` README still says "Early Access for Enterprise customers"; it is stale relative to the docs/changelog.)
- Model: `/oauth/token` token-exchange request whose `subject_token_type` is a **custom URI in your own namespace** ("must not use reserved OAuth namespaces (IETF or vendor-controlled)"); it maps to a Token Exchange Profile that runs one Action (`custom-token-exchange` trigger) which validates the subject token and calls `api.authentication.setUserById()` / `setUserByConnection()`. Listed use case: "Getting Auth0 tokens for another audience". Requires a confidential client (HTTP Basic auth in the SDK). Auth0's steer: "If you don't have custom token validation requirements and only need to propagate delegation context between your own services, use On-Behalf-Of Token Exchange instead. It doesn't run a custom Action per request and supports significantly higher throughput." Auth0 has a Python MCP sample doing CTE (`fastmcp-mcp-customtokenexchange-python`).
- Rate: 4/s (Essentials/Professional), 15/s (Enterprise). Not on Free.

### 3.3 Token Vault (federated connections)
Source: https://auth0.com/docs/secure/tokens/token-vault
- "Token Vault securely stores the access and refresh tokens of supported external providers." It is for Google/GitHub/Microsoft/Slack/etc. tokens (grant `urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token`), not for minting tokens for your own Auth0 API. Not applicable to Liebre. Pricing rows: Free 2, Essentials/Professional "3 + ADD-ON", Enterprise "4 + ADD-ON".

### 3.4 Can one token carry two custom API audiences? No.
- "Tokens can have multiple audiences in only one scenario": a custom API (RS256) plus `/userinfo` when `openid` is requested; then "`aud` claim will be an array rather than a string." https://auth0.com/docs/secure/tokens/access-tokens/get-access-tokens
- Auth0 staff (2021-05-03): "Each API should have its own audience. You will need an access token with that specific audience to access that API. So you will have multiple access tokens for multiple APIs." https://community.auth0.com/t/access-token-for-multiple-apis/62092
- The only "one token for several backends" pattern is a single **logical API** with scopes covering all backends (same audience). https://auth0.com/docs/get-started/apis/set-logical-api . For Luca that would mean making the MCP server accept Liebre-API-audience tokens, which the MCP spec forbids (tokens must be audience-bound to the MCP server).
- `client_credentials` + `organization`: not investigated for this brief (it yields an app-identity token, not a user token, so it cannot replace OBO).

---

## 4. Verifying Auth0 RS256 access tokens in Python

Facts: PyJWT 2.13.0 (released 2026-05-21). `PyJWKClient(uri, cache_keys=False, max_cached_keys=16, cache_jwk_set=True, lifespan=300, headers=None, timeout=30, ssl_context=None)`; `get_signing_key_from_jwt(token)` picks the key by `kid` and refetches the JWKS if the `kid` is unknown; `jwt.decode(..., algorithms=[...], audience=<str or iterable>, issuer=<str or container>, leeway=<seconds or timedelta>, options={"require": [...]})`; `leeway` applies to `exp`, `nbf`, `iat`. https://pyjwt.readthedocs.io/en/stable/api.html , https://pyjwt.readthedocs.io/en/stable/usage.html . Auth0 issuer is `https://<domain>/` **with trailing slash** (metadata `issuer` verified above; token `iss` follows the domain used for the request, canonical or custom).

```python
# pip install "PyJWT[crypto]>=2.10"
import jwt
from jwt import PyJWKClient

AUTH0_DOMAIN = "dev-xxxx.us.auth0.com"          # or your custom domain, whichever issued the token
ISSUER = f"https://{AUTH0_DOMAIN}/"             # trailing slash is part of Auth0's iss
AUDIENCE = "https://api-dev.liebre.ai/api/v1"   # API identifier; aud may be an array incl. /userinfo

_jwks = PyJWKClient(
    f"https://{AUTH0_DOMAIN}/.well-known/jwks.json",
    cache_jwk_set=True, lifespan=300,   # cache the JWKS document 5 min; unknown kid triggers a refetch
    cache_keys=True, max_cached_keys=16,
    timeout=5,
)

def verify(token: str) -> dict:
    key = _jwks.get_signing_key_from_jwt(token)      # matches header.kid; raises PyJWKClientError
    return jwt.decode(
        token,
        key,                                          # PyJWK accepted directly in PyJWT >= 2.10
        algorithms=["RS256"],                         # never accept alg from the token
        audience=AUDIENCE,                            # ok when aud is ["<api>", "https://<domain>/userinfo"]
        issuer=ISSUER,
        leeway=30,                                    # seconds of clock skew for exp/nbf/iat
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
```

Notes: create the `PyJWKClient` once per process (it holds the cache); `jwks.json` is rate-limited (20/s on Free) so do not construct it per request; catch `jwt.ExpiredSignatureError`, `jwt.InvalidAudienceError`, `jwt.InvalidIssuerError`, `jwt.PyJWKClientError`. If the token was issued for the MCP-server API with `token_dialect: rfc9068_profile*`, RBAC permissions appear in `permissions` and the header `typ` is `at+jwt` (PyJWT does not check `typ`). Auth0's own alternative is `auth0-api-python` (`ApiClient(...).verify_access_token(token, required_claims=[...])`), which does discovery, JWKS caching and DPoP for you, but it is still versioned `1.0.0b10`.

---

## 5. Tenant hygiene and the "Luca MCP" application

- **One tenant per environment**: "Simply create a new tenant for each environment to guarantee isolation between them." Tag each tenant Development / Staging / Production; "Production rate limits only apply to tenants tagged as `Production`." "For production environments, we strongly recommend using custom domains." Tenant names: lowercase alphanumerics and hyphens, 3–63 chars, "cannot be changed after creation"; regional domains `{tenant}.us.auth0.com`, `.eu.`, `.au.`, `.jp.`, `.uk.`, `.ca.`. https://auth0.com/docs/get-started/auth0-overview/create-tenants/set-up-multiple-environments , https://auth0.com/docs/get-started/auth0-overview/create-tenants . **[could not verify]** documentation of the auto-generated `dev-xxxxxxxx` naming.
- **Custom domains**: pricing shows Free "1 Custom Domain*" (needs a credit card on file, not charged), paid tiers "Included". "Auth0 issues tokens with the `iss` claim for the domain you used with the token request"; "Your existing integrations using `YOUR_DOMAIN.auth0.com` will continue to work". Consequence: pick one domain for `/authorize`, `authorization_servers` in the PRM, and the verifier's `issuer`/JWKS; don't mix. https://auth0.com/docs/customize/custom-domains , https://auth0.com/pricing . **[could not verify]** that `/.well-known/oauth-authorization-server` is served on custom domains (very likely, untested).
- **Application types**: Native, Single Page, Regular Web, Machine to Machine, plus the new Custom API Client (`app_type: resource_server`). Regular Web and M2M are confidential; SPA and Native are public. https://auth0.com/docs/get-started/applications/application-settings , https://auth0.com/docs/get-started/applications/confidential-and-public-applications
- **Allowed Callback URLs**: comma-separated; subdomain wildcards only and "Not recommended for production environments"; localhost discouraged in production; no documented maximum count for callbacks (Allowed Web Origins is capped at "up to 100 URLs"); the Management API `callbacks` array has no documented limit; strict third-party apps: exact URLs only. https://auth0.com/docs/get-started/applications/application-settings , https://auth0.com/docs/get-started/applications/wildcards-for-subdomains
- **Entity limits**: applications and APIs per tenant: Free 10 / 10, Self-service 100 / 100, Enterprise 100,000; client grants Free 100. **[could not verify]** whether DCR-created `tpc_` clients count toward the application limit (if they do, a Free tenant's 10-app cap is exhausted after a handful of DCR registrations). https://auth0.com/docs/troubleshoot/customer-support/operational-policies/entity-limit-policy
- **Application metadata**: up to 10 key/value pairs, 255 chars each.

### 5.1 Settings for a "Luca MCP" application — Option B (OAuth proxy, Auth0 upstream)
1. Application type **Regular Web Application**, "OIDC Conformant" on.
2. Credentials > Authentication Method: `client_secret_post` or `client_secret_basic` (or `private_key_jwt`); never `none`.
3. Advanced > Grant Types: `authorization_code`, `refresh_token` only (drop `implicit`).
4. Allowed Callback URLs: exactly `https://<mcp-host>/oauth/callback` (one per environment tenant). No localhost in production.
5. Refresh Token: rotation on, `leeway` small (e.g. 0–10 s), expiration on with absolute and idle lifetimes chosen to match your MCP-side session policy (defaults 30 days idle; both max 1 year).
6. Liebre API (resource server): "Allow Offline Access" on; "Allow Skipping User Consent" on (first-party); "Refresh Token Revocation Deletes Grant" per preference; keep the `email` Action.
7. Authorize request: `response_type=code`, `code_challenge_method=S256`, `scope=openid email offline_access`, `audience=<Liebre API>`, `state`, optionally `login_hint`/`max_age`/`prompt=login`.
No tenant flags (DCR, CIMD, resource profile) are required for this option.

### 5.2 Settings for Option A (Auth0 is the AS for MCP clients)
1. Tenant: enable "Resource Parameter Compatibility Profile" (`resource_parameter_profile: compatibility`), "Include Issuer in Authorization Responses", "Client ID Metadata Document Registration", and, if you accept it, "Dynamic Client Registration (DCR)" with `dynamic_client_registration_security_mode: strict`; `flags.use_scope_descriptions_for_consent`.
2. Connections: `is_domain_connection: true` on every connection MCP users may log in with (e.g. the Liebre database connection, Google).
3. New **API** "Luca MCP": identifier = canonical MCP URL (e.g. `https://mcp.liebre.ai/mcp`, absolute URI; immutable, max 600 chars), RS256, `token_dialect: rfc9068_profile_authz` if you want `permissions`, `enforce_policies: true`, scopes = tool scopes, "Allow Offline Access" on, token lifetime as desired, **default permissions for third-party apps** = the scopes DCR/CIMD clients may get.
4. New **Custom API client** linked to that API (`app_type: resource_server`, `resource_server_identifier` = same identifier), confidential auth method, `token_exchange.allow_any_profile_of_type: ["on_behalf_of_token_exchange"]`, `grant_types` including `urn:ietf:params:oauth:grant-type:token-exchange`.
5. **User-delegated client grant** (`subject_type: "user"`) from the Custom API client to the Liebre API with the needed scopes; Liebre API must allow skipping consent for first-party clients.
6. MCP clients: pre-register CIMD/manual clients where possible; otherwise DCR. Expect the consent screen on first connect per user per client.
7. Post-login Action: keep adding `email`; it will run for `oauth2-token-exchange` too.

---

## 6. Decision table — Auth0-as-AS (direct) vs OAuth-proxy with Auth0 upstream

| Aspect | A. Auth0 is the AS for MCP clients (Auth for MCP) | B. MCP server is the AS (proxy); Auth0 upstream via one RWA |
|---|---|---|
| Standards coverage seen by MCP clients | RFC 8414 metadata: yes (verified). RFC 7591 DCR: yes (open, 5 rps, third-party strict). CIMD: yes (manual admin import). RFC 8707: yes with compatibility profile (`audience` wins). RFC 9728: **you** host PRM. PKCE S256 required. | Whatever your proxy implements (DCR, PKCE, PRM, resource). Auth0 only sees a normal confidential client. |
| Tenant admin must | Enable resource profile + iss flag; enable CIMD and/or DCR; promote connections to domain level; create MCP API + default third-party permissions; create Custom API client, OBO toggle, user-delegated grant; set "skip consent" on Liebre API; keep Liebre API `Allow Offline Access` for MCP API if refresh tokens wanted (on MCP API). | Create one Regular Web App with fixed callback, confidential auth, rotation/expiry; ensure Liebre API has `Allow Offline Access` and skip-consent. Nothing else. |
| Token to Liebre API | Via OBO exchange per user (cache for its lifetime; 2/8/30 rps by tier; add-on lifts). `sub` preserved, `act` added, `email` via Action. | The Auth0 access token obtained upstream (24 h) plus refresh token; no exchange. |
| User consent UX | Consent screen on first use per user per MCP client (cannot be skipped; API scopes only). | None for the Auth0 hop if Liebre API skips consent (first-party). Your proxy decides its own consent UX. |
| Loopback clients (Claude Code, Codex, Cursor) | DCR per-instance registration with exact `http://localhost:<port>/callback` should match exactly; CIMD static docs have the port problem; **loopback port-agnostic matching by Auth0 unverified**. | Your proxy can implement RFC 8252 loopback rules exactly as you like. |
| ID token / `email` / `/userinfo` for the MCP client | Not available to strict third-party clients ("Planned for a future release"); MCP server learns identity from the access token (`sub`, custom `email` claim). | MCP client gets whatever your proxy issues; the proxy has the full Auth0 ID token/userinfo. |
| Refresh tokens for MCP clients | Auth0 refresh tokens (must expire; rotation default for public third-party apps; 200 active per user per app). | Issued and stored by your proxy; Auth0 refresh tokens held server-side. |
| What it breaks / risks | Open DCR endpoint (Tenant ACL is Enterprise-only); every DCR client consumes an application slot (limit impact unverified); strict-mode gaps (no OIDC, no logout endpoint, exact URLs); OBO rate limit on Free is 2 rps; two-tenant config drift (dev/prod) grows. | You own an authorization server (key management, DCR storage, PKCE, replay/CSRF, token revocation) and its security review; MCP spec expects the AS to be discoverable via PRM, which your proxy must publish; no Auth0 audit trail of which MCP client acted (only your logs). |
| Plan/tier constraints seen | Custom Token Exchange (if used instead of OBO) Professional+; Tenant ACL Enterprise; private_key_jwt CIMD clients Enterprise; DCR plan gate unverified. | None beyond ordinary limits (Free: 300 auth requests/min, /oauth/token 30/s). |
| Auth0's own position | This is the pattern Auth0 documents and ships samples for (Python included). | Not documented by Auth0 as an MCP pattern; it is ordinary OIDC RP usage. |

## 7. "Could not verify" register (consolidated)
1. DCR plan gating (third-party claim: Professional/Enterprise minimum).
2. Auth0's redirect-URI matching for loopback (`127.0.0.1`/`localhost`, port-agnostic) for DCR/CIMD clients.
3. `prompt=none` success for strict third-party apps after a stored consent grant.
4. Default value of `resource_parameter_profile` for newly created tenants (docs conflict).
5. Multiple `resource` values in one request.
6. Whether `registration_endpoint` / CIMD / iss flags in AS metadata reflect tenant toggles.
7. RFC 8414 endpoint on custom domains (untested).
8. Whether DCR-created clients count against the applications entity limit.
9. Explicit statement that OBO is available on Free (inferred from the rate-limit table).
10. Post-login Action execution on `refresh_token` grants (not read on a fetched page).
11. Which MCP hosts (claude.ai, Claude Code, Codex, Cursor, ChatGPT) send CIMD client IDs vs DCR (client-side question).
12. How `offline_access` is displayed on the strict third-party consent dialog.

## 8. Source index (all fetched 2026-09-08)
- Live metadata: https://auth0.auth0.com/.well-known/oauth-authorization-server , https://auth0.auth0.com/.well-known/openid-configuration , https://samples.auth0.com/.well-known/oauth-authorization-server
- Auth for MCP: https://auth0.com/ai/docs/mcp/intro/overview , https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server , https://auth0.com/ai/docs/mcp/get-started/call-your-apis-on-users-behalf , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/dynamic-client-registration , https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/manual-cimd-registration , https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile , https://auth0.com/ai/docs/mcp/guides/test-your-mcp-server-with-mcp-inspector , https://auth0.com/blog/auth0-auth-for-mcp-servers-generally-available/ , https://auth0.com/blog/cimd-vs-dcr-mcp-registration/ , https://auth0.com/changelog , https://github.com/auth0-samples/auth0-ai-samples/tree/main/auth-for-mcp , https://github.com/auth0/auth0-api-python , https://github.com/auth0/auth0-mcp-server
- DCR / third-party / CIMD: https://auth0.com/docs/get-started/applications/dynamic-client-registration , https://auth0.com/docs/api/authentication/dynamic-application-client-registration/dynamic-application-registration , https://auth0.com/docs/get-started/applications/third-party-applications , https://auth0.com/docs/get-started/applications/third-party-applications/security-controls , https://auth0.com/docs/get-started/applications/third-party-applications/configure-third-party-applications , https://auth0.com/docs/get-started/applications/third-party-applications/troubleshooting , https://auth0.com/docs/get-started/applications/third-party-applications/permissive-mode , https://auth0.com/docs/get-started/applications/confidential-and-public-applications/user-consent-and-third-party-applications , https://auth0.com/docs/troubleshoot/product-lifecycle/deprecations-and-migrations/migrate-to-enhanced-security-third-party-applications , https://auth0.com/docs/get-started/auth0-overview/create-applications/register-applications-with-cimd , https://auth0.com/docs/secure/tenant-access-control-list , https://auth0.com/docs/authenticate/login/configure-silent-authentication , https://apievangelist.com/2026/07/26/auth0-right-endpoint-still-asks-permission/
- Tenant / resource / audience: https://auth0.com/docs/get-started/tenant-settings , https://auth0.com/docs/api/management/v2/tenants/patch-settings , https://auth0.com/docs/secure/tokens/access-tokens/get-access-tokens , https://support.auth0.com/center/s/article/How-to-stop-getting-JWEs-when-JWT-is-required , https://community.auth0.com/t/rfc-8707-implementation-audience-vs-resource/188990 , https://community.auth0.com/t/provide-a-well-known-oauth-authorization-server-for-mcp-authorization/187508 , https://github.com/microsoft/vscode/issues/274226
- Tokens: https://auth0.com/docs/secure/tokens/refresh-tokens , https://auth0.com/docs/secure/tokens/refresh-tokens/get-refresh-tokens , https://auth0.com/docs/secure/tokens/refresh-tokens/use-refresh-tokens , https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation , https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation , https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-expiration , https://auth0.com/docs/api/authentication/revoke-refresh-token/revoke-refresh-token , https://auth0.com/docs/secure/tokens/access-tokens/update-access-token-lifetime , https://auth0.com/docs/get-started/apis/api-settings , https://auth0.com/docs/api/management/v2/resource-servers/post-resource-servers , https://auth0.com/docs/api/management/v2/clients/post-clients , https://auth0.com/docs/api/management/v2/client-grants/post-client-grants , https://auth0.com/docs/secure/tokens/json-web-tokens/create-custom-claims , https://auth0.com/docs/customize/actions/triggers/post-login/api-object , https://auth0.com/docs/api/authentication/user-profile/get-user-info , https://auth0.com/docs/secure/attack-protection/state-parameters , https://auth0.com/docs/authenticate/login/max-age-reauthentication , https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow/add-login-auth-code-flow , https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce/call-your-api-using-the-authorization-code-flow-with-pkce , https://auth0.com/docs/get-started/applications/confidential-and-public-applications , https://auth0.com/docs/get-started/applications/application-grant-types
- Exchange: https://auth0.com/docs/secure/call-apis-on-users-behalf/on-behalf-of-token-exchange , https://community.auth0.com/t/unable-to-get-a-token-on-behalf-of-a-user-client-must-be-a-custom-api-client-with-resource-server-id/200896 , https://auth0.com/docs/authenticate/custom-token-exchange , https://auth0.com/blog/developers-guide-flexible-token-exchange-auth0/ , https://auth0.com/docs/secure/tokens/token-vault , https://auth0.com/docs/get-started/apis/set-logical-api , https://community.auth0.com/t/access-token-for-multiple-apis/62092
- Rate limits / limits / tenants: https://auth0.com/docs/troubleshoot/customer-support/operational-policies/rate-limit-policy/rate-limit-configurations/free-public , https://auth0.com/docs/troubleshoot/customer-support/operational-policies/rate-limit-policy/rate-limit-configurations/essentials-professional-b2b , https://auth0.com/docs/troubleshoot/customer-support/operational-policies/rate-limit-policy/rate-limit-configurations/enterprise-public , https://auth0.com/docs/troubleshoot/customer-support/operational-policies/entity-limit-policy , https://auth0.com/docs/get-started/auth0-overview/create-tenants/set-up-multiple-environments , https://auth0.com/docs/get-started/auth0-overview/create-tenants , https://auth0.com/docs/customize/custom-domains , https://auth0.com/docs/get-started/applications/application-settings , https://auth0.com/docs/get-started/applications/wildcards-for-subdomains , https://auth0.com/pricing
- Python: https://pyjwt.readthedocs.io/en/stable/api.html , https://pyjwt.readthedocs.io/en/stable/usage.html , https://auth0.com/docs/quickstart/backend/python/interactive , https://pypi.org/pypi/PyJWT/json , https://pypi.org/pypi/auth0-api-python/json
