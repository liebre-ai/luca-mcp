"""Session and diagnostics tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

from luca_mcp import __version__
from luca_mcp.errors import LucaToolError
from luca_mcp.identity import current_identity
from luca_mcp.liebre.client import LiebreClient
from luca_mcp.settings import Settings
from luca_mcp.tenant import load_tenant
from luca_mcp.tools._common import READ_ONLY


def _iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=UTC).isoformat().replace("+00:00", "Z")


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="whoami", annotations=READ_ONLY)
    async def whoami() -> dict[str, Any]:
        """Who am I logged in as, what can I access, and what is this server? Call this first
        when something fails.

        Returns the login identity Luca acts as (e-mail), when the upstream login expires, the
        MCP client of this session, the server version/build, and the firms and businesses the
        user can access (or the reason they cannot be listed). It never raises for access problems;
        look at `access.error`.
        """
        token = get_access_token()
        claims: dict[str, Any] = dict((token.claims if token else None) or {})
        result: dict[str, Any] = {
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
        try:
            identity = current_identity()
            ctx = await load_tenant(LiebreClient(settings, identity), settings, force=True)
            result["access"] = {
                "firms": [
                    {
                        "accounting_firm_id": f.get("accounting_firm_id"),
                        "name": f.get("commercial_name") or f.get("legal_name"),
                    }
                    for f in ctx.firms
                ],
                "businesses": [ref.summary() for ref in ctx.business_list],
                "business_count": len(ctx.businesses),
                "problems": ctx.problems,
            }
        except LucaToolError as exc:
            result["access"] = {"error": exc.envelope()["error"]}
        return result
