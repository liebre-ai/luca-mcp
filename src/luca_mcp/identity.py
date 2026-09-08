"""Who is calling: the identity behind the current request's access token."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastmcp.server.dependencies import get_access_token

from luca_mcp.errors import NO_EMAIL_CLAIM, NOT_AUTHENTICATED, LucaToolError


@dataclass(frozen=True)
class Identity:
    email: str
    subject: str | None
    upstream_token: str  # the user's genuine Auth0 access token (never returned to clients)
    upstream_expires_at: int | None
    mcp_client_id: str | None
    scopes: tuple[str, ...]
    claims: dict[str, Any]

    @property
    def cache_key(self) -> str:
        # Keyed by subject AND e-mail so two accounts can never share a cache entry.
        return f"{self.subject or '-'}|{self.email.lower()}"


def current_identity() -> Identity:
    """Return the caller's identity or fail closed with a clear, actionable error."""
    token = get_access_token()
    if token is None:
        raise LucaToolError(
            NOT_AUTHENTICATED,
            "This request carries no valid Luca session.",
            hint="Run /mcp (or your client's connect action) and log in to Liebre.",
        )
    claims: dict[str, Any] = dict(token.claims or {})
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise LucaToolError(
            NO_EMAIL_CLAIM,
            "Your login did not include an e-mail address, so Luca cannot act on your behalf.",
            hint=(
                "Liebre identifies users by e-mail. Log out and in again; if it persists, ask "
                "Liebre support to check the identity provider configuration for Luca MCP."
            ),
        )
    return Identity(
        email=email,
        subject=claims.get("sub"),
        upstream_token=token.token,
        upstream_expires_at=token.expires_at,
        mcp_client_id=claims.get("luca_client_id") or token.client_id,
        scopes=tuple(token.scopes),
        claims=claims,
    )
