"""Real Claude Code CLI session: log in through the `login` tool, then use the data tools.

    uv run python tests/e2e/claude_code_login.py [--source local|git] [--identity EMAIL] [--rai-url URL]

What happens: a fresh credential directory is created (nobody is logged in), Claude Code is started
headless with the Luca MCP server (`uv run luca-mcp` from this checkout, or the uvx install from
GitHub with --source git) and asked to call whoami, log in, and read bu-2. The `login` tool opens
"the browser": BROWSER points at a wrapper around fake_browser.py, which completes the Liebre login
at the dev mock IdP as the chosen identity. The script prints Claude's answer and checks that the
credentials now exist. Requires: rai dev server (LUCA_RAI_URL), mock IdP (dev/run_local.sh),
`claude` on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / ".e2e-artifacts"
GIT_SOURCE = os.environ.get("LUCA_E2E_GIT_SOURCE", "git+https://github.com/liebre-ai/luca-mcp")

PROMPT = (
    "You have the 'luca' MCP server. Do exactly this, in order: "
    "1) call whoami and note whether I am logged in; "
    "2) if not logged in, call login (it opens my browser and returns when I finished) and then call "
    "whoami again; "
    "3) call list_businesses with query 'curiel'; "
    "4) call get_trial_balance for business bu-2 with start_period_id 202608. "
    "Then answer in at most 6 short lines: whether the first whoami said logged in, the e-mail and "
    "Liebre user id after login, the business ids returned by the query, and the trial balance "
    "total_debits, total_credits and row_count. No preamble."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("local", "git"), default="local")
    parser.add_argument("--identity", default="ygreen@company.com")
    parser.add_argument(
        "--rai-url", default=os.environ.get("LUCA_RAI_URL", "http://localhost:3030")
    )
    parser.add_argument("--max-turns", type=int, default=14)
    args = parser.parse_args()

    if not shutil.which("claude"):
        print("claude CLI not found on PATH", file=sys.stderr)
        return 2

    run_dir = ARTIFACTS / "claude-code-login"
    shutil.rmtree(run_dir, ignore_errors=True)
    config_dir = run_dir / "config"
    config_dir.mkdir(parents=True)

    python = (
        sys.executable
    )  # the venv interpreter itself; resolving the symlink would leave the venv
    browser = run_dir / "browser.sh"
    browser.write_text(
        "#!/bin/sh\n"
        f'exec "{python}" "{REPO / "tests/e2e/fake_browser.py"}" "$1" >> "{run_dir / "browser.log"}" 2>&1\n'
    )
    browser.chmod(0o755)

    if args.source == "git":
        command, cmd_args = "uvx", ["--from", GIT_SOURCE, "luca-mcp"]
    else:
        command, cmd_args = "uv", ["run", "--project", str(REPO), "luca-mcp"]
    server_env = {
        "LUCA_RAI_URL": args.rai_url,
        "LUCA_MCP_CONFIG_DIR": str(config_dir),
        "LUCA_MCP_CREDENTIAL_STORE": "file",
        "LUCA_MCP_LOGIN_TIMEOUT": "120",
        "BROWSER": str(browser),
        "LUCA_E2E_IDENTITY": args.identity,
        "PATH": os.environ.get("PATH", ""),
    }
    mcp_config = run_dir / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "luca": {
                        "type": "stdio",
                        "command": command,
                        "args": cmd_args,
                        "env": server_env,
                    }
                }
            },
            indent=2,
        )
    )

    cmd = [
        "claude",
        "-p",
        PROMPT,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__luca__whoami",
        "mcp__luca__login",
        "mcp__luca__list_businesses",
        "mcp__luca__get_trial_balance",
        "--output-format",
        "json",
        "--max-turns",
        str(args.max_turns),
    ]
    # An empty temporary cwd: Claude Code must not pick up this repository's CLAUDE.md or .mcp.json.
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900
        )
    (run_dir / "claude.stdout.json").write_text(proc.stdout)
    (run_dir / "claude.stderr.txt").write_text(proc.stderr)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"claude exited {proc.returncode}; stdout was not JSON:\n{proc.stdout[:800]}")
        print(proc.stderr[-800:], file=sys.stderr)
        return 1

    credentials = config_dir / "credentials.json"
    stored = json.loads(credentials.read_text()) if credentials.exists() else {}
    entry = stored.get(args.rai_url.rstrip("/")) or {}
    summary = {
        "source": args.source,
        "identity": args.identity,
        "claude_exit": proc.returncode,
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "credentials_stored": bool(entry.get("access_token")),
        "refresh_token_stored": bool(entry.get("refresh_token")),
        "browser_log": (run_dir / "browser.log").read_text()[-300:]
        if (run_dir / "browser.log").exists()
        else None,
    }
    print(json.dumps(summary, indent=2))
    print("\n--- Claude's answer ---\n" + str(result.get("result", ""))[:2000])
    ok = (
        proc.returncode == 0
        and not result.get("is_error")
        and summary["credentials_stored"]
        and summary["refresh_token_stored"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
