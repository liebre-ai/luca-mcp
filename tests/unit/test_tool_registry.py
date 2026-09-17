"""Offline checks of what the server registers: no rai, no login.

`tests/e2e/tools_suite.py` proves behaviour against a real rai; this only guards the registry in
`luca_mcp.tools`, so a new module cannot silently drop, duplicate or mis-annotate a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from luca_mcp import tools
from luca_mcp.api import RaiClient
from luca_mcp.config import Config
from luca_mcp.server import create_server

# In listing order: MODULES order in luca_mcp/tools/__init__.py, then definition order.
EXPECTED_TOOLS = [
    "login",
    "logout",
    "whoami",
    "list_businesses",
    "get_business",
    "list_periods",
    "get_trial_balance",
    "get_balance_sheet",
    "get_income_statement",
    "get_vat_determination",
    "search_journal_entries",
    "list_journal_entries_for_period",
    "get_journal_entry",
    "list_declaraciones",
    "list_sat_archives",
    "get_sat_archive",
    "list_article_69b_matches",
    "list_documents",
    "get_document",
]
SESSION_TOOLS = {"login", "logout"}  # change local credentials; everything else only reads


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("LUCA_MCP_CREDENTIAL_STORE", "file")
    return Config(
        rai_url="http://rai.invalid",
        client_id="luca-mcp",
        scopes="openid email",
        config_dir=tmp_path,
        request_timeout=5.0,
        login_timeout=5.0,
    )


def _hint(tool: Any, name: str) -> bool | None:
    dumped = tool.annotations.model_dump() if tool.annotations else {}
    camel = name.split("_")[0] + "".join(w.title() for w in name.split("_")[1:])
    return dumped.get(name, dumped.get(camel))


async def test_tools_are_listed_in_module_order(config: Config) -> None:
    async with Client(create_server(config)) as client:
        listed = await client.list_tools()
    assert [t.name for t in listed] == EXPECTED_TOOLS


async def test_every_tool_is_described_and_annotated(config: Config) -> None:
    async with Client(create_server(config)) as client:
        listed = await client.list_tools()
    for tool in listed:
        assert (tool.description or "").strip(), f"{tool.name} has no description"
        assert _hint(tool, "destructive_hint") is False, f"{tool.name} must not be destructive"
        expected_read_only = tool.name not in SESSION_TOOLS
        assert _hint(tool, "read_only_hint") is expected_read_only, tool.name


async def test_each_module_registers_its_own_tools(config: Config) -> None:
    """Every module in MODULES contributes at least one tool and none overlap."""
    seen: dict[str, str] = {}
    for module in tools.MODULES:
        mcp = FastMCP(module.__name__)
        module.register(mcp, RaiClient(config))
        async with Client(mcp) as client:
            names = [t.name for t in await client.list_tools()]
        assert names, f"{module.__name__} registers no tool"
        for name in names:
            assert name not in seen, f"{name} registered by {seen[name]} and {module.__name__}"
            seen[name] = module.__name__
    assert list(seen) == EXPECTED_TOOLS


async def test_prompts_are_registered(config: Config) -> None:
    async with Client(create_server(config)) as client:
        prompts = await client.list_prompts()
    assert {p.name for p in prompts} == {"luca_guidelines", "luca_navigation"}
