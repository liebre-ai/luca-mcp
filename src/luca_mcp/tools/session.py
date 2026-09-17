"""Session tools: log in through the browser, log out, and say who is logged in."""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Context, FastMCP

from luca_mcp import __version__, oauth
from luca_mcp.api import RaiClient
from luca_mcp.errors import NOT_LOGGED_IN, LucaError
from luca_mcp.tools._common import READ_ONLY, SESSION

# Seconds between progress notifications while the browser login is pending.
LOGIN_PROGRESS_INTERVAL = 5.0


def register(mcp: FastMCP, client: RaiClient) -> None:
    config = client.config

    @mcp.tool(name="login", annotations=SESSION)
    async def login(ctx: Context | None = None) -> dict[str, Any]:
        """Log in to Luca with your Liebre account. Opens the browser; call this when a tool says you
        are not logged in or your session expired. Blocks until the login completes (up to 5 min).
        Returns who you are logged in as."""
        urls: list[str] = []
        try:
            task = asyncio.ensure_future(
                asyncio.to_thread(oauth.login, config, open_browser=True, on_url=urls.append)
            )
            elapsed = 0.0
            while True:
                done, _ = await asyncio.wait({task}, timeout=LOGIN_PROGRESS_INTERVAL)
                if done:
                    break
                elapsed += LOGIN_PROGRESS_INTERVAL
                if ctx is not None:
                    message = (
                        f"Waiting for the browser login ({elapsed:g}s of {config.login_timeout:g}s)"
                    )
                    if urls:
                        message += f"; if no browser opened, open: {urls[0]}"
                    try:
                        await ctx.report_progress(
                            progress=elapsed, total=config.login_timeout, message=message
                        )
                    except Exception:
                        pass
            tokens = task.result()
        except oauth.LoginError as exc:
            raise LucaError(
                {
                    "error": {
                        "code": "login_failed",
                        "message": str(exc),
                        "hint": "If no browser opened, open the URL from the message manually.",
                        "authorize_url": urls[0] if urls else None,
                    }
                }
            ) from exc
        me = await client.fetch("/whoami")
        return {
            "logged_in": True,
            "session_expires_at": tokens.expires_at,
            **(me if isinstance(me, dict) else {}),
        }

    @mcp.tool(name="logout", annotations=SESSION)
    async def logout() -> dict[str, Any]:
        """Log out of Luca on this machine: revokes the session at the server and deletes the stored
        credentials."""
        tokens = oauth.load_tokens(config)
        if tokens:
            await asyncio.to_thread(oauth.revoke, config, tokens)
        oauth.clear_tokens(config)
        return {"logged_out": True, "logged_in": False, "rai_url": config.rai_url}

    @mcp.tool(name="whoami", annotations=READ_ONLY)
    async def whoami() -> dict[str, Any]:
        """Who am I logged in as, what can I access, and which Luca server is this client using?
        Never fails: when not logged in it says so and tells you to call `login`."""
        tokens = oauth.current_tokens(config)
        if tokens is None:
            return {
                "logged_in": False,
                "rai_url": config.rai_url,
                "client_version": __version__,
                **NOT_LOGGED_IN,
            }
        try:
            me = await client.fetch("/whoami")
        except LucaError as exc:
            return {
                "logged_in": False,
                "rai_url": config.rai_url,
                "client_version": __version__,
                **exc.envelope,
            }
        return {
            "logged_in": True,
            "rai_url": config.rai_url,
            "client_version": __version__,
            **(me if isinstance(me, dict) else {"server": me}),
        }
