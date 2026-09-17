"""Businesses the user can work on, their profile and their accounting periods."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import READ_ONLY, segment


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="list_businesses", annotations=READ_ONLY)
    async def list_businesses(query: str | None = None, limit: int = 50, offset: int = 0) -> Any:
        """List the businesses (companies) the logged-in user can work on, with their firm.

        Call this first; every other tool needs one of these `business_id` values (format `bu-<n>`).
        Returns legal and commercial name, RFC (tax id), status and the accounting firm, favourites
        first. Firm administrators can see hundreds of businesses: use `query` (case-insensitive
        match on id, names or RFC) and `limit`/`offset` (max 200 per page).
        """
        return await client.fetch("/businesses", query=query, limit=limit, offset=offset)

    @mcp.tool(name="get_business", annotations=READ_ONLY)
    async def get_business(business_id: str, accounting_firm_id: str | None = None) -> Any:
        """Profile of one business: legal/commercial name, RFC, fiscal regime, currency, status,
        creation date and its last open period. Confirm the company before reporting figures."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}",
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_periods", annotations=READ_ONLY)
    async def list_periods(
        business_id: str, year: int | None = None, accounting_firm_id: str | None = None
    ) -> Any:
        """List accounting periods of a business with their status.

        Periods are `YYYYMM` strings; month 13 is the annual closing period. Status is one of
        before_beginning_period, not_opened, open, closed; validation status is nothing_pending,
        pending_journal_entries, pending_reconciliation or pending_invoices. Also returns
        `last_open_period`, the period most tools should default to. Filter by `year` to shorten
        the list (Liebre returns a 13-period calendar for any year; look at `open_periods`).
        """
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/periods",
            year=year,
            accounting_firm_id=accounting_firm_id,
        )
