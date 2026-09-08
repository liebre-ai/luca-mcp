"""Session and diagnostics tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations

from luca_mcp import __version__
from luca_mcp.settings import Settings

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=UTC).isoformat().replace("+00:00", "Z")


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="whoami", annotations=READ_ONLY)
    async def whoami() -> dict[str, Any]:
        """Who am I logged in as, and what is this server? Call this first when something fails.

        Returns the login identity Luca will act as (e-mail), when the upstream login expires, the
        MCP client this session belongs to, and the server version/build. No business data.
        """
        token = get_access_token()
        claims: dict[str, Any] = dict((token.claims if token else None) or {})
        return {
            "server": {
                "name": "luca",
                "version": __version__,
                "build": settings.luca_mcp_build,
                "environment": settings.luca_mcp_env,
                "liebre_api": settings.liebre_api_root,
                "platform": settings.liebre_app_root,
                "read_only": True,
            },
            "session": {
                "email": claims.get("email"),
                "subject": claims.get("sub"),
                "mcp_client_id": claims.get("luca_client_id")
                or (token.client_id if token else None),
                "scopes": list(token.scopes) if token else [],
                "upstream_login_expires_at": _iso(token.expires_at if token else None),
                "luca_token_expires_at": _iso(claims.get("luca_exp")),
            },
        }
