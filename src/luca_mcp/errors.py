"""One error envelope for every tool.

Tools never leak raw HTTP: they raise :class:`LucaError`, which fastmcp turns into an ``isError``
tool result whose text is a JSON envelope::

    {"error": {"code": "...", "message": "...", "hint": "...", ...}}

rai produces most envelopes and the client passes them through verbatim. The client builds its
own only for local validation (``invalid_input``), a missing session (``not_authenticated``), a
refresh that failed (``login_expired``) and an unreachable server (``upstream_unavailable``).
Codes are documented in the README so skills can branch on them.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.exceptions import ToolError

NOT_LOGGED_IN = {
    "error": {
        "code": "not_authenticated",
        "message": "You are not logged in to Luca.",
        "hint": "Call the `login` tool (opens your browser), or run `luca-mcp login` in a terminal.",
    }
}


class LucaError(ToolError):
    """Tool error carrying a JSON envelope verbatim (rai's, or one built here)."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        super().__init__(json.dumps(envelope, ensure_ascii=False, default=str))


def error_envelope(
    code: str, message: str, hint: str | None = None, **extra: Any
) -> dict[str, Any]:
    """Build a client-side envelope; ``None`` extras are dropped."""
    error: dict[str, Any] = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    error.update({k: v for k, v in extra.items() if v is not None})
    return {"error": error}
