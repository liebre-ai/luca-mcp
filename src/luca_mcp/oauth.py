"""OAuth 2.1 login against rai's authorization server, and token storage.

Flow (authorization code + PKCE, public client ``luca-mcp``):
  discover rai metadata -> open the browser at /oauth/authorize -> the user logs in with their Liebre
  account -> rai redirects to a loopback callback served here -> exchange the code at /oauth/token
  -> store access + refresh tokens (OS keychain, file fallback).

Tokens are rai-issued ``mcp_access`` JWTs (30 minutes) with rotating refresh tokens; nothing from
Auth0 ever reaches this machine.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from luca_mcp.config import SERVICE_NAME, Config

logger = logging.getLogger("luca_mcp.oauth")


class LoginError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str
    rai_url: str
    obtained_at: float

    def expired(self, skew: float = 60.0) -> bool:
        return time.time() >= self.expires_at - skew


# ------------------------------------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------------------------------------


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError  # noqa: F401

        backend = keyring.get_keyring()
        if "fail" in type(backend).__module__ or "null" in type(backend).__module__:
            return None
        return keyring
    except Exception:  # keyring unavailable (no backend, headless box)
        return None


def _account(config: Config) -> str:
    return hashlib.sha256(config.rai_url.encode()).hexdigest()[:16]


def save_tokens(config: Config, tokens: Tokens) -> str:
    payload = json.dumps(asdict(tokens))
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE_NAME, _account(config), payload)
            return "keychain"
        except Exception as exc:  # fall back to the file
            logger.debug("keyring write failed: %s", exc)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    path = config.credentials_file
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    data[config.rai_url] = json.loads(payload)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)
    return "file"


def load_tokens(config: Config) -> Tokens | None:
    kr = _keyring()
    if kr is not None:
        try:
            raw = kr.get_password(SERVICE_NAME, _account(config))
            if raw:
                return Tokens(**json.loads(raw))
        except Exception as exc:
            logger.debug("keyring read failed: %s", exc)
    path = config.credentials_file
    if path.exists():
        try:
            data = json.loads(path.read_text())
            entry = data.get(config.rai_url)
            if entry:
                return Tokens(**entry)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def clear_tokens(config: Config) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE_NAME, _account(config))
        except Exception:
            pass
    path = config.credentials_file
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.pop(config.rai_url, None)
            path.write_text(json.dumps(data, indent=2))
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------
# Discovery and token endpoint
# ------------------------------------------------------------------------------------------------


def discover(config: Config) -> dict[str, Any]:
    url = f"{config.rai_url}/.well-known/oauth-authorization-server"
    try:
        response = httpx.get(url, timeout=15, follow_redirects=True)
        response.raise_for_status()
        meta = response.json()
    except Exception as exc:
        raise LoginError(f"Could not read rai's OAuth metadata at {url}: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint"):
        if key not in meta:
            raise LoginError(f"rai's OAuth metadata lacks {key}")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise LoginError("rai does not advertise PKCE S256")
    return meta


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _token_request(config: Config, meta: dict[str, Any], data: dict[str, str]) -> dict[str, Any]:
    response = httpx.post(meta["token_endpoint"], data=data, timeout=30)
    if response.status_code != 200:
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:200]}
        raise LoginError(f"rai token endpoint answered {response.status_code}: {body}")
    return response.json()


def _tokens_from_response(
    config: Config, body: dict[str, Any], previous: Tokens | None = None
) -> Tokens:
    now = time.time()
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else None),
        expires_at=now + float(body.get("expires_in", 1800)),
        scope=body.get("scope") or (previous.scope if previous else config.scopes),
        rai_url=config.rai_url,
        obtained_at=now,
    )


def refresh(config: Config, tokens: Tokens) -> Tokens:
    if not tokens.refresh_token:
        raise LoginError("No refresh token stored; run `luca-mcp login`.")
    meta = discover(config)
    body = _token_request(
        config,
        meta,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": config.client_id,
        },
    )
    new_tokens = _tokens_from_response(config, body, previous=tokens)
    save_tokens(config, new_tokens)
    return new_tokens


def revoke(config: Config, tokens: Tokens) -> None:
    try:
        meta = discover(config)
        endpoint = meta.get("revocation_endpoint")
        if endpoint and tokens.refresh_token:
            httpx.post(
                endpoint,
                data={"token": tokens.refresh_token, "client_id": config.client_id},
                timeout=15,
            )
    except Exception as exc:  # best effort; local credentials are cleared regardless
        logger.debug("revocation failed: %s", exc)


# ------------------------------------------------------------------------------------------------
# Loopback callback + browser login
# ------------------------------------------------------------------------------------------------


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None
    expected_state: str = ""
    event: threading.Event

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).result = query
        ok = "code" in query and query.get("state") == type(self).expected_state
        body = (
            "<h2>Luca: login complete. You can close this tab.</h2>"
            if ok
            else f"<h2>Luca: login failed</h2><pre>{query.get('error', 'state mismatch or missing code')}: {query.get('error_description', '')}</pre>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        type(self).event.set()

    def log_message(self, *_: Any) -> None:  # silence the default stderr logging
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def login(config: Config, *, open_browser: bool = True, on_url=None) -> Tokens:
    """Run the browser login and store tokens. ``on_url`` receives the authorize URL (for prompts
    or automated tests). Blocks until the callback arrives or ``login_timeout`` passes."""
    meta = discover(config)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{meta['authorization_endpoint']}?{urlencode(params)}"

    handler = type(
        "LucaCallback",
        (_Callback,),
        {"result": None, "expected_state": state, "event": threading.Event()},
    )
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_url:
            on_url(authorize_url)
        if open_browser:
            try:
                webbrowser.open(authorize_url, new=1, autoraise=True)
            except Exception:
                pass
        if not handler.event.wait(config.login_timeout):
            raise LoginError(
                f"Timed out after {int(config.login_timeout)} s waiting for the browser login. "
                f"Open this URL and finish logging in: {authorize_url}"
            )
    finally:
        server.shutdown()
        server.server_close()
    result = handler.result or {}
    if "error" in result:
        raise LoginError(
            f"rai refused the login: {result.get('error')} {result.get('error_description', '')}".strip()
        )
    if result.get("state") != state or "code" not in result:
        raise LoginError("Login callback did not match this login attempt (state mismatch).")
    body = _token_request(
        config,
        meta,
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "code_verifier": verifier,
        },
    )
    tokens = _tokens_from_response(config, body)
    save_tokens(config, tokens)
    return tokens


def current_tokens(config: Config, *, allow_refresh: bool = True) -> Tokens | None:
    tokens = load_tokens(config)
    if tokens is None:
        return None
    if tokens.expired() and allow_refresh:
        try:
            tokens = refresh(config, tokens)
        except LoginError as exc:
            logger.info("refresh failed: %s", exc)
            return None
    return tokens
