"""Drive a REAL Codex CLI login against the local Luca MCP server (bounded, no human).

`codex mcp login <name>` prints the authorization URL (and opens a browser); this runs it in a
pseudo-terminal with BROWSER=true so nothing opens, follows the URL through consent and the mock
IdP, delivers the loopback callback, and reports what Codex printed.

Usage: uv run python tests/e2e/codex_login.py [server_name] [identity]
"""

from __future__ import annotations

import json
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
AUTHORIZE_URL = re.compile(r"(http://localhost:8765/authorize\?[^\s\x1b\]\"']+)")


def run_login(server_name: str = "luca", identity: str = h.DEFAULT_IDENTITY) -> dict:
    master, slave = pty.openpty()
    env = {**os.environ, "BROWSER": "true", "NO_COLOR": "1"}
    proc = subprocess.Popen(
        ["codex", "mcp", "login", server_name],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=env,
    )
    os.close(slave)
    output, authorize_url, final_url, delivered = "", None, None, None
    started = time.time()
    while time.time() - started < 60:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                output += os.read(master, 65536).decode("utf-8", "replace")
            except OSError:
                break
        if authorize_url is None:
            match = AUTHORIZE_URL.search(ANSI.sub("", output))
            if match:
                authorize_url = match.group(1)
                params = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
                final_url = h.drive_authorize_url(authorize_url, params["redirect_uri"], identity)
                try:
                    httpx.get(final_url, timeout=10)
                    delivered = "loopback GET"
                except Exception as exc:  # noqa: BLE001
                    delivered = f"loopback GET failed: {exc!r}"
        if proc.poll() is not None:
            deadline = time.time() + 2
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if not ready:
                    continue
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
    q = parse_qs(urlparse(authorize_url).query) if authorize_url else {}
    return {
        "exit_code": proc.returncode,
        "authorize_url_seen": bool(authorize_url),
        "client_id": q.get("client_id", [None])[0],
        "scope": q.get("scope", [None])[0],
        "resource": q.get("resource", [None])[0],
        "redirect_uri": q.get("redirect_uri", [None])[0],
        "delivered_via": delivered,
        "output_tail": ANSI.sub("", output).strip().splitlines()[-8:],
    }


if __name__ == "__main__":
    result = run_login(*sys.argv[1:3])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["exit_code"] == 0 else 1)
