"""Dev-only mock OpenID provider that stands in for the Liebre Auth0 tenant.

WHY THIS EXISTS
    The real "Luca MCP" Auth0 application is owned by the tenant admin. Until it exists, this
    server lets the whole chain run locally: MCP client -> Luca MCP (OIDCProxy) -> this IdP ->
    Liebre dev API. It mints RS256 access tokens shaped like Auth0's (``iss``, ``aud`` with the
    Liebre API identifier, ``scope``, ``email``), which the dev Liebre API accepts because it
    resolves users by the ``email`` claim.

NEVER DEPLOY THIS. It has no real authentication: the login page lets you pick an identity.
Only use identities that are synthetic dev accounts (default: ygreen@company.com, the account the
API repo's own fixtures use).

Run:  uv run python dev/mock_idp.py            (port 9400 by default)
Env:  MOCK_IDP_PORT, MOCK_IDP_ISSUER, MOCK_IDP_CLIENT_ID, MOCK_IDP_CLIENT_SECRET,
      MOCK_IDP_AUDIENCE, MOCK_IDP_ACCESS_TTL (seconds), MOCK_IDP_REFRESH_TTL (seconds),
      MOCK_IDP_ALLOWED_REDIRECTS (comma list), MOCK_IDP_IDENTITIES (comma list of emails),
      MOCK_IDP_KEY_PATH (PEM persisted across restarts so stored tokens stay verifiable).

Special identities for edge-case testing (selectable on the login page):
    ygreen@company.com        happy path (allowed on bu-2, firm admin of af-2)
    unknown.user@yopmail.com  email that is NOT a Liebre user -> Liebre answers 401
    __no_email__              access token WITHOUT an email claim -> Luca must fail closed
Any other e-mail can be typed into the free-text field.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

PORT = int(os.getenv("MOCK_IDP_PORT", "9400"))
ISSUER = os.getenv("MOCK_IDP_ISSUER", f"http://localhost:{PORT}").rstrip("/")
CLIENT_ID = os.getenv("MOCK_IDP_CLIENT_ID", "luca-mcp-dev")
CLIENT_SECRET = os.getenv("MOCK_IDP_CLIENT_SECRET", "change-me")
AUDIENCE = os.getenv("MOCK_IDP_AUDIENCE", "https://api-dev.liebre.ai/api/v1")
ACCESS_TTL = int(os.getenv("MOCK_IDP_ACCESS_TTL", "86400"))
REFRESH_TTL = int(os.getenv("MOCK_IDP_REFRESH_TTL", str(30 * 24 * 3600)))
ALLOWED_REDIRECTS = [
    u.strip()
    for u in os.getenv("MOCK_IDP_ALLOWED_REDIRECTS", "http://localhost:8765/auth/callback").split(
        ","
    )
    if u.strip()
]
IDENTITIES = [
    e.strip()
    for e in os.getenv(
        "MOCK_IDP_IDENTITIES", "ygreen@company.com,unknown.user@yopmail.com,__no_email__"
    ).split(",")
    if e.strip()
]
KEY_PATH = Path(os.getenv("MOCK_IDP_KEY_PATH", ".e2e-artifacts/mock_idp_key.pem"))
KID = "mock-idp-key-1"
SUPPORTED_SCOPES = ["openid", "profile", "email", "offline_access"]


def _load_or_create_key() -> rsa.RSAPrivateKey:
    if KEY_PATH.exists():
        return serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)  # type: ignore[return-value]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


PRIVATE_KEY = _load_or_create_key()
PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": KID,
            "n": _b64url_uint(PUBLIC_NUMBERS.n),
            "e": _b64url_uint(PUBLIC_NUMBERS.e),
        }
    ]
}

# In-memory state (dev only).
AUTH_CODES: dict[str, dict] = {}
REFRESH_TOKENS: dict[str, dict] = {}
REVOKED_ACCESS_JTIS: set[str] = set()
EVENTS: list[dict] = []  # observable log for the e2e harness (/__test/events)


def _event(kind: str, **data: object) -> None:
    EVENTS.append({"t": time.time(), "kind": kind, **data})
    if len(EVENTS) > 500:
        del EVENTS[: len(EVENTS) - 500]


def _oauth_error(code: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": description}, status_code=status)


def _mint_access_token(email: str | None, scope: str, nonce: str | None = None) -> tuple[str, str]:
    now = int(time.time())
    jti = secrets.token_urlsafe(16)
    subject = f"auth0|mock-{hashlib.sha256((email or 'anonymous').encode()).hexdigest()[:24]}"
    claims: dict[str, object] = {
        "iss": f"{ISSUER}/",
        "sub": subject,
        "aud": [AUDIENCE, f"{ISSUER}/userinfo"],
        "iat": now,
        "exp": now + ACCESS_TTL,
        "scope": scope,
        "azp": CLIENT_ID,
        "jti": jti,
    }
    if email:
        claims["email"] = email
        claims["email_verified"] = True
    token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID, "typ": "JWT"})
    return token, jti


def _mint_id_token(email: str | None, nonce: str | None) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": f"{ISSUER}/",
        "sub": f"auth0|mock-{hashlib.sha256((email or 'anonymous').encode()).hexdigest()[:24]}",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + ACCESS_TTL,
    }
    if email:
        claims["email"] = email
        claims["email_verified"] = True
    if nonce:
        claims["nonce"] = nonce
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID, "typ": "JWT"})


def _issue_tokens(email: str | None, scope: str, nonce: str | None, refresh_family: str) -> dict:
    access, jti = _mint_access_token(email, scope, nonce)
    body: dict[str, object] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "scope": scope,
    }
    if "openid" in scope.split():
        body["id_token"] = _mint_id_token(email, nonce)
    if "offline_access" in scope.split():
        refresh = "mockrt_" + secrets.token_urlsafe(32)
        REFRESH_TOKENS[refresh] = {
            "email": email,
            "scope": scope,
            "family": refresh_family,
            "expires_at": time.time() + REFRESH_TTL,
        }
        body["refresh_token"] = refresh
    _event("tokens_issued", email=email, scope=scope, jti=jti, refresh="refresh_token" in body)
    return body


# --------------------------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------------------------


async def discovery(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": f"{ISSUER}/",
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
            "revocation_endpoint": f"{ISSUER}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": SUPPORTED_SCOPES,
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "claims_supported": ["sub", "email", "email_verified", "iss", "aud", "iat", "exp"],
        }
    )


async def jwks(_: Request) -> JSONResponse:
    return JSONResponse(JWKS)


def _render_login(params: dict[str, str], error: str | None = None) -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in params.items()
    )
    options = "".join(f'<option value="{escape(e)}">{escape(e)}</option>' for e in IDENTITIES)
    err = f'<p style="color:#b3402e">{escape(error)}</p>' if error else ""
    html = f"""<!doctype html><meta charset="utf-8"><title>Mock IdP (dev)</title>
<body style="font-family:system-ui;max-width:32rem;margin:3rem auto">
<h2>Mock identity provider <small style="color:#888">(dev only, no real auth)</small></h2>
<p>Client: <code>{escape(params.get("client_id", ""))}</code><br>Scope: <code>{escape(params.get("scope", ""))}</code><br>
Audience: <code>{escape(params.get("audience", ""))}</code></p>{err}
<form method="post" action="/authorize">{hidden}
<label>Identity <select name="identity">{options}</select></label><br><br>
<label>or type an e-mail <input name="custom_email" placeholder="someone@yopmail.com"></label><br><br>
<button type="submit" name="decision" value="allow">Log in</button>
<button type="submit" name="decision" value="deny">Deny</button>
</form></body>"""
    return HTMLResponse(html)


def _validate_authorize(params: dict[str, str]) -> str | None:
    if params.get("client_id") != CLIENT_ID:
        return "unknown client_id"
    if params.get("response_type") != "code":
        return "response_type must be code"
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in ALLOWED_REDIRECTS:
        return f"redirect_uri not allowed: {redirect_uri}"
    if params.get("code_challenge") and params.get("code_challenge_method", "S256") != "S256":
        return "only S256 is supported"
    return None


async def authorize(request: Request) -> Response:
    if request.method == "GET":
        params = {k: v for k, v in request.query_params.items()}
        error = _validate_authorize(params)
        if error:
            _event("authorize_rejected", error=error)
            return HTMLResponse(f"<h3>invalid_request</h3><p>{escape(error)}</p>", status_code=400)
        _event("authorize_shown", client_id=params.get("client_id"), scope=params.get("scope"))
        return _render_login(params)

    form = await request.form()
    params = {
        k: str(v) for k, v in form.items() if k not in ("identity", "custom_email", "decision")
    }
    error = _validate_authorize(params)
    if error:
        return HTMLResponse(f"<h3>invalid_request</h3><p>{escape(error)}</p>", status_code=400)
    redirect_uri = params["redirect_uri"]
    state = params.get("state", "")
    if form.get("decision") == "deny":
        _event("login_denied")
        return RedirectResponse(
            f"{redirect_uri}?{urlencode({'error': 'access_denied', 'error_description': 'User denied', 'state': state})}",
            status_code=302,
        )
    identity = str(form.get("custom_email") or form.get("identity") or "").strip()
    email: str | None = None if identity == "__no_email__" else identity
    if email is not None and "@" not in email:
        return _render_login(params, error="Pick an identity or type a valid e-mail")
    requested = [s for s in params.get("scope", "").split() if s in SUPPORTED_SCOPES]
    code = "mockcode_" + secrets.token_urlsafe(24)
    AUTH_CODES[code] = {
        "email": email,
        "scope": " ".join(requested),
        "redirect_uri": redirect_uri,
        "code_challenge": params.get("code_challenge"),
        "nonce": params.get("nonce"),
        "audience": params.get("audience"),
        "expires_at": time.time() + 300,
    }
    _event("login_ok", email=email, scope=" ".join(requested), audience=params.get("audience"))
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state, 'iss': ISSUER + '/'})}",
        status_code=302,
    )


def _client_authenticated(request: Request, form: dict[str, str]) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
            cid, secret = decoded.split(":", 1)
            return cid == CLIENT_ID and secrets.compare_digest(secret, CLIENT_SECRET)
        except Exception:
            return False
    return form.get("client_id") == CLIENT_ID and secrets.compare_digest(
        form.get("client_secret", ""), CLIENT_SECRET
    )


async def token(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not _client_authenticated(request, form):
        _event("token_invalid_client")
        return _oauth_error("invalid_client", "client authentication failed", status=401)
    grant = form.get("grant_type")
    if grant == "authorization_code":
        record = AUTH_CODES.pop(form.get("code", ""), None)
        if not record or record["expires_at"] < time.time():
            _event("token_invalid_code")
            return _oauth_error("invalid_grant", "unknown or expired code")
        if record["redirect_uri"] != form.get("redirect_uri"):
            return _oauth_error("invalid_grant", "redirect_uri mismatch")
        if record["code_challenge"]:
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != record["code_challenge"]:
                _event("token_pkce_failed")
                return _oauth_error("invalid_grant", "PKCE verification failed")
        family = secrets.token_urlsafe(8)
        return JSONResponse(
            _issue_tokens(record["email"], record["scope"], record["nonce"], family)
        )
    if grant == "refresh_token":
        presented = form.get("refresh_token", "")
        record = REFRESH_TOKENS.pop(presented, None)
        if not record:
            _event("refresh_rejected", reason="unknown_or_reused")
            return _oauth_error(
                "invalid_grant", "unknown, expired, rotated or revoked refresh token"
            )
        if record["expires_at"] < time.time():
            _event("refresh_rejected", reason="expired")
            return _oauth_error("invalid_grant", "refresh token expired")
        requested = form.get("scope")
        scope = record["scope"]
        if requested:
            narrowed = [s for s in requested.split() if s in scope.split()]
            if len(narrowed) != len(requested.split()):
                _event("refresh_rejected", reason="scope_escalation", requested=requested)
                return _oauth_error("invalid_scope", "requested scope exceeds the original grant")
            scope = " ".join(narrowed)
        _event("refresh_ok", email=record["email"], scope=scope)
        return JSONResponse(_issue_tokens(record["email"], scope, None, record["family"]))
    return _oauth_error("unsupported_grant_type", f"unsupported grant_type {grant!r}")


async def revoke(request: Request) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not _client_authenticated(request, form):
        return _oauth_error("invalid_client", "client authentication failed", status=401)
    presented = form.get("token", "")
    if presented in REFRESH_TOKENS:
        del REFRESH_TOKENS[presented]
        _event("revoked_refresh_token")
    else:
        _event("revoke_noop", token_prefix=presented[:12])
    return Response(status_code=200)


async def userinfo(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    try:
        claims = jwt.decode(
            auth.split(" ", 1)[1],
            PRIVATE_KEY.public_key(),
            algorithms=["RS256"],
            audience=f"{ISSUER}/userinfo",
            issuer=f"{ISSUER}/",
        )
    except jwt.PyJWTError as exc:
        return JSONResponse(
            {"error": "invalid_token", "error_description": str(exc)}, status_code=401
        )
    return JSONResponse({k: claims[k] for k in ("sub", "email", "email_verified") if k in claims})


async def test_events(_: Request) -> JSONResponse:
    return JSONResponse({"events": EVENTS, "refresh_tokens_active": len(REFRESH_TOKENS)})


async def test_mint(request: Request) -> JSONResponse:
    """Mint an access token directly (bypasses OAuth). For harness use against the Liebre API."""
    email = request.query_params.get("email") or IDENTITIES[0]
    email_value: str | None = None if email == "__no_email__" else email
    scope = request.query_params.get("scope", "openid email")
    access, _ = _mint_access_token(email_value, scope)
    return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL})


async def test_expire_all_refresh(_: Request) -> JSONResponse:
    """Simulate Auth0 revoking every refresh token (reuse detection / admin revoke)."""
    count = len(REFRESH_TOKENS)
    REFRESH_TOKENS.clear()
    _event("all_refresh_revoked", count=count)
    return JSONResponse({"revoked": count})


app = Starlette(
    routes=[
        Route("/.well-known/openid-configuration", discovery),
        Route("/.well-known/oauth-authorization-server", discovery),
        Route("/.well-known/jwks.json", jwks),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/oauth/revoke", revoke, methods=["POST"]),
        Route("/userinfo", userinfo),
        Route("/__test/events", test_events),
        Route("/__test/mint", test_mint),
        Route("/__test/expire_all_refresh", test_expire_all_refresh, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    import uvicorn

    print(
        f"mock IdP issuer={ISSUER}/ client_id={CLIENT_ID} audience={AUDIENCE} access_ttl={ACCESS_TTL}s"
    )
    print(f"identities={IDENTITIES} allowed_redirects={ALLOWED_REDIRECTS}")
    uvicorn.run(
        app, host="127.0.0.1", port=PORT, log_level=os.getenv("LOG_LEVEL", "warning").lower()
    )
