"""`luca-mcp` command line: run the stdio MCP server, or manage the login.

luca-mcp            start the MCP server over stdio (what agents run)
luca-mcp login      log in with your Liebre account (opens the browser)
luca-mcp logout     revoke the session and delete stored credentials
luca-mcp status     show which server you are connected to and who you are
luca-mcp serve      same as no arguments
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from luca_mcp import __version__, oauth
from luca_mcp.api import LucaError, RaiClient
from luca_mcp.config import load_config


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="luca-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"luca-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    p_login = sub.add_parser("login", help="log in with your Liebre account")
    p_login.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening a browser"
    )
    sub.add_parser("logout", help="revoke the session and delete stored credentials")
    sub.add_parser("status", help="show server and session status")
    args = parser.parse_args(argv)
    config = load_config()

    if args.command in (None, "serve"):
        from luca_mcp.server import run_stdio

        run_stdio()
        return 0

    if args.command == "login":

        def show(url: str) -> None:
            print(f"Log in to Luca ({config.rai_url}) in your browser:\n  {url}\n", file=sys.stderr)

        try:
            tokens = oauth.login(config, open_browser=not args.no_browser, on_url=show)
        except oauth.LoginError as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1
        try:
            me = RaiClient(config).get("/whoami")
            email = (me.get("session") or {}).get("email") if isinstance(me, dict) else None
        except LucaError as exc:
            email = None
            print(
                f"Logged in, but the server rejected the first call: {exc.envelope}",
                file=sys.stderr,
            )
        print(
            f"Logged in{f' as {email}' if email else ''}. Session valid until {time.strftime('%H:%M', time.localtime(tokens.expires_at))} (refreshes automatically)."
        )
        return 0

    if args.command == "logout":
        tokens = oauth.load_tokens(config)
        if tokens:
            oauth.revoke(config, tokens)
        oauth.clear_tokens(config)
        print(f"Logged out of {config.rai_url}.")
        return 0

    if args.command == "status":
        tokens = oauth.current_tokens(config)
        status: dict[str, object] = {
            "rai_url": config.rai_url,
            "client_version": __version__,
            "logged_in": tokens is not None,
        }
        if tokens:
            status["session_expires_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(tokens.expires_at)
            )
            try:
                status["whoami"] = RaiClient(config).get("/whoami")
            except LucaError as exc:
                status["whoami_error"] = exc.envelope
        _print(status)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
