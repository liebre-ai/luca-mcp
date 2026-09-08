"""Tool registration. Each module exposes ``register(mcp, settings)``."""

from __future__ import annotations

from fastmcp import FastMCP

from luca_mcp.settings import Settings
from luca_mcp.tools import system


def register_all_tools(mcp: FastMCP, settings: Settings) -> None:
    system.register(mcp, settings)
