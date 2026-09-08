"""One error envelope for every tool.

Tools never leak raw HTTP; they raise :class:`LucaToolError`, which fastmcp turns into an
``isError`` tool result whose text is a JSON envelope::

    {"error": {"code": "...", "message": "...", "hint": "...", "platform_url": "...", "build": "..."}}

Codes are stable and documented in the README so skills can branch on them.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import ToolError

# Stable error codes.
NOT_AUTHENTICATED = "not_authenticated"
NO_EMAIL_CLAIM = "no_email_claim"
LOGIN_EXPIRED = "login_expired"  # Liebre answered 401
FORBIDDEN = "forbidden"  # Liebre answered 403
NOT_FOUND = "not_found"
INVALID_INPUT = "invalid_input"  # our own validation or Liebre 400/422
UNKNOWN_BUSINESS = "unknown_business"
AMBIGUOUS_FIRM = "ambiguous_firm"
CONFLICT = "conflict"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"  # timeouts, 5xx, 429 after retries
UPSTREAM_ERROR = "upstream_error"  # anything else from Liebre
NOT_SUPPORTED = "not_supported"  # excluded operations: answer with the platform link


class LucaToolError(ToolError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        platform_url: str | None = None,
        details: Any = None,
        build: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.platform_url = platform_url
        self.details = details
        self.build = build
        super().__init__(self.render())

    def envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        if self.platform_url:
            error["platform_url"] = self.platform_url
        if self.details is not None:
            error["details"] = self.details
        if self.build:
            error["build"] = self.build
        return {"error": error}

    def render(self) -> str:
        return json.dumps(self.envelope(), ensure_ascii=False, default=str)
