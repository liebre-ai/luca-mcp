"""Token lifetime suite: Luca access-token expiry and transparent upstream refresh.

Restarts the local stack with short lifetimes (mock IdP access tokens of 330 s so the 300 s
refresh threshold is crossed after ~30 s; Luca access tokens of 40 s), then:

1. login -> whoami works;
2. after the Luca access token expires, the bearer is rejected (401) and the client refreshes
   with its Luca refresh token -> new token works;
3. once the upstream token is within the refresh threshold, a tool call triggers exactly one
   transparent upstream refresh whose scope is the pinned set, and the next call does not
   refresh again;
4. after the IdP revokes every refresh token, a call inside the threshold still succeeds on the
   still-valid upstream token (refresh failure is not fatal while the token is valid).

Restores the default stack at the end. Run: uv run python tests/e2e/expiry_suite.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import harness as h  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MOCK = "http://localhost:9400"
PINNED = "openid email offline_access"


def restart(env: dict[str, str] | None = None) -> None:
    merged = {**os.environ, **(env or {})}
    subprocess.run(
        [str(ROOT / "dev" / "run_local.sh")], check=True, env=merged, cwd=ROOT, capture_output=True
    )


def events() -> list[dict]:
    return httpx.get(f"{MOCK}/__test/events", timeout=10).json()["events"]


def refresh_events(since: int) -> list[dict]:
    return [e for e in events()[since:] if e["kind"] in ("refresh_ok", "refresh_rejected")]


def whoami_ok(token: str) -> str:
    import asyncio

    out = asyncio.run(h.mcp_call(token, "whoami"))
    if out["is_error"]:
        raise AssertionError(f"whoami failed: {out['text'][:200]}")
    return out["structured_content"]["session"]["email"]


def main() -> int:
    report: dict[str, object] = {}
    try:
        print("restarting stack with short lifetimes (mock access 330 s, Luca access 40 s)…")
        restart({"MOCK_IDP_ACCESS_TTL": "330", "LUCA_MCP_ACCESS_TOKEN_TTL_SECONDS": "40"})
        t0 = time.time()
        session = h.login(save=False)
        report["expires_in"] = session.token_response.get("expires_in")
        assert session.token_response.get("expires_in") == 40, report
        assert whoami_ok(session.access_token or "") == h.DEFAULT_IDENTITY
        baseline = len(events())
        print("step 1 ok: login + whoami")

        # 2. Luca access token expiry -> 401 -> client refresh
        time.sleep(max(0, 42 - (time.time() - t0)))
        expired = h.raw_mcp_post(
            session.access_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert expired.status_code == 401, f"expired Luca token accepted: {expired.status_code}"
        refreshed = h.refresh_tokens(
            session.as_meta, session.client_id or "", session.refresh_token or ""
        )
        assert refreshed.status_code == 200, (
            f"client refresh failed: {refreshed.status_code} {refreshed.text[:200]}"
        )
        new_access = refreshed.json()["access_token"]
        new_refresh = refreshed.json().get("refresh_token")
        assert new_refresh and new_refresh != session.refresh_token, (
            "Luca refresh token not rotated"
        )
        print("step 2 ok: expired Luca token rejected, client refresh issued new tokens")

        # 3. upstream token within threshold (330 - 300 = 30 s after issue) -> one transparent refresh
        assert whoami_ok(new_access) == h.DEFAULT_IDENTITY
        first = refresh_events(baseline)
        assert len(first) >= 1 and first[-1]["kind"] == "refresh_ok", (
            f"no upstream refresh: {first}"
        )
        assert first[-1]["scope"] == PINNED, (
            f"upstream refresh scope {first[-1]['scope']!r} != pinned"
        )
        count_after_first = len(events())
        assert whoami_ok(new_access) == h.DEFAULT_IDENTITY
        assert not refresh_events(count_after_first), (
            "a second call refreshed again inside the fresh window"
        )
        report["upstream_refresh"] = first[-1]
        print("step 3 ok: exactly one transparent upstream refresh with pinned scopes")

        # 4. IdP revokes all refresh tokens; inside the threshold the still-valid token keeps working
        time.sleep(32)
        httpx.post(f"{MOCK}/__test/expire_all_refresh", timeout=10)
        before = len(events())
        assert whoami_ok(new_access) == h.DEFAULT_IDENTITY, "valid upstream token should still work"
        rejected = [e for e in events()[before:] if e["kind"] == "refresh_rejected"]
        report["refresh_rejected_events"] = len(rejected)
        assert rejected, "expected the transparent refresh attempt to be rejected by the IdP"
        print("step 4 ok: refresh rejected upstream but the valid token still serves calls")
        print("PASS expiry suite", json.dumps(report, default=str))
        return 0
    except AssertionError as exc:
        print("FAIL expiry suite:", exc)
        return 1
    finally:
        print("restoring default stack…")
        restart()


if __name__ == "__main__":
    sys.exit(main())
