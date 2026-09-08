#!/usr/bin/env python3
"""A "browser" for automated logins: `fake_browser.py <authorize_url>`.

Point the BROWSER environment variable at a wrapper that runs this script (see
claude_code_login.py). When the client's `login` tool calls webbrowser.open(url), this script plays
the person: it follows rai's authorize redirect to the dev mock IdP, submits the login form for
LUCA_E2E_IDENTITY (default ygreen@company.com) and lands on the client's loopback callback.
Dev only: it needs the mock IdP as rai's identity provider.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import harness as h  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fake_browser.py <authorize_url>", file=sys.stderr)
        return 2
    identity = os.environ.get("LUCA_E2E_IDENTITY", h.DEFAULT_IDENTITY)
    decision = os.environ.get("LUCA_E2E_LOGIN_DECISION", "allow")
    trace: list[str] = []
    try:
        final = h.drive_authorize_url(sys.argv[1], identity, login_decision=decision, trace=trace)
    except Exception as exc:  # noqa: BLE001 - reported to the log, the client sees the timeout
        print(f"fake browser failed: {exc!r}; trace={trace}", file=sys.stderr)
        return 1
    print(f"fake browser done: {final[:100]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
