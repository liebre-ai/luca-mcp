"""OAuth 2.1 for MCP clients: an OIDC proxy in front of the Liebre Auth0 tenant.

Luca MCP is the authorization server MCP clients talk to (CIMD + DCR registration, PKCE S256,
per-client consent, refresh rotation, revocation, RFC 9728/8414 metadata) and one confidential
client of Auth0 upstream. Clients only ever hold a Luca-issued reference token; the user's Auth0
access/refresh tokens stay encrypted in the state store and are swapped in per request, so tool
handlers receive the genuine Auth0 token they must forward to the Liebre API.

Built on ``fastmcp.server.auth.oidc_proxy.OIDCProxy`` (fastmcp 4.0.3, pinned) with the overrides
the design review required (docs/plans/2026-09-08-luca-mcp-plan.md §3.2):

1. ``revoke_token`` really revokes: deletes the JTI mapping and the upstream token set and revokes
   the *Auth0 refresh token* upstream. Stock behaviour only dropped refresh metadata and posted the
   Luca JWT to Auth0.
2. ``_prepare_scopes_for_upstream_refresh`` sends the pinned upstream scope set, not whatever the
   MCP client registered, so refresh never narrows the grant.
3. Construction retries the synchronous OIDC discovery fetch so a slow IdP cannot crash-loop the
   revision at boot.
4. The protected-resource metadata is also served at the root well-known path.
5. Access tokens carry the Luca ``jti`` and the MCP client id in their claims so revocation and
   audit can correlate them (tools read identity from ``claims["email"]``; missing email fails
   closed in the tool layer, see ``luca_mcp.identity``).
"""

from __future__ import annotations

import logging
import time
from functools import partial
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth.identity_assertion import normalize_resource_url
from fastmcp.server.auth.oauth_proxy.models import _hash_token
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.server.auth.routes import cors_middleware
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from luca_mcp.settings import Settings

logger = logging.getLogger("luca_mcp.auth")

REDIRECT_PATH = "/auth/callback"
PRM_ROOT_PATH = "/.well-known/oauth-protected-resource"


class LucaOIDCProxy(OIDCProxy):
    """OIDCProxy with revocation, refresh-scope and metadata fixes (see module docstring)."""

    def __init__(self, *, upstream_scopes: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._luca_upstream_scopes = list(upstream_scopes)
        # OIDCProxy does not plumb the discovery document's revocation endpoint through to
        # OAuthProxy; do it here so upstream revocation has somewhere to go.
        if not getattr(self, "_upstream_revocation_endpoint", None):
            revocation = getattr(self.oidc_config, "revocation_endpoint", None)
            if revocation:
                self._upstream_revocation_endpoint = str(revocation)

    # -- override 2: refresh with the pinned upstream scopes ------------------------------------
    def _prepare_scopes_for_upstream_refresh(self, scopes: list[str]) -> list[str]:
        return list(self._luca_upstream_scopes)

    # -- override 5: correlate the swapped token with the Luca JWT that carried it -------------
    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        validated = await super().load_access_token(token)
        if validated is None:
            return None
        try:
            payload = self.jwt_issuer.verify_token(token)
        except Exception:  # the base class already accepted it; never fail here
            return validated
        validated = validated.model_copy(deep=True)
        claims = dict(validated.claims or {})
        claims["luca_jti"] = payload.get("jti")
        claims["luca_client_id"] = payload.get("client_id")
        claims["luca_exp"] = payload.get("exp")
        validated.claims = claims
        # The SDK's revocation handler only acts when token.client_id equals the MCP client
        # authenticating at /revoke. The upstream verifier fills client_id from Auth0's ``azp``
        # (our own Auth0 app id), which never matches; use the MCP client id instead.
        if payload.get("client_id"):
            validated.client_id = str(payload["client_id"])
        return validated

    # -- override 1: revocation that revokes ------------------------------------------------------
    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:  # type: ignore[override]
        jti: str | None = None
        if isinstance(token, RefreshToken):
            await self._refresh_token_store.delete(key=_hash_token(token.token))
            try:
                payload = self.jwt_issuer.verify_token(token.token, expected_token_use="refresh")
                jti = payload.get("jti")
            except Exception:
                jti = None
        else:
            jti = (token.claims or {}).get("luca_jti")

        upstream_refresh_token: str | None = None
        if jti:
            mapping = await self._jti_mapping_store.get(key=jti)
            if mapping:
                token_set = await self._upstream_token_store.get(key=mapping.upstream_token_id)
                if token_set:
                    upstream_refresh_token = token_set.refresh_token
                    await self._upstream_token_store.delete(key=mapping.upstream_token_id)
                await self._jti_mapping_store.delete(key=jti)
                logger.info(
                    "Revoked Luca session (jti=%s…, upstream set deleted=%s)",
                    jti[:8],
                    bool(token_set),
                )
            else:
                logger.info("Revoke: no live session for jti=%s… (already gone)", jti[:8])
        else:
            logger.info("Revoke: token carried no Luca jti; nothing to correlate")

        if upstream_refresh_token and self._upstream_revocation_endpoint:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        self._upstream_revocation_endpoint,
                        data={
                            "token": upstream_refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": self._upstream_client_id,
                            "client_secret": self._upstream_client_secret.get_secret_value(),
                        },
                    )
                logger.info("Upstream refresh token revocation -> HTTP %s", response.status_code)
            except Exception as exc:  # revocation is best effort; the local session is already dead
                logger.warning("Upstream refresh token revocation failed: %s", exc)

    # -- /revoke handler tolerant of public clients ------------------------------------------------
    async def _handle_revoke(self, request: Request) -> Response:
        """RFC 7009 revocation endpoint.

        Replaces the SDK handler, whose request model declares ``client_secret`` without a
        default: pydantic then treats it as required and every public client (Claude Code, Codex,
        Cursor register with ``token_endpoint_auth_method: none``) gets 400 on logout. Same
        semantics otherwise: authenticate the client, find the token, only revoke the caller's own.
        """
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message}, status_code=401
            )
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token is required"},
                status_code=400,
            )
        loaders = [self.load_access_token, partial(self.load_refresh_token, client)]
        if form.get("token_type_hint") == "refresh_token":
            loaders.reverse()
        found: AccessToken | RefreshToken | None = None
        for loader in loaders:
            found = await loader(token)
            if found is not None:
                break
        if found is None:
            logger.info("Revoke: unknown or already-dead token (client=%s)", client.client_id)
        elif found.client_id != client.client_id:
            logger.warning(
                "Revoke: token belongs to client %s but caller is %s; ignored",
                found.client_id,
                client.client_id,
            )
        else:
            await self.revoke_token(found)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    # -- RFC 8707: reject authorization requests for a resource that is not this server ----------
    def _with_resource_check(self, inner: Any) -> Any:
        """fastmcp's authorize handler never reads ``resource``, so its own check is unreachable.

        Clients that send a resource for another server get ``invalid_target`` (delivered to
        their registered redirect URI when possible). A trailing slash or query string on our own
        URL is tolerated, matching fastmcp's normaliser. Token audiences are unaffected either
        way: Luca always mints tokens for its own ``/mcp`` URL.
        """

        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                params: dict[str, str] = dict(request.query_params)
            else:
                params = {k: str(v) for k, v in (await request.form()).items()}
            resource = params.get("resource")
            if resource and self._resource_url:
                if normalize_resource_url(resource) != normalize_resource_url(
                    str(self._resource_url)
                ):
                    logger.warning(
                        "Rejecting authorize for resource %r (this server is %s)",
                        resource,
                        self._resource_url,
                    )
                    error = {
                        "error": "invalid_target",
                        "error_description": (
                            f"Resource {resource!r} is not this server; use {self._resource_url}"
                        ),
                    }
                    client_id, redirect_uri = params.get("client_id"), params.get("redirect_uri")
                    client = await self.get_client(client_id) if client_id else None
                    if client and redirect_uri:
                        try:
                            client.validate_redirect_uri(AnyUrl(redirect_uri))
                        except Exception:
                            return JSONResponse(error, status_code=400)
                        if params.get("state"):
                            error["state"] = params["state"]
                        separator = "&" if "?" in redirect_uri else "?"
                        return RedirectResponse(
                            f"{redirect_uri}{separator}{urlencode(error)}", status_code=302
                        )
                    return JSONResponse(error, status_code=400)
            return await inner(request)

        return endpoint

    # -- override 4: also serve the protected-resource metadata at the root path -----------------
    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        replaced: list[Route] = []
        for r in routes:
            if isinstance(r, Route) and r.path == "/revoke":
                replaced.append(
                    Route(
                        "/revoke",
                        endpoint=cors_middleware(self._handle_revoke, ["POST", "OPTIONS"]),
                        methods=["POST", "OPTIONS"],
                    )
                )
            elif isinstance(r, Route) and r.path == "/authorize":
                replaced.append(
                    Route(
                        "/authorize",
                        endpoint=self._with_resource_check(r.endpoint),
                        methods=sorted(r.methods or {"GET", "POST"}),
                    )
                )
            else:
                replaced.append(r)
        routes = replaced
        prm_routes = [
            r for r in routes if isinstance(r, Route) and r.path.startswith(PRM_ROOT_PATH)
        ]
        if prm_routes and not any(r.path == PRM_ROOT_PATH for r in prm_routes):
            source = prm_routes[0]
            routes.append(
                Route(
                    PRM_ROOT_PATH,
                    endpoint=source.endpoint,
                    methods=sorted(source.methods or {"GET", "OPTIONS"}),
                    name="luca_prm_root_alias",
                )
            )
        return routes


def build_state_store(settings: Settings) -> FernetEncryptionWrapper:
    """Encrypted key-value store for clients, transactions, codes and upstream tokens.

    Memory for the single-instance beta, Redis when ``REDIS_URL`` is set. Encryption is ours:
    fastmcp only wraps *its* default store, never a user-supplied one.
    """
    fernet = Fernet(settings.luca_mcp_storage_key.encode())  # raises if the key is malformed
    if settings.redis_url:
        from key_value.aio.stores.redis import RedisStore

        backend = RedisStore(url=settings.redis_url, default_collection="luca-mcp-oauth")
        logger.info("OAuth state store: redis")
    else:
        backend = MemoryStore()
        logger.warning(
            "OAuth state store: in-memory (single instance only; every restart logs users out)"
        )
    return FernetEncryptionWrapper(key_value=backend, fernet=fernet)


def build_auth_provider(settings: Settings) -> LucaOIDCProxy:
    """Construct the provider, retrying the upstream discovery fetch (override 3)."""
    # Key/store problems must fail fast with their own message, not as "discovery failed".
    storage = build_state_store(settings)
    last_error: Exception | None = None
    for attempt in range(1, settings.auth0_discovery_retries + 1):
        try:
            provider = LucaOIDCProxy(
                upstream_scopes=settings.luca_mcp_advertised_scopes,
                config_url=str(settings.auth0_config_url),
                client_id=settings.auth0_client_id,
                client_secret=settings.auth0_client_secret,
                audience=settings.auth0_audience,
                timeout_seconds=settings.auth0_discovery_timeout_seconds,
                base_url=settings.base_url,
                issuer_url=settings.base_url,
                redirect_path=REDIRECT_PATH,
                required_scopes=settings.luca_mcp_required_scopes,
                valid_scopes=settings.luca_mcp_advertised_scopes,
                allowed_client_redirect_uris=settings.luca_mcp_allowed_redirect_uris,
                client_storage=storage,
                jwt_signing_key=settings.luca_mcp_jwt_signing_key,
                token_endpoint_auth_method="client_secret_post",
                require_authorization_consent=settings.consent_setting,
                # Always request the full upstream scope set at authorize time, whatever the MCP
                # client asked for; refresh is pinned to the same set by override 2.
                extra_authorize_params={"scope": " ".join(settings.luca_mcp_advertised_scopes)},
                # Auth0 selects the API through `audience` (set by OIDCProxy); forwarding the
                # client's `resource` adds nothing and is one more thing Universal Login could
                # reject.
                forward_resource=False,
                fastmcp_access_token_expiry_seconds=settings.luca_mcp_access_token_ttl_seconds,
                token_expiry_threshold_seconds=settings.luca_mcp_upstream_refresh_threshold_seconds,
                enable_cimd=True,
            )
            logger.info(
                "Auth provider ready: issuer=%s upstream=%s consent=%s",
                settings.base_url,
                settings.auth0_config_url,
                settings.luca_mcp_consent_mode,
            )
            return provider
        except Exception as exc:  # discovery fetch / validation failures
            last_error = exc
            logger.warning(
                "Upstream OIDC discovery failed (attempt %s/%s): %s",
                attempt,
                settings.auth0_discovery_retries,
                exc,
            )
            if attempt < settings.auth0_discovery_retries:
                time.sleep(min(5 * attempt, 15))
    raise RuntimeError(
        f"Could not initialise the upstream identity provider after {settings.auth0_discovery_retries} attempts"
    ) from last_error
