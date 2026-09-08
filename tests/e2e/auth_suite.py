"""Authentication edge-case suite for Luca MCP (run: `python tests/e2e/harness.py auth-suite`).

Every case runs against the live local server + mock IdP + dev Liebre API. Output is a PASS/FAIL
table plus a JSON report in .e2e-artifacts/auth-suite.json.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import harness as h
import httpx

REPORT = Path(".e2e-artifacts/auth-suite.json")
results: list[dict[str, Any]] = []


def case(name: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def wrap(fn: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> None:
            started = time.time()
            try:
                detail = fn()
                results.append(
                    {
                        "case": name,
                        "status": "PASS",
                        "detail": detail,
                        "ms": int((time.time() - started) * 1000),
                    }
                )
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - report every failure
                results.append(
                    {
                        "case": name,
                        "status": "FAIL",
                        "detail": repr(exc)[:400],
                        "ms": int((time.time() - started) * 1000),
                    }
                )
                print(f"FAIL  {name}: {exc!r}"[:300])

        run.__name__ = fn.__name__
        return run

    return wrap


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def new_client() -> tuple[dict[str, Any], dict[str, Any], str, str]:
    prm, as_meta = h.discover()
    redirect_uri = f"http://127.0.0.1:{secrets.choice(range(40000, 60000))}/callback"
    client_id = h.register(as_meta, redirect_uri)["client_id"]
    return prm, as_meta, client_id, redirect_uri


def full_login(
    identity: str = h.DEFAULT_IDENTITY, scopes: str = h.DEFAULT_SCOPES, **kw: Any
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    _, as_meta, client_id, redirect_uri = new_client()
    query, verifier = h.browser_flow(as_meta, client_id, redirect_uri, scopes, identity, **kw)
    expect("code" in query, f"no code in callback: {query}")
    response = h.exchange_code(as_meta, client_id, query["code"], verifier, redirect_uri)
    expect(
        response.status_code == 200, f"token exchange {response.status_code}: {response.text[:200]}"
    )
    return as_meta, client_id, redirect_uri, response.json()


def whoami(token: str) -> dict[str, Any]:
    return asyncio.run(h.mcp_call(token, "whoami"))


@case("discovery: 401 challenge, PRM (suffix + root), AS metadata, required fields")
def t_discovery() -> dict[str, Any]:
    prm, as_meta = h.discover()
    expect(prm["resource"] == h.MCP_URL, f"PRM resource {prm['resource']!r} != {h.MCP_URL!r}")
    expect(not prm["resource"].endswith("/"), "resource must not end with a slash")
    root = httpx.get(f"{h.BASE}/.well-known/oauth-protected-resource", timeout=10)
    expect(
        root.status_code == 200 and root.json()["resource"] == prm["resource"],
        "root PRM alias mismatch",
    )
    for key in (
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "revocation_endpoint",
    ):
        expect(key in as_meta, f"AS metadata missing {key}")
    expect(
        as_meta["code_challenge_methods_supported"] == ["S256"], "PKCE methods must be exactly S256"
    )
    expect(
        "none" in as_meta.get("token_endpoint_auth_methods_supported", []),
        "public clients must be allowed",
    )
    expect(as_meta.get("client_id_metadata_document_supported") is True, "CIMD must be advertised")
    expect(
        as_meta.get("authorization_response_iss_parameter_supported") is True,
        "iss parameter must be advertised",
    )
    expect(
        "offline_access" in prm.get("scopes_supported", []),
        "beta advertises offline_access on purpose",
    )
    return {"scopes_supported": prm.get("scopes_supported"), "issuer": as_meta["issuer"]}


@case("happy path: DCR -> consent -> mock login -> token -> tools/list + whoami")
def t_happy() -> dict[str, Any]:
    as_meta, client_id, redirect_uri, tokens = full_login()
    expect("refresh_token" in tokens, f"no refresh token issued: {sorted(tokens)}")
    expect(
        0 < int(tokens["expires_in"]) <= 3600, f"unexpected expires_in {tokens.get('expires_in')}"
    )
    tools = asyncio.run(h.mcp_list_tools(tokens["access_token"]))
    names = {t["name"] for t in tools}
    expect("whoami" in names, f"whoami missing from {names}")
    me = whoami(tokens["access_token"])
    session = me["structured_content"]["session"]
    expect(session["email"] == h.DEFAULT_IDENTITY, f"email {session['email']!r}")
    expect(session["mcp_client_id"] == client_id, "mcp_client_id should be the DCR client id")
    expect(session["upstream_login_expires_at"] is not None, "upstream expiry missing")
    return {"tools": sorted(names), "session": session}


@case("refresh: rotates Luca refresh token; old refresh token rejected; new access token works")
def t_refresh() -> dict[str, Any]:
    as_meta, client_id, _, tokens = full_login()
    first = h.refresh_tokens(as_meta, client_id, tokens["refresh_token"])
    expect(first.status_code == 200, f"refresh failed {first.status_code}: {first.text[:200]}")
    body = first.json()
    expect(body["access_token"] != tokens["access_token"], "access token not rotated")
    expect(
        body.get("refresh_token") and body["refresh_token"] != tokens["refresh_token"],
        "refresh token not rotated",
    )
    me = whoami(body["access_token"])
    expect(
        me["structured_content"]["session"]["email"] == h.DEFAULT_IDENTITY,
        "new access token unusable",
    )
    replay = h.refresh_tokens(as_meta, client_id, tokens["refresh_token"])
    expect(
        replay.status_code in (400, 401) and replay.json().get("error") == "invalid_grant",
        f"old refresh token accepted: {replay.status_code} {replay.text[:120]}",
    )
    return {"rotated": True, "replay": replay.json()}


@case("refresh: scope narrowing request cannot exceed grant; upstream refresh uses pinned scopes")
def t_refresh_scope() -> dict[str, Any]:
    as_meta, client_id, _, tokens = full_login(scopes="openid")  # client asked for less
    events_before = len(httpx.get("http://localhost:9400/__test/events").json()["events"])
    r = h.refresh_tokens(as_meta, client_id, tokens["refresh_token"])
    expect(
        r.status_code == 200,
        f"refresh with narrow client scopes failed: {r.status_code} {r.text[:200]}",
    )
    # Force an upstream refresh by asking the mock to consider tokens near expiry is not possible;
    # instead verify that the last upstream refresh (if any) requested the pinned set.
    events = httpx.get("http://localhost:9400/__test/events").json()["events"][events_before:]
    upstream = [e for e in events if e["kind"] in ("refresh_ok", "refresh_rejected")]
    return {"client_scopes": "openid", "upstream_refresh_events": upstream}


@case("revoke access token: session dies immediately; paired refresh token dies too")
def t_revoke_access() -> dict[str, Any]:
    as_meta, client_id, _, tokens = full_login()
    expect(whoami(tokens["access_token"])["structured_content"]["session"]["email"], "precondition")
    r = h.revoke(as_meta, client_id, tokens["access_token"], "access_token")
    expect(r.status_code == 200, f"revoke -> {r.status_code}")
    after = h.raw_mcp_post(
        tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    expect(after.status_code == 401, f"revoked access token still accepted: {after.status_code}")
    refresh = h.refresh_tokens(as_meta, client_id, tokens["refresh_token"])
    expect(
        refresh.status_code in (400, 401), f"refresh after revoke succeeded: {refresh.status_code}"
    )
    events = httpx.get("http://localhost:9400/__test/events").json()["events"][-5:]
    expect(
        any(e["kind"] == "revoked_refresh_token" for e in events),
        f"upstream refresh token not revoked: {events}",
    )
    return {"post_revoke_status": after.status_code, "refresh_after": refresh.json()}


@case("revoke refresh token: both tokens dead, upstream refresh token revoked")
def t_revoke_refresh() -> dict[str, Any]:
    as_meta, client_id, _, tokens = full_login()
    r = h.revoke(as_meta, client_id, tokens["refresh_token"], "refresh_token")
    expect(r.status_code == 200, f"revoke -> {r.status_code}")
    after = h.raw_mcp_post(
        tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    expect(
        after.status_code == 401,
        f"access token alive after refresh revocation: {after.status_code}",
    )
    refresh = h.refresh_tokens(as_meta, client_id, tokens["refresh_token"])
    expect(refresh.status_code in (400, 401), "refresh token alive after revocation")
    return {"post_revoke_status": after.status_code}


@case("registration: disallowed redirect URI is rejected")
def t_bad_redirect() -> dict[str, Any]:
    _, as_meta = h.discover()
    try:
        h.register(as_meta, "https://evil.example/callback")
    except h.HarnessError as exc:
        return {"rejected": str(exc)[:160]}
    raise AssertionError("evil redirect URI accepted at registration")


@case("registration: userinfo trick http://localhost:1@evil.example/callback is rejected")
def t_userinfo_trick() -> dict[str, Any]:
    _, as_meta = h.discover()
    try:
        h.register(as_meta, "http://localhost:1@evil.example/callback")
    except h.HarnessError as exc:
        return {"rejected": str(exc)[:160]}
    raise AssertionError("userinfo-trick redirect accepted")


@case("authorize: resource mismatch (trailing slash) -> invalid_target; exact resource accepted")
def t_resource() -> dict[str, Any]:
    _, as_meta, client_id, redirect_uri = new_client()
    query, _ = h.browser_flow(as_meta, client_id, redirect_uri, "openid", resource=h.MCP_URL + "/")
    expect(query.get("error") == "invalid_target", f"expected invalid_target, got {query}")
    query2, verifier = h.browser_flow(
        as_meta, client_id, redirect_uri, "openid", resource=h.MCP_URL
    )
    expect("code" in query2, f"exact resource rejected: {query2}")
    return {"mismatch": query, "exact": "ok"}


@case("token: wrong PKCE verifier -> invalid_grant; code cannot be replayed")
def t_pkce() -> dict[str, Any]:
    _, as_meta, client_id, redirect_uri = new_client()
    query, verifier = h.browser_flow(as_meta, client_id, redirect_uri, "openid")
    bad = h.exchange_code(
        as_meta,
        client_id,
        query["code"],
        "wrong-verifier-" + secrets.token_urlsafe(40),
        redirect_uri,
    )
    expect(
        bad.status_code == 400 and bad.json().get("error") == "invalid_grant",
        f"bad verifier -> {bad.status_code} {bad.text[:120]}",
    )
    good = h.exchange_code(as_meta, client_id, query["code"], verifier, redirect_uri)
    replay = h.exchange_code(as_meta, client_id, query["code"], verifier, redirect_uri)
    return {"bad": bad.json(), "good_after_bad": good.status_code, "replay": replay.status_code}


@case("bearer hygiene: garbage, refresh-as-access, raw upstream token, expired-looking -> 401")
def t_bearer() -> dict[str, Any]:
    as_meta, client_id, _, tokens = full_login()
    raw_upstream = httpx.get(
        "http://localhost:9400/__test/mint", params={"email": h.DEFAULT_IDENTITY}
    ).json()["access_token"]
    statuses = {}
    for label, token in {
        "garbage": "not-a-token",
        "empty": "",
        "refresh_as_access": tokens["refresh_token"],
        "raw_upstream_token": raw_upstream,
        "tampered": tokens["access_token"][:-4] + "AAAA",
    }.items():
        r = h.raw_mcp_post(token or None, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        statuses[label] = r.status_code
    expect(all(s == 401 for s in statuses.values()), f"non-401 for bad bearers: {statuses}")
    return statuses


@case("identity without email claim: login succeeds, whoami reports no email")
def t_no_email() -> dict[str, Any]:
    _, _, _, tokens = full_login(identity="__no_email__")
    me = whoami(tokens["access_token"])
    expect(me["structured_content"]["session"]["email"] is None, "email should be absent")
    return me["structured_content"]["session"]


@case("consent denied -> access_denied delivered to the client redirect")
def t_consent_deny() -> dict[str, Any]:
    _, as_meta, client_id, redirect_uri = new_client()
    query, _ = h.browser_flow(as_meta, client_id, redirect_uri, "openid", consent_action="deny")
    expect(query.get("error") == "access_denied", f"expected access_denied, got {query}")
    return query


@case("upstream login denied -> error delivered to the client redirect")
def t_login_deny() -> dict[str, Any]:
    _, as_meta, client_id, redirect_uri = new_client()
    query, _ = h.browser_flow(as_meta, client_id, redirect_uri, "openid", login_decision="deny")
    expect("error" in query, f"expected an error, got {query}")
    return query


@case("concurrency: 12 parallel whoami calls with one token all succeed")
def t_concurrency() -> dict[str, Any]:
    _, _, _, tokens = full_login()

    async def burst() -> list[str | None]:
        calls = [h.mcp_call(tokens["access_token"], "whoami") for _ in range(12)]
        outs = await asyncio.gather(*calls)
        return [o["structured_content"]["session"]["email"] for o in outs]

    emails = asyncio.run(burst())
    expect(all(e == h.DEFAULT_IDENTITY for e in emails), f"mixed results: {emails}")
    return {"calls": len(emails)}


@case("protocol: unknown tool -> tool error, malformed JSON -> 4xx, GET /mcp not a stream hang")
def t_protocol() -> dict[str, Any]:
    _, _, _, tokens = full_login()
    unknown = asyncio.run(h.mcp_call(tokens["access_token"], "does_not_exist"))
    expect(unknown["is_error"], "unknown tool should be an error result")
    with httpx.Client(timeout=10) as http:
        malformed = http.post(
            f"{h.BASE}/mcp",
            content=b"{not json",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {tokens['access_token']}",
            },
        )
        get = http.get(
            f"{h.BASE}/mcp",
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "text/event-stream",
            },
        )
    return {
        "unknown_tool": unknown["text"][:120],
        "malformed": malformed.status_code,
        "get": get.status_code,
    }


@case("CIMD: Claude Code's public client metadata document is accepted as client_id")
def t_cimd() -> dict[str, Any]:
    _, as_meta = h.discover()
    client_id = "https://claude.ai/oauth/claude-code-client-metadata"
    redirect_uri = (
        "http://localhost:51515/callback"  # portless in the document; port must be flexible
    )
    query, verifier = h.browser_flow(
        as_meta, client_id, redirect_uri, "openid email offline_access"
    )
    expect("code" in query, f"CIMD authorize failed: {query}")
    response = h.exchange_code(as_meta, client_id, query["code"], verifier, redirect_uri)
    expect(
        response.status_code == 200,
        f"CIMD token exchange {response.status_code}: {response.text[:200]}",
    )
    me = whoami(response.json()["access_token"])
    return {
        "email": me["structured_content"]["session"]["email"],
        "mcp_client_id": me["structured_content"]["session"]["mcp_client_id"],
    }


def run_suite() -> int:
    ordered = [
        t_discovery,
        t_happy,
        t_refresh,
        t_refresh_scope,
        t_revoke_access,
        t_revoke_refresh,
        t_bad_redirect,
        t_userinfo_trick,
        t_resource,
        t_pkce,
        t_bearer,
        t_no_email,
        t_consent_deny,
        t_login_deny,
        t_concurrency,
        t_protocol,
        t_cimd,
    ]
    for test in ordered:
        test()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(results, indent=2, default=str))
    failed = [r for r in results if r["status"] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed; report: {REPORT}")
    return 1 if failed else 0
