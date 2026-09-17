"""What every tool module shares: annotations and path-segment validation."""

from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import Field

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


# ------------------------------------------------------------------------------------------------
# Parameter descriptions. Every tool parameter carries a JSON-schema description (agents read them
# when picking a tool and filling its arguments); the ones shared by several tools live here so the
# wording stays identical. Wrap the existing type, never change it: Annotated[<type>, Field(...)].
# ------------------------------------------------------------------------------------------------

DESC_BUSINESS_ID = (
    "Business id exactly as returned by `list_businesses` (format `bu-<n>`); never guess it."
)
DESC_ACCOUNTING_FIRM_ID = (
    "Accounting firm id (`af-<n>`) to act under. Only needed when the business is linked to more "
    "than one of your firms (error `ambiguous_firm`); otherwise omit it."
)
DESC_PERIOD_ID = (
    "Accounting period as `YYYYMM` (for example 202608); month 13 is the annual closing period "
    "(202613)."
)
DESC_START_PERIOD_ID = (
    "First period of the range as `YYYYMM` (month 13 = annual closing period). Also the only "
    "period when `end_period_id` is omitted."
)
DESC_END_PERIOD_ID = (
    "Last period of the range as `YYYYMM`, inclusive; must not be before `start_period_id`. Omit "
    "for a single period."
)
DESC_YEAR = "Calendar year as a four-digit number, for example 2026."
DESC_LIMIT = "Page size: how many rows to return, at most 200."
DESC_OFFSET = "How many rows to skip before the first returned row; use with `limit` to page."

BusinessId = Annotated[str, Field(description=DESC_BUSINESS_ID)]
AccountingFirmId = Annotated[str | None, Field(description=DESC_ACCOUNTING_FIRM_ID)]
PeriodId = Annotated[str | int, Field(description=DESC_PERIOD_ID)]
StartPeriodId = Annotated[str | int, Field(description=DESC_START_PERIOD_ID)]
EndPeriodId = Annotated[str | int | None, Field(description=DESC_END_PERIOD_ID)]
Limit = Annotated[int, Field(description=DESC_LIMIT)]
Offset = Annotated[int, Field(description=DESC_OFFSET)]
