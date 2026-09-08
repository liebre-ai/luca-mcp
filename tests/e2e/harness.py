"""E2E harness: behaves like an MCP client doing the full OAuth 2.1 dance against Luca MCP.

It drives the browser hops itself (consent page, mock IdP login page) with an HTTP client and a
cookie jar, so the whole chain can be exercised without a human: discovery -> registration ->
/authorize -> consent -> mock login -> /auth/callback -> code -> /token -> tools.

Usage (server + mock IdP running, see dev/run_local.sh):
    uv run python tests/e2e/harness.py login [--identity ygreen@company.com] [--scopes "openid email offline_access"]
    uv run python tests/e2e/harness.py tools
    uv run python tests/e2e/harness.py call whoami '{}'
    uv run python tests/e2e/harness.py refresh
    uv run python tests/e2e/harness.py revoke [--hint refresh_token|access_token]
    uv run python tests/e2e/harness.py auth-suite          # the authentication edge-case suite

State is kept in .e2e-artifacts/session.json so other scripts can reuse the login.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

BASE = "http://localhost:8765"
MCP_URL = f"{BASE}/mcp"
ARTIFACTS = Path(".e2e-artifacts")
SESSION_FILE = ARTIFACTS / "session.json"
DEFAULT_IDENTITY = "ygreen@company.com"
DEFAULT_SCOPES = "openid email offline_access"
HIDDEN_INPUT = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
HIDDEN_INPUT_ALT = re.compile(r'<input[^>]*name="([^"]+)"[^>]*type="hidden"[^>]*value="([^"]*)"')
FORM_ACTION = re.compile(r'<form[^>]*action="([^"]*)"')


class HarnessError(RuntimeError):
    pass


@dataclass
class Session:
    base: str = BASE
    client_id: str | None = None
    redirect_uri: str = "http://127.0.0.1:53131/callback"
    identity: str = DEFAULT_IDENTITY
    scopes: str = DEFAULT_SCOPES
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None
    token_response: dict[str, Any] = field(default_factory=dict)
    prm: dict[str, Any] = field(default_factory=dict)
    as_meta: dict[str, Any] = field(default_factory=dict)

    def save(self, path: Path = SESSION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> Session:
        if not path.exists():
            raise HarnessError("no session; run `harness.py login` first")
        return cls(**json.loads(path.read_text()))


# ------------------------------------------------------------------------------------------------
# OAuth steps
# ------------------------------------------------------------------------------------------------


def discover(base: str = BASE) -> tuple[dict[str, Any], dict[str, Any]]:
    """Follow the spec: 401 -> resource_metadata -> PRM -> authorization server metadata."""
    with httpx.Client(timeout=10) as http:
        challenge = http.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        if challenge.status_code != 401:
            raise HarnessError(f"expected 401 challenge, got {challenge.status_code}")
        www = challenge.headers.get("www-authenticate", "")
        match = re.search(r'resource_metadata="([^"]+)"', www)
        if not match:
            raise HarnessError(f"no resource_metadata in WWW-Authenticate: {www!r}")
        prm = http.get(match.group(1)).raise_for_status().json()
        issuer = prm["authorization_servers"][0]
        parsed = urlparse(issuer)
        path = parsed.path.rstrip("/")
        candidates = [
            f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{path}",
            f"{parsed.scheme}://{parsed.netloc}/.well-known/openid-configuration{path}",
        ]
        for url in candidates:
            response = http.get(url)
            if response.status_code == 200:
                as_meta = response.json()
                if as_meta.get("issuer") != issuer:
                    raise HarnessError(
                        f"issuer mismatch: metadata {as_meta.get('issuer')!r} != PRM {issuer!r}"
                    )
                return prm, as_meta
        raise HarnessError(f"no authorization server metadata at {candidates}")


def register(
    as_meta: dict[str, Any], redirect_uri: str, client_name: str = "luca e2e harness"
) -> dict[str, Any]:
    with httpx.Client(timeout=10) as http:
        response = http.post(
            as_meta["registration_endpoint"],
            json={
                "client_name": client_name,
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "application_type": "native",
            },
        )
    if response.status_code not in (200, 201):
        raise HarnessError(f"registration failed {response.status_code}: {response.text[:300]}")
    return response.json()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _hidden_fields(html: str) -> dict[str, str]:
    fields = dict(HIDDEN_INPUT.findall(html))
    fields.update(dict(HIDDEN_INPUT_ALT.findall(html)))
    return fields


def browser_flow(
    as_meta: dict[str, Any],
    client_id: str,
    redirect_uri: str,
    scopes: str,
    identity: str = DEFAULT_IDENTITY,
    *,
    resource: str | None = MCP_URL,
    consent_action: str = "approve",
    login_decision: str = "allow",
    extra_authorize_params: dict[str, str] | None = None,
    trace: list[str] | None = None,
) -> tuple[dict[str, str], str]:
    """Run /authorize -> consent -> mock login -> /auth/callback. Returns (callback_query, verifier)."""
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = resource
    if extra_authorize_params:
        params.update(extra_authorize_params)
    url = f"{as_meta['authorization_endpoint']}?{urlencode(params)}"
    log = trace if trace is not None else []
    with httpx.Client(timeout=15, follow_redirects=False) as http:
        for _hop in range(20):
            log.append(f"GET {url[:140]}")
            response = http.get(
                url, headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = urljoin(url, response.headers["location"])
                if location.startswith(redirect_uri):
                    query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
                    if query.get("state") != state:
                        raise HarnessError(f"state mismatch: {query.get('state')} != {state}")
                    return query, verifier
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
            if "csrf_token" in fields and "txn_id" in fields:  # Luca consent page
                fields["action"] = consent_action
                log.append(f"POST consent ({consent_action})")
            elif "client_id" in fields and "redirect_uri" in fields:  # mock IdP login page
                fields["identity"] = identity
                fields["decision"] = login_decision
                log.append(f"POST mock login ({identity}, {login_decision})")
            else:
                raise HarnessError(f"unrecognised page at {url[:100]}: {html[:200]}")
            response = http.post(action, data=fields, headers={"Sec-Fetch-Site": "same-origin"})
            if response.status_code not in (301, 302, 303, 307, 308):
                raise HarnessError(
                    f"form POST {action} -> {response.status_code}: {response.text[:300]}"
                )
            location = urljoin(action, response.headers["location"])
            if location.startswith(redirect_uri):
                query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
                return query, verifier
            url = location
    raise HarnessError("too many hops in the browser flow")


def exchange_code(
    as_meta: dict[str, Any],
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    resource: str | None = MCP_URL,
) -> httpx.Response:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if resource:
        data["resource"] = resource
    with httpx.Client(timeout=15) as http:
        return http.post(as_meta["token_endpoint"], data=data)


def refresh_tokens(
    as_meta: dict[str, Any], client_id: str, refresh_token: str, scope: str | None = None
) -> httpx.Response:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if scope:
        data["scope"] = scope
    with httpx.Client(timeout=15) as http:
        return http.post(as_meta["token_endpoint"], data=data)


def revoke(
    as_meta: dict[str, Any], client_id: str, token: str, hint: str | None = None
) -> httpx.Response:
    data = {"token": token, "client_id": client_id}
    if hint:
        data["token_type_hint"] = hint
    with httpx.Client(timeout=15) as http:
        return http.post(as_meta["revocation_endpoint"], data=data)


def login(
    identity: str = DEFAULT_IDENTITY,
    scopes: str = DEFAULT_SCOPES,
    *,
    base: str = BASE,
    save: bool = True,
) -> Session:
    prm, as_meta = discover(base)
    session = Session(base=base, identity=identity, scopes=scopes, prm=prm, as_meta=as_meta)
    session.redirect_uri = f"http://127.0.0.1:{secrets.choice(range(40000, 60000))}/callback"
    registration = register(as_meta, session.redirect_uri)
    session.client_id = registration["client_id"]
    query, verifier = browser_flow(
        as_meta, session.client_id, session.redirect_uri, scopes, identity
    )
    if "error" in query:
        raise HarnessError(f"authorization error: {query}")
    response = exchange_code(
        as_meta, session.client_id, query["code"], verifier, session.redirect_uri
    )
    if response.status_code != 200:
        raise HarnessError(f"token exchange failed {response.status_code}: {response.text[:300]}")
    session.token_response = response.json()
    session.access_token = session.token_response["access_token"]
    session.refresh_token = session.token_response.get("refresh_token")
    session.expires_at = time.time() + int(session.token_response.get("expires_in", 0))
    if save:
        session.save()
    return session


# ------------------------------------------------------------------------------------------------
# MCP calls
# ------------------------------------------------------------------------------------------------


async def mcp_list_tools(token: str, base: str = BASE) -> list[dict[str, Any]]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    async with Client(f"{base}/mcp", auth=BearerAuth(token), timeout=60) as client:
        tools = await client.list_tools()
        return [t.model_dump(mode="json") for t in tools]


async def mcp_call(
    token: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    base: str = BASE,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    async with Client(f"{base}/mcp", auth=BearerAuth(token), timeout=120) as client:
        result = await client.call_tool(tool, arguments or {}, raise_on_error=raise_on_error)
        return {
            "is_error": result.is_error,
            "structured_content": result.structured_content,
            "text": "\n".join(
                getattr(c, "text", "") for c in result.content if getattr(c, "text", None)
            ),
        }


def raw_mcp_post(token: str | None, body: dict[str, Any], base: str = BASE) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=30) as http:
        return http.post(f"{base}/mcp", json=body, headers=headers)


# ------------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------------


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login")
    p_login.add_argument("--identity", default=DEFAULT_IDENTITY)
    p_login.add_argument("--scopes", default=DEFAULT_SCOPES)
    sub.add_parser("tools")
    p_call = sub.add_parser("call")
    p_call.add_argument("tool")
    p_call.add_argument("arguments", nargs="?", default="{}")
    sub.add_parser("refresh")
    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("--hint", choices=["access_token", "refresh_token"], default=None)
    sub.add_parser("auth-suite")
    args = parser.parse_args(argv)

    if args.cmd == "login":
        session = login(args.identity, args.scopes)
        _print(
            {
                "client_id": session.client_id,
                "identity": session.identity,
                "scopes": session.scopes,
                "token_response_keys": sorted(session.token_response),
                "expires_in": session.token_response.get("expires_in"),
            }
        )
        return 0
    if args.cmd == "tools":
        session = Session.load()
        tools = asyncio.run(mcp_list_tools(session.access_token or ""))
        for tool in tools:
            print(f"- {tool['name']}: {(tool.get('description') or '').splitlines()[0][:100]}")
        return 0
    if args.cmd == "call":
        session = Session.load()
        _print(
            asyncio.run(mcp_call(session.access_token or "", args.tool, json.loads(args.arguments)))
        )
        return 0
    if args.cmd == "refresh":
        session = Session.load()
        response = refresh_tokens(
            session.as_meta, session.client_id or "", session.refresh_token or ""
        )
        print(response.status_code, response.text[:300])
        if response.status_code == 200:
            body = response.json()
            session.access_token = body["access_token"]
            session.refresh_token = body.get("refresh_token", session.refresh_token)
            session.token_response = body
            session.save()
        return 0
    if args.cmd == "revoke":
        session = Session.load()
        token = session.refresh_token if args.hint == "refresh_token" else session.access_token
        response = revoke(session.as_meta, session.client_id or "", token or "", args.hint)
        print(response.status_code, response.text[:200])
        return 0
    if args.cmd == "auth-suite":
        from auth_suite import run_suite  # local import: tests/e2e/auth_suite.py

        return run_suite()
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
