"""Businesses and periods."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from luca_mcp.settings import Settings
from luca_mcp.tenant import BusinessRef
from luca_mcp.tools._common import (
    READ_ONLY,
    business_scope,
    page,
    platform_url,
    require_optional_str,
    user_scope,
    validate_limit,
    validate_offset,
    validate_year,
)


def _matches(ref: BusinessRef, needle: str) -> bool:
    haystack = " ".join(
        str(part or "")
        for part in (ref.business_id, ref.legal_name, ref.commercial_name, ref.tax_id)
    ).lower()
    return needle in haystack


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="list_businesses", annotations=READ_ONLY)
    async def list_businesses(
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List the businesses (companies) the logged-in user can work on, with their firm.

        Call this first; every other tool needs one of these `business_id` values (format
        `bu-<n>`). Returns legal and commercial name, RFC (tax id), status and the accounting firm
        each business belongs to, favourites first. Firm administrators can see hundreds of
        businesses: use `query` (case-insensitive match on id, names or RFC, e.g. "curiel" or
        "CCO070716M34") and `limit`/`offset` (max 200 per page). An empty list with no `query`
        means the user has no access granted yet.
        """
        needle = (require_optional_str(query, "query") or "").lower()
        limit_value = validate_limit(limit, default=50)
        offset_value = validate_offset(offset)
        _, ctx = await user_scope(settings)
        firm_names = {
            f.get("accounting_firm_id"): f.get("commercial_name") or f.get("legal_name")
            for f in ctx.firms
        }
        refs = sorted(ctx.business_list, key=lambda r: (not r.favorite, r.business_id))
        if needle:
            refs = [ref for ref in refs if _matches(ref, needle)]
        businesses = [
            {**ref.summary(), "accounting_firm_name": firm_names.get(ref.accounting_firm_id)}
            for ref in refs
        ]
        paged = page(businesses, limit_value, offset_value)
        result: dict[str, Any] = {
            "user": ctx.email,
            "firms": [
                {
                    "accounting_firm_id": f.get("accounting_firm_id"),
                    "name": f.get("commercial_name") or f.get("legal_name"),
                    "tax_id": f.get("tax_id"),
                    "sandbox": f.get("sandbox"),
                }
                for f in ctx.firms
            ],
            "query": needle or None,
            "businesses": paged["items"],
            "count": paged["count"],
            "total": paged["total"],
            "offset": paged["offset"],
            "limit": paged["limit"],
            "has_more": paged["has_more"],
            "platform_url": platform_url(settings),
        }
        if paged["has_more"]:
            result["hint"] = (
                f"{paged['total']} businesses match; showing {paged['count']} from offset "
                f"{paged['offset']}. Narrow with `query` or page with `offset`."
            )
        if ctx.problems:
            result["problems"] = ctx.problems
        if not businesses and needle:
            result["message"] = (
                f"No business matches {query!r} among the {len(ctx.business_list)} you can access."
            )
        elif not businesses:
            result["message"] = (
                "No businesses are available to you. Either no firm has granted you access yet, "
                "or listing is restricted for your role. Ask your firm administrator in the Liebre "
                "platform."
            )
        return result

    @mcp.tool(name="list_periods", annotations=READ_ONLY)
    async def list_periods(
        business_id: str,
        year: int | None = None,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """List accounting periods of a business with their status.

        Periods are `YYYYMM` strings; month 13 is the annual closing period. `period_status` is one
        of before_beginning_period, not_opened, open, closed; `period_validation_status` is
        nothing_pending, pending_journal_entries, pending_reconciliation or pending_invoices.
        Also returns `last_open_period`, the period most tools should default to. Filter by `year`
        (e.g. 2026) to shorten the list.
        """
        year_value = validate_year(year, allow_none=True)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        base = f"/businesses/{ref.business_id}"
        body = await client.get(
            f"{base}/periods",
            firm_id=ref.accounting_firm_id,
            params={"year": year_value},
            what=f"periods of {ref.business_id}",
        )
        periods = [
            {
                "period_id": str(p.get("period_id")),
                "status": p.get("period_status"),
                "validation_status": p.get("period_validation_status"),
                "is_annual_close": str(p.get("period_id", "")).endswith("13"),
            }
            for p in (body.get("data", []) if isinstance(body, dict) else [])
        ]
        last_open: dict[str, Any] | None = None
        try:
            lo = await client.get(
                f"{base}/periods/last_open", firm_id=ref.accounting_firm_id, what="last open period"
            )
            if isinstance(lo, dict) and lo.get("period_id"):
                last_open = {
                    "period_id": str(lo["period_id"]),
                    "status": lo.get("period_status"),
                    "validation_status": lo.get("period_validation_status"),
                }
        except Exception:  # informational only; the list itself already answered
            last_open = None
        return {
            "business_id": ref.business_id,
            "accounting_firm_id": ref.accounting_firm_id,
            "year": year_value,
            "periods": periods,
            "count": len(periods),
            "open_periods": [p["period_id"] for p in periods if p["status"] == "open"],
            "last_open_period": last_open,
            "platform_url": platform_url(settings, ref.business_id),
        }

    @mcp.tool(name="get_business", annotations=READ_ONLY)
    async def get_business(
        business_id: str,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Profile of one business: legal/commercial name, RFC, fiscal regime, currency, status,
        creation date and its last open period.

        Use it to confirm you are looking at the right company before reporting figures.
        """
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        base = f"/businesses/{ref.business_id}"
        body = await client.get(
            base, firm_id=ref.accounting_firm_id, what=f"business {ref.business_id}"
        )
        body = body if isinstance(body, dict) else {}
        localized = body.get("localized_data") or {}
        last_open: dict[str, Any] | None = None
        try:
            lo = await client.get(
                f"{base}/periods/last_open", firm_id=ref.accounting_firm_id, what="last open period"
            )
            if isinstance(lo, dict) and lo.get("period_id"):
                last_open = {
                    "period_id": str(lo["period_id"]),
                    "status": lo.get("period_status"),
                    "validation_status": lo.get("period_validation_status"),
                }
        except Exception:
            last_open = None
        return {
            "business_id": body.get("business_id", ref.business_id),
            "accounting_firm_id": ref.accounting_firm_id,
            "legal_name": body.get("legal_name"),
            "commercial_name": body.get("commercial_name"),
            "tax_id": body.get("tax_id"),
            "business_type": body.get("business_type"),
            "status": body.get("status"),
            "country_code": body.get("country_code"),
            "currency": body.get("currency"),
            "creation_date": body.get("creation_date"),
            "fiscal_regime": {
                "code": localized.get("regimen_fiscal_codigo"),
                "name": localized.get("regimen_fiscal_nombre"),
            },
            "postal_code": localized.get("código_postal") or localized.get("codigo_postal"),
            "last_open_period": last_open,
            "platform_url": platform_url(settings, ref.business_id),
        }
