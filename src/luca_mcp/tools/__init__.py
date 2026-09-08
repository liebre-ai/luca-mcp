"""Tool and prompt registration. Each module exposes ``register(mcp, settings)``."""

from __future__ import annotations

from fastmcp import FastMCP

from luca_mcp.prompts import luca as prompts
from luca_mcp.settings import Settings
from luca_mcp.tools import business, journal_entries, reports, sat, system


def register_all_tools(mcp: FastMCP, settings: Settings) -> None:
    system.register(mcp, settings)
    business.register(mcp, settings)
    reports.register(mcp, settings)
    journal_entries.register(mcp, settings)
    sat.register(mcp, settings)
    prompts.register(mcp, settings)
