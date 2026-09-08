"""Drive a REAL Claude Code CLI login against the local Luca MCP server, without a human.

`claude mcp login <name> --no-browser` prints the authorization URL and waits (on a TTY) for
the redirect URL to be pasted back. This script runs it inside a pseudo-terminal, follows the
authorize URL through Luca's consent page and the dev mock IdP (identity ygreen@company.com),
delivers the callback, and reports what Claude Code printed.

Prerequisites: `dev/run_local.sh` running; the server registered in the target project dir:
    claude mcp add --transport http luca http://localhost:8765/mcp -s local   (run inside the dir)

Usage: uv run python tests/e2e/claude_code_login.py <project_dir> [server_name] [identity]
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import harness as h  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x07\x1b]*(?:\x07|\x1b\\)")
AUTHORIZE_URL = re.compile(r"(http://localhost:8765/authorize\?[^\s\x1b\]]+)")


def run_login(
    project_dir: str, server_name: str = "luca", identity: str = h.DEFAULT_IDENTITY
) -> dict:
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["claude", "mcp", "login", server_name, "--no-browser"],
        cwd=project_dir,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = ""
    authorize_url: str | None = None
    final_url: str | None = None
    delivered_via: str | None = None
    started = time.time()
    while time.time() - started < 90:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(master, 65536).decode("utf-8", "replace")
            except OSError:
                break
            output += chunk
        if authorize_url is None:
            match = AUTHORIZE_URL.search(ANSI.sub("", output))
            if match:
                authorize_url = match.group(1).rstrip(".")
                params = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
                redirect_uri = params["redirect_uri"]
                trace: list[str] = []
                final_url = h.drive_authorize_url(
                    authorize_url, redirect_uri, identity, trace=trace
                )
                # Claude Code listens on its loopback callback even in --no-browser mode; deliver
                # the code there first, and paste the URL back as the documented fallback.
                try:
                    httpx.get(final_url, timeout=5)
                    delivered_via = "loopback GET"
                except Exception:
                    os.write(master, (final_url + "\r").encode())
                    delivered_via = "pasted into the prompt"
        if proc.poll() is not None:
            # drain
            while True:
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    break
                try:
                    chunk = os.read(master, 65536).decode("utf-8", "replace")
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            break
    if proc.poll() is None:
        proc.kill()
    clean = ANSI.sub("", output)
    return {
        "exit_code": proc.returncode,
        "authorize_url_seen": bool(authorize_url),
        "client_id": (
            parse_qs(urlparse(authorize_url).query).get("client_id", [None])[0]
            if authorize_url
            else None
        ),
        "scope": (
            parse_qs(urlparse(authorize_url).query).get("scope", [None])[0]
            if authorize_url
            else None
        ),
        "resource": (
            parse_qs(urlparse(authorize_url).query).get("resource", [None])[0]
            if authorize_url
            else None
        ),
        "callback_query_keys": sorted(parse_qs(urlparse(final_url).query)) if final_url else None,
        "delivered_via": delivered_via,
        "output_tail": clean.strip().splitlines()[-6:],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    result = run_login(sys.argv[1], *(sys.argv[2:4]))
    import json

    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["exit_code"] == 0 else 1)
