"""The Luca MCP stdio server: thin tools that forward to rai's MCP-facing endpoints.

Tool logic (validation, shaping, access checks) lives in ant-rai; this process only holds the
user's Luca session and speaks MCP to the agent. The tools live in ``luca_mcp.tools``, one module
per domain; this module assembles the server.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from luca_mcp import __version__, tools
from luca_mcp.api import RaiClient
from luca_mcp.config import Config, load_config
from luca_mcp.prompts import luca as prompts

INSTRUCTIONS = """You are connected to Luca, the accounting assistant of the Liebre platform, through a local
MCP client that acts on behalf of the logged-in Liebre user. If a tool answers that you are not
logged in, call `login` (it opens the user's browser for the Liebre login) and retry.

Start with `list_businesses` to learn which businesses the user can work on, then pass the chosen
`business_id` to every other tool. Periods are `YYYYMM` strings (13 = annual closing period).
Amounts are numbers in the business currency, exactly as Liebre reports them.

Luca is read-only: it cannot create or change journal entries, periods, accounts, credentials or
filings. When a user asks for a change, say so plainly and point them to the Liebre platform link
returned by the tools. Never guess a business_id: call `list_businesses`."""


def create_server(config: Config | None = None) -> FastMCP:
    config = config or load_config()
    client = RaiClient(config)
    mcp = FastMCP("luca", instructions=INSTRUCTIONS, version=__version__, mask_error_details=False)
    tools.register_all(mcp, client)
    prompts.register(mcp)
    return mcp


def run_stdio() -> None:
    logging.basicConfig(level=logging.WARNING)
    create_server().run(transport="stdio")
