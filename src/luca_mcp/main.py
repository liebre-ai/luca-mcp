"""Server factory. Run with ``uvicorn luca_mcp.main:create_app --factory``."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastmcp import FastMCP
from starlette.applications import Starlette

from luca_mcp import __version__
from luca_mcp.auth import build_auth_provider
from luca_mcp.settings import Settings, get_settings
from luca_mcp.tools import register_all_tools

INSTRUCTIONS = """You are connected to Luca, the accounting assistant of the Liebre platform, on behalf of a
logged-in Liebre user. Tools read the user's accounting data from the Liebre API with the user's own
permissions. Start with `list_businesses` to learn which businesses the user can work on, then pass
the chosen `business_id` to every other tool. Periods are `YYYYMM` strings (13 = annual closing
period). Amounts are decimal strings in the business currency unless a field says otherwise.

This server is read-only for now: it cannot create or change journal entries, periods, accounts,
credentials or filings. When a user asks for a change, say so plainly and point them to the Liebre
platform link returned by the tools. Never guess a business_id: if you do not know it, call
`list_businesses`. If a tool answers that the login has expired, tell the user to run /mcp (or the
client's reconnect action) to log in again."""


def _allowed_hosts(settings: Settings) -> list[str]:
    hosts = [urlparse(settings.base_url).netloc, *settings.luca_mcp_allowed_hosts]
    if settings.luca_mcp_env in ("local", "dev"):
        hosts += ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    return sorted(set(hosts))


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    auth = build_auth_provider(settings)
    mcp = FastMCP(
        "luca",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        # Unexpected exceptions are masked outside local/dev so internals never reach clients.
        mask_error_details=settings.luca_mcp_env not in ("local", "dev"),
    )
    register_all_tools(mcp, settings)
    return mcp


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    mcp = create_server(settings)
    return mcp.http_app(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        # DNS-rebinding protection (spec: servers MUST validate Origin). Strict mode rejects any
        # Host/Origin outside the base URL host, LUCA_MCP_ALLOWED_HOSTS and (local/dev) loopback.
        host_origin_protection=True,
        allowed_hosts=_allowed_hosts(settings),
        allowed_origins=[settings.base_url],
    )
