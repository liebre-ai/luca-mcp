"""What every tool module shares: annotations and path-segment validation."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from mcp.types import ToolAnnotations

from luca_mcp.errors import LucaError, error_envelope

# Data tools: they only read from rai.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# login / logout: change local credentials and talk to the browser, but destroy nothing.
SESSION = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

_SEGMENT_FORBIDDEN = re.compile(r"[\s/\\?#%]")


def segment(value: Any, name: str) -> str:
    """An identifier that becomes one path segment of the rai URL. Anything that could change the
    path (slashes, whitespace, query/fragment characters, dot-segments) is rejected here with a
    clear message instead of turning into a request for a different route."""
    if not isinstance(value, str) or not value.strip():
        raise LucaError(
            error_envelope("invalid_input", f"{name} is required and must be a non-empty string.")
        )
    text = value.strip()
    if _SEGMENT_FORBIDDEN.search(text) or text.startswith("."):
        raise LucaError(
            error_envelope(
                "invalid_input",
                f"{name} {text[:40]!r} is not a valid identifier.",
                hint="Use an id exactly as returned by the listing tools.",
            )
        )
    return quote(text, safe="")
