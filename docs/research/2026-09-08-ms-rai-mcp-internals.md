> Research snapshot captured 2026-09-08 while planning luca-mcp. Point-in-time; re-verify before relying on a specific version, URL, or setting. Source: read-only inspection of the sibling repo ant-rai (main @ 66773a5c3) and its installed fastmcp 2.14.5.

# Brief: ms-rai MCP + OAuth internals (input for the luca-mcp design)

Repo: `/Users/anand/scratch/ant-rai`, branch `main` at `66773a5c3` (clean, checked 2026-09-08). All paths are relative to the repo root. Nothing was edited or executed against databases or servers. Where I inferred behaviour from source rather than observing it at runtime, I say so. Library facts marked "installed package" were verified by reading `.venv/lib/python3.12/site-packages/`.

## 0. Headline findings

1. **Framework**: both MCP servers use jlowin's `fastmcp` **2.14.5** (uv.lock:1586-1588) on top of the official `mcp` SDK **1.26.0** (uv.lock:3260-3262). pyproject pins `fastmcp>=2.14.0` (pyproject.toml:13). Auth stack: `authlib` 1.6.6, `python-jose` 3.5.0 (HS256), `itsdangerous` (Starlette SessionMiddleware), `cryptography` (Fernet). Also present but unused for this purpose: `pyjwt` 2.11.0 (transitive).
2. **The OAuth authorization server exists and is complete enough for Claude Code** (DCR, PKCE S256, code exchange, refresh rotation, revocation, RFC 8414 metadata), **but no token is ever verified on the MCP path.** `http_app()` is called without `auth=`, `require_scope()` has zero callers outside tests, and no main.py middleware inspects `/mcp*`. The `.mcp.json` entry for luca-management has no auth block. In practice Claude Code connects unauthenticated because the server never returns 401 / `WWW-Authenticate`, so the OAuth flow is never triggered. The only remaining gate would be Cloud Run ingress/IAM, which I cannot verify from the repo.
3. **Tool handlers have no identity context.** Writes stamp fixed strings (`created_by="mcp_prompt_engineering"`, `"mcp_luca_management"`). The design spec's "inject user_email for audit" and "check user is_active" steps were never implemented (the plan deferred transport-level auth, see §8).
4. **DCR is process-local memory.** Registered `client_id`s live in a Python set; a client registered on Cloud Run instance A is "Unknown client_id" on instance B and after any restart. Codes and refresh tokens are in Postgres and survive.
5. **The stdio accounting MCP talks to the Liebre Postgres directly via SQLAlchemy DAOs, not the Liebre HTTP API.** The HTTP client (`utils/liebre_api`) authenticates as a **machine principal via Auth0 client-credentials**, with no user identity forwarded. rai's own API bearer check is a presence-only placeholder; user identity reaches rai only through a `User-Id` header set by the ant-liebre-api proxy.
6. **The `mcp_access` JWT is not usable against ant-liebre-api and does not map to any stored Liebre credential.** It is a rai-local HS256 token; the only Liebre credential rai holds is the shared per-environment Auth0 M2M client. Details in §2.7.

## 1. `src/mcp_prompts/` — the "luca-management" HTTP MCP

### 1.1 Server construction

`src/mcp_prompts/server.py:8-46` (verbatim, trimmed instructions):

```python
from fastmcp import FastMCP
...
# Initialize DAOs at module level (singleton per process)
prompt_dao = PromptDAO()
prompt_history_dao = PromptHistoryDAO()
template_dao = GlobalTemplateDAO()
guide_dao = GuideDAO()
config_variable_dao = ConfigVariableDAO()
config_variable_override_dao = ConfigVariableOverrideDAO()
acl_dao = ACLBusinessDAO()

# Create the FastMCP server
mcp = FastMCP(
    "luca-management",
    instructions=(
        "You are a prompt engineering assistant. ..."
    ),
)
```

No `auth=` argument. fastmcp would also accept an auth provider from the env var `FASTMCP_SERVER_AUTH` (installed package `fastmcp/server/server.py:243-245`, `fastmcp/settings.py:300`); no `FASTMCP_*` variable appears in any env file, Dockerfile, CI file, or doc in the repo. Cloud Run runtime env is outside the repo, so treat "no env-injected auth" as very likely but unverified.

Tool modules each export `register(mcp, *daos)` and are wired at `server.py:49-61`. DAOs are module-level singletons sharing the app's `PostgresConnection` pools (`src/database/psql_connection.py:117-330`, one engine per database name, default `DB_NAME` = `rai_dev`).

### 1.2 Mount, transport, lifespan

`src/main.py:1534-1542` and `1576-1580`:

```python
# Mount MCP prompt engineering sub-app FIRST (prefix match priority)
from mcp_prompts.server import mcp as mcp_prompts  # noqa: E402

# WS5 6.1: stateless HTTP so an Mcp-Session-Id issued by instance A isn't
# rejected when the load balancer routes the next call to instance B. Safe here:
# the exposed tools are request-scoped API passthroughs (no cross-call MCP
# session state).
mcp_prompts_app = mcp_prompts.http_app(path="/", stateless_http=True)
app.mount("/mcp/prompts", mcp_prompts_app)
...
mcp_app = mcp.http_app(path="/", stateless_http=True)
app.mount("/mcp", mcp_app)
```

`src/main.py:1681-1693`:

```python
# Wrap your existing lifespan so FastMCP's lifespan runs too
_original_lifespan = app.router.lifespan_context

@asynccontextmanager
async def _combined_lifespan(app_: FastAPI):
    async with _original_lifespan(app_):
        async with mcp_prompts_app.lifespan(app_):
            async with mcp_app.lifespan(app_):
                yield

app.router.lifespan_context = _combined_lifespan
```

The second MCP app is auto-generated from the FastAPI OpenAPI (`src/main.py:1546-1573`): `FastMCP.from_fastapi(app=app, name="Ant RAI MCP", route_maps=[...])` publishing every `/api/v1/*` route as a tool, with explicit `MCPType.EXCLUDE` for config-variable writes and accounting-profile mutations.

Transport facts (installed package, `fastmcp/server/`):

| Aspect | Value | Evidence |
|---|---|---|
| Transport | Streamable HTTP (`transport="http"` default) | `server.py:2586-2617` |
| Statefulness | `stateless_http=True` → `StreamableHTTPSessionManager(stateless=True)`, new transport per request, no `Mcp-Session-Id` needed | `http.py:291-297` |
| `json_response` | not passed → default `False` → responses stream as SSE | `http.py:262`, `server.py:2624` |
| Route inside sub-app | `Route("/", endpoint=streamable_http_app)` with **no** `RequireAuthMiddleware` because `auth is None` | `http.py:302-337` |
| Trailing slash | Starlette `Mount` compiles `path + "/{path:path}"` (installed `starlette/routing.py:395`, Starlette 0.50.0), so only `/mcp/prompts/...` matches. A request to `/mcp/prompts` (no slash) falls through to `Mount("/mcp")`, which matches with `path="prompts"`, and the OpenAPI MCP app 404s it. That is why `.mcp.json:56` uses `https://dev-ms-rai-92624805037.us-central1.run.app/mcp/prompts/` with the slash. Inferred from source and mount order, not runtime-tested. |
| Mount order | `/mcp/prompts` must be mounted before `/mcp` (prefix collision), documented in the design spec | `docs/superpowers/specs/2026-03-16-mcp-prompt-engineering-design.md:63` |
| Lifespan | Both sub-app lifespans are chained inside the FastAPI lifespan (required to start the session manager task group) | `main.py:1686-1690` |
| Stateless commit | `c0b8ba94a 2026-07-03 feat(scale): stateless MCP sub-apps ... (WS5)` | git log |

### 1.3 Auth as implemented (end to end)

`src/mcp_prompts/auth.py` in full:

```python
"""MCP authentication middleware.

Validates Bearer tokens and enforces scope-based access control.
"""
from __future__ import annotations
from typing import Optional
from auth.jwt_handler import McpAccessJwt, decode_mcp_access_jwt
from utils.logger import Logger

logger = Logger(name="mcp_prompts.auth")

def validate_mcp_token(token: str) -> Optional[McpAccessJwt]:
    """Validate an MCP access token. Returns claims or None."""
    claims, status = decode_mcp_access_jwt(token)
    if status != "valid":
        return None
    return claims

def check_scope(token_scope: str, required_scope: str) -> bool:
    """Check if token's scope string includes the required scope."""
    granted = set(token_scope.split())
    return required_scope in granted

def require_scope(token: str, scope: str) -> McpAccessJwt:
    """Validate token and check scope. Raises ValueError on failure."""
    claims = validate_mcp_token(token)
    if claims is None:
        raise ValueError("Invalid or expired access token")
    if not check_scope(claims.scope, scope):
        raise ValueError(f"Insufficient scope. Required: {scope}")
    return claims
```

Call graph, verified by grep over `src/`:

- `require_scope` / `validate_mcp_token`: referenced only by `tests/mcp_prompts/test_auth.py`. No tool imports them. The `token` parameter would have to be passed explicitly by the caller; nothing reads the `Authorization` header.
- Middleware stack in `main.py:910-931` is `SessionMiddleware`, `MetricsMiddleware`, `AccountingFirmIdWarningMiddleware`, `TracingFlushMiddleware`, `RequestTraceMiddleware`, `CORSMiddleware`. None checks bearer tokens. The only `/mcp` path check anywhere is the SPA catch-all returning 404 (`main.py:1640-1644`).
- No usage of `fastmcp.server.auth`, `TokenVerifier`, `get_access_token`, `get_http_headers`, or `Context` anywhere in `src/`.

So the intended chain is: Claude Code → `/.well-known/oauth-authorization-server` → `/oauth/register` → browser `/oauth/authorize` → Google → `/oauth/callback` → `/oauth/token` → Bearer JWT on MCP calls. The implemented chain stops at "Bearer JWT is minted"; nothing on `/mcp/prompts/` consumes it. Identity, business and firm are therefore **not** resolved per request. Tools that need a firm take `business_id`/`accounting_firm_id` as ordinary tool arguments, with a hardcoded `"af-2"` fallback in `prompt_tools.py:414` and `utility_tools.py:121`, and `config_tools.py:79` deriving the firm from the Liebre `accounting_firm_business` link (§3.2).

### 1.4 Tool inventory (32 tools, no prompts, no resources)

All handlers return `str` (JSON via `json.dumps(..., default=str)`), wrap everything in `try/except Exception` and return `format_error(code, message)` → `{"error": true, "code": ..., "message": ...}` (`src/mcp_prompts/utils.py:87-89`). Codes used: `NOT_FOUND`, `VALIDATION_ERROR`, `INTERNAL_ERROR`. Write tools return a small confirmation object. Lists use `limit`/`offset` with `count`; long text is cut with `truncate(text, 200|500)`.

| Module | Tool | R/W | One line |
|---|---|---|---|
| prompt_tools | `list_prompts(status, model_name, limit=50, offset)` | R | Window-function query: identifier, active/latest version, model, status |
| | `read_prompt(identifier, version)` | R | Full content; active version, falls back to latest |
| | `search_prompts(query, limit=20)` | R | ILIKE on content/description with snippet |
| | `update_prompt(identifier, content, model_name, reasoning_effort, description, prompt_metadata)` | W | New DRAFT version; carries forward metadata; clears prompt cache |
| | `activate_prompt(identifier, version)` | W | Sets ACTIVE, deprecates others, clears cache |
| | `diff_prompt_versions(identifier, a, b)` | R | Unified diff + model/effort changes |
| | `list_prompt_variables(identifier, version)` | R | Extracts `$$var$$` and `{var}` placeholders |
| | `render_prompt_preview(identifier, version, variables, business_id)` | R | Multi-pass substitution; merges guides for business (firm hardcoded `af-2`) |
| | `read_prompt_history(identifier, limit=10, start_date, end_date)` | R | Recent executions with tokens/cost |
| guide_tools | `list_guides(scope_type, limit=50, offset)` | R | Latest version per identifier/scope |
| | `read_guide(identifier, scope_type, scope_id, version)` | R | Full guide content |
| | `search_guides(query, limit)` | R | ILIKE on content |
| | `update_guide(identifier, scope_type, scope_id, content)` | W | New inactive version |
| | `activate_guide(identifier, scope_type, scope_id, version)` | W | Sets active version for scope |
| | `diff_guide_versions(...)` | R | Unified diff |
| template_tools | `list_templates(status, journal_entry_type, language, limit, offset)` | R | Template summaries |
| | `read_template(template_name, version)` | R | Full template JSON + metadata |
| | `search_templates(query, limit)` | R | Search purpose/type/content |
| | `update_template(template_name, template_json, purpose, je_type, language, metadata)` | W | New DRAFT; validates `entry_lines`; carries forward metadata |
| | `activate_template(template_name, version)` | W | `template_dao.activate_version` (`template_tools.py:255`) |
| | `diff_template_versions(template_name, a, b)` | R | JSON diff |
| | `duplicate_template(template_name, version, new_name)` | W | Clone to new name |
| | `rename_template(template_name, version, new_name)` | W | `template_dao.rename_template_name` (`template_tools.py:369`) |
| utility_tools | `get_prompt_stats(identifier, days=30)` | R | Aggregated tokens/cost/executions |
| | `resolve_guide(identifier, business_id, accounting_firm_id)` | R | Inheritance resolution user → business → firm (default `af-2`) |
| config_tools | `list_config_variables()` | R | All config variable definitions |
| | `resolve_configs(business_id, accounting_firm_id)` | R | Effective values with provenance; firm derived from Liebre link when omitted |
| | `read_config_variable(key)` | R | Definition plus all overrides |
| | `upsert_config_override(key, value, business_id, accounting_firm_id)` | W | JSON value; enum validation |
| | `delete_config_override(key, business_id, accounting_firm_id)` | W | Remove override |
| acl_tools | `read_business_access(business_id)` | R | Latest ACL snapshot |
| | `update_business_access(business_id, platform_access, skills_access, workflows_access, notes)` | W | New immutable ACL row, `created_by="mcp_luca_management"` |

### 1.5 Known issues found in code and docs

- Unauthenticated endpoint (§0.2). Also true for the auto-generated `/mcp` app: fastmcp forwards the inbound client headers, including `Authorization`, into the in-process FastAPI call (installed `fastmcp/server/openapi/components.py:94`, `fastmcp/server/dependencies.py:528-573`), and rai's `require_auth` only checks that some bearer string is present (`src/endpoints/deps.py:51-64`). Config-variable and accounting-profile mutations were excluded from that app for exactly this reason (`main.py:1549-1570`).
- Metadata blanking bug fixed 2026-04-09 (`docs/plans/2026-04-09-fix-mcp-update-blanks-metadata.md`, commits `688d91a61`, `963bd66e5`).
- Firm hardcoded to `af-2` in two tools (above).
- `src/mcp_prompts/AGENTS.md:12` still says `FastMCP("prompt-engineering")` and 4 tool modules; the code has `"luca-management"` and 6 modules.
- Design spec promised an `iss` claim, an `is_active` user check, scope-per-tool enforcement and `UNAUTHORIZED`/`FORBIDDEN` error codes. None are implemented.

## 2. `src/endpoints/oauth/` — the OAuth 2.1 authorization server

### 2.1 Endpoints and RFC coverage

| Endpoint | File:line | Notes |
|---|---|---|
| `GET /.well-known/oauth-authorization-server` | `src/main.py:1509-1528` | RFC 8414 document (below) |
| `POST /oauth/register` | `src/endpoints/oauth/router.py:23-32` | RFC 7591 DCR, returns 201 |
| `GET /oauth/authorize` | `router.py:35-91` | Validates `response_type=code`, `code_challenge_method=S256`, allow-listed `client_id`; stores pending row; redirects to Google via Authlib |
| `GET /oauth/callback` | `router.py:94-136` | Google callback; domain allow-list; user upsert; mints auth code; 302 to client `redirect_uri?code&state` |
| `POST /oauth/token` | `router.py:139-174` | Form-encoded; `authorization_code` and `refresh_token` grants |
| `POST /oauth/revoke` | `router.py:177-184` | Revokes refresh token; always `{"status":"ok"}` |

| Capability | Status | Where |
|---|---|---|
| PKCE (RFC 7636), S256 only | Yes, enforced at authorize and token | `router.py:50-51`, `service.py:71-81` |
| Dynamic Client Registration (RFC 7591) | Yes, but in-memory allow-list, metadata not persisted, `redirect_uris` not enforced later | `service.py:37-69` |
| AS metadata (RFC 8414) | Yes, minimal; no `scopes_supported`, no `token_endpoint_auth_methods_supported` | `main.py:1509-1528` |
| Protected Resource Metadata (RFC 9728) | **No** `/.well-known/oauth-protected-resource`, no `WWW-Authenticate` challenge | grep |
| Resource indicators (RFC 8707) | **No** (`resource` param ignored, no `aud`) | `router.py`, `src/auth/jwt_handler.py:154-161` |
| Refresh tokens | Yes, rotated on every refresh, 30-day life, sha256-hashed at rest | `service.py:163-199`, `src/database/services/oauth_dao.py:142-168` |
| Revocation (RFC 7009) | Refresh tokens only; access JWTs cannot be revoked | `service.py:201-203` |
| Client authentication | Public client, `token_endpoint_auth_method: none`, no secret | `service.py:51-54` |
| Consent screen | **None**; straight redirect to Google | `router.py:91` |
| Scope validation | **None**; requested `scope` string is copied verbatim into the JWT | `service.py:146` |
| redirect_uri validation | Only equality between authorize and token requests; any URI accepted at authorize | `service.py:131-133` |

Verbatim metadata route (`src/main.py:1509-1528`):

```python
@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """OAuth2 authorization server metadata (RFC 8414)."""
    # Cloud Run terminates TLS at the load balancer, so request.base_url
    # reports http://. Use X-Forwarded-Proto to get the real scheme.
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://") :]
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "revocation_endpoint": f"{base_url}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
    }
```

### 2.2 DCR and client allow-list (`src/endpoints/oauth/service.py:26-69`)

```python
    def __init__(self) -> None:
        self.dao = OAuthDAO()
        self.settings = load_auth_settings()
        self._sessions = SessionManager()
        self.allowed_client_ids: set[str] = set(
            os.getenv("OAUTH_ALLOWED_CLIENT_IDS", "claude-code").split(",")
        )

    def validate_client_id(self, client_id: str) -> bool:
        return client_id.strip() in self.allowed_client_ids

    def register_dynamic_client(self, client_metadata: dict) -> dict:
        """RFC 7591 Dynamic Client Registration.

        Generates a client_id for the registering MCP client and
        adds it to the allow-list for the lifetime of this process.
        """
        import time

        client_id = f"mcp-{secrets.token_urlsafe(16)}"
        self.allowed_client_ids.add(client_id)

        response = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "token_endpoint_auth_method": client_metadata.get(
                "token_endpoint_auth_method", "none"
            ),
        }
        # Echo back registered metadata fields
        for field in ("redirect_uris", "grant_types", "response_types",
                      "client_name", "client_uri", "logo_uri", "scope"):
            if field in client_metadata:
                response[field] = client_metadata[field]
        return response
```

`oauth_service = OAuthService()` is a module singleton (`service.py:226`). The allow-list dies with the process; with Cloud Run at 1 worker per instance and up to 80-100 instances (`.gitlab-ci.yml:80-82`), a DCR'd client will intermittently get `400 Unknown client_id` at authorize or `{"error":"invalid_client"}` at token.

### 2.3 Authorize → Google → callback (`router.py:35-136`)

Key parts verbatim:

```python
    pending = oauth_service.create_pending_authorization(
        client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        scope=scope, state=state,
    )
    ...
    request.session["oauth_pending_id"] = str(pending.id)
    request.session["oauth_client_state"] = state

    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    google_redirect_uri = str(request.url_for("oauth_callback"))
    # Cloud Run terminates TLS, so url_for produces http://
    if request.headers.get("x-forwarded-proto") == "https" and google_redirect_uri.startswith("http://"):
        google_redirect_uri = "https://" + google_redirect_uri[len("http://") :]
    return await oauth.google.authorize_redirect(request, google_redirect_uri)
```

```python
@router.get("/callback", name="oauth_callback")
async def callback(request: Request):
    ...
    google_token = await oauth.google.authorize_access_token(request)
    userinfo = google_token.get("userinfo", {})
    email = userinfo.get("email", "")
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain not in settings.allowed_email_domains:
        raise HTTPException(403, f"Email domain '{domain}' is not allowed")

    user = oauth_service.upsert_user_from_google(userinfo)

    pending_id = request.session.pop("oauth_pending_id", None)
    client_state = request.session.pop("oauth_client_state", "")
    if not pending_id:
        raise HTTPException(400, "Missing OAuth session state")

    raw_code, redirect_uri = oauth_service.finalize_authorization(
        row_id=UUID(pending_id), user_id=user.user_id
    )
    separator = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{separator}code={raw_code}&state={client_state}"
    return RedirectResponse(url=target, status_code=302)
```

Mechanics worth copying or avoiding:

- The pending authorization is a Postgres row (`oauth_authorization_code`, `code` NULL until finalized). Its id and the client's `state` ride in the **Starlette signed session cookie** (`SessionMiddleware`, secret = `jwt_secret_key`, `main.py:914`). Authlib's own Google `state` also lives in that cookie. This is what makes the browser hop instance-agnostic. Cookie defaults are Starlette's: name `session`, SameSite lax, not `https_only`.
- `finalize_authorization_code` stores `sha256(code)` and `expires_at = now + 300s` (`oauth_dao.py:84-105`). Codes are single-use via `used` flag (`oauth_dao.py:129-138`).
- Identity: `SessionManager.upsert_user_from_google` (`src/auth/session_manager.py:67-127`) upserts into `users` by email first, then `google_id`, creating `user_id = f"us-{uuid4()}"`, `role=normal`. Same table as the dashboard. There is no consent page and no per-client grant record.
- The Google access/refresh tokens returned in this MCP flow are **discarded**: the callback never calls `SessionManager.create_session`, which is the only code path that stores (Fernet-encrypted) Google tokens, and it is used by the dashboard flow only (`src/endpoints/auth/service.py:393-398`).
- The Google client must have `https://<service>/oauth/callback` registered (plan Task 17, `docs/superpowers/plans/2026-03-16-mcp-prompt-engineering.md:3153-3160`).

### 2.4 Token endpoint and token format

`service.py:113-161`:

```python
    def exchange_code_for_tokens(self, *, code, code_verifier, client_id, redirect_uri):
        row = self.dao.get_valid_code(code)
        if not row: ...return None
        if row.client_id != client_id: ...return None
        if row.redirect_uri != redirect_uri: ...return None
        if not self.verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
            ...return None
        self.dao.mark_code_used(row.id)

        access_token = create_mcp_access_jwt(
            user_id=row.user_id,
            email=self._get_user_email(row.user_id),
            scope=row.scope or "",
            expires_in_minutes=self.settings.jwt_access_token_expire_minutes,
        )
        raw_refresh, _ = self.dao.create_refresh_token(
            user_id=row.user_id, client_id=client_id, scope=row.scope,
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self.settings.jwt_access_token_expire_minutes * 60,
            "refresh_token": raw_refresh,
        }
```

Refresh (`service.py:163-199`) looks up the hashed token, checks `client_id`, revokes it, mints a new access JWT and a new refresh token. Errors are `{"error":"invalid_grant"}` 400, `{"error":"invalid_client"}` 400, `{"error":"unsupported_grant_type"}` 400 (`router.py:146-174`). `scope` is not echoed in the token response.

`src/auth/jwt_handler.py:131-229` (the "token verifier" pair):

```python
@dataclass(frozen=True, slots=True)
class McpAccessJwt:
    user_id: str
    email: str
    scope: str
    token_type: str
    exp: int
    iat: int

def create_mcp_access_jwt(*, user_id, email, scope, expires_in_minutes) -> str:
    settings = load_auth_settings()
    if not settings.jwt_secret_key:
        raise RuntimeError("JWT_SECRET_KEY is required")
    now = _utc_now()
    exp = now + timedelta(minutes=max(1, int(expires_in_minutes)))
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "scope": scope,
        "token_type": "mcp_access",
        "iat": _to_int_ts(now),
        "exp": _to_int_ts(exp),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def decode_mcp_access_jwt(token: str) -> Tuple[McpAccessJwt, Literal["valid", "invalid"]]:
    ...
    decoded = jwt.decode(raw, settings.jwt_secret_key,
                         algorithms=[settings.jwt_algorithm], options={"verify_exp": True})
    ...
    token_type = str(decoded.get("token_type") or "").strip()
    if token_type != "mcp_access":
        return (McpAccessJwt(...empty...), "invalid")
    ...
```

| Property | Value |
|---|---|
| Format | JWT, HS256 (`src/auth/config.py:84`), `python-jose` |
| Claims | `sub` (user_id `us-…`), `email`, `scope` (space-separated, unvalidated), `token_type: "mcp_access"`, `iat`, `exp`. No `iss`, `aud`, `jti`, `client_id` |
| Access lifetime | `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, default 30, shared with dashboard session JWTs |
| Refresh lifetime | 30 days, opaque `secrets.token_urlsafe(48)`, sha256 hashed, rotated |
| Code lifetime | 5 min, single use, sha256 hashed |
| Signing key | `JWT_SECRET_KEY` env, else `sha256("ant-rai:jwt:<google_client_id>:<google_client_secret>")`, else a **hardcoded default string** `DEFAULT_JWT_SECRET_KEY` (`src/auth/config.py:19,28-32,106-109`) |
| Session JWT vs MCP JWT | Same key; the `token_type` check is the only thing that stops a dashboard cookie JWT being accepted as an MCP token (`tests/auth/test_jwt_handler_mcp.py:41-52`) |

### 2.5 Storage

Tables in the rai database (`DB_NAME`, default `rai_dev`; commit `d3b4e7839` fixed a hardcoded `ant_rai`). Migration `src/database/migrations/versions/e968dffecc0d_add_oauth_tables.py`.

`src/database/tables/oauth_authorization_code.py:23-53`: `id UUID pk`, `code String(128) nullable indexed` (hash), `user_id Text FK users.user_id nullable`, `client_id String(100)`, `redirect_uri String(500)`, `code_challenge String(256)`, `code_challenge_method String(10) default 'S256'`, `scope String(500)`, `state String(256)`, `expires_at tz`, `used bool`, `created_at`.

`src/database/tables/oauth_refresh_token.py:23-50`: `id UUID pk`, `token_hash String(128) unique`, `user_id FK users`, `client_id`, `scope`, `expires_at`, `is_revoked`, `created_at`, `last_used_at`.

DAO `src/database/services/oauth_dao.py` (263 lines): `create_authorization_code`, `get_authorization_code_by_state`, `finalize_authorization_code`, `get_valid_code`, `mark_code_used`, `create_refresh_token`, `get_refresh_token`, `revoke_refresh_token`, `revoke_all_user_tokens`, `touch_refresh_token` (never called), `cleanup_expired_codes` (never scheduled). Clients are **not** stored.

### 2.6 Tests

`tests/endpoints/oauth/test_oauth_service.py` (PKCE, allow-list, DCR), `tests/endpoints/oauth/test_oauth_router.py` (route paths only), `tests/auth/test_jwt_handler_mcp.py`, `tests/mcp_prompts/test_auth.py`, `tests/database/services/test_oauth_dao.py`, `tests/database/tables/test_oauth_models.py`. No end-to-end test exercises the MCP endpoint with or without a token.

### 2.7 Is the `mcp_access` token usable against ant-liebre-api? No, and it maps to nothing Liebre-side

Direct answer to the team lead's question:

1. **Different issuer, key and audience.** The token is minted by rai with `python-jose` HS256 under rai's `JWT_SECRET_KEY` (`src/auth/jwt_handler.py:141-164`). From rai's own client code, ant-liebre-api expects an **Auth0** access token obtained with `grant_type=client_credentials` and `audience=<LIEBRE_API_SERVICE_URL_*>` (`src/utils/liebre_api/auth.py:74-90`). The rai JWT carries no `iss`, no `aud`, and is not signed by Auth0, so Liebre cannot validate it. (Liebre's actual verifier lives in the sibling repo and was not read; this conclusion rests on what rai sends today.)
2. **No server-side mapping to a Liebre credential or JWT.** The persisted state for the MCP OAuth flow is only `oauth_authorization_code` and `oauth_refresh_token` (user_id, client_id, scope, hashes, expiry) plus the `users` row (google_id, email, role). Nothing per user references Auth0, Liebre, or any downstream token. The Google tokens received during `/oauth/callback` are dropped (see §2.3). The `user_sessions` table with encrypted Google tokens belongs to the dashboard cookie flow, not to this OAuth server, and even those are Google tokens, not Liebre tokens.
3. **The only Liebre credential rai holds is shared and environment-wide**: the Auth0 M2M client configured by `LIEBRE_API_AUTH0_{DOMAIN,CLIENT_ID,CLIENT_SECRET}_{DEV,STG,PROD}`, fetched and cached in-process per environment (`auth.py:18-22, 178-211`). Every `LiebreAPI` call uses it, with an `Accounting-Firm-ID` header and **no user identity** (`client.py:273-278, 880`). The `LiebreAPI(auth_token=...)` override that could carry a user token exists but has no callers.
4. **In practice the MCP tools do not call Liebre HTTP at all.** luca-management tools hit rai's own Postgres via DAOs (plus a read-only reflection of Liebre's `accounting_firm_business` table for firm derivation); the accounting MCP hits the Liebre Postgres directly via DAOs. So the rai token never needs to reach Liebre, which is also why nobody noticed that it cannot.

Implication for luca-mcp: whatever token luca-mcp issues to Claude Code, it will have to either (a) hold a Liebre-accepted credential itself (M2M plus a trusted `User-Id`/firm header contract with Liebre, mirroring the existing Liebre→rai proxy convention), or (b) obtain a per-user Liebre/Auth0 token through a flow Liebre supports. rai proves only (a) minus the user header.

## 3. `src/auth/` and the platform auth model

### 3.1 Two unrelated auth systems inside rai

1. **Dashboard auth** (`src/auth/*`, `src/endpoints/auth/*`): Google OIDC via Authlib. `GET /api/v1/auth/login` → Google → `GET /api/v1/auth/callback` creates a `user_sessions` row with Fernet-encrypted Google tokens and sets an HttpOnly cookie `ant_rai_session` holding a short-lived `SessionJwt` (`sub`, `sid`, `email`, `role`, `iat`, `exp`; `src/auth/jwt_handler.py:36-60`). `RequireGoogleUser` (`src/endpoints/auth/deps_google.py:24-92`) validates the cookie, loads the session and user (`is_revoked`, `expires_at`, `is_active`), optional role check, rotates the JWT near expiry. Roles: `normal | pro | admin` (`src/database/tables/user.py:18-23`), managed in DB, not from Google. Domain allow-list `ALLOWED_EMAIL_DOMAINS` default `liebre.ai,lkmx.io` (`src/auth/config.py:14`).
2. **API auth** (`src/endpoints/deps.py:10-80`): `HTTPBearer(auto_error=False)`; `RequireAuth.__call__` only raises 401 if no bearer is present and returns `AuthContext(token=..., role=UserRole.normal)`. Verbatim comment: `# Placeholder behavior: only ensure a Bearer token is present.` Role requirements are accepted but not enforced.

### 3.2 Where identity, business and firm actually come from

- **User**: `User-Id` header, "forwarded by the ant-liebre-api proxy" (`src/endpoints/deps.py:234-285`, `resolve_user_id`, `resolve_user_id_required`). rai has no user↔business membership table; the only membership-like tables are Liebre-owned.
- **Business**: path parameter under `/api/v1/businesses/{business_id}` (`main.py`, `README.md:17`). `require_platform_access` is stubbed out with an early `return` (`deps.py:83-114`).
- **Firm**: `Accounting-Firm-Id` header (`deps.py:117-182`), legacy `accounting_firm_id` query fallback, default `af-2`; a rollout flag `ACCOUNTING_FIRM_ID_REQUIRE_HEADER` makes it mandatory. `AccountingFirmIdWarningMiddleware` adds a `Warning: 299` header on `/api/` responses lacking it (`main.py:225-262`). Canonical firm derivation reads the read-only Liebre table `accounting_firm_business` (`src/database/tables/liebre_dev/accounting_firm.py:26-36`, composite PK `(accounting_firm_id, business_id)`, "effectively 1:1") through `src/rai/config/firm_resolution.py:78-90`, cached 300 s per instance (`FIRM_RESOLUTION_CACHE_TTL_SECONDS`), with a strict mode `CONFIG_FIRM_RESOLUTION_STRICT`.
- **Service-to-service**: Liebre → rai is bearer-presence only plus `User-Id`/`Accounting-Firm-Id` headers. rai → Liebre is Auth0 client-credentials (§6).

## 4. `src/mcp_accounting/` — the stdio "accounting" MCP

### 4.1 Architecture

- `FastMCP("accounting", instructions="You are an AI accountant. Before starting any work, call the 'accounting_guidelines' prompt ... then ... 'accounting_navigation' ...")` (`src/mcp_accounting/server.py:22-29`). Run with `python -m mcp_accounting` → `mcp.run()` = **stdio** (`__main__.py`). `.mcp.json:41-53` launches it from the repo venv with `PYTHONPATH=src`, `AI_ACCOUNTING_BUSINESS_ID=bu-830`, `AI_ACCOUNTING_FIRM_ID=af-2`.
- **Data access is direct SQL against the Liebre database**, not HTTP: `AccountingConfig.database_name` defaults to `LIEBRE_DATABASE_NAME` or `liebre_dev` (`config.py:54-56`), and `create_dao_registry` instantiates 20 DAOs from `database.services.liebre_dev` against it (`config.py:85-111`). Requires `DB_*` connection vars. `firm_id` is loaded but not used by the handlers I read; `business_id` is closed over by every handler and never accepted from the agent (`src/mcp_accounting/AGENTS.md:28-29`).
- Registration pattern: each tools module exports `register(mcp, config: AccountingConfig, daos: DAORegistry)`; handlers are closures (`journal_entry_tools.py:32-35`).
- Every handler: `try: ... return formatter(...) except Exception as e: return format_error(str(e))` where `format_error` returns the plain string `"ERROR: {message}"` (`formatters.py:17-19`). Validators raise `ValueError` with agent-facing wording (`validation/common.py`, `journal_entry_validators.py`, `period_validators.py`, `ledger_account_validators.py`): allowed JE status transitions, period must be open, accounts active and leaf, lines balance, type hierarchy exists, currency configured, `validate_business_scope` on every fetched record.

### 4.2 Response-shaping conventions (`src/mcp_accounting/formatters.py`)

- Single record → key/value block; list → markdown table with a `## Title (N)` heading; empty → `"No … found."`.
- UUIDs printed in full (`short_uuid` name is historical, `formatters.py:22-26`).
- Money: `_dec(val, scale)` → fixed decimals, default 4; balances 2; FX 6; JE line rate 8. `None` → `"0"`. No thousands separators, no currency symbol.
- Truncation in tables: JE description `[:40]`, statement-line description `[:30]`, reference `[:15]`, entry-line description `[:25]`, activity description `[:30]`.
- Pagination: `limit`/`offset` params; defaults `limit=50` (10 tools), `100`, `200`, `24` (monthly trend) in isolated cases. `list_journal_entries` accepts one filter at a time (`journal_entry_tools.py:46-48`).
- Enums via `.value`; dates via `isoformat()`; tree view for COA with NBSP indentation and Leaf column (`formatters.py:109-167`).
- Optional strings default to `""`, optional ints to `0`, JSON payloads (entry lines) arrive as a **JSON string** parameter (`lines: str = "[]"`).

### 4.3 Prompts

- `accounting_guidelines` (`src/mcp_accounting/prompts/guidelines.py`): double-entry rules and D/C natures; NIF standards list (A-1, A-2, B-3, B-15, C-3, C-9, D-3, D-4); SAT Código Agrupador 100–800 levels; IVA 16%/0%, ISR 30%, DIOT, CFDI types and PUE/PPD; status lifecycles for JEs, periods, accounts, statement lines; enum constraints (create only, never update/delete); currency/FX rules (Banxico, NIF B-15); best practices; error-message glossary.
- `accounting_navigation` (`src/mcp_accounting/prompts/navigation.py`): getting-started sequence; workflows for record a transaction, bank reconciliation, close a period, COA management, financial position, search, reverse/duplicate; a tool reference that says "71 tools" (stale, code has 76: it omits the 4 statement tools and the non-leaf audit).

### 4.4 Complete tool inventory (76 tools; 47 read, 29 write)

| Domain | Tool | R/W | One line |
|---|---|---|---|
| Reference | `get_business_info()` | R | Business legal/commercial name, RFC, currency, numbering strategy |
| | `list_global_currencies()` | R | Global currency table |
| | `get_global_currency(code)` | R | One currency |
| | `get_exchange_rate(source, target, date)` | R | Official rate for a date |
| | `get_exchange_rates_for_range(source, target, start, end)` | R | Rates over a range |
| | `list_business_currencies()` | R | Currencies enabled for the business |
| | `add_business_currency(code, exchange_rate_mode)` | W | Enable a currency |
| Enums | `list_ledger_account_types()` | R | |
| | `list_ledger_account_sub_types(type)` | R | |
| | `list_ledger_account_sub_sub_types(type, sub_type)` | R | |
| | `list_journal_entry_types()` | R | |
| | `list_journal_entry_sub_types(type)` | R | |
| | `create_ledger_account_type(type)` | W | |
| | `create_ledger_account_sub_type(type, sub_type)` | W | |
| | `create_ledger_account_sub_sub_type(type, sub_type, sub_sub_type)` | W | |
| | `create_journal_entry_type(type)` | W | |
| | `create_journal_entry_sub_type(type, sub_type)` | W | |
| Chart of accounts | `list_ledger_accounts(status, type, sub_type, hierarchical, limit, offset)` | R | Flat or tree table |
| | `get_ledger_account(account_number)` | R | |
| | `get_child_accounts(parent_account_number)` | R | |
| | `create_ledger_account(number, name, currency, nature, type, sub_type, sub_sub_type, parent_account_number, cash_flow_group)` | W | Uniqueness, hierarchy, currency validated |
| | `update_ledger_account(account_number, name, status, type, sub_type, sub_sub_type, cash_flow_group)` | W | |
| Periods | `list_periods(status, limit, offset)` | R | |
| | `get_period(year_month)` | R | |
| | `create_period(year_month, status)` | W | |
| | `update_period_status(year_month, status)` | W | `not_opened→open→closed`; blocks close with pending JEs |
| Journal entries | `list_journal_entries(year_month, status, start_date, end_date, journal_entry_type, limit=50, offset)` | R | |
| | `get_journal_entry(journal_entry_id)` | R | Header + lines |
| | `create_journal_entry(date, description, currency, type, sub_type, lines)` | W | Atomic; all validations before write |
| | `update_journal_entry(journal_entry_id, description, date)` | W | Draft only |
| | `update_journal_entry_status(journal_entry_id, status)` | W | Transition table enforced |
| | `add_entry_lines(journal_entry_id, lines)` | W | Draft only |
| | `delete_entry_line(entry_line_id)` | W | Draft only |
| Banking | `list_money_accounts(status, type)` | R | |
| | `get_money_account(money_account_id)` | R | |
| | `create_money_account(id, name, currency, type, number, external_number, ledger_account_number)` | W | |
| | `update_money_account(id, name, status, ledger_account_number)` | W | |
| | `list_statement_lines(money_account_id, period_id, start_date, end_date, reconciliation_status, limit, offset)` | R | |
| | `get_statement_line(statement_line_id)` | R | |
| | `create_statement_line(money_account_id, date, amount, description, reference, period_id)` | W | |
| | `update_statement_line(statement_line_id, description, reference, reconciliation_status)` | W | |
| Statements | `list_statements(money_account_id, status, limit, offset)` | R | |
| | `get_statement(money_account_id, period_id)` | R | |
| | `create_statement(money_account_id, period_id, opening_balance, status)` | W | |
| | `update_statement(money_account_id, period_id, opening_balance, closing_balance, status)` | W | |
| Reconciliation | `create_reconciliation(statement_line_id, entry_line_id)` | W | Links and marks `auto_reconciled` atomically (commit `7d530a2d2`); refuses draft SLs |
| | `delete_reconciliation(statement_line_id, entry_line_id)` | W | |
| | `list_reconciliations_for_statement_line(statement_line_id)` | R | |
| | `list_unreconciled_entry_lines(start_date, end_date, ledger_account_number, limit)` | R | |
| Counterparties | `list_customers(status, limit, offset)` | R | |
| | `get_customer(customer_id, tax_id)` | R | By id or RFC |
| | `create_customer(legal_name, tax_id, origin, associated_account_numbers)` | W | |
| | `update_customer(customer_id, legal_name, tax_id, status, associated_account_numbers)` | W | |
| | `list_suppliers(status, limit, offset)` | R | |
| | `get_supplier(supplier_id, tax_id)` | R | |
| | `create_supplier(legal_name, tax_id, origin, country, associated_account_numbers)` | W | |
| | `update_supplier(supplier_id, legal_name, tax_id, status, country, associated_account_numbers)` | W | |
| Reporting | `get_account_balance(account_number, start_date, end_date)` | R | |
| | `get_trial_balance(year_month, as_of_date)` | R | |
| | `get_account_activity(account_number, start_date, end_date, limit)` | R | |
| Financial statements | `get_accounting_overview()` | R | One-call dashboard |
| | `get_period_summary(year_month)` | R | |
| | `get_income_statement(start_date, end_date)` | R | |
| | `get_balance_sheet(as_of_date)` | R | |
| | `get_cash_flow_statement(start_date, end_date)` | R | Uses `cash_flow_group` |
| | `get_account_balance_by_period(account_number, start_ym, end_ym)` | R | Monthly trend, limit 24 |
| | `get_iva_summary(year_month)` | R | IVA trasladado vs acreditable |
| Search | `search_ledger_accounts(query, status, limit)` | R | ILIKE on name/number |
| | `search_journal_entries(query, year_month, start_date, end_date, status, limit)` | R | |
| | `search_statement_lines(money_account_id, query, start_date, end_date, reconciliation_status, limit)` | R | |
| | `list_journal_entries_by_account(account_number, start_date, end_date, status, limit)` | R | |
| Workflow | `reverse_journal_entry(journal_entry_id, date, description)` | W | New JE with D/C swapped |
| | `duplicate_journal_entry(journal_entry_id, new_date)` | W | New draft |
| | `validate_period_for_close(year_month)` | R | Pre-flight checklist |
| | `get_reconciliation_summary(money_account_id, year_month)` | R | |
| | `get_invalid_journal_entries_non_leaf_accounts()` | R | Audit of JEs posting to parent accounts (classified by docstring; body not read in full) |

Read/write classification comes from an AST scan of DAO write calls in each handler (`create_*`, `update(`, `delete(`, `add(`, …) plus manual correction for the two template tools; the last row is the only one I did not verify line by line.

## 5. `src/mcp-tools/` — dead prototype

Single file `src/mcp-tools/main.py` (28 lines): uses the **official SDK** import `from mcp.server.fastmcp import FastMCP`, has a syntactically broken `sys.path.append()` with no argument (line 5), imports `rai.database.services.liebre_dev` which does not exist (`src/rai/database` is absent), and exposes one stub tool returning a hardcoded string. Not mounted, not referenced, not tested. Ignore it.

## 6. `src/utils/liebre_api/` and the cross-repo skill

### 6.1 Auth to ant-liebre-api: Auth0 client credentials, machine identity only

`src/utils/liebre_api/auth.py:74-90`:

```python
def _fetch_auth0_token(config: EnvironmentConfig, ...) -> Auth0Token:
    token_url = config.auth0_domain
    payload: Dict[str, Any] = {
        "client_id": config.auth0_client_id,
        "client_secret": config.auth0_client_secret,
        "audience": config.service_url,
        "grant_type": "client_credentials",
    }
    headers: Dict[str, str] = {"content-type": "application/json"}
    response = requests.post(token_url, json=payload, headers=headers, timeout=...)
```

`src/utils/liebre_api/client.py:273-278`:

```python
    def _default_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.auth_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
```

Per call, every business-scoped method adds the firm header, e.g. `client.py:880`: `headers: Dict[str, str] = {"Accounting-Firm-ID": accounting_firm_id}`. The proxy router documents the mapping from rai's inbound `Accounting-Firm-Id` to upstream `Accounting-Firm-ID` (`src/endpoints/liebre/router.py:23-26`). **No `User-Id` or user token is ever sent to Liebre**; the constructor has an `auth_token` override parameter (`client.py:105`) but no caller uses it. Whether ant-liebre-api accepts end-user Auth0 tokens is a question for the sibling repo; rai only proves the M2M path.

Token cache: per-environment in-process store with a 60 s skew (`auth.py:18-22, 59-64`), lock per env, double-checked refresh to avoid stampedes (`auth.py:214-264`). On a 401/403 the client force-refreshes once and re-sends the same request (`client.py:544-590`).

### 6.2 Settings and base URLs

`pydantic-settings` class `LiebreAPISettings` with prefix `LIEBRE_API_` (`src/utils/liebre_api/settings.py:22-62`); `LiebreAPI(environment=...)` defaults to `ENVIRONMENT` env, default `"dev"` (`client.py:118`).

| Env | `LIEBRE_API_SERVICE_URL_*` | Auth0 vars |
|---|---|---|
| dev | `https://api-dev.liebre.ai/api/v1` | `LIEBRE_API_AUTH0_DOMAIN_DEV` (full token URL, e.g. `https://dev-xxx.us.auth0.com/oauth/token`), `LIEBRE_API_AUTH0_CLIENT_ID_DEV`, `LIEBRE_API_AUTH0_CLIENT_SECRET_DEV` |
| stg | `https://api-stg.liebre.ai/api/v1` | same with `_STG` |
| prod | `https://api.liebre.ai/api/v1` | same with `_PROD` |

URL values come from `.env.rai-local` (URLs only; secrets were not read or printed). Product hosts: `app-dev.liebre.ai`, `app-stg.liebre.ai`, `app.liebre.ai`.

### 6.3 Client shape, retries, timeouts, parsing, caching

- Synchronous `requests.Session`; default `timeout_seconds=30`, `auth_timeout_seconds=30`, optional overall `deadline` (monotonic) that trims every timeout and raises `TimeoutError` when exhausted (`client.py:100-140, 260-266`).
- Retries: `max_retries=3`, exponential backoff base 1 s, cap 30 s, jitter; on `Timeout`/`ConnectionError` and on HTTP `{429,500,502,503,504}` honouring `Retry-After` (`client.py:436-437, 496-535, 593-629, 721-729`). Non-idempotent uploads pass `max_retries=0`.
- Errors: `LiebreAPIError(message, status_code, url, response_text)`; alias `HttpServiceError`; `LiebreAPIResponseMismatchError` when required fields are missing (`errors.py`). Parsing is lenient: drifted optional fields are pruned and logged rather than failing (`parsing.lenient_model_validate`, `client.py:752-840`).
- Observability: OpenTelemetry span per call (full-detail mode only), Sentry span, metrics `external.liebre.calls` / `external.liebre.latency_ms` (`client.py:322-435`).
- Cache: TTL per method name keyed by environment plus args (`cache.py:27-40`): `get_chart_of_accounts` 30 s, `get_business` 3 h, `get_money_accounts` 1 h, suppliers/customers 30 min, `get_trial_balance` 15 s, `get_last_open_period`/`get_period` 15 s, `get_fx_rate` 30 min, `get_ledger_account_journal_entries`/`get_journal_entry` 60 s, `get_cfdi_xml` 3 h.
- Endpoints used (all under `/businesses/{business_id}`): `GET .` (business), `GET chart_of_accounts/{period}`, `POST/PATCH chart_of_accounts/ledger_accounts[/{id}]`, `GET .../ledger_accounts/{id}/journal_entries`, `GET/POST/PUT/DELETE periods/{period}/journal_entries[/{id}]`, `GET periods/{period}`, `GET periods/last_open`, `GET reports/trial_balance`, `GET fx_rates/{src}/{tgt}`, `GET money_accounts`, `POST money_accounts/{id}/statements/{period}/statement_lines/bulk`, `.../statement_lines/{id}/restore`, `.../statement_lines/queue-for-auto-reconciliation`, `POST money_accounts/{id}/reconciliation/{period}/statement_lines/reconcile`, `GET suppliers`, `GET customers`, `GET cfdis/{uuid}/xml`, `POST cfdis/{uuid}/restore`, `cfdi_drafts` CRUD, `sat_archives` list/get/create/latest/types, `article_69b/{rfc}`, `article_69b/matches` (`client.py:875-2242`).

### 6.4 `.claude/skills/cross-repo-liebre/SKILL.md`

Ownership contract: Liebre owns accounting write semantics and reconcile links; rai creates JEs through Liebre, never deletes JEs (`delete_journal_entry` returns 405 in dev), and self-reconciles only in the N:1 flow. Sibling clone at `/Users/anand/scratch/ant-liebre-api` (plain checkout, read-only discipline, no venv). Everything about their code is "drift-prone by construction; re-verify in their clone before citing."

## 7. Deployment

- **Image**: `Dockerfile` builds the Svelte dashboards with Node 22, then `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, `uv sync --locked --no-dev`, Playwright Chromium, `CMD bash docker-entrypoint.sh`.
- **Process**: `hypercorn src.main:app --bind 0.0.0.0:8000 --workers 1` (`docker-entrypoint.sh:22`). Migrations are not run at boot (`DB_AUTO_MIGRATE` default 0); a Cloud Run job runs `alembic upgrade head` per release (`.gitlab-ci.yml:76-128`).
- **CI/CD**: GitHub Action `.github/workflows/version.yml` creates `vX.Y.Z` tags with `@logickernel/agileflow` and pushes them to GitLab `solutions/liebre/rai`; `.gitlab-ci.yml` builds `${REGISTRY_PATH}/ant-rai:${IMAGE_TAG}` (registry host `us-central1-docker.pkg.dev/logickernel-fleet/registry`), then `gcloud run services update ${CLOUD_RUN_SERVICE} --region=us-central1 --update-env-vars=DEPLOYED_VERSION=...`. Environments: development (auto), staging (manual), production (manual, gated on staging).
- **Hostnames**: `https://dev-ms-rai-92624805037.us-central1.run.app`, `https://stg-ms-rai-92624805037.us-central1.run.app`, `https://prod-ms-rai-92624805037.us-central1.run.app` (`src/endpoints/auth/service.py:241-243`). Service names in CI are env-scoped variables, not in the repo.
- **Scale characteristics relevant to MCP**: TLS terminates at the Cloud Run LB (hence the `X-Forwarded-Proto` fix-ups in `main.py:1516-1518` and `router.py:87-90`); up to 80-100 instances; `--session-affinity` is best-effort and only for pinned surfaces (`docs/sse-deployment-requirements.md:167-202`), which explicitly lists the MCP sub-apps as stateless since WS5 6.1. Redis (`REDIS_URL`) backs the SSE event bus and terminal-state store only; it is **not** used for MCP sessions or OAuth state.

Relevant env var names (values not recorded): `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `JWT_SECRET_KEY`, `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, `ALLOWED_EMAIL_DOMAINS`, `OAUTH_ALLOWED_CLIENT_IDS`, `TOKEN_ENCRYPTION_KEY`, `SESSION_COOKIE_*`, `DB_HOST/PORT/USER/PASSWORD/NAME`, `LIEBRE_DATABASE_NAME`, `ENVIRONMENT`, `LIEBRE_API_AUTH0_DOMAIN_{DEV,STG,PROD}`, `LIEBRE_API_AUTH0_CLIENT_ID_{…}`, `LIEBRE_API_AUTH0_CLIENT_SECRET_{…}`, `LIEBRE_API_SERVICE_URL_{…}`, `AI_ACCOUNTING_BUSINESS_ID`, `AI_ACCOUNTING_FIRM_ID`, `ACCOUNTING_FIRM_ID_REQUIRE_HEADER`, `CONFIG_FIRM_RESOLUTION_STRICT`, `FIRM_RESOLUTION_CACHE_TTL_SECONDS`, `REDIS_URL`.

## 8. Plans and docs about MCP / OAuth / luca-mcp

- `docs/superpowers/specs/2026-03-16-mcp-prompt-engineering-design.md`: the design for everything in §1-2, including the intended auth middleware (extract bearer, decode, check `is_active`, per-tool scopes, inject `user_email`), the scope table (`prompts:read|write`, `guides:read|write`, `templates:read|write`), and a Claude Code config sample with an `auth.type: oauth2` block (`:450-469`).
- `docs/superpowers/plans/2026-03-16-mcp-prompt-engineering.md`: the implementation plan. Task 8 carries the admission that explains today's gap (`:1394-1397`): `# NOTE: Auth enforcement at the MCP transport level depends on FastMCP's auth hook support. Write tools accept a _user_email parameter ... Read tools don't require auth context.` The `_user_email` parameter was never added either.
- `docs/plans/2026-04-09-fix-mcp-update-blanks-metadata.md`: metadata carry-forward fix.
- `docs/sse-deployment-requirements.md:201-202`: stateless MCP note.
- History: `bd77f1b2d 2025-12-12 "MCP of Luca service implemented"` added the `FastMCP.from_fastapi` `/mcp` mount and `.cursor/mcp.json`; `368bd80ab 2026-02-15` added `mcp_accounting`; the OAuth/luca-management series landed 2026-03-16 (`2883e3c69` … `c90fef96a`, `0d035528b`); config/ACL tools 2026-04-08 (`56037f279`).
- **No file in `docs/`, `.omc/`, or `.claude/` mentions `luca-mcp` or `luca_mcp`; `.omc/plans/` does not exist.**

## 9. What fastmcp 2.14.5 already provides (installed package, for the design decision)

- `FastMCP(..., auth=AuthProvider)`; `create_streamable_http_app` wraps the endpoint in `RequireAuthMiddleware`, adds the provider's routes, and builds the RFC 9728 resource metadata URL (`fastmcp/server/http.py:302-337`). `RequireAuthMiddleware` returns 401/403 with `WWW-Authenticate: Bearer ... resource_metadata="..."` (`fastmcp/server/auth/middleware.py:22-65`).
- Provider classes (`fastmcp/server/auth/auth.py`): `TokenVerifier` (resource server only), `RemoteAuthProvider` (verifier + RFC 9728 metadata pointing at external AS), `OAuthProvider` (full AS incl. DCR and revocation options).
- `JWTVerifier(public_key=..., jwks_uri=..., issuer=..., audience=..., algorithm=..., required_scopes=...)`; `public_key` may be a shared secret for HS256/384/512 (`fastmcp/server/auth/providers/jwt.py:165-230`). `StaticTokenVerifier` for dev.
- `OAuthProxy(...)` (`fastmcp/server/auth/oauth_proxy.py:514+`, `__init__` at ~`:642`): DCR-compliant façade over a non-DCR IdP, fixed upstream redirect, loopback redirect allow-list with wildcards (default localhost only), PKCE forwarding, consent screen (`require_authorization_consent=True`), own JWTs via `jwt_signing_key`, refresh and revocation, pluggable `client_storage: AsyncKeyValue` (default: Fernet-encrypted `DiskStore` under the fastmcp home dir, `oauth_proxy.py:813-826`). Installed `key_value.aio.stores` includes `redis`, `valkey`, `memory`, `disk`, `mongodb`, `dynamodb`, `elasticsearch`, `memcached`, `vault`.
- `GoogleProvider(client_id, client_secret, base_url, redirect_path="/auth/callback", required_scopes, allowed_client_redirect_uris, client_storage, jwt_signing_key, require_authorization_consent, extra_authorize_params)` = `OAuthProxy` preconfigured for Google, verifying via Google tokeninfo and exposing `claims` with `sub`, `email`, `name`, `picture` (`fastmcp/server/auth/providers/google.py:74-229`).
- Handler-side identity: `fastmcp.server.dependencies.get_access_token()` returns `AccessToken(token, client_id, scopes, expires_at, resource, claims)` (`dependencies.py:574`, `auth/auth.py:43-46`); also `get_http_request()`, `get_http_headers()`.
- Env-driven provider selection: `FASTMCP_SERVER_AUTH=<dotted path to AuthProvider>` (`fastmcp/settings.py:300`).

## 10. Lessons and pitfalls to carry into luca-mcp

1. **Wire the verifier into the transport, not into the tools.** rai wrote a correct `decode_mcp_access_jwt` and then never attached it. Without a 401 challenge Claude Code never starts OAuth, which is exactly the current state.
2. **Handlers must read identity from the request context** (`get_access_token()`), and stamp `created_by` from it; never hardcode.
3. **Do not reinvent the AS unless you must.** `OAuthProxy`/`GoogleProvider` already cover DCR-for-MCP-clients over a fixed Google app, dynamic loopback redirect URIs, PKCE forwarding, consent, refresh, revocation, and RFC 8414 + 9728 metadata. If a hand-rolled AS is kept, the rai one lacks RFC 9728, `iss`/`aud`, scope validation, redirect-URI enforcement, and a consent step.
4. **Persist OAuth state somewhere shared.** rai's DCR set is per process; `OAuthProxy` defaults to an encrypted on-disk store, equally wrong on Cloud Run with many instances. Use a Redis/Valkey `AsyncKeyValue` store or Postgres for clients, codes, refresh tokens, and transactions.
5. **Keep the browser hop instance-agnostic.** rai's trick of carrying the pending-authorization id and client `state` in a signed cookie, with the pending row in Postgres, works across instances; keep that property.
6. **Stateless streamable HTTP is the right default behind a load balancer** (`stateless_http=True`); it forgoes server-initiated messages and resumability, fine for request-scoped tools. Decide `json_response` explicitly.
7. **Mount at a path that ends in a slash, or serve the MCP at the app root.** With Starlette `Mount`, `/mcp` without a trailing slash is not the MCP endpoint, and a sibling mount can swallow it silently. A standalone luca-mcp can use `http_app(path="/mcp")` on its own app and avoid nested mounts.
8. **Chain the fastmcp lifespan** if the MCP app is mounted inside another ASGI app; otherwise the session manager task group is never started.
9. **Behind Cloud Run, fix the scheme from `X-Forwarded-Proto`** for issuer URLs, redirect URIs, and metadata, or configure `base_url`/`issuer_url` explicitly from env rather than deriving from the request.
10. **Do not derive signing secrets from other secrets or ship defaults.** rai has a hardcoded fallback JWT key and derives the real one from the Google client secret. Require an explicit key and rotate independently of Google credentials.
11. **Separate token types by `aud`/`resource`, not just a custom claim.** rai relies on `token_type == "mcp_access"` sharing one HS256 key with dashboard session JWTs. Use RFC 8707 resource indicators and audience checks so a luca-mcp token cannot be replayed elsewhere.
12. **Scope model.** Decide scopes up front (`accounting:read`, `accounting:write`, per-domain if needed), validate them at registration/authorization, advertise them (`valid_scopes`), and enforce per tool. The accounting inventory gives a clean read/write split.
13. **Business and firm must come from the token or from an explicit, authorised argument.** rai's stdio MCP hides `business_id` in env (safe for one user, one business); the HTTP MCP takes `business_id` as a free argument with `af-2` fallbacks (unsafe multi-tenant). Resolve the user's businesses from Liebre per request and reject anything outside that set; derive the firm from the `accounting_firm_business` link rather than defaulting.
14. **Liebre API auth for luca-mcp is an open question rai does not answer** (§2.7). rai only ever calls Liebre as an Auth0 machine client with `audience=<service_url>` plus `Accounting-Firm-ID`. If luca-mcp should act as the user, ant-liebre-api must accept user tokens or a delegated exchange, or a trusted `User-Id` header contract like the Liebre→rai proxy. Confirm in the sibling repo before designing the token flow.
15. **Reuse the client's resilience settings**: 30 s timeouts, overall deadline budget, 3 retries with jittered backoff, `Retry-After` on 429, single forced token refresh on 401/403, lenient response parsing with drift logging, short per-method caches, per-environment M2M token cache with a 60 s skew.
16. **Response shaping that worked for agents**: markdown tables for lists, key/value blocks for single records, one error envelope (`"ERROR: …"` or `{"error":true,"code":…}`), full UUIDs, fixed-decimal money, `limit`/`offset` with sane defaults, one-filter-at-a-time listing, validation errors that name the fix. The `accounting_guidelines` and `accounting_navigation` prompts are worth porting nearly verbatim.
17. **Housekeeping rai never did**: schedule expired-code cleanup, touch refresh tokens on use, add an end-to-end test that hits the MCP endpoint with no token and asserts a 401 with `WWW-Authenticate`, and keep the tool count in the navigation prompt in sync with the registry.
