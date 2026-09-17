"""The tools, one module per Liebre domain. Each module exposes ``register(mcp, client)`` and
defines its tools inside it; the docstring of a tool is the description the agent reads.

Adding a tool: put it in the module of its domain, or create a module and add it to ``MODULES``.
The order of ``MODULES`` is the order agents see in the tool list (and the README table).
"""

from __future__ import annotations

from fastmcp import FastMCP

from luca_mcp.api import RaiClient
from luca_mcp.tools import businesses, documents, journal_entries, reports, sat, session

MODULES = (session, businesses, reports, journal_entries, sat, documents)


def register_all(mcp: FastMCP, client: RaiClient) -> None:
    for module in MODULES:
        module.register(mcp, client)
