"""E2E harness for the Luca MCP client against a rai server (local dev by default).

Drives the browser hops of the client's login without a human (rai's /oauth/authorize -> the dev
mock IdP login page -> rai's /oauth/callback -> the client's loopback callback), then calls tools
through the real tool code (in-process) or the real stdio process.

Usage (mock IdP via dev/run_local.sh, rai dev server on :3030 configured for it):
    LUCA_RAI_URL=http://localhost:3030 uv run python tests/e2e/harness.py login [--identity EMAIL]
    LUCA_RAI_URL=http://localhost:3030 uv run python tests/e2e/harness.py tools
    LUCA_RAI_URL=http://localhost:3030 uv run python tests/e2e/harness.py call whoami '{}' [--stdio]
    LUCA_RAI_URL=http://localhost:3030 uv run python tests/e2e/harness.py status | logout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from luca_mcp import oauth
from luca_mcp.config import Config, load_config

DEFAULT_IDENTITY = "ygreen@company.com"
HIDDEN_INPUT = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
HIDDEN_INPUT_ALT = re.compile(r'<input[^>]*name="([^"]+)"[^>]*type="hidden"[^>]*value="([^"]*)"')
FORM_ACTION = re.compile(r'<form[^>]*action="([^"]*)"')
LOOPBACK = ("http://127.0.0.1:", "http://localhost:")


class HarnessError(RuntimeError):
    pass


def _hidden_fields(html: str) -> dict[str, str]:
    fields = dict(HIDDEN_INPUT.findall(html))
    fields.update(dict(HIDDEN_INPUT_ALT.findall(html)))
    return fields


def _client_redirect_uri(start_url: str) -> str:
    """The client's own loopback redirect URI, taken from the authorize URL it built. Matching on
    "looks like localhost" is not enough: a rai running on localhost has its own /oauth/callback."""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(start_url).query)
    redirect_uri = (query.get("redirect_uri") or [""])[0]
    if not redirect_uri.startswith(LOOPBACK):
        raise HarnessError(f"authorize URL has no loopback redirect_uri: {start_url[:120]}")
    return redirect_uri


def _is_client_callback(location: str, redirect_uri: str) -> bool:
    return location.startswith(redirect_uri)


def drive_authorize_url(
    start_url: str,
    identity: str = DEFAULT_IDENTITY,
    *,
    login_decision: str = "allow",
    trace: list[str] | None = None,
    max_hops: int = 20,
) -> str:
    """Follow the authorize URL like a browser until a loopback callback is reached; GET that
    callback so the client's listener receives the code. Returns the final URL."""
    url = start_url
    redirect_uri = _client_redirect_uri(start_url)
    log = trace if trace is not None else []
    with httpx.Client(timeout=20, follow_redirects=False) as http:
        for _ in range(max_hops):
            log.append(f"GET {url[:140]}")
            response = http.get(url)
            if response.status_code in (301, 302, 303, 307, 308):
                location = urljoin(url, response.headers["location"])
                if _is_client_callback(location, redirect_uri):
                    httpx.get(location, timeout=10)
                    return location
                url = location
                continue
            if response.status_code != 200:
                raise HarnessError(
                    f"unexpected {response.status_code} at {url[:120]}: {response.text[:300]}"
                )
            html = response.text
            fields = _hidden_fields(html)
            action_match = FORM_ACTION.search(html)
            action = (
                urljoin(url, action_match.group(1))
                if action_match and action_match.group(1)
                else url
            )
            if "client_id" in fields and "redirect_uri" in fields:  # mock IdP login page
                fields["identity"] = identity
                fields["decision"] = login_decision
                log.append(f"POST mock login ({identity}, {login_decision})")
            elif "request" in fields:  # mock Liebre app /luca/connect page
                fields["identity"] = identity
                fields["decision"] = login_decision
                log.append(f"POST mock connect ({identity}, {login_decision})")
            else:
                raise HarnessError(f"unrecognised page at {url[:100]}: {html[:200]}")
            response = http.post(action, data=fields)
            if response.status_code not in (301, 302, 303, 307, 308):
                raise HarnessError(
                    f"form POST {action} -> {response.status_code}: {response.text[:300]}"
                )
            location = urljoin(action, response.headers["location"])
            if _is_client_callback(location, redirect_uri):
                httpx.get(location, timeout=10)
                return location
            url = location
    raise HarnessError("too many hops in the browser flow")


def login(
    identity: str = DEFAULT_IDENTITY,
    *,
    config: Config | None = None,
    login_decision: str = "allow",
) -> oauth.Tokens:
    """Run the client's real login (loopback listener + token exchange) while a thread plays the
    browser. Raises oauth.LoginError exactly as a user would see it."""
    config = config or load_config()
    trace: list[str] = []
    errors: list[BaseException] = []

    def browser(url: str) -> None:
        def run() -> None:
            try:
                drive_authorize_url(url, identity, login_decision=login_decision, trace=trace)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the caller after the wait
                errors.append(exc)

        threading.Thread(target=run, daemon=True).start()

    try:
        return oauth.login(config, open_browser=False, on_url=browser)
    except oauth.LoginError:
        if errors:
            raise HarnessError(f"browser drive failed: {errors[0]!r}; trace={trace}") from errors[0]
        raise


# ------------------------------------------------------------------------------------------------
# MCP calls
# ------------------------------------------------------------------------------------------------


def _shape(result: Any) -> dict[str, Any]:
    return {
        "is_error": result.is_error,
        "structured_content": result.structured_content,
        "text": "\n".join(
            getattr(c, "text", "") for c in result.content if getattr(c, "text", None)
        ),
    }


async def mcp_list_tools(config: Config | None = None) -> list[dict[str, Any]]:
    from fastmcp import Client

    from luca_mcp.server import create_server

    async with Client(create_server(config or load_config()), timeout=120) as client:
        return [t.model_dump(mode="json") for t in await client.list_tools()]


async def mcp_call(
    tool: str, arguments: dict[str, Any] | None = None, *, config: Config | None = None
) -> dict[str, Any]:
    """Call through the real tool code, in-process (no subprocess)."""
    from fastmcp import Client

    from luca_mcp.server import create_server

    async with Client(create_server(config or load_config()), timeout=180) as client:
        return _shape(await client.call_tool(tool, arguments or {}, raise_on_error=False))


async def mcp_call_stdio(
    tool: str, arguments: dict[str, Any] | None = None, *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Same call through the real `uv run luca-mcp` stdio process."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command="uv",
        args=["run", "luca-mcp"],
        env={**os.environ, **(env or {})},
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    async with Client(transport, timeout=180) as client:
        return _shape(await client.call_tool(tool, arguments or {}, raise_on_error=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login")
    p_login.add_argument("--identity", default=DEFAULT_IDENTITY)
    sub.add_parser("tools")
    p_call = sub.add_parser("call")
    p_call.add_argument("tool")
    p_call.add_argument("arguments", nargs="?", default="{}")
    p_call.add_argument("--stdio", action="store_true")
    sub.add_parser("status")
    sub.add_parser("logout")
    args = parser.parse_args(argv)
    config = load_config()
    if args.cmd == "login":
        tokens = login(args.identity, config=config)
        print(
            json.dumps(
                {
                    "rai_url": config.rai_url,
                    "expires_at": tokens.expires_at,
                    "has_refresh": bool(tokens.refresh_token),
                }
            )
        )
        return 0
    if args.cmd == "tools":
        for tool in asyncio.run(mcp_list_tools(config)):
            print(f"- {tool['name']}: {(tool.get('description') or '').splitlines()[0][:100]}")
        return 0
    if args.cmd == "call":
        arguments = json.loads(args.arguments)
        out = asyncio.run(
            mcp_call_stdio(args.tool, arguments)
            if args.stdio
            else mcp_call(args.tool, arguments, config=config)
        )
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    if args.cmd == "status":
        tokens = oauth.current_tokens(config)
        print(
            json.dumps(
                {
                    "rai_url": config.rai_url,
                    "logged_in": tokens is not None,
                    "expires_at": tokens.expires_at if tokens else None,
                }
            )
        )
        return 0
    if args.cmd == "logout":
        tokens = oauth.load_tokens(config)
        if tokens:
            oauth.revoke(config, tokens)
        oauth.clear_tokens(config)
        print("logged out")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
