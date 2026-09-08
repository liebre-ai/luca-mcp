"""Async HTTP client for the Liebre API, acting as the user.

The ONLY place in the server that reads the user's upstream token. The host is pinned to
``LIEBRE_API_BASE_URL``; tools never pass URLs, only paths. Retries are applied to GETs only
(429/5xx/timeouts, honouring ``Retry-After``), under an overall per-tool deadline. Every failure
is translated into a :class:`LucaToolError` with a stable code and a human hint.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from luca_mcp.errors import (
    CONFLICT,
    FORBIDDEN,
    INVALID_INPUT,
    LOGIN_EXPIRED,
    NOT_FOUND,
    UPSTREAM_ERROR,
    UPSTREAM_UNAVAILABLE,
    LucaToolError,
)
from luca_mcp.identity import Identity
from luca_mcp.settings import Settings

logger = logging.getLogger("luca_mcp.liebre")

FIRM_HEADER = "Accounting-Firm-ID"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

_http_client: httpx.AsyncClient | None = None


def http_client(settings: Settings) -> httpx.AsyncClient:
    """Process-wide connection pool (httpx clients are safe to share across tasks)."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=settings.liebre_api_root,
            timeout=httpx.Timeout(settings.liebre_timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            headers={"Accept": "application/json", "User-Agent": "luca-mcp"},
            follow_redirects=False,
        )
    return _http_client


def _detail(body: Any) -> dict[str, Any]:
    """Liebre's envelope is {"detail": {"error", "message", "errors": [...]}}; be lenient."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail
        if isinstance(detail, str):
            return {"message": detail}
        if isinstance(detail, list):  # FastAPI validation errors
            return {"error": "validation_error", "errors": detail}
    return {}


class LiebreClient:
    def __init__(self, settings: Settings, identity: Identity) -> None:
        self._settings = settings
        self._identity = identity
        self._http = http_client(settings)
        self._deadline = time.monotonic() + settings.liebre_tool_deadline_seconds

    # ---------------------------------------------------------------------------------------
    @property
    def identity(self) -> Identity:
        return self._identity

    @property
    def email(self) -> str:
        return self._identity.email

    def _headers(self, firm_id: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._identity.upstream_token}"}
        if firm_id:
            headers[FIRM_HEADER] = firm_id
        return headers

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    async def get(
        self,
        path: str,
        *,
        firm_id: str | None,
        params: dict[str, Any] | None = None,
        platform_url: str | None = None,
        what: str | None = None,
    ) -> Any:
        """GET a JSON resource. ``what`` names the thing for error messages ("business bu-2")."""
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            remaining = self._remaining()
            if remaining <= 0:
                raise LucaToolError(
                    UPSTREAM_UNAVAILABLE,
                    "Liebre took too long to answer.",
                    hint="Try again in a minute, or narrow the request (fewer periods/rows).",
                )
            timeout = min(self._settings.liebre_timeout_seconds, remaining)
            started = time.monotonic()
            try:
                response = await self._http.get(
                    path, params=clean_params, headers=self._headers(firm_id), timeout=timeout
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning("liebre GET %s attempt %s failed: %s", path, attempt, exc)
                if attempt >= self._settings.liebre_max_retries:
                    raise LucaToolError(
                        UPSTREAM_UNAVAILABLE,
                        "Liebre is not reachable right now.",
                        hint="Try again in a minute.",
                    ) from exc
                await asyncio.sleep(self._backoff(attempt, None))
                continue
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "liebre GET %s -> %s (%sms, firm=%s, user=%s)",
                path,
                response.status_code,
                elapsed_ms,
                firm_id,
                _mask(self._identity.email),
            )
            if (
                response.status_code in RETRYABLE_STATUSES
                and attempt < self._settings.liebre_max_retries
            ):
                await asyncio.sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue
            return self._handle(response, path, platform_url=platform_url, what=what)

    async def get_bytes(
        self, path: str, *, firm_id: str | None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """GET a binary resource (exports, files). Caller inspects the response."""
        response = await self._http.get(
            path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers=self._headers(firm_id),
            timeout=min(self._settings.liebre_timeout_seconds, max(self._remaining(), 1.0)),
        )
        if response.status_code >= 400:
            self._handle(response, path)
        return response

    # ---------------------------------------------------------------------------------------
    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 15.0)
            except ValueError:
                pass
        return min(1.0 * (2 ** (attempt - 1)), 8.0) + random.uniform(0, 0.5)

    def _handle(
        self,
        response: httpx.Response,
        path: str,
        *,
        platform_url: str | None = None,
        what: str | None = None,
    ) -> Any:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if status < 400:
            return body
        detail = _detail(body)
        message = (
            detail.get("message")
            or (body if isinstance(body, str) else None)
            or response.text[:200]
        )
        subject = what or path
        build = self._settings.luca_mcp_build

        if status == 401:
            raise LucaToolError(
                LOGIN_EXPIRED,
                f"Liebre did not accept your login ({message}).",
                hint=(
                    "Your Luca session may have expired, or your e-mail "
                    f"({self._identity.email}) is not a Liebre user. Run /mcp to log in again; "
                    "if it persists, ask your firm admin to check your Liebre account."
                ),
                build=build,
            )
        if status == 403:
            if message == "Not authenticated":  # missing bearer: our bug, not the user's
                raise LucaToolError(
                    UPSTREAM_ERROR, "Luca sent a request without credentials.", build=build
                )
            raise LucaToolError(
                FORBIDDEN,
                f"Your Liebre role does not allow this ({subject}).",
                hint="Ask your firm administrator for the permission, or do it in the Liebre platform.",
                platform_url=platform_url,
                details=detail or None,
                build=build,
            )
        if status == 404:
            raise LucaToolError(
                NOT_FOUND,
                f"{subject} was not found in Liebre ({message}).",
                hint="Check the id with list_businesses / list_periods and try again.",
                build=build,
            )
        if status in (400, 422):
            errors = detail.get("errors")
            fields = (
                ", ".join(
                    str(e.get("field")) for e in errors if isinstance(e, dict) and e.get("field")
                )
                if isinstance(errors, list)
                else ""
            )
            if "Accounting-Firm-ID" in fields:
                raise LucaToolError(
                    UPSTREAM_ERROR,
                    "Luca did not send the firm header.",
                    details=detail,
                    build=build,
                )
            raise LucaToolError(
                INVALID_INPUT,
                f"Liebre rejected the request ({message}).",
                hint=f"Fields: {fields}" if fields else None,
                details=detail or None,
                build=build,
            )
        if status == 409:
            raise LucaToolError(
                CONFLICT,
                f"Liebre reported a conflict ({message}).",
                details=detail or None,
                build=build,
            )
        if status in RETRYABLE_STATUSES:
            raise LucaToolError(
                UPSTREAM_UNAVAILABLE,
                f"Liebre is busy or unavailable (HTTP {status}).",
                hint="Try again in a minute.",
                build=build,
            )
        raise LucaToolError(
            UPSTREAM_ERROR,
            f"Liebre answered HTTP {status} ({message}).",
            details=detail or None,
            build=build,
        )


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"
