"""HTTP client for rai's MCP-facing endpoints, acting with the stored Luca session."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from luca_mcp import __version__, oauth
from luca_mcp.config import Config
from luca_mcp.errors import NOT_LOGGED_IN, LucaError, error_envelope

logger = logging.getLogger("luca_mcp.api")


class RaiClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.api_root,
            timeout=httpx.Timeout(config.request_timeout, connect=10.0),
            headers={"Accept": "application/json", "User-Agent": f"luca-mcp/{__version__}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    # ---------------------------------------------------------------------------------------
    def _tokens(self) -> oauth.Tokens:
        tokens = oauth.current_tokens(self._config)
        if tokens is None:
            raise LucaError(NOT_LOGGED_IN)
        return tokens

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a rai MCP endpoint; one refresh-and-retry on 401; errors become LucaError."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        tokens = self._tokens()
        response = self._request(path, clean, tokens.access_token)
        if response.status_code == 401 and tokens.refresh_token:
            try:
                tokens = oauth.refresh(self._config, tokens)
            except oauth.LoginError as exc:
                raise LucaError(
                    error_envelope(
                        "login_expired",
                        "Your Luca session has expired.",
                        "Call the `login` tool or run `luca-mcp login`.",
                        details=str(exc)[:200],
                    )
                ) from exc
            response = self._request(path, clean, tokens.access_token)
        return self._handle(response, path)

    def _request(self, path: str, params: dict[str, Any], token: str) -> httpx.Response:
        try:
            return self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LucaError(
                error_envelope(
                    "upstream_unavailable",
                    f"Could not reach Luca's server at {self._config.rai_url}.",
                    "Check your network or LUCA_RAI_URL, then try again.",
                    details=str(exc)[:200],
                )
            ) from exc

    def _handle(self, response: httpx.Response, path: str) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code < 400:
            return body
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            envelope = body  # rai's envelope, passed through verbatim
        elif (
            isinstance(body, dict)
            and isinstance(body.get("detail"), dict)
            and "error" in body["detail"]
        ):
            envelope = body["detail"]
        else:
            code = {
                401: "login_expired",
                403: "forbidden",
                404: "not_found",
                422: "invalid_input",
            }.get(response.status_code, "upstream_error")
            envelope = error_envelope(
                code,
                f"Luca's server answered HTTP {response.status_code} for {path}.",
                details=str(body or response.text)[:300],
            )
        if response.status_code == 401:
            envelope["error"].setdefault("hint", "Call the `login` tool or run `luca-mcp login`.")
        raise LucaError(envelope)
