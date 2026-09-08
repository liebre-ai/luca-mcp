"""Businesses and periods."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from luca_mcp.settings import Settings
from luca_mcp.tools._common import (
    READ_ONLY,
    business_scope,
    platform_url,
    user_scope,
    validate_year,
)


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="list_businesses", annotations=READ_ONLY)
    async def list_businesses() -> dict[str, Any]:
        """List the businesses (companies) the logged-in user can work on, with their firm.

        Call this first; every other tool needs one of these `business_id` values (format
        `bu-<n>`). Returns legal and commercial name, RFC (tax id), status and the accounting firm
        each business belongs to. An empty list means the user has no access granted yet.
        """
        _, ctx = await user_scope(settings)
        firm_names = {
            f.get("accounting_firm_id"): f.get("commercial_name") or f.get("legal_name")
            for f in ctx.firms
        }
        businesses = [
            {**ref.summary(), "accounting_firm_name": firm_names.get(ref.accounting_firm_id)}
            for ref in sorted(ctx.business_list, key=lambda r: (not r.favorite, r.business_id))
        ]
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
            "businesses": businesses,
            "count": len(businesses),
            "platform_url": platform_url(settings),
        }
        if ctx.problems:
            result["problems"] = ctx.problems
        if not businesses:
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
