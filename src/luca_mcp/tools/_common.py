"""Shared plumbing for tools: scoping, validation, shaping."""

from __future__ import annotations

import re
from typing import Any

from mcp.types import ToolAnnotations

from luca_mcp.errors import INVALID_INPUT, LucaToolError
from luca_mcp.identity import current_identity
from luca_mcp.liebre.client import LiebreClient
from luca_mcp.settings import Settings
from luca_mcp.tenant import BusinessRef, TenantContext, load_tenant

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

PERIOD_RE = re.compile(r"^(\d{4})(\d{2})$")
MAX_ROWS = 200


async def user_scope(settings: Settings) -> tuple[LiebreClient, TenantContext]:
    identity = current_identity()
    client = LiebreClient(settings, identity)
    ctx = await load_tenant(client, settings)
    return client, ctx


async def business_scope(
    settings: Settings, business_id: str, accounting_firm_id: str | None
) -> tuple[LiebreClient, BusinessRef]:
    """Resolve the business the caller may act on; never trust a bare business_id."""
    business_id = require_str(business_id, "business_id").strip()
    firm = require_optional_str(accounting_firm_id, "accounting_firm_id")
    client, ctx = await user_scope(settings)
    return client, ctx.resolve(business_id, firm)


def require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LucaToolError(INVALID_INPUT, f"{name} is required and must be a non-empty string.")
    return value


def require_optional_str(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LucaToolError(INVALID_INPUT, f"{name} must be a string when given.")
    return value.strip()


def validate_period(value: Any, name: str = "period_id", *, allow_none: bool = False) -> str | None:
    """Periods are YYYYMM strings; month 13 is the annual closing period. Accepts ints too."""
    if value is None or value == "":
        if allow_none:
            return None
        raise LucaToolError(INVALID_INPUT, f"{name} is required (format YYYYMM, e.g. 202608).")
    text = str(value).strip()
    match = PERIOD_RE.match(text)
    if not match:
        raise LucaToolError(
            INVALID_INPUT,
            f"{name}={value!r} is not a period. Use YYYYMM, e.g. 202608 (13 = annual close).",
        )
    year, month = int(match.group(1)), int(match.group(2))
    if not (2000 <= year <= 2100) or not (1 <= month <= 13):
        raise LucaToolError(
            INVALID_INPUT,
            f"{name}={value!r} is out of range: year 2000-2100, month 01-12 or 13 (annual close).",
        )
    return text


def validate_year(value: Any, name: str = "year", *, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise LucaToolError(INVALID_INPUT, f"{name} is required (e.g. 2026).")
    try:
        year = int(str(value).strip())
    except ValueError as exc:
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} is not a year (e.g. 2026).") from exc
    if not (2000 <= year <= 2100):
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} must be between 2000 and 2100.")
    return year


def validate_limit(
    value: Any, name: str = "limit", *, default: int = 50, cap: int = MAX_ROWS
) -> int:
    if value is None:
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise LucaToolError(INVALID_INPUT, f"{name} must be an integer.") from exc
    if limit < 1:
        raise LucaToolError(INVALID_INPUT, f"{name} must be at least 1.")
    return min(limit, cap)


def validate_offset(value: Any, name: str = "offset") -> int:
    if value is None:
        return 0
    try:
        offset = int(value)
    except (TypeError, ValueError) as exc:
        raise LucaToolError(INVALID_INPUT, f"{name} must be an integer.") from exc
    if offset < 0:
        raise LucaToolError(INVALID_INPUT, f"{name} must be 0 or more.")
    return offset


def platform_url(settings: Settings, business_id: str | None = None) -> str:
    """Deep links are not known yet (frontend repo not inspected); return the platform root."""
    return f"{settings.liebre_app_root}/"


def page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    total = len(items)
    window = items[offset : offset + limit]
    return {
        "items": window,
        "count": len(window),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(window) < total,
    }
