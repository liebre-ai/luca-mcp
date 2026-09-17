"""The tools, one module per Liebre domain. Each module exposes ``register(mcp, client)`` and
defines its tools inside it; the docstring of a tool is the description the agent reads.

Adding a tool: put it in the module of its domain, or create a module and add it to ``MODULES``.
The order of ``MODULES`` is the order agents see in the tool list (and the README table). Every
module is also a *toolset* users can switch on selectively with ``LUCA_MCP_TOOLSETS`` (clients
such as Cursor cap the number of active tools across servers); the session tools are always on.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastmcp import FastMCP

from luca_mcp.api import RaiClient
from luca_mcp.tools import businesses, documents, journal_entries, reports, sat, session

MODULES = (session, businesses, reports, journal_entries, sat, documents)
ALWAYS_ON = (session,)
TOOLSETS = {module.__name__.rsplit(".", 1)[-1]: module for module in MODULES}


def register_all(mcp: FastMCP, client: RaiClient, toolsets: Iterable[str] | None = None) -> None:
    """Register every module, or only the named toolsets plus the ones that are always on."""
    selected = set(TOOLSETS) if toolsets is None else set(toolsets)
    unknown = sorted(selected - set(TOOLSETS))
    if unknown:
        raise ValueError(
            f"Unknown toolset(s) {', '.join(unknown)} in LUCA_MCP_TOOLSETS; "
            f"known: {', '.join(TOOLSETS)}."
        )
    for name, module in TOOLSETS.items():
        if module in ALWAYS_ON or name in selected:
            module.register(mcp, client)
