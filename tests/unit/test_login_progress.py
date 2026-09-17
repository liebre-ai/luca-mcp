"""Progress notifications for the `login` tool: offline, no network, no real browser.

Exercises the polling loop in `luca_mcp.tools.session.login` that reports progress every
`LOGIN_PROGRESS_INTERVAL` seconds while `oauth.login` runs in a worker thread, and confirms the
injected `ctx: Context | None` parameter never shows up in the tool's JSON schema.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from luca_mcp import oauth
from luca_mcp.api import RaiClient
from luca_mcp.config import Config
from luca_mcp.server import create_server
from luca_mcp.tools import session

AUTHORIZE_URL = "https://rai.invalid/oauth/authorize?x=1"


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


async def test_login_reports_progress_and_returns_who_is_logged_in(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "LOGIN_PROGRESS_INTERVAL", 0.05)

    def fake_login(cfg: Config, *, open_browser: bool, on_url: Any) -> oauth.Tokens:
        on_url(AUTHORIZE_URL)
        time.sleep(0.3)  # runs in a worker thread; long enough for several progress ticks
        return oauth.Tokens(
            access_token="a",
            refresh_token="r",
            expires_at=time.time() + 1800,
            scope="openid email",
            rai_url=cfg.rai_url,
            obtained_at=time.time(),
        )

    monkeypatch.setattr(session.oauth, "login", fake_login)

    async def fake_fetch(self: RaiClient, path: str, **params: Any) -> Any:
        return {"session": {"email": "ygreen@company.com"}}

    monkeypatch.setattr(RaiClient, "fetch", fake_fetch)

    events: list[tuple[float, float | None, str | None]] = []

    async def handler(progress: float, total: float | None, message: str | None) -> None:
        events.append((progress, total, message))

    async with Client(create_server(config), progress_handler=handler) as client:
        result = await client.call_tool("login", {})

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["logged_in"] is True
    assert "session" in result.structured_content
    assert events, "expected at least one progress notification while the login was pending"
    assert all(total == 5.0 for _, total, _ in events)
    assert any(AUTHORIZE_URL in (message or "") for _, _, message in events)


async def test_login_failure_reports_the_authorize_url_in_the_error_envelope(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "LOGIN_PROGRESS_INTERVAL", 0.05)

    def fake_login(cfg: Config, *, open_browser: bool, on_url: Any) -> oauth.Tokens:
        on_url(AUTHORIZE_URL)
        raise oauth.LoginError("boom")

    monkeypatch.setattr(session.oauth, "login", fake_login)

    async with Client(create_server(config)) as client:
        result = await client.call_tool("login", {}, raise_on_error=False)

    assert result.is_error
    envelope = json.loads(result.content[0].text)["error"]
    assert envelope["code"] == "login_failed"
    assert envelope["message"] == "boom"
    assert envelope["authorize_url"] == AUTHORIZE_URL


async def test_login_schema_has_no_visible_parameters(config: Config) -> None:
    async with Client(create_server(config)) as client:
        tools = await client.list_tools()
    login_tool = next(t for t in tools if t.name == "login")
    dumped = login_tool.model_dump(mode="json", exclude_none=True)
    assert dumped["input_schema"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
